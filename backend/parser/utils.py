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

# Каждая часть ФИО начинается с заглавной буквы: «Иванов Иван Иванович»
# или «ИВАНОВ Иван Иванович». Строчные фразы («приказ об этом») не являются ФИО.
_NAME_TOKEN = r"[А-ЯЁ][А-ЯЁа-яё]{1,24}(?:-[А-ЯЁ][А-ЯЁа-яё]{1,24})?"
PERSON_NAME_RE = re.compile(
    rf"(?<![{CYRILLIC}\-])({_NAME_TOKEN})\s+({_NAME_TOKEN})\s+({_NAME_TOKEN})(?![{CYRILLIC}\-])"
)
# «Иванов И. И.», «Иванов И.И.» и «И. И. Иванов».
PERSON_INITIALS_RE = re.compile(
    rf"(?<![{CYRILLIC}\-])(?:({_NAME_TOKEN})\s+([А-ЯЁ])\.\s*([А-ЯЁ])\.?(?![А-ЯЁа-яё])"
    rf"|([А-ЯЁ])\.\s*([А-ЯЁ])\.\s*({_NAME_TOKEN})(?![{CYRILLIC}\-]))"
)
PATRONYMIC_RE = re.compile(
    r"(?:вич|вна|ична|инична|оглы|кызы|улы|уулу)$", re.IGNORECASE
)
# Числа из реквизитов (ИНН, ОГРН, КПП, счета) похожи на телефоны, но ими не являются.
REQUISITE_CONTEXT_RE = re.compile(
    r"(?:инн|огрн(?:ип)?|кпп|окпо|октмо|окато|оквэд|окогу|окфс|окопф|бик|"
    r"р/с|к/с|л/с|расч[её]тный\s+сч[её]т|корр?\.?\s*сч[её]т|лицевой\s+сч[её]т|"
    r"сч[её]т|рег\.?\s*номер|регистрационный\s+номер|№\s*лицензии|лицензи[яи])"
    r"[\s:№.\-]*$",
    re.IGNORECASE,
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
DATE_DOT_RE = re.compile(r"(?<![\d.])(\d{1,2})([./])(\d{1,2})\2(\d{4}|\d{2})(?![\d.]\d)")
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
    "/profile/edit",
    "/cart",
    "/basket",
    "/checkout",
    "/register",
    "javascript:",
    "mailto:",
    "tel:",
)

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "yclid",
    "ysclid",
    "gclid",
    "fbclid",
    "_openstat",
    "from",
    "ref",
    "_ga",
}

# Публичные суффиксы второго уровня: «spb.ru» — не домен организации.
PUBLIC_SECOND_LEVEL = {
    "spb.ru", "msk.ru", "com.ru", "net.ru", "org.ru", "pp.ru", "edu.ru",
    "ac.ru", "gov.ru", "mil.ru", "int.ru", "nsk.ru", "nnov.ru", "kazan.ru",
    "perm.ru", "samara.ru", "tomsk.ru", "omsk.ru", "e-burg.ru", "ekb.ru",
    "krasnoyarsk.ru", "novosibirsk.ru", "nsk.su", "com.ua", "co.uk", "ac.uk",
    "org.uk", "com.kz", "edu.kz", "org.kz", "by.ru", "spb.su", "msk.su",
}


def registrable_domain(host: str) -> str:
    """Домен организации без поддоменов: ``lib.ifmo.spb.ru`` → ``ifmo.spb.ru``."""

    host = (host or "").lower().strip(".").split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    if ".".join(parts[-2:]) in PUBLIC_SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


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
    content: bytes = field(default=b"", repr=False)


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


# Сокращения, после которых точка не завершает предложение.
SENTENCE_ABBREVIATIONS = {
    "г", "гг", "им", "ул", "д", "т", "см", "пр", "просп", "проф", "акад", "доц",
    "др", "н", "э", "к", "т.е", "т.к", "т.н", "т.д", "т.п", "руб", "млн", "млрд",
    "тыс", "стр", "рис", "табл", "англ", "лат", "ст", "чл", "корр", "напр",
    "наб", "пл", "пер", "обл", "р", "св", "ок", "кв", "каб", "тел", "доб",
}


def sentence_boundaries(text: str) -> list[int]:
    """Позиции начала предложений (после «. »), без разрыва на сокращениях.

    «Работал в г. Москве», «им. Ломоносова», инициалы «А. С. Пушкин» не
    делят текст; «МГУ. Затем» — делит.
    """

    starts: list[int] = []
    for match in re.finditer(r"(?<=[А-ЯЁа-яёA-Za-z0-9»)\]])\.\s+(?=[«\"(]?[А-ЯЁA-Z])", text or ""):
        word_match = re.search(r"([А-ЯЁа-яёA-Za-z.]+)$", text[: match.start()])
        word = word_match.group(1) if word_match else ""
        bare = word.lower().strip(".")
        if bare in SENTENCE_ABBREVIATIONS or bare.split(".")[-1] in SENTENCE_ABBREVIATIONS:
            continue
        if len(word) == 1 and word.isupper():
            continue  # инициал
        starts.append(match.end())
    return starts


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
        if key.startswith(TRACKING_QUERY_PREFIXES) or key in TRACKING_QUERY_KEYS:
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
    if not left or not right:
        return False
    if left == right:
        return True
    # Поддомены одной организации считаются одним сайтом, но публичный
    # суффикс («spb.ru», «ru») организацией не является.
    left_root = registrable_domain(left)
    right_root = registrable_domain(right)
    if left_root != right_root or "." not in left_root:
        return False
    return left.endswith("." + right) or right.endswith("." + left) or left_root in {left, right}


def is_probably_content_link(url: str) -> bool:
    """Отсекает поиск, теги, файлы и служебные ссылки."""

    lowered = url.lower().strip()
    if not lowered or lowered.startswith("#"):
        return False
    if any(part in lowered for part in JUNK_URL_PARTS):
        return False
    path = urlparse(lowered).path
    if re.search(r"\.(?:jpg|jpeg|png|gif|svg|webp|pdf|docx?|xlsx?|pptx?|zip|rar|mp4|mp3)$", path):
        return False
    return True


def url_path_lower(url: str) -> str:
    """Путь URL без query/fragment в нижнем регистре (для проверки расширений)."""

    return urlparse(url or "").path.lower()


def _image_src(image: object) -> str:
    """Лучший источник картинки: lazy-атрибуты, затем крупнейший из srcset."""

    for attribute in ("data-src", "data-original", "data-lazy-src", "data-lazy"):
        value = image.get(attribute)  # type: ignore[attr-defined]
        if value:
            return str(value)
    srcset = str(image.get("srcset") or image.get("data-srcset") or "")  # type: ignore[attr-defined]
    if srcset:
        best, best_width = "", -1
        for candidate in srcset.split(","):
            pieces = candidate.strip().split()
            if not pieces:
                continue
            width = 0
            if len(pieces) > 1:
                match = re.match(r"(\d+)", pieces[1])
                width = int(match.group(1)) if match else 0
            if width > best_width:
                best, best_width = pieces[0], width
        if best:
            return best
    return str(image.get("src") or "")  # type: ignore[attr-defined]


def find_photo(soup: object, base_url: str, full_name: str) -> str:
    """Ищет качественное фото человека в HTML страницы.

    Учитывает ``<img>``, CSS ``background-image``, ``og:image`` и
    ``link[rel=image_src]``. Возвращает абсолютный URL или пустую строку.
    """

    tokens = [
        token.lower()
        for token in re.findall(r"[А-ЯЁA-Zа-яёa-z\-]{4,}", full_name or "")
    ]
    tokens += [slugify(token) for token in tokens]
    candidates: list[tuple[int, str]] = []

    def add(
        src: str,
        alt: str = "",
        attrs: str = "",
        width: str = "",
        height: str = "",
    ) -> None:
        src = normalize_whitespace(src)
        if not src or src.lower().startswith("data:"):
            return
        lowered = f"{src} {alt} {attrs}".lower()
        if any(
            marker in lowered
            for marker in (
                "logo",
                "icon",
                "sprite",
                "placeholder",
                "blank",
                "spacer",
                "social",
                "favicon",
            )
        ):
            return
        if url_path_lower(src).endswith((".svg", ".gif", ".ico")):
            return
        score = 0
        name_hit = any(token in lowered for token in tokens)
        if name_hit:
            score += 6
        elif re.search(r"/(?:news|novosti|press|events|afisha)/", lowered):
            # Фото из новостей и афиши — не портрет человека.
            return
        if any(
            marker in lowered
            for marker in ("rector", "ректор", "avatar", "person", "staff", "portrait", "rukovod", "портрет")
        ):
            score += 4
        try:
            if int(width or 0) >= 100 and int(height or 0) >= 100:
                score += 2
        except ValueError:
            pass
        if any(
            marker in lowered
            for marker in ("900", "1200", "1600", "original", "large")
        ):
            score += 2
        if score:
            candidates.append((score, canonical_url(src, base_url)))

    for image in soup.find_all("img"):  # type: ignore[attr-defined]
        add(
            _image_src(image),
            str(image.get("alt") or image.get("title") or ""),
            f"{image.get('class') or ''} {image.get('id') or ''}",
            str(image.get("width") or ""),
            str(image.get("height") or ""),
        )

    for node in soup.find_all(style=True):  # type: ignore[attr-defined]
        for value in re.findall(r"url\(([^)]+)\)", str(node.get("style") or "")):
            add(value.strip("'\" "), "", "background")
    for style in soup.find_all("style"):  # type: ignore[attr-defined]
        for value in re.findall(r"url\(([^)]+)\)", style.get_text(" ", strip=True)):
            add(value.strip("'\" "), "", "background")

    for meta in soup.find_all("meta"):  # type: ignore[attr-defined]
        if meta.get("property") in {"og:image", "twitter:image"}:
            add(str(meta.get("content") or ""), str(meta.get("property") or ""), "meta")
    for link in soup.find_all("link"):  # type: ignore[attr-defined]
        if "image_src" in (link.get("rel") or []):
            add(str(link.get("href") or ""), "image_src", "link")

    if not candidates:
        return _content_image(soup, base_url, full_name)
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


CONTENT_IMAGE_SKIP = (
    "logo",
    "icon",
    "sprite",
    "placeholder",
    "blank",
    "spacer",
    "social",
    "favicon",
    "default_images",
    "counter",
    "banner",
)


def _content_image(soup: object, base_url: str, full_name: str = "") -> str:
    """Портрет без alt на странице человека.

    Картинка принимается, только если рядом с ней (в ближайших родителях)
    упомянута фамилия человека. Иначе это может быть фото новости или
    баннер, и ректору приписывать его нельзя.
    """

    tokens = [
        token.lower()
        for token in re.findall(r"[А-ЯЁA-Zа-яёa-z\-]{4,}", full_name or "")
        if token.lower() not in {"ректор", "rector"}
    ]
    if not tokens:
        return ""
    containers = soup.select(  # type: ignore[attr-defined]
        "main, article, [class*=content], [class*=article], [class*=body]"
    ) or [soup]
    for container in containers:
        for image in container.find_all("img"):  # type: ignore[attr-defined]
            src = _image_src(image).strip()
            if not src:
                continue
            lowered = src.lower()
            if any(marker in lowered for marker in CONTENT_IMAGE_SKIP):
                continue
            if not url_path_lower(src).endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            alt = str(image.get("alt") or "").lower()
            if "программа" in alt or "проект" in alt or "новост" in alt:
                continue
            parent = image.parent
            nearby = ""
            for _ in range(3):
                if parent is None:
                    break
                nearby = parent.get_text(" ", strip=True).lower()
                if len(nearby) > 40:
                    break
                parent = parent.parent
            if any(token in nearby[:1500] for token in tokens):
                return canonical_url(src, base_url)
    return ""


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


def is_requisite_number(text: str, start: int) -> bool:
    """Число стоит после подписи реквизита (ИНН, ОГРН, КПП, счёт и т. п.)."""

    window = (text or "")[max(0, start - 40):start]
    return bool(REQUISITE_CONTEXT_RE.search(window))


def iter_phone_matches(text: str) -> Iterator[re.Match[str]]:
    """Совпадения PHONE_RE, похожие на телефон и не являющиеся реквизитами."""

    for match in PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group(0))
        if not _plausible_phone_digits(digits):
            continue
        if is_requisite_number(text, match.start()):
            continue
        yield match


def clean_phone(value: str) -> str:
    """Извлекает первый телефон из строки и нормализует его."""

    if not value:
        return ""
    match = next(iter_phone_matches(value), None)
    if match is None:
        return ""
    return normalize_phone(match.group(0))


def extract_phones(text: str) -> list[str]:
    """Извлекает все телефоны из текста (без ИНН/ОГРН/счетов)."""

    if not text:
        return []
    phones = [normalize_phone(match.group(0)) for match in iter_phone_matches(text)]
    return dedupe_preserve(phones)


def phone_key(value: str) -> str:
    """Ключ сравнения телефонов: ``8 (495) 1…`` и ``+7 (495) 1…`` совпадают."""

    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


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

    for match in DATE_DOT_RE.finditer(text):
        day_text, _, month_text, year_text = match.groups()
        # «1.2.10» — скорее номер версии/пункта, чем дата: для двузначного
        # года требуем двузначные день и месяц.
        if len(year_text) == 2 and (len(day_text) < 2 or len(month_text) < 2):
            continue
        day, month, year = int(day_text), int(month_text), int(year_text)
        if year < 100:
            current_short = datetime.now(timezone.utc).year % 100
            year += 2000 if year <= current_short + 1 else 1900
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            continue

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
        guessed_year = False
        if year_text:
            year = int(year_text)
        elif default_year:
            year = default_year
        else:
            year = datetime.now(timezone.utc).year
            guessed_year = True
        try:
            result = datetime(year, month, day).date()
        except ValueError:
            return None
        if guessed_year:
            # «15 декабря» в январской ленте — это прошлый год, а не будущее.
            today = datetime.now(timezone.utc).date()
            if (result - today).days > 31:
                try:
                    result = result.replace(year=year - 1)
                except ValueError:
                    return None
        return result.isoformat()
    return None


def strip_name(value: str) -> str:
    """Убирает лишние подписи вокруг ФИО."""

    text = normalize_whitespace(value)
    # Граница слова обязательна: «Ректорова Анна» — фамилия, а не «ректор».
    text = re.sub(
        r"^(?:фио|руководитель|ректор|проректор|директор|декан)(?![А-ЯЁа-яё])\s*[:\-–—]?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*\((?:и\.?\s*о\.?|исполняющий обязанности)\)\s*", " ", text, flags=re.I)
    text = text.strip(" ,;:-")
    # Точку инициала («Петров П. П.») сохраняем, остальные — убираем.
    if not re.search(r"(?<![А-ЯЁа-яёA-Za-z])[А-ЯЁA-Z]\.$", text):
        text = text.rstrip(".").strip(" ,;:-")
    return text


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
        for bad in ("неприменимо", "вакансия", "вакантно", "отсутствует", "нет данных", "не указан")
    ):
        return False

    match = PERSON_NAME_RE.fullmatch(text)
    initials = None if match else PERSON_INITIALS_RE.fullmatch(text)
    if match or initials:
        tokens = [part for part in (match or initials).groups() if part]
        words = [token for token in tokens if len(token) >= 2]
        if any(token.lower() in NAME_BLACKLIST for token in words):
            return False
        # Слова-организации («Совет», «Центр») отсекаем только целиком:
        # фамилия «Советов» или «Центров» остаётся допустимой, если у
        # человека есть отчество.
        has_patronymic = any(PATRONYMIC_RE.search(token) for token in words)
        if not has_patronymic and any(_is_org_word(token) for token in words):
            return False
        # Полное русское ФИО из трёх слов содержит отчество: «Творчество
        # Спорт Устойчивое» из меню — не человек.
        if match and not has_patronymic:
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


ORG_WORD_STEMS = (
    "университет", "институт", "факультет", "кафедр", "департамент",
    "управлени", "центр", "отдел", "совет", "комисси", "лаборатори",
    "программ", "проект", "академи", "колледж", "филиал",
)


def _is_org_word(token: str) -> bool:
    lowered = token.lower()
    return any(
        re.fullmatch(stem + r"[а-яё]{0,3}", lowered) for stem in ORG_WORD_STEMS
    )


def find_person_names(text: str) -> list[str]:
    """Находит в тексте последовательности, похожие на ФИО.

    Поиск идёт с перекрытием: в «Ректор Иванов Иван Иванович» сначала
    встречается тройка «Ректор Иванов Иван», а настоящее ФИО — со сдвигом.
    """

    if not text:
        return []
    found: list[tuple[int, str]] = []
    covered_until = -1
    tokens = list(re.finditer(rf"{_NAME_TOKEN}", text))
    for index in range(len(tokens) - 2):
        first, third = tokens[index], tokens[index + 2]
        if first.start() < covered_until:
            continue
        candidate = text[first.start():third.end()]
        if not re.fullmatch(rf"{_NAME_TOKEN}\s+{_NAME_TOKEN}\s+{_NAME_TOKEN}", candidate):
            continue
        if looks_like_person_name(candidate):
            found.append((first.start(), strip_name(candidate)))
            covered_until = third.end()
    for match in PERSON_INITIALS_RE.finditer(text):
        if any(start <= match.start() < start + len(name) for start, name in found):
            continue
        candidate = normalize_whitespace(match.group(0))
        if looks_like_person_name(candidate):
            found.append((match.start(), candidate))
    found.sort(key=lambda item: item[0])
    return dedupe_preserve(name for _, name in found)


def contains_position(text: str) -> bool:
    """Есть ли в тексте название руководящей должности."""

    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in POSITION_KEYWORDS)


def detect_encoding(response: requests.Response, content: bytes | None = None) -> str:
    """Определяет кодировку ответа (включая старые cp1251-сайты)."""

    encoding = (response.encoding or "").lower()
    if not encoding or encoding in {"iso-8859-1", "ascii"}:
        if content is not None:
            from bs4 import UnicodeDammit
            encoding = (UnicodeDammit(content).original_encoding or "utf-8").lower()
        else:
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
    # Заглушки антибота короткие. Длинная страница, где просто упомянуты
    # Cloudflare или капча (например, форма обратной связи), — обычный контент.
    if len(text) > 3000:
        return False
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
        "что вы не бот",
        "проверка браузера",
        "checking your browser",
    )
    return any(marker in text for marker in markers)


ROBOTS_AGENT = "VuzCardBot"
ROBOTS_USER_AGENT = "VuzCardBot/1.0 (+research; contact: local)"
MAX_RETRY_AFTER = 60.0


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

    def __init__(self, timeout: float = 10.0, session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self.session = session
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
            get = self.session.get if self.session is not None else requests.get
            response = get(
                robots_url,
                headers={"User-Agent": ROBOTS_USER_AGENT},
                timeout=self.timeout,
            )
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                delay = parser.crawl_delay(ROBOTS_AGENT) or parser.crawl_delay("*")
                if delay:
                    self._delays[host] = min(float(delay), 30.0)
            elif response.status_code >= 500:
                # RFC 9309: сервер robots.txt недоступен — обход временно запрещён.
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(["User-agent: *", "Disallow: /"])
                LOGGER.warning("robots.txt вернул %s (%s), обход отложен", response.status_code, robots_url)
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
        allowed = parser.can_fetch(ROBOTS_AGENT, url)
        return allowed, self._delays.get(host, 0.0)


class HttpClient:
    """HTTP-клиент: задержки, User-Agent, retry, robots.txt, блокировки."""

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self.limiter = RateLimiter(settings.request_delay, settings.delay_jitter)
        self.robots = RobotsPolicy(timeout=min(settings.timeout, 15.0), session=self.session)
        self._counter_lock = threading.Lock()
        self._ua_index = 0
        self._ua_lock = threading.Lock()
        self._requests_made = 0
        self.last_error: str = ""
        self.last_error_code: str = ""

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
        self.last_error_code = ""
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
            with self._counter_lock:
                if self._requests_made >= self.settings.max_total_requests:
                    self.last_error = "Достигнут лимит запросов на обход"
                    break
                self._requests_made += 1
            is_last_attempt = attempt >= self.settings.max_retries
            self.limiter.wait(url)
            started = time.monotonic()
            response = None
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(url),
                    timeout=self.settings.timeout,
                    allow_redirects=True,
                    stream=True,
                )
                content = bytearray()
                for block in response.iter_content(chunk_size=65536):
                    content.extend(block)
                    if len(content) > self.settings.max_response_bytes:
                        self.last_error_code = "response_too_large"
                        self.last_error = f"Слишком большой ответ (лимит {self.settings.max_response_bytes} байт): {url}"
                        LOGGER.warning(self.last_error)
                        return None
                content = bytes(content)
                elapsed = time.monotonic() - started
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                is_pdf = content.startswith(b"%PDF-") or content_type.lower() == "application/pdf"
                encoding = "" if is_pdf else detect_encoding(response, content)
                try:
                    body = "" if is_pdf else content.decode(encoding, errors="replace")
                except LookupError:
                    encoding, body = "utf-8", content.decode("utf-8", errors="replace")
                LOGGER.info(
                    "GET %s -> %s (%.0f КБ, %.1f с)",
                    url,
                    response.status_code,
                    len(content) / 1024,
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
                    if not is_last_attempt:
                        time.sleep(
                            min(retry_after or _backoff_seconds(attempt, self.settings.backoff_factor), MAX_RETRY_AFTER)
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
                    if not is_last_attempt:
                        time.sleep(
                            min(retry_after or _backoff_seconds(attempt, self.settings.backoff_factor), MAX_RETRY_AFTER)
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
                    content=content if is_pdf else b"",
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
                if not is_last_attempt:
                    time.sleep(
                        _backoff_seconds(attempt, self.settings.backoff_factor)
                    )
            finally:
                if response is not None:
                    response.close()

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
    # Отрезаем только последний сегмент «| Название сайта»: в заголовке
    # «Ректор — Иванов И. И. | ВШЭ» нужная часть стоит до последнего разделителя.
    parts = re.split(r"\s+[|—–-]\s+", title)
    if len(parts) > 1 and len(" ".join(parts[:-1])) >= 3:
        title = re.sub(r"\s+[|—–-]\s+(?!.*\s[|—–-]\s).*$", "", title).strip()
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
