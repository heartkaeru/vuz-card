"""HTTP-загрузка агрегаторов, поисковиков и резервная загрузка страниц.

Основной транспорт — ``curl_cffi`` с TLS/HTTP-отпечатком Chrome. Если сайт
отдаёт 403/Cloudflare/капчу или пустую SPA-оболочку и разрешён Playwright,
используется браузерный рендеринг. Загрузчик соблюдает паузу между запросами
к одному хосту, ограничивает размер ответа и в режиме ``public_only`` не
обращается к адресам внутренней сети.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

BLOCK_MARKERS = (
    "cloudflare",
    "ddos-guard",
    "just a moment",
    "are you not a robot",
    "вы не робот",
    "подтвердите, что вы не робот",
    "captcha",
    "access denied",
    "request blocked",
    "что вы не бот",
    "вы не бот",
    "проверка браузера",
    "checking your browser",
    "проверяем браузер",
)

DEFAULT_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

MAX_RESPONSE_BYTES = 8_000_000

# --- Когда нужен браузер ------------------------------------------------------
# Chromium запускается только там, где без него страницу не получить:
# 1) JS-проверка браузера антибота (Cloudflare «Just a moment», DDoS-Guard,
#    «Проверяем браузер»): она пропускает после выполнения JavaScript;
# 2) SPA-сайт отдал пустую оболочку (контент рисует JavaScript);
# 3) TLS/соединение отвергнуто для обычного клиента, но не таймаут и не DNS.
# Капчу («вы не робот», «введите символы») браузер не решает — её не трогаем.
# Поисковики и справочные API браузером не открываются никогда.
JS_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "проверка браузера",
    "проверяем браузер",
    "что вы не бот",
    "ddos-guard",
    "cf-chl",
    "challenge-platform",
    "enable javascript and cookies",
    "включите javascript",
)
CAPTCHA_MARKERS = ("captcha", "вы не робот", "введите символы", "smartcaptcha", "recaptcha")
CHALLENGE_SERVERS = ("cloudflare", "ddos-guard", "qrator", "variti", "stormwall")
SPA_MARKERS = (
    'id="app"', "id='app'", 'id="root"', "id='root'", "id=\"__next\"", "__next_data__",
    "__nuxt", "data-sveltekit", "_app/immutable", "ng-version", "ng-app", "data-reactroot",
    "window.__initial_state__", "<app-root",
)
NO_BROWSER_HOSTS = (
    "yandex.ru", "yandex.com", "google.com", "googleapis.com", "bing.com",
    "duckduckgo.com", "yahoo.com", "wikipedia.org", "wikidata.org",
)
BROWSER_NETWORK_ERRORS = ("ssl", "tls", "certificate", "handshake", "connection reset", "http/2", "http2")


def browser_allowed_for(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not any(host == item or host.endswith("." + item) for item in NO_BROWSER_HOSTS)


def needs_js_challenge(status: int, text: str, headers: dict | None = None) -> bool:
    """Ответ — JS-проверка антибота, которую проходит только браузер."""

    sample = (text or "")[:200_000].lower()
    if any(marker in sample for marker in CAPTCHA_MARKERS) and not any(
        marker in sample for marker in JS_CHALLENGE_MARKERS
    ):
        return False  # капча: браузер не поможет
    server = " ".join(str(value) for value in (headers or {}).values()).lower()
    if status in {403, 503} and any(name in server for name in CHALLENGE_SERVERS):
        return True
    if visible_text_length(text) > 3000:
        return False
    return any(marker in sample for marker in JS_CHALLENGE_MARKERS)


def looks_like_spa_shell(html: str, content_type: str = "text/html") -> bool:
    """Пустая HTML-оболочка SPA: текста почти нет, контент рисует JavaScript."""

    if content_type and "html" not in content_type.lower():
        return False
    if visible_text_length(html) >= 400:
        return False
    lowered = (html or "")[:300_000].lower()
    if "<html" not in lowered and "<!doctype" not in lowered:
        return False
    if any(marker in lowered for marker in SPA_MARKERS):
        return True
    # Много скриптов и почти нет текста — тоже оболочка.
    return lowered.count("<script") >= 5 and visible_text_length(html) < 200


def network_error_needs_browser(error: str) -> bool:
    """Таймаут и DNS браузер не исправит; отказ TLS/сброс соединения — может."""

    lowered = (error or "").lower()
    if any(word in lowered for word in ("timed out", "timeout", "resolve", "name or service", "refused")):
        return False
    return any(word in lowered for word in BROWSER_NETWORK_ERRORS)

# --- Ограничения Chromium для небольшого сервера (1 vCPU / 2 ГБ) ---------------
# Один headless-Chromium с открытой страницей занимает ~250–400 МБ. Одновременно
# держим не больше PARSER_MAX_BROWSERS браузеров (по умолчанию один): задачи,
# пришедшие параллельно, ждут своей очереди, а не выедают всю память.
MAX_BROWSERS = max(1, int(os.getenv("PARSER_MAX_BROWSERS", "1") or 1))
BROWSER_WAIT_SECONDS = float(os.getenv("PARSER_BROWSER_WAIT", "180") or 180)
CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",  # в Docker /dev/shm по умолчанию всего 64 МБ
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--no-first-run",
    "--mute-audio",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256",
]
# Картинки, видео и шрифты для разбора текста не нужны (адрес фото берётся из
# HTML), а памяти и трафика они требуют больше всего.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

_BROWSER_SLOTS = threading.BoundedSemaphore(MAX_BROWSERS)
_THREAD_BROWSER = threading.local()


def _acquire_browser(headless: bool):
    """Браузер текущего потока: один на поток, не больше MAX_BROWSERS всего.

    Объекты sync-API Playwright привязаны к потоку, поэтому загрузчики одного
    задания (парсер, агрегаторы, поиск ректора) делят один браузер.
    """

    state = _THREAD_BROWSER
    if getattr(state, "browser", None) is not None:
        state.users += 1
        return state.browser
    if not _BROWSER_SLOTS.acquire(timeout=BROWSER_WAIT_SECONDS):
        raise RuntimeError("browser_busy: все браузеры заняты другими заданиями")
    try:
        from playwright.sync_api import sync_playwright

        runtime = sync_playwright().start()
        try:
            browser = runtime.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        except Exception:
            runtime.stop()
            raise
    except Exception:
        _BROWSER_SLOTS.release()
        raise
    state.runtime, state.browser, state.users = runtime, browser, 1
    return browser


def _release_browser() -> None:
    state = _THREAD_BROWSER
    if getattr(state, "browser", None) is None:
        return
    state.users -= 1
    if state.users > 0:
        return
    for closer in (state.browser.close, state.runtime.stop):
        try:
            closer()
        except Exception:  # noqa: BLE001 - закрытие не должно ронять сбор
            pass
    state.browser = state.runtime = None
    _BROWSER_SLOTS.release()


@dataclass
class StealthResponse:
    url: str
    status_code: int
    text: str
    content_type: str = "text/html"
    engine: str = "curl_cffi"
    error: str = ""
    final_url: str = ""

    @property
    def ok(self) -> bool:
        """Страница получена и это не ошибка/заглушка."""

        return 200 <= self.status_code < 400 and bool(self.text)


SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?([\w\-]+)", re.IGNORECASE
)


def visible_text(html: str) -> str:
    text = SCRIPT_STYLE_RE.sub(" ", html or "")
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def visible_text_length(html: str) -> int:
    """Приблизительная длина видимого текста (без скриптов, стилей и тегов)."""

    return len(visible_text(html))


def _decode(content: bytes, content_type: str) -> str:
    """Декодирует ответ: заголовок → meta charset → автоопределение."""

    declared = []
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        declared.append(match.group(1).lower())
    meta = META_CHARSET_RE.search(content[:4096])
    if meta:
        declared.append(meta.group(1).decode("ascii", "ignore").lower())
    for encoding in declared:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    # Явной кодировки нет или она неверна: пробуем UTF-8 строго, затем
    # автоопределение (старые сайты на windows-1251).
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - определение кодировки не критично
        pass
    try:
        return content.decode("cp1251")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def ensure_public_url(url: str) -> None:
    """Адрес публичный: схема http(s), хост разрешается только в глобальные IP."""

    from .public_http import public_url

    public_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("public_website_required") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or any(
        not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast
        for address in addresses
    ):
        raise ValueError("public_website_required")


class StealthFetcher:
    """Загрузчик страниц агрегаторов, поисковой выдачи и резервный транспорт."""

    def __init__(
        self,
        timeout: float = 30.0,
        enable_playwright: bool = False,
        headless: bool = True,
        *,
        min_delay: float = 1.0,
        public_only: bool = False,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.timeout = timeout
        self.enable_playwright = enable_playwright
        self.headless = headless
        self.min_delay = max(0.0, min_delay)
        self.public_only = public_only
        self.max_bytes = max_bytes
        self._session = None
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()
        self._browser = None

    def _curl_session(self):
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as curl_requests

            self._session = curl_requests.Session(impersonate="chrome")
        except ImportError:  # pragma: no cover - fallback окружения
            import requests

            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    def _wait_turn(self, url: str) -> None:
        """Пауза между запросами к одному хосту: меньше капч и банов."""

        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            last = self._last_request.get(host, 0.0)
            wait_for = self.min_delay - (time.monotonic() - last)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request[host] = time.monotonic()

    @staticmethod
    def looks_blocked(status_code: int, text: str) -> bool:
        if status_code in {403, 429, 503}:
            return True
        visible = visible_text((text or "")[:200_000])
        # Заглушки антибота короткие. На обычных страницах слова «captcha»
        # или «cloudflare» встречаются в скриптах и формах — это не блокировка.
        if len(visible) > 3000:
            return False
        lowered = visible.lower()
        return any(marker in lowered for marker in BLOCK_MARKERS)

    def get(self, url: str, timeout: float | None = None) -> StealthResponse:
        timeout = timeout or self.timeout
        if self.public_only:
            try:
                ensure_public_url(url)
            except ValueError:
                return StealthResponse(url, 0, "", error="public_website_required")
        self._wait_turn(url)
        try:
            session = self._curl_session()
            response = session.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            final_url = str(response.url)
            if self.public_only and final_url != url:
                ensure_public_url(final_url)
            content = response.content or b""
            if len(content) > self.max_bytes:
                return StealthResponse(url, 0, "", error="response_too_large", final_url=final_url)
            content_type = response.headers.get("Content-Type", "text/html")
            text = _decode(content, content_type)
            status = int(response.status_code)
        except ValueError:
            return StealthResponse(url, 0, "", error="public_website_required")
        except Exception as exc:  # noqa: BLE001 - сеть/SSL/таймаут
            LOGGER.warning("Не удалось загрузить %s: %s", url, exc)
            if (
                self.enable_playwright
                and browser_allowed_for(url)
                and network_error_needs_browser(str(exc))
            ):
                LOGGER.info("%s: соединение отвергнуто (%s), пробую браузер", url, exc)
                return self._playwright_get(url, timeout, error=str(exc))
            return StealthResponse(url, 0, "", error=str(exc))

        if not (self.enable_playwright and browser_allowed_for(url)):
            return StealthResponse(url, status, text, content_type, final_url=final_url)

        if needs_js_challenge(status, text, dict(response.headers)):
            LOGGER.info("%s: JS-проверка антибота, открываю в браузере", url)
            rendered = self._playwright_get(url, timeout)
            if rendered.ok and not self.looks_blocked(rendered.status_code, rendered.text):
                return rendered
        elif status < 400 and looks_like_spa_shell(text, content_type):
            # SPA-сайты (например, МГУ на SvelteKit) отдают пустую оболочку:
            # весь контент дорисовывается в браузере.
            LOGGER.info("%s: пустая HTML-оболочка SPA, рендерю в браузере", url)
            rendered = self._playwright_get(url, timeout)
            if rendered.ok and visible_text_length(
                rendered.text
            ) > visible_text_length(text):
                return rendered

        return StealthResponse(
            url, status, text, content_type, final_url=final_url
        )

    def _ensure_browser(self):
        if self._browser is None:
            self._browser = _acquire_browser(self.headless)
        return self._browser

    def _playwright_get(
        self, url: str, timeout: float, error: str = ""
    ) -> StealthResponse:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return StealthResponse(
                url, 0, "", engine="playwright", error="playwright не установлен"
            )

        context = None
        try:
            browser = self._ensure_browser()
            context = browser.new_context(
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            context.add_init_script(STEALTH_SCRIPT)
            public_only = self.public_only

            def guard(route):
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                    return route.abort()
                if public_only:
                    try:
                        ensure_public_url(route.request.url)
                    except ValueError:
                        return route.abort()
                return route.continue_()

            context.route(re.compile(r"^https?://"), guard)
            page = context.new_page()
            response = None
            try:
                response = page.goto(
                    url,
                    timeout=int(timeout * 1000),
                    wait_until="networkidle",
                )
            except Exception:  # noqa: BLE001 - networkidle не дождались
                response = page.goto(
                    url,
                    timeout=int(timeout * 1000),
                    wait_until="domcontentloaded",
                )
            page.wait_for_timeout(1500)
            html = page.content()
            status = response.status if response is not None else 0
            return StealthResponse(
                url, status, html, engine="playwright", error=error, final_url=page.url
            )
        except Exception as exc:  # noqa: BLE001 - браузер не должен ронять сбор
            LOGGER.warning("Playwright не смог загрузить %s: %s", url, exc)
            return StealthResponse(
                url, 0, "", engine="playwright", error=str(exc)
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self._browser is not None:
            self._browser = None
            _release_browser()
