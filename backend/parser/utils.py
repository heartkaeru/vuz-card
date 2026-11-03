"""Вспомогательные функции парсера: HTTP, robots.txt, даты, контакты, файлы."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from .config import Settings


LOGGER = logging.getLogger(__name__)

CYRILLIC = "А-ЯЁа-яё"
MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}

PERSON_NAME_RE = re.compile(
    rf"\b([{CYRILLIC}][{CYRILLIC}\-]{{1,24}})\s+"
    rf"([{CYRILLIC}][{CYRILLIC}\-]{{1,24}})\s+"
    rf"([{CYRILLIC}][{CYRILLIC}\-]{{1,24}})\b"
)
PERSON_NAME_LATIN_RE = re.compile(
    r"\b([A-Z][a-z]{1,20})\s+([A-Z]\.\s*)?([A-Z][a-z]{1,24})\b"
)
EMAIL_RE = re.compile(
    r"(?<![\w.+%-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}"
    r"@[A-Za-z0-9][A-Za-z0-9.\-]{0,190}\.[A-Za-z]{2,24})(?![\w.-])"
)
PHONE_RE = re.compile(
    r"(?<![\d+])(?:\+7|8|7)?[\s\-().]*"
    r"(?:\d[\s\-().]*){10}(?!\d)"
)
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
DATE_DOT_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b")
DATE_RU_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(январ[яе]?|феврал[яе]?|март[ае]?|апрел[яе]?|ма[йя]|июн[яе]?|"
    r"июл[яе]?|август[ае]?|сентябр[яе]?|октябр[яе]?|ноябр[яе]?|декабр[яе]?)"
    r"(?:\s+(\d{4}))?",
    re.IGNORECASE,
)

NAME_BLACKLIST = {
    "университет",
    "государственный",
    "российский",
    "федеральный",
    "национальный",
    "исследовательский",
    "политехнический",
    "технический",
    "гуманитарный",
    "педагогический",
    "медицинский",
    "экономический",
    "юридический",
    "высшая",
    "высшее",
    "школа",
    "экономики",
    "образования",
    "науки",
    "научный",
    "институт",
    "факультет",
    "кафедра",
    "департамент",
    "управление",
    "дирекция",
    "центр",
    "отдел",
    "служба",
    "сектор",
    "лаборатория",
    "совет",
    "ректорат",
    "проректор",
    "ректор",
    "президент",
    "директор",
    "декан",
    "руководитель",
    "начальник",
    "заведующий",
    "профессор",
    "доцент",
    "академик",
    "член",
    "корреспондент",
    "министерство",
    "правительство",
    "россии",
    "рф",
    "москва",
    "санкт",
    "петербург",
    "новосибирск",
    "екатеринбург",
    "казань",
    "томск",
    "проект",
    "программа",
    "грант",
    "мероприятие",
    "конференция",
    "форум",
    "олимпиада",
    "студент",
    "студенты",
    "абитуриент",
    "образование",
    "развитие",
    "цифровой",
    "кафедрой",
    "института",
    "университета",
    "российской",
    "федерации",
}

POSITION_KEYWORDS = (
    "ректор",
    "проректор",
    "президент",
    "директор",
    "декан",
    "руководитель",
    "начальник",
    "заведующий",
    "председатель",
    "профессор",
    "академик",
    "секретарь",
)

JUNK_URL_PARTS = (
    "/search",
    "/poisk",
    "?s=",
    "/tag/",
    "/tags/",
    "/author/",
    "/print",
    "/login",
    "/signin",
    "/sign-in",
    "/account",
    "/profile",
    "/cart",
    "/basket",
    "/checkout",
    "/register",
    "javascript:",
    "mailto:",
    "tel:",
    "#",
)

TRACKING_QUERY_PREFIXES = ("utm_", "yclid", "gclid", "fbclid", "from", "ref")


class FetchError(RuntimeError):
    """Базовая ошибка загрузки."""


class BlockedError(FetchError):
    """Сайт вернул страницу-заглушку/капчу или запретил доступ."""


class RobotsDeniedError(FetchError):
    """robots.txt запрещает обход URL."""


@dataclass
class FetchResult:
    """Результат HTTP-запроса."""

    url: str
    final_url: str
    status_code: int
    text: str
    content_type: str = ""
    encoding: str = ""
    elapsed: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


def setup_logging(
    level: str = "INFO",
    log_file: Path | str | None = None,
) -> None:
    """Настраивает консольное и файловое логирование (UTF-8)."""

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Повторный вызов не должен дублировать обработчики.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Сторонние библиотеки не должны засорять журнал.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def normalize_whitespace(text: str) -> str:
    """Убирает NBSP, zero-width символы и лишние пробелы."""

    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\u2011", "-").replace("\u2013", "–").replace("\u2014", "—")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_text(text: str, limit: int) -> str:
    """Обрезает текст по границе предложения/слова."""

    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    for separator in (". ", "! ", "? ", "\n", " "):
        position = cut.rfind(separator)
        if position > limit * 0.6:
            return cut[: position + len(separator)].rstrip()
    return cut.rstrip()


def sha256_text(text: str) -> str:
    """SHA-256 очищенного текста (для отслеживания изменений)."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(value: str) -> str:
    """Транслитерация названия в безопасный slug для имени файла."""

    translit = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    lowered = value.lower().strip()
    result = "".join(translit.get(char, char) for char in lowered)
    result = re.sub(r"[^a-z0-9]+", "-", result).strip("-")
    return result or "university"


def atomic_write_json(path: Path | str, data: object) -> Path:
    """Атомарно сохраняет JSON в UTF-8."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    os.replace(temporary, target)
    return target


def load_json(path: Path | str) -> dict | None:
    """Читает JSON, возвращает ``None`` для отсутствующего/битого файла."""

    target = Path(path)
    if not target.exists():
        return None
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Не удалось прочитать %s: %s", target, exc)
        return None


def dedupe_preserve(items: Iterable[str]) -> list[str]:
    """Удаляет дубликаты, сохраняя порядок."""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = normalize_whitespace(item).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def canonical_url(url: str, base_url: str = "") -> str:
    """Приводит URL к абсолютному виду и убирает якоря/UTM-метки."""

    absolute = urljoin(base_url, url.strip()) if base_url else url.strip()
    parsed = urlparse(absolute)
    query_parts = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key.startswith(TRACKING_QUERY_PREFIXES):
            continue
        query_parts.append(part)
    return urlunparse(
        (
            parsed.scheme or "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            "",
            "&".join(query_parts),
            "",
        )
    )


def is_same_domain(url: str, base_url: str) -> bool:
    """Проверяет, относится ли URL к тому же сайту (с учётом поддоменов)."""

    def _host(value: str) -> str:
        host = urlparse(value).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host

    left = _host(url)
    right = _host(base_url)
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def is_probably_content_link(url: str) -> bool:
    """Отсекает поиск, теги, файлы и служебные ссылки."""

    lowered = url.lower()
    if any(part in lowered for part in JUNK_URL_PARTS):
        return False
    if re.search(r"\.(?:jpg|jpeg|png|gif|svg|webp|pdf|docx?|xlsx?|pptx?|zip|rar|mp4|mp3)(?:\?|$)", lowered):
        return False
    return True


def extract_emails(text: str) -> list[str]:
    """Извлекает и нормализует email-адреса."""

    if not text:
        return []
    prepared = re.sub(r"\s*[\[(]\s*(?:at|собака)\s*[\])]\s*", "@", text, flags=re.I)
    prepared = re.sub(r"\s*[\[(]\s*(?:dot|точка)\s*[\])]\s*", ".", prepared, flags=re.I)
    found = []
    for match in EMAIL_RE.finditer(prepared):
        email = clean_email(match.group(1))
        if email:
            found.append(email)
    return dedupe_preserve(found)


def clean_email(value: str) -> str:
    """Очищает email от ``mailto:``, пробелов и завершающей пунктуации."""

    email = normalize_whitespace(value or "")
    email = re.sub(r"^mailto:", "", email, flags=re.I)
    email = email.strip(" \t\r\n.,;:<>()[]\"'")
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    domain = domain.strip(".").lower()
    if not local or not domain or "." not in domain:
        return ""
    if re.search(r"\.(?:png|jpe?g|gif|svg|webp|css|js)$", domain, re.I):
        return ""
    return f"{local}@{domain}"


def normalize_phone(value: str) -> str:
    """Нормализует российский телефон к виду ``+7 (999) 123-45-67``."""

    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return (
            f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
        )
    if len(digits) >= 11:
        return "+" + digits
    return normalize_whitespace(value)


def _plausible_phone_digits(digits: str) -> bool:
    """Отсекает случайные числа, похожие на телефон.

    Российский номер — 11 цифр (7/8 + 10) или 10 цифр. Код города/оператора
    не может начинаться с 0 или 1, иначе это, скорее всего, фрагмент
    адреса, ИНН или номера дома.
    """

    if len(digits) == 11 and digits[0] in "78":
        return digits[1] in "3456789"
    if len(digits) == 10:
        return digits[0] in "3456789"
    if 11 <= len(digits) <= 15:
        # Международный номер другого формата.
        return digits[0] not in "01"
    return False


def clean_phone(value: str) -> str:
    """Извлекает первый телефон из строки и нормализует его."""

    if not value:
        return ""
    match = PHONE_RE.search(value)
    if not match:
        return ""
    candidate = match.group(0)
    digits = re.sub(r"\D", "", candidate)
    if not _plausible_phone_digits(digits):
        return ""
    return normalize_phone(candidate)


def extract_phones(text: str) -> list[str]:
    """Извлекает все телефоны из текста."""

    if not text:
        return []
    phones = []
    for match in PHONE_RE.finditer(text):
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        if _plausible_phone_digits(digits):
            phones.append(normalize_phone(candidate))
    return dedupe_preserve(phones)


def parse_date(value: str, default_year: int | None = None) -> str | None:
    """Извлекает первую дату и приводит её к ISO ``YYYY-MM-DD``.

    Поддерживаются ISO, ``DD.MM.YYYY``, ``2 ноября 2026``, диапазоны
    ``21 сентября — 10 ноября 2026`` (берётся начало диапазона).
    """

    if not value:
        return None
    text = normalize_whitespace(value)

    match = DATE_ISO_RE.search(text)
    if match:
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).date().isoformat()
        except ValueError:
            pass

    match = DATE_DOT_RE.search(text)
    if match:
        day, month, year = (int(part) for part in match.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass

    match = DATE_RU_RE.search(text)
    if match:
        day = int(match.group(1))
        month_word = match.group(2).lower()
        year_text = match.group(3)
        month = None
        for stem, number in MONTHS.items():
            if month_word.startswith(stem):
                month = number
                break
        if month is None:
            return None
        if year_text:
            year = int(year_text)
        elif default_year:
            year = default_year
        else:
            year = datetime.now(timezone.utc).year
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    return None


def strip_name(value: str) -> str:
    """Убирает лишние подписи вокруг ФИО."""

    text = normalize_whitespace(value)
    text = re.sub(
        r"^(?:фио|руководитель|ректор|проректор|директор|декан)\s*[:\-–—]?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*\((?:и\.?\s*о\.?|исполняющий обязанности)\)\s*", " ", text, flags=re.I)
    return text.strip(" ,;:.-")


def looks_like_person_name(value: str) -> bool:
    """Эвристика: похоже ли значение на полное ФИО человека."""

    text = strip_name(value)
    if not text or len(text) > 80:
        return False
    if re.search(r"\d|@|https?://|/|,", text):
        return False

    lowered = text.lower()
    if any(
        bad in lowered
        for bad in (
            "неприменимо",
            "вакансия",
            "вакантно",
            "отсутствует",
            "нет данных",
            "не указан",
            "университет",
            "институт",
            "факультет",
            "кафедра",
            "департамент",
            "управление",
            "центр",
            "отдел",
            "совет",
            "комиссия",
            "лаборатория",
            "программа",
            "проект",
        )
    ):
        return False

    match = PERSON_NAME_RE.fullmatch(text)
    if match:
        tokens = [part for part in match.groups() if part]
        if len(tokens) != 3:
            return False
        if any(token.lower() in NAME_BLACKLIST for token in tokens):
            return False
        if not all(len(token) >= 2 for token in tokens):
            return False
        return True

    # Латиница: "Carol T. Christ", "John Smith".
    latin = PERSON_NAME_LATIN_RE.fullmatch(text)
    if latin:
        tokens = [part for part in latin.groups() if part]
        return len(tokens) >= 2 and not any(
            token.lower().strip(".") in NAME_BLACKLIST for token in tokens
        )
    return False


def find_person_names(text: str) -> list[str]:
    """Находит в тексте последовательности, похожие на ФИО."""

    if not text:
        return []
    found = []
    for match in PERSON_NAME_RE.finditer(text):
        candidate = match.group(0)
        if looks_like_person_name(candidate):
            found.append(strip_name(candidate))
    return dedupe_preserve(found)


def contains_position(text: str) -> bool:
    """Есть ли в тексте название руководящей должности."""

    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in POSITION_KEYWORDS)


def detect_encoding(response: requests.Response) -> str:
    """Определяет кодировку ответа (включая старые cp1251-сайты)."""

    encoding = (response.encoding or "").lower()
    if not encoding or encoding in {"iso-8859-1", "ascii"}:
        encoding = (response.apparent_encoding or "utf-8").lower()
    if encoding in {"windows-1251", "cp1251"}:
        return "cp1251"
    return encoding or "utf-8"


def visible_text_from_html(html: str) -> str:
    """Грубо вырезает script/style для проверки страницы-заглушки."""

    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text, flags=re.I)
    return normalize_whitespace(text).lower()


def looks_like_block_page(html: str, status_code: int) -> bool:
    """Распознаёт капчу, Cloudflare, DDoS-Guard и текстовые блокировки."""

    if status_code in {403, 429, 503}:
        return True
    if not html or len(html) > 200_000:
        return False
    text = visible_text_from_html(html)
    markers = (
        "подтвердите, что вы не робот",
        "подтвердите что вы не робот",
        "вы не робот",
        "введите капчу",
        "captcha",
        "access denied",
        "доступ ограничен",
        "доступ запрещён",
        "доступ запрещен",
        "cloudflare",
        "ddos-guard",
        "incapsula",
        "request blocked",
        "too many requests",
    )
    return any(marker in text for marker in markers)


class RateLimiter:
    """Вежливая задержка между запросами к одному хосту."""

    def __init__(self, delay: float = 2.5, jitter: float = 0.0) -> None:
        self.base_delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._last_request: dict[str, float] = {}
        self._host_delays: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_host_delay(self, host: str, delay: float) -> None:
        with self._lock:
            self._host_delays[host] = max(0.0, delay)

    def current_delay(self, host: str) -> float:
        with self._lock:
            return max(self.base_delay, self._host_delays.get(host, 0.0))

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            delay = max(self.base_delay, self._host_delays.get(host, 0.0))
            if self.jitter:
                delay += random.uniform(0, self.jitter)
            last = self._last_request.get(host, 0.0)
            now = time.monotonic()
            wait_for = delay - (now - last)
            if wait_for > 0:
                LOGGER.debug("Пауза %.2f с перед запросом к %s", wait_for, host)
                time.sleep(wait_for)
            self._last_request[host] = time.monotonic()


class RobotsPolicy:
    """Кэшированная проверка robots.txt с учётом Crawl-delay."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._delays: dict[str, float] = {}
        self._lock = threading.Lock()

    def _load(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        with self._lock:
            if host in self._parsers:
                return self._parsers[host]

        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        parser: RobotFileParser | None = None
        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": "VuzCardBot/1.0 (+research; contact: local)"},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                delay = parser.crawl_delay("*")
                if delay:
                    self._delays[host] = float(delay)
            elif response.status_code in {401, 403}:
                # RFC 9309: доступ к robots.txt запрещён — обход запрещён.
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(["User-agent: *", "Disallow: /"])
                LOGGER.warning("robots.txt недоступен (%s), обход запрещён", robots_url)
            else:
                LOGGER.info("robots.txt не найден (%s), обход разрешён", robots_url)
        except requests.RequestException as exc:
            LOGGER.warning("Не удалось получить robots.txt %s: %s", robots_url, exc)
            parser = None

        with self._lock:
            self._parsers[host] = parser
        return parser

    def can_fetch(self, url: str) -> tuple[bool, float]:
        """Возвращает (разрешено ли, Crawl-delay)."""

        parser = self._load(url)
        if parser is None:
            return True, 0.0
        host = urlparse(url).netloc.lower()
        allowed = parser.can_fetch("*", url)
        return allowed, self._delays.get(host, 0.0)


class HttpClient:
    """HTTP-клиент: задержки, User-Agent, retry, robots.txt, блокировки."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.limiter = RateLimiter(settings.request_delay, settings.delay_jitter)
        self.robots = RobotsPolicy(timeout=min(settings.timeout, 15.0))
        self._ua_index = 0
        self._ua_lock = threading.Lock()
        self._requests_made = 0
        self.last_error: str = ""

    def _next_user_agent(self) -> str:
        with self._ua_lock:
            agent = self.settings.user_agents[self._ua_index % len(self.settings.user_agents)]
            self._ua_index += 1
            return agent

    def _headers(self, url: str) -> dict[str, str]:
        return {
            "User-Agent": self._next_user_agent(),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
        }

    def get(self, url: str) -> FetchResult | None:
        """Скачивает страницу. Возвращает ``None`` после исчерпания попыток."""

        self.last_error = ""
        if self._requests_made >= self.settings.max_total_requests:
            self.last_error = (
                "Достигнут лимит запросов на обход "
                f"({self.settings.max_total_requests}): {url}"
            )
            LOGGER.warning(self.last_error)
            return None
        if self.settings.respect_robots:
            allowed, crawl_delay = self.robots.can_fetch(url)
            if crawl_delay:
                self.limiter.set_host_delay(urlparse(url).netloc.lower(), crawl_delay)
            if not allowed:
                self.last_error = f"robots.txt запрещает обход: {url}"
                LOGGER.warning(self.last_error)
                return None

        last_exception: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            if self._requests_made >= self.settings.max_total_requests:
                self.last_error = "Достигнут лимит запросов на обход"
                break
            self._requests_made += 1
            self.limiter.wait(url)
            started = time.monotonic()
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(url),
                    timeout=self.settings.timeout,
                    allow_redirects=True,
                )
                elapsed = time.monotonic() - started
                if len(response.content) > self.settings.max_response_bytes:
                    self.last_error = (
                        f"Слишком большой ответ ({len(response.content)} байт): {url}"
                    )
                    LOGGER.warning(self.last_error)
                    return None
                encoding = detect_encoding(response)
                try:
                    body = response.content.decode(encoding, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    body = response.text

                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                LOGGER.info(
                    "GET %s -> %s (%.0f КБ, %.1f с)",
                    url,
                    response.status_code,
                    len(body.encode("utf-8")) / 1024,
                    elapsed,
                )

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    host = urlparse(url).netloc.lower()
                    self.limiter.set_host_delay(
                        host, self.limiter.current_delay(host) * 1.5
                    )
                    self.last_error = f"429 Too Many Requests: {url}"
                    LOGGER.warning(
                        "%s; пауза %.1f с (попытка %s/%s)",
                        self.last_error,
                        retry_after or self.settings.backoff_factor * attempt,
                        attempt,
                        self.settings.max_retries,
                    )
                    time.sleep(
                        retry_after
                        or _backoff_seconds(attempt, self.settings.backoff_factor)
                    )
                    continue

                if response.status_code in {401, 403}:
                    self.last_error = (
                        f"Доступ запрещён ({response.status_code}): {url}. "
                        "Обход блокировки не выполняется."
                    )
                    LOGGER.error(self.last_error)
                    return None

                if response.status_code >= 500:
                    self.last_error = f"Ошибка сервера {response.status_code}: {url}"
                    LOGGER.warning(self.last_error)
                    last_exception = FetchError(self.last_error)
                    host = urlparse(url).netloc.lower()
                    self.limiter.set_host_delay(
                        host, self.limiter.current_delay(host) * 1.5
                    )
                    retry_after = (
                        _parse_retry_after(response.headers.get("Retry-After"))
                        if response.status_code == 503
                        else None
                    )
                    time.sleep(
                        retry_after
                        or _backoff_seconds(attempt, self.settings.backoff_factor)
                    )
                    continue

                if response.status_code >= 400:
                    self.last_error = f"HTTP {response.status_code}: {url}"
                    LOGGER.error(self.last_error)
                    return None

                if looks_like_block_page(body, response.status_code):
                    self.last_error = (
                        f"Похоже на капчу/заглушку Anti-DDoS: {url}. "
                        "Автоматический обход не выполняется."
                    )
                    LOGGER.error(self.last_error)
                    return None

                return FetchResult(
                    url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    text=body,
                    content_type=content_type,
                    encoding=encoding,
                    elapsed=elapsed,
                    headers=dict(response.headers),
                )
            except requests.RequestException as exc:
                last_exception = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning(
                    "Попытка %s/%s для %s не удалась: %s",
                    attempt,
                    self.settings.max_retries,
                    url,
                    exc,
                )
                if attempt < self.settings.max_retries:
                    time.sleep(
                        _backoff_seconds(attempt, self.settings.backoff_factor)
                    )

        if last_exception:
            self.last_error = f"{type(last_exception).__name__}: {last_exception}"
        LOGGER.error("Не удалось загрузить %s: %s", url, self.last_error)
        return None

    def close(self) -> None:
        self.session.close()


def _backoff_seconds(attempt: int, factor: float) -> float:
    """Экспоненциальная задержка: factor, 2*factor, 4*factor…"""

    return min(60.0, factor * (2 ** max(0, attempt - 1)))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def pick_title(html_title: str | None, fallback: str = "") -> str:
    """Очищает ``<title>`` от названия сайта."""

    title = normalize_whitespace(html_title or "")
    # Отрезаем хвост «| Название сайта», «— Сайт», « - Сайт».
    title = re.sub(r"\s+[|—–-]\s+.*$", "", title).strip()
    return title or fallback


def iter_lines(text: str) -> Iterator[str]:
    """Итерирует непустые нормализованные строки."""

    for line in (text or "").splitlines():
        cleaned = normalize_whitespace(line)
        if cleaned:
            yield cleaned


def first_non_empty(values: Iterable[str | None]) -> str:
    """Возвращает первое непустое значение."""

    for value in values:
        if value and normalize_whitespace(str(value)):
            return normalize_whitespace(str(value))
    return ""


def safe_filename(value: str) -> str:
    """Безопасное имя файла."""

    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._-]+", "_", value).strip("._")
    return cleaned[:120] or "file"
