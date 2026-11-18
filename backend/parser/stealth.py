"""HTTP-загрузка с обходом простых защит.

Основной транспорт — ``curl_cffi`` с TLS/HTTP-отпечатком Chrome. Если сайт
всё равно отдаёт 403/Cloudflare/капчу и разрешён Playwright, используется
браузерный рендеринг с минимальным stealth-набором.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

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


@dataclass
class StealthResponse:
    url: str
    status_code: int
    text: str
    content_type: str = "text/html"
    engine: str = "curl_cffi"
    error: str = ""
    final_url: str = ""


def _decode(content: bytes, content_type: str) -> str:
    # charset_normalizer точнее угадывает windows-1251/UTF-8, когда сервер
    # присылает неверный Content-Type.
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - определение кодировки не критично
        pass

    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1).lower()
    for encoding in (charset, "utf-8", "cp1251"):
        if not encoding:
            continue
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


class StealthFetcher:
    """Загрузчик страниц агрегаторов и поисковой выдачи."""

    def __init__(
        self,
        timeout: float = 30.0,
        enable_playwright: bool = False,
        headless: bool = True,
    ) -> None:
        self.timeout = timeout
        self.enable_playwright = enable_playwright
        self.headless = headless
        self._session = None

    def _curl_session(self):
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as curl_requests

            self._session = curl_requests.Session(impersonate="chrome")
        except ImportError:  # pragma: no cover - fallback окружения
            import requests

            self._session = requests.Session()
        return self._session

    @staticmethod
    def looks_blocked(status_code: int, text: str) -> bool:
        if status_code in {403, 429, 503}:
            return True
        lowered = (text or "")[:5000].lower()
        return any(marker in lowered for marker in BLOCK_MARKERS)

    def get(self, url: str, timeout: float | None = None) -> StealthResponse:
        timeout = timeout or self.timeout
        try:
            session = self._curl_session()
            response = session.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )
            content_type = response.headers.get("Content-Type", "text/html")
            text = _decode(response.content, content_type)
            status = int(response.status_code)
        except Exception as exc:  # noqa: BLE001 - сеть/SSL/таймаут
            LOGGER.warning("Не удалось загрузить %s: %s", url, exc)
            if not self.enable_playwright:
                return StealthResponse(url, 0, "", error=str(exc))
            return self._playwright_get(url, timeout, error=str(exc))

        if self.looks_blocked(status, text) and self.enable_playwright:
            LOGGER.info("Блокировка %s, пробую Playwright", url)
            rendered = self._playwright_get(url, timeout)
            if rendered.status_code and rendered.text:
                return rendered

        return StealthResponse(
            url, status, text, content_type, final_url=str(response.url)
        )

    def _playwright_get(
        self, url: str, timeout: float, error: str = ""
    ) -> StealthResponse:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return StealthResponse(
                url, 0, "", engine="playwright", error="playwright не установлен"
            )

        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=self.headless)
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
                page = context.new_page()
                page.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                html = page.content()
                browser.close()
            return StealthResponse(
                url, 200, html, engine="playwright", error=error
            )
        except Exception as exc:  # noqa: BLE001 - браузер не должен ронять сбор
            LOGGER.warning("Playwright не смог загрузить %s: %s", url, exc)
            return StealthResponse(
                url, 0, "", engine="playwright", error=str(exc)
            )

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None
