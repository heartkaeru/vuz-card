"""Главный модуль парсера «Карточка ВУЗа».

Запуск из корня проекта::

    python -m backend.parser.main --profile spbu --with-bios
    python -m backend.parser.main --url https://example.ru --name "Пример"

Модуль собирает сведения о руководстве, контактах, проектах и мероприятиях,
очищает HTML, формирует JSON-карточку и документы с фрагментами для LLM.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

from bs4 import BeautifulSoup
from bs4.element import Tag

if __package__ in (None, ""):  # запуск как `python backend/parser/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from parser import cleaner, config, models, store, utils  # type: ignore

    # Ниже используются обычные импорты (`from cleaner import ...`), чтобы
    # модуль можно было запускать и как пакет, и как отдельный файл.
    sys.modules.setdefault("cleaner", cleaner)
    sys.modules.setdefault("config", config)
    sys.modules.setdefault("models", models)
    sys.modules.setdefault("store", store)
    sys.modules.setdefault("utils", utils)
else:
    from . import cleaner, config, models, store, utils

    sys.modules.setdefault("cleaner", cleaner)
    sys.modules.setdefault("config", config)
    sys.modules.setdefault("models", models)
    sys.modules.setdefault("store", store)
    sys.modules.setdefault("utils", utils)

from cleaner import HTMLCleaner
from config import (
    DEFAULT_BATCH_PROFILES,
    DEFAULT_CONTENT_SELECTORS,
    DEFAULT_ITEM_SELECTORS,
    EVENT_TYPE_KEYWORDS,
    PROJECT_TYPE_KEYWORDS,
    SECTION_LINK_PATTERNS,
    SVEDEN_SECTIONS,
    UNIVERSITY_PROFILES,
    SectionSpec,
    Settings,
    UniversityProfile,
)
from models import (
    Achievement,
    ChangeReport,
    Contact,
    ContactOwner,
    Event,
    Grant,
    Leadership,
    Partnership,
    Project,
    ResearchItem,
    SourceDocument,
    Unit,
    UniversityData,
    UniversityInfo,
    utc_now,
)
from store import CrawlCache, DocumentStore
from utils import (
    BlockedError,
    FetchResult,
    HttpClient,
    canonical_url,
    clean_email,
    clean_phone,
    dedupe_preserve,
    extract_emails,
    extract_phones,
    find_person_names,
    first_non_empty,
    is_probably_content_link,
    is_same_domain,
    looks_like_person_name,
    normalize_whitespace,
    parse_date,
    pick_title,
    safe_filename,
    setup_logging,
    sha256_text,
    slugify,
    strip_name,
    truncate_text,
)

LOGGER = logging.getLogger("parser")

GENERIC_LINK_TITLES = {
    "подробнее",
    "читать далее",
    "все новости",
    "все мероприятия",
    "смотреть",
    "перейти",
    "ссылка",
    "назад",
    "вперед",
    "вперёд",
    "далее",
    "ещё",
    "еще",
    "на главную",
    "конференция",
    "семинар",
    "лекция",
    "выставка",
    "встреча",
    "экскурсия",
    "форум",
    "олимпиада",
    "тренинг",
}

POSITION_RANK = {
    "первый проректор": 2,
    "проректор": 3,
    "научный руководитель": 4,
    "ректор": 0,
    "президент": 1,
    "директор": 5,
    "декан": 6,
    "руководитель": 7,
    "начальник": 8,
    "заведующий": 9,
    "председатель": 10,
    "советник": 20,
    "помощник": 21,
    "заместитель": 22,
}

OWNER_RANK = {
    "person": 5,
    "office": 4,
    "department": 3,
    "organization": 2,
    "unknown": 1,
}

# Приоритет сбора: руководство и контакты → проекты/партнёры → новости →
# научные центры и достижения → общие сведения.
SECTION_ORDER = {
    "leadership": 0,
    "contacts": 1,
    "projects": 2,
    "grants": 3,
    "partnerships": 4,
    "news": 5,
    "project_news": 5,
    "research": 6,
    "achievements": 7,
    "events": 8,
    "about": 9,
    "homepage": 10,
}

AMOUNT_RE = re.compile(
    r"(\d[\d\s]*(?:[.,]\d+)?\s*(?:млн|млрд|тыс\.?)?\s*"
    r"(?:руб(?:лей|\.)?|₽|rub))",
    re.IGNORECASE,
)
ORGANIZER_RE = re.compile(
    r"организатор[а-яё]*\s*[:\-–—]?\s*([^.;\n]{3,140})",
    re.IGNORECASE,
)
WINNERS_RE = re.compile(
    r"победител[а-яё]*\s*[:\-–—]?\s*([^.;\n]{3,180})",
    re.IGNORECASE,
)
PARTNERS_RE = re.compile(
    r"партн[её]р[а-яё]*\s*[:\-–—]?\s*([^.;\n]{3,220})",
    re.IGNORECASE,
)
HEAD_RE = re.compile(
    r"(?:руководитель|заведующий|head)\s*[:\-–—]?\s*"
    r"([А-ЯЁA-Z][^.;\n]{3,90})",
    re.IGNORECASE,
)


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def _cell_text(cell: Tag) -> str:
    return normalize_whitespace(cell.get_text(" ", strip=True))


def _cell_link(cell: Tag, base_url: str) -> str:
    anchor = cell.find("a", href=True)
    if anchor:
        href = normalize_whitespace(anchor["href"])
        if href and not href.lower().startswith(("javascript:", "mailto:", "tel:")):
            return canonical_url(href, base_url)
    text = _cell_text(cell)
    if text.startswith(("http://", "https://", "www.")):
        return canonical_url(text, base_url)
    return ""


def _looks_like_table_name(value: str) -> bool:
    """ФИО в таблице: 2–4 слова с заглавной буквы, без организаций."""

    text = strip_name(value)
    if not text or len(text) > 90:
        return False
    lowered = text.lower()
    exact_bad = {
        "неприменимо",
        "вакансия",
        "вакантно",
        "отсутствует",
        "нет данных",
        "не указан",
        "нет",
        "нет данных",
    }
    if lowered in exact_bad:
        return False
    org_words = (
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
        "филиал",
        "представительство",
    )
    if any(word in lowered for word in org_words):
        return False
    tokens = re.findall(r"[А-ЯЁA-Z][а-яёa-z\-]+", text)
    if not 2 <= len(tokens) <= 4:
        return False
    if any(token.lower() in utils.NAME_BLACKLIST for token in tokens):
        return False
    return True


def _position_rank(position: str) -> int:
    lowered = (position or "").lower()
    # Явные проверки с границами слов: «ректор» не должен совпадать
    # внутри «проректор», а «председатель, ректор» — получать ранг
    # председателя.
    if re.search(r"\bи\.?\s*о\.?\s+ректор", lowered) or re.search(
        r"\bректор\b", lowered
    ):
        return 0
    if re.search(r"\bпрезидент\b", lowered):
        return 1
    if re.search(r"\bпервый\s+проректор", lowered):
        return 2
    if re.search(r"\bпроректор\b", lowered):
        return 3
    if re.search(r"\bнаучный\s+руководитель\b", lowered):
        return 4
    # Проверяем длинные названия раньше коротких: иначе «директор»
    # совпадёт внутри «директор по развитию» и т. п.
    for keyword, rank in sorted(
        POSITION_RANK.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if keyword in lowered:
            return rank
    return 50


def _classify(text: str, mapping: dict[str, tuple[str, ...]], default: str) -> str:
    lowered = (text or "").lower()
    for label, keywords in mapping.items():
        if any(keyword in lowered for keyword in keywords):
            return label
    return default


GENERIC_MAILBOXES = {
    "info",
    "office",
    "rector",
    "rectorat",
    "priem",
    "priemnaya",
    "press",
    "contact",
    "contacts",
    "mail",
    "admin",
    "spbu",
    "hse",
    "rsuh",
    "university",
}


def _mailbox(email: str) -> str:
    """Локальная часть email в нижнем регистре без служебных символов."""

    local = (email or "").split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9]", "", local)


def _email_matches_name(email: str, full_name: str) -> bool:
    """Похож ли email на личный адрес человека (по транслитерации ФИО)."""

    local = _mailbox(email)
    if not local:
        return False
    for token in re.findall(r"[А-ЯЁа-яёA-Za-z\-]{4,}", full_name or ""):
        translit = re.sub(r"[^a-z0-9]", "", slugify(token))
        if len(translit) >= 4 and translit in local:
            return True
    return False


# Служебные email-префиксы: такие адреса принадлежат подразделению,
# приёмной или вузу в целом и не должны попадать в личную карточку.
GENERIC_EMAIL_PREFIXES = (
    "office",
    "info",
    "rector",
    "priem",
    "press",
    "contact",
    "mail",
    "admin",
    "support",
    "webmaster",
    "post",
    "kanc",
    "general",
)


def _is_generic_email(email: str) -> bool:
    """Служебный (не персональный) email: office@, info@, rector@ и т. п."""

    local = _mailbox(email)
    if not local:
        return True
    mailbox = (email or "").split("@", 1)[0].lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", mailbox) if token]
    generic_tokens = GENERIC_MAILBOXES | set(GENERIC_EMAIL_PREFIXES) | {
        "protocol",
        "secretary",
        "sekretar",
        "reception",
        "dept",
        "department",
        "division",
        "upr",
        "otdel",
        "kaf",
        "kafedra",
        "laboratory",
        "help",
        "helpdesk",
        "event",
        "events",
        "study",
        "student",
        "students",
    }
    if any(token in generic_tokens for token in tokens):
        return True
    return local in generic_tokens


# Подписи контактных строк на страницах биографий.
PHONE_LABEL_PATTERN = r"(?:телефон|тел\.?|phone|tel\.?)"
FAX_LABEL_PATTERN = r"(?:факс|fax)"
PHONE_VALUE_RE = re.compile(
    rf"{PHONE_LABEL_PATTERN}\s*[:№]?\s*([+\d][\d\s()\-]{{7,}})",
    re.IGNORECASE,
)
FAX_VALUE_RE = re.compile(
    rf"{FAX_LABEL_PATTERN}\s*[:№]?\s*([+\d][\d\s()\-]{{7,}})",
    re.IGNORECASE,
)
# «каб. А308», «каб. A308», «кабинет 12», «office 5», «к. А308».
OFFICE_RE = re.compile(
    r"(?:каб(?:инет)?\.?|офис|office|room|к\.)\s*[:№]?\s*"
    r"([А-ЯЁA-Z0-9][А-ЯЁA-Z0-9\-/]*)",
    re.IGNORECASE,
)
CONTACT_LABEL_RE = re.compile(
    r"(?:^|\s)(?:"
    r"телефон|тел\.|факс|fax|phone|tel\.|"
    r"e-?mail|эл\.?\s*почта|почта|"
    r"адрес|address|каб\.|кабинет|офис|office|room"
    r")\s*[:№]",
    re.IGNORECASE,
)


def _normalize_office_number(value: str) -> str:
    """Приводит номер кабинета к виду ``каб. А308``."""

    match = OFFICE_RE.search(normalize_whitespace(value or ""))
    if not match:
        return ""
    number = normalize_whitespace(match.group(1)).upper()
    if not number:
        return ""
    return f"каб. {number}"


def _contacts_from_contact_text(text: str) -> dict[str, str]:
    """Извлекает телефон, факс, кабинет и личный email из контактной строки."""

    values: dict[str, str] = {}
    normalized = normalize_whitespace((text or "").replace("\xa0", " "))
    if not normalized:
        return values

    # Факс проверяем раньше телефона: «Тел: … Факс: …» содержит обе подписи.
    fax_match = FAX_VALUE_RE.search(normalized)
    if fax_match:
        fax = clean_phone(fax_match.group(1))
        if fax:
            values["fax"] = fax

    phone_match = PHONE_VALUE_RE.search(normalized)
    if phone_match:
        phone = clean_phone(phone_match.group(1))
        if phone:
            values["office_phone"] = phone
    if "office_phone" not in values:
        # Кириллическая подпись «Тел.» без двоеточия встречается в карточках
        # сотрудников: «Тел. +7 (495) …».
        phone = clean_phone(normalized)
        if phone and re.search(PHONE_LABEL_PATTERN, normalized, re.IGNORECASE):
            values["office_phone"] = phone

    office_number = _normalize_office_number(normalized)
    if office_number:
        values["office_number"] = office_number

    for email in extract_emails(normalized):
        if _is_generic_email(email):
            continue
        values["email"] = email
        break

    return values


def _contact_chunks(doc: models.SourceDocument) -> list[str]:
    """Короткие текстовые блоки, похожие на контактную информацию."""

    raw: list[str] = [block.text for block in doc.blocks if block.text]
    raw.extend(re.split(r"[\r\n]+", doc.text or ""))

    chunks: list[str] = []
    seen: set[str] = set()
    for text in raw:
        value = normalize_whitespace(text)
        if not value or len(value) > 300:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        label_hit = bool(CONTACT_LABEL_RE.search(value))
        office_hit = bool(OFFICE_RE.search(value))
        phone_hit = bool(re.search(r"\+7\s*\(\d{3}\)", value)) or bool(
            extract_phones(value)
        )
        if not (label_hit or office_hit or phone_hit):
            continue
        # Длинный абзац, где слово «телефон» просто упомянуто, — не контакты.
        if len(value) > 160 and not label_hit and not office_hit:
            continue
        seen.add(lowered)
        chunks.append(value)
    return chunks


def _owner_from_row(row: dict[str, str], email_count: int) -> tuple[str, str]:
    """Владелец контакта из строки таблицы.

    Если одинаковый email повторяется в нескольких строках, это общий
    контакт подразделения, а не личный контакт руководителя. Личным
    контактом считаем только адрес, похожий на транслит ФИО.
    """

    unit = normalize_whitespace(row.get("unit", ""))
    position = normalize_whitespace(row.get("position", ""))
    name = normalize_whitespace(row.get("name", ""))
    email = row.get("email", "")
    if email_count <= 1 and name and _looks_like_table_name(name):
        if _email_matches_name(email, name):
            return name, "person"
        if _position_rank(position) <= 1 and _mailbox(email) in GENERIC_MAILBOXES:
            return position or name, "office"
    if unit:
        return unit, "department"
    if position and any(
        word in position.lower()
        for word in ("приёмная", "приемная", "ректорат", "управление", "отдел", "департамент")
    ):
        return position, "office"
    return "ВУЗ", "organization"


def _name_key(value: str) -> str:
    return re.sub(r"\s+", " ", strip_name(value).lower())


def _title_key(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_whitespace(value).lower())[:200]


BIO_URL_RE = re.compile(
    r"(staff|person|people|rector|prorector|pro-rector|biograf|rukovod|"
    r"leadership|sotrudnik|employee|team|prepod|professor)",
    re.IGNORECASE,
)

BIO_TEXT_KEYWORDS = (
    "родил",
    "окончил",
    "образование",
    "доктор наук",
    "кандидат наук",
    "профессор",
    "доцент",
    "академик",
    "награжд",
    "научные интересы",
    "трудовая деятельность",
    "биография",
    "стаж работы",
)

NAV_LIKE_KEYWORDS = (
    "приемная комиссия",
    "образовательные программы",
    "виртуальная приемная",
    "подразделения и руководство",
    "олимпиада школьников",
    "документы",
    "контакты",
    "поступающим",
    "студентам",
    "выпускникам",
    "работодателям",
)


def _is_bio_like_url(url: str) -> bool:
    """Похож ли URL на личную страницу/биографию, а не на раздел целиком."""

    return bool(BIO_URL_RE.search(url or ""))


def _is_navigation_like(text: str) -> bool:
    """Плотный список пунктов меню, попавший в текстовый блок."""

    lowered = (text or "").lower()
    hits = sum(1 for keyword in NAV_LIKE_KEYWORDS if keyword in lowered)
    return hits >= 4 and len(text) < 600


def _first_sentence(text: str, limit: int = 160) -> str:
    """Берёт первое предложение (или аккуратно обрезает по слову)."""

    cleaned = normalize_whitespace(text)
    match = re.search(r"[.!?](?:\s|$)", cleaned[: limit + 40])
    if match and match.start() >= 15:
        return cleaned[: match.start() + 1].strip()
    return truncate_text(cleaned, limit)


DATE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    r"|\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|"
    r"сентябр|октябр|ноябр|декабр)[а-яё]*\s*\d{4}\s*(?:г\.?)?"
    r")\s*[:—–-]?\s*",
    re.IGNORECASE,
)


def _strip_leading_date(value: str) -> str:
    """Убирает дату из начала заголовка карточки мероприятия."""

    return DATE_PREFIX_RE.sub("", normalize_whitespace(value)).strip(" :—–-")


# ----------------------------------------------------------------------
# Разбор стандартных таблиц /sveden/struct/
# ----------------------------------------------------------------------
class SvedenTableParser:
    """Разбирает таблицы «Структура и органы управления».

    Формат регламентирован Приказом Минобрнауки № 785 и совпадает у многих
    вузов, поэтому парсер не зависит от CSS-классов конкретного сайта.
    """

    def __init__(self, page_url: str) -> None:
        self.page_url = page_url

    def parse(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for table in soup.find_all("table"):
            rows.extend(self._parse_table(table))
        return rows

    def _parse_table(self, table: Tag) -> list[dict[str, str]]:
        table_rows = table.find_all("tr")
        if not table_rows:
            return []

        header_row = None
        header_cells: list[str] = []
        for tr in table_rows[:3]:
            cells = tr.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            texts = [_cell_text(cell) for cell in cells]
            has_name = any(
                ("фио" in text.lower()) or ("руководитель" in text.lower())
                for text in texts
            )
            has_position = any(
                ("должност" in text.lower())
                or ("орган" in text.lower() and "управлен" in text.lower())
                or ("подраздел" in text.lower())
                for text in texts
            )
            if has_name and has_position:
                header_row = tr
                header_cells = texts
                break

        if header_row is None or not header_cells:
            return []

        header_blob = " ".join(header_cells).lower()
        if any(word in header_blob for word in ("филиал", "представительство", "партнер")):
            # Филиалы и представительства не являются ректоратом.
            return []

        columns = self._map_columns(header_cells)
        if "name" not in columns:
            return []

        parsed: list[dict[str, str]] = []
        seen_after_header = False
        for tr in table_rows:
            if tr is header_row:
                seen_after_header = True
                continue
            if not seen_after_header:
                continue
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            data = self._row_to_dict(cells, columns)
            if data.get("name") or data.get("position"):
                parsed.append(data)
        return parsed

    @staticmethod
    def _map_columns(headers: Sequence[str]) -> dict[str, int]:
        columns: dict[str, int] = {}
        for index, header in enumerate(headers):
            text = header.lower()
            if "фио" in text or "руководитель" in text:
                columns.setdefault("name", index)
            elif "должност" in text:
                columns.setdefault("position", index)
            elif "контакт" in text:
                columns.setdefault("contacts", index)
            elif "наименование" in text or "курируем" in text or (
                "орган" in text and "управлен" in text
            ):
                columns.setdefault("unit", index)
            elif "почт" in text or "email" in text or "e-mail" in text:
                columns.setdefault("email", index)
            elif "телефон" in text:
                columns.setdefault("phone", index)
            elif "сайт" in text or ("адрес" in text and "сети" in text):
                columns.setdefault("site", index)
            elif "местонахожд" in text or (
                "адрес" in text and "сайт" not in text
            ):
                columns.setdefault("address", index)
        if "unit" not in columns and len(headers) > 0:
            columns["unit"] = 0
        return columns

    def _row_to_dict(
        self, cells: Sequence[Tag], columns: dict[str, int]
    ) -> dict[str, str]:
        def value(field: str) -> str:
            index = columns.get(field)
            if index is None or index >= len(cells):
                return ""
            return _cell_text(cells[index])

        raw_name = value("name")
        name = strip_name(raw_name)
        position = value("position")
        if not position and "," in raw_name:
            parts = [part.strip() for part in raw_name.split(",") if part.strip()]
            if parts:
                name = strip_name(parts[0])
                position = ", ".join(parts[1:])
        unit = value("unit")
        address = value("address")
        contacts = value("contacts")
        emails = extract_emails(
            " ".join(value(field) for field in ("email", "address", "contacts"))
        )
        phones = extract_phones(
            " ".join(value(field) for field in ("phone", "address", "contacts"))
        )
        site = ""
        site_index = columns.get("site")
        if site_index is not None and site_index < len(cells):
            site = _cell_link(cells[site_index], self.page_url)
        name_link = ""
        name_index = columns.get("name")
        if name_index is not None and name_index < len(cells):
            name_link = _cell_link(cells[name_index], self.page_url)

        return {
            "name": name,
            "position": position,
            "unit": unit,
            "address": address,
            "email": emails[0] if emails else "",
            "phone": phones[0] if phones else "",
            "site": site or name_link,
            "name_link": name_link,
        }


# ----------------------------------------------------------------------
# Основной парсер
# ----------------------------------------------------------------------
class UniversityParser:
    """Собирает карточку ВУЗа по профилю или по указанному URL."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: HttpClient | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.client = client or HttpClient(self.settings)
        self.logger = logging.getLogger("parser")
        self.profile: UniversityProfile | None = None
        self._page_cache: dict[str, tuple[SourceDocument, BeautifulSoup]] = {}
        self._failed_urls: set[str] = set()
        self._documents: dict[str, SourceDocument] = {}
        self._leadership: list[Leadership] = []
        self._contacts: list[Contact] = []
        self._contact_index: dict[tuple[str, str], Contact] = {}
        self._projects: list[Project] = []
        self._grants: list[Grant] = []
        self._research: list[ResearchItem] = []
        self._partnerships: list[Partnership] = []
        self._achievements: list[Achievement] = []
        self._events: list[Event] = []
        self._warnings: list[str] = []
        self._errors: list[str] = []
        self._detail_urls: dict[str, str] = {}
        self._about: str = ""
        self._about_doc: SourceDocument | None = None
        self._base_url: str = ""
        self._university_name: str = ""
        self._official_domain: str = ""
        self._cache: CrawlCache | None = None
        self._store: DocumentStore | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def parse(
        self,
        url: str | None = None,
        name: str | None = None,
        profile: UniversityProfile | str | None = None,
    ) -> UniversityData:
        """Собирает данные и возвращает валидированную карточку."""

        self._reset()
        profile_obj = self._resolve_profile(url, profile)
        self.profile = profile_obj
        base_url = (url or (profile_obj.url if profile_obj else "")).strip()
        if not base_url:
            raise ValueError("Не указан URL вуза и не выбран профиль")

        self._base_url = base_url
        self._university_name = name or (profile_obj.name if profile_obj else "")
        self._official_domain = (
            utils.urlparse(base_url).netloc.lower().split(":")[0]
        )
        if self._official_domain.startswith("www."):
            self._official_domain = self._official_domain[4:]

        if self.settings.use_cache:
            self._cache = CrawlCache(
                self.settings.cache_dir / "pages.json",
                self.settings.cache_ttl_hours,
            )
        if self.settings.use_sqlite:
            self._store = DocumentStore(self.settings.db_path)

        LOGGER.info(
            "=== Начало сбора: %s (%s) ===",
            self._university_name,
            base_url,
        )

        homepage_doc, homepage_soup = self._get_page(base_url, section="homepage")
        if homepage_doc is None:
            self._errors.append(
                f"Не удалось загрузить главную страницу: {base_url}"
            )

        sections = list(profile_obj.sections) if profile_obj else []
        # Дополняем профиль и автопоиск разделами, которых нет в профиле:
        # гранты, партнёры, достижения, научная деятельность и т. д.
        discovered = self._discover_sections(base_url, homepage_soup)
        by_name = {section.name: section for section in sections}
        for section in discovered:
            by_name.setdefault(section.name, section)
        # Если отдельного раздела науки нет, используем страницу проектов:
        # её карточки (лаборатории, центры, исследования) полезны и как
        # научные направления.
        if "research" not in by_name and "projects" in by_name:
            project_section = by_name["projects"]
            by_name["research"] = SectionSpec(
                name="research",
                url=project_section.url,
                item_selectors=project_section.item_selectors,
                content_selectors=project_section.content_selectors,
            )
        sections = list(by_name.values())
        sections.sort(key=lambda section: SECTION_ORDER.get(section.name, 99))

        if not sections:
            self._warnings.append(
                "Не найдены целевые разделы. Проверьте URL или добавьте профиль в config.py."
            )

        for section in sections:
            try:
                self._parse_section(section)
            except Exception as exc:  # noqa: BLE001 - один раздел не должен ронять запуск
                message = f"Ошибка разбора раздела {section.name} ({section.url}): {exc}"
                LOGGER.exception(message)
                self._errors.append(message)

        self._enrich_bios()
        card = self._build_card(base_url, name, profile_obj, homepage_doc)
        if self._cache is not None:
            self._cache.prune()
            self._cache.flush()
        LOGGER.info("Сбор завершён: %s", card.summary())
        return card

    def close(self) -> None:
        if self._cache is not None:
            self._cache.flush()
        if self._store is not None:
            self._store.close()
            self._store = None
        self.client.close()

    # ------------------------------------------------------------------
    # Профили и разделы
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        if self._cache is not None:
            self._cache.flush()
            self._cache = None
        if self._store is not None:
            self._store.close()
            self._store = None
        self._page_cache.clear()
        self._failed_urls.clear()
        self._documents.clear()
        self._leadership.clear()
        self._contacts.clear()
        self._contact_index.clear()
        self._projects.clear()
        self._grants.clear()
        self._research.clear()
        self._partnerships.clear()
        self._achievements.clear()
        self._events.clear()
        self._warnings.clear()
        self._errors.clear()
        self._detail_urls.clear()
        self._about = ""
        self._about_doc = None

    @staticmethod
    def _resolve_profile(
        url: str | None, profile: UniversityProfile | str | None
    ) -> UniversityProfile | None:
        if isinstance(profile, UniversityProfile):
            return profile
        if isinstance(profile, str):
            key = profile.strip().lower()
            if key in UNIVERSITY_PROFILES:
                return UNIVERSITY_PROFILES[key]
            raise ValueError(
                f"Неизвестный профиль '{profile}'. Доступны: "
                + ", ".join(sorted(UNIVERSITY_PROFILES))
            )
        if not url:
            return None
        host = utils.urlparse(url).netloc.lower().split(":")[0]
        host = host[4:] if host.startswith("www.") else host
        for candidate in UNIVERSITY_PROFILES.values():
            profile_host = utils.urlparse(candidate.url).netloc.lower().split(":")[0]
            profile_host = (
                profile_host[4:] if profile_host.startswith("www.") else profile_host
            )
            if host == profile_host or host.endswith("." + profile_host):
                return candidate
            if any(alias in host for alias in candidate.aliases if "." in alias):
                return candidate
        return None

    def _discover_sections(
        self, base_url: str, homepage_soup: BeautifulSoup | None
    ) -> list[SectionSpec]:
        """Ищет разделы по ссылкам главной страницы и sitemap.xml."""

        if homepage_soup is None:
            homepage_doc, fetched_soup = self._get_page(base_url, section="homepage")
            if homepage_doc is None:
                return []
            homepage_soup = fetched_soup

        soup = homepage_soup or _make_soup("")
        links: list[tuple[str, str, str]] = []
        seen_urls: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = normalize_whitespace(anchor.get("href", ""))
            text = normalize_whitespace(anchor.get_text(" ", strip=True))
            if not href or not text or len(text) > 120:
                continue
            absolute = canonical_url(href, base_url)
            if absolute in seen_urls:
                continue
            if not is_same_domain(absolute, base_url):
                continue
            if not is_probably_content_link(absolute):
                continue
            # Раздел ищем на основном домене и его официальных поддоменах,
            # но не в статье/карточке.
            anchor_path = utils.urlparse(absolute).path
            anchor_segments = [part for part in anchor_path.split("/") if part]
            if (
                anchor_path.lower().endswith((".html", ".htm", ".php"))
                or len(anchor_segments) > 4
                or re.search(r"\d{5,}", anchor_path)
            ):
                continue
            seen_urls.add(absolute)
            links.append((absolute, text, (text + " " + href).lower()))

        # Карта сайта даёт ссылки на разделы, до которых не добраться с
        # главной страницы. Глубина обхода при этом не превышает лимит.
        for loc in self._sitemap_links(base_url):
            absolute = canonical_url(loc, base_url)
            if absolute in seen_urls:
                continue
            if not is_same_domain(absolute, base_url):
                continue
            if not is_probably_content_link(absolute):
                continue
            seen_urls.add(absolute)
            links.append((absolute, "", absolute.lower()))

        sections: list[SectionSpec] = []
        used_urls: set[str] = set()
        for section_name, patterns in SECTION_LINK_PATTERNS.items():
            candidates: list[tuple[int, str]] = []
            for absolute, text, haystack in links:
                score = 0
                for pattern in patterns:
                    if pattern in haystack:
                        score += len(pattern)
                if not score:
                    continue
                # Поощряем короткие «раздельные» URL и штрафуем новости.
                if section_name not in {"news", "project_news"} and "/news" in absolute:
                    score -= 5
                candidates.append((score, absolute))
            if not candidates:
                continue
            candidates.sort(key=lambda item: (-item[0], len(item[1])))
            url = candidates[0][1]
            if url in used_urls:
                continue
            used_urls.add(url)
            sections.append(
                SectionSpec(
                    name=section_name,
                    url=url,
                    item_selectors=DEFAULT_ITEM_SELECTORS
                    if section_name
                    in {
                        "projects",
                        "research",
                        "grants",
                        "partnerships",
                        "achievements",
                        "news",
                        "project_news",
                        "events",
                    }
                    else (),
                )
            )

        # Стандартные страницы «Сведения об образовательной организации».
        if not any(section.name == "leadership" for section in sections):
            parsed = utils.urlparse(base_url)
            standard = f"{parsed.scheme}://{parsed.netloc}{SVEDEN_SECTIONS['leadership']}"
            sections.append(SectionSpec(name="leadership", url=standard))
        if not any(section.name == "contacts" for section in sections):
            parsed = utils.urlparse(base_url)
            standard = f"{parsed.scheme}://{parsed.netloc}{SVEDEN_SECTIONS['contacts']}"
            sections.append(SectionSpec(name="contacts", url=standard))

        sections.sort(key=lambda section: SECTION_ORDER.get(section.name, 99))
        return sections

    def _sitemap_links(self, base_url: str) -> list[str]:
        """Читает /sitemap.xml и вложенные карты сайта (не более 5)."""

        parsed = utils.urlparse(base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        queue = [f"{root}/sitemap.xml"]
        visited: set[str] = set()
        urls: list[str] = []
        while queue and len(visited) < 5:
            sitemap_url = queue.pop(0)
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            result = self.client.get(sitemap_url)
            if result is None:
                continue
            locations = re.findall(
                r"<loc>\s*([^<\s]+)\s*</loc>", result.text, re.IGNORECASE
            )
            for location in locations[:5000]:
                location = location.strip()
                if not location:
                    continue
                if location.lower().endswith(".xml"):
                    if len(queue) < 5:
                        queue.append(location)
                    continue
                if not is_same_domain(location, base_url):
                    continue
                # Для поиска разделов берём только «раздельные» URL:
                # статьи, карточки и глубокие страницы не должны
                # становиться разделом.
                path = utils.urlparse(location).path
                segments = [segment for segment in path.split("/") if segment]
                if (
                    path.lower().endswith((".html", ".htm", ".php"))
                    or len(segments) > 4
                    or re.search(r"\d{5,}", path)
                ):
                    continue
                urls.append(location)
        return dedupe_preserve(urls)[:5000]

    LIST_SECTIONS = {
        "projects",
        "research",
        "grants",
        "partnerships",
        "achievements",
        "news",
        "project_news",
        "events",
    }

    def _parse_section(self, section: SectionSpec) -> None:
        for page_url in self._section_page_urls(section):
            doc, soup = self._get_page(
                page_url,
                section=section.name,
                content_selectors=section.content_selectors,
            )
            if doc is None or soup is None:
                continue
            self._dispatch_section(section, doc, soup)

    def _section_page_urls(self, section: SectionSpec) -> list[str]:
        urls = [section.url]
        if (
            section.name not in self.LIST_SECTIONS
            or self.settings.max_pages_per_section <= 1
        ):
            return urls
        for page in range(2, self.settings.max_pages_per_section + 1):
            separator = "&" if "?" in section.url else "?"
            urls.append(f"{section.url}{separator}page={page}")
        return urls

    def _dispatch_section(
        self, section: SectionSpec, doc: SourceDocument, soup: BeautifulSoup
    ) -> None:
        if section.name == "about":
            self._parse_about(doc, soup)
        elif section.name == "leadership":
            self._parse_leadership(doc, soup)
        elif section.name == "contacts":
            self._parse_contacts(doc, soup)
        elif section.name == "projects":
            self._parse_projects(doc, soup, section)
        elif section.name in {"research", "grants", "partnerships", "achievements"}:
            self._parse_categorized(doc, soup, section)
        elif section.name == "news":
            self._parse_news(doc, soup, section)
        elif section.name == "events":
            self._parse_events(doc, soup, section)

    # ------------------------------------------------------------------
    # Загрузка и очистка
    # ------------------------------------------------------------------
    def _get_page(
        self,
        url: str,
        section: str,
        content_selectors: Sequence[str] = (),
    ) -> tuple[SourceDocument | None, BeautifulSoup | None]:
        canonical = canonical_url(url)
        if canonical in self._page_cache:
            return self._page_cache[canonical]
        if canonical in self._failed_urls:
            return None, None
        if self._cache is not None:
            failure = self._cache.get_failure(canonical)
            if failure is not None:
                self._warnings.append(
                    f"{section}: пропущено после недавней ошибки "
                    f"({failure.get('error')}): {canonical}"
                )
                self._failed_urls.add(canonical)
                return None, None

        html = ""
        final_url = canonical
        content_type = "text/html"
        cached = self._cache.get_page(canonical) if self._cache else None
        if cached is not None:
            html = str(cached.get("html") or "")
            final_url = str(cached.get("final_url") or canonical)
            content_type = str(cached.get("content_type") or "text/html")
            LOGGER.info("CACHE %s", canonical)
        else:
            result = self.client.get(canonical)
            if result is None:
                message = (
                    f"{section}: {self.client.last_error or 'не удалось загрузить'} "
                    f"({canonical})"
                )
                if self._cache is not None:
                    self._cache.set_failure(canonical, None, self.client.last_error)
                if self._store is not None:
                    self._store.record_status(
                        canonical, None, self.client.last_error
                    )
                stale = (
                    self._cache.get_page(canonical, allow_stale=True)
                    if self._cache is not None
                    else None
                )
                if stale is not None:
                    html = str(stale.get("html") or "")
                    final_url = str(stale.get("final_url") or canonical)
                    content_type = str(stale.get("content_type") or "text/html")
                    self._warnings.append(
                        message + " — использую устаревшую копию из кэша"
                    )
                else:
                    self._warnings.append(message)
                    self._failed_urls.add(canonical)
                    return None, None
            else:
                if result.final_url and not is_same_domain(
                    result.final_url, canonical
                ):
                    self._warnings.append(
                        f"{section}: редирект на сторонний домен "
                        f"{result.final_url} ({canonical}) — пропущено"
                    )
                    self._failed_urls.add(canonical)
                    return None, None
                if result.content_type and not any(
                    marker in result.content_type.lower()
                    for marker in ("html", "xml", "text")
                ):
                    self._warnings.append(
                        f"{section}: пропущен не-HTML документ "
                        f"{result.content_type}: {canonical}"
                    )
                    if self._store is not None:
                        self._store.record_status(
                            canonical,
                            result.status_code,
                            f"пропущен не-HTML документ: {result.content_type}",
                        )
                    self._failed_urls.add(canonical)
                    return None, None

                html = result.text
                final_url = result.final_url or canonical
                content_type = result.content_type or "text/html"
                if self._cache is not None:
                    self._cache.set_page(
                        canonical, html, final_url, content_type, result.status_code
                    )
                if self._store is not None:
                    self._store.record_status(canonical, result.status_code, "")

        if not html:
            self._warnings.append(f"{section}: пустой ответ ({canonical})")
            self._failed_urls.add(canonical)
            return None, None

        soup = _make_soup(html)
        cleaner_obj = HTMLCleaner(
            content_selectors=content_selectors
            or (self.profile.content_selectors if self.profile else ())
            or DEFAULT_CONTENT_SELECTORS,
            max_chars=self.settings.max_document_chars,
        )
        doc = cleaner_obj.to_document(
            html,
            url=final_url,
            section=section,
            content_type=content_type,
        )

        # Если HTML не содержит контента (SPA/JS), пробуем браузерный
        # рендеринг — только если он явно разрешён настройкой.
        if len(doc.text) < 200 and self.settings.enable_playwright:
            rendered = self._render_with_playwright(canonical)
            if rendered and rendered != html:
                html = rendered
                soup = _make_soup(html)
                doc = cleaner_obj.to_document(
                    html,
                    url=final_url,
                    section=section,
                    content_type=content_type,
                )
                if self._cache is not None:
                    self._cache.set_page(
                        canonical, html, final_url, content_type, 200
                    )

        if not doc.text:
            self._warnings.append(
                f"{section}: после очистки не осталось текста ({canonical}). "
                "Возможно, контент рендерится JavaScript."
            )
        previous_hash = self._cache.get_text_hash(canonical) if self._cache else ""
        if self._cache is not None:
            self._cache.set_text_hash(canonical, doc.text_hash)
        if self._store is not None:
            if not previous_hash or previous_hash != doc.text_hash:
                self._store.save_document(
                    doc, self._university_name, self._official_domain
                )
        self._page_cache[canonical] = (doc, soup)
        self._documents[canonical] = doc
        return doc, soup

    def _render_with_playwright(self, url: str) -> str:
        """Опциональный рендеринг JS-страницы через Playwright."""

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            LOGGER.info("Playwright не установлен, рендеринг пропущен: %s", url)
            return ""
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=int(self.settings.timeout * 1000))
                html = page.content()
                browser.close()
            LOGGER.info("Playwright отрендерил %s (%s символов)", url, len(html))
            return html
        except Exception as exc:  # noqa: BLE001 - рендеринг не должен ронять сбор
            LOGGER.warning("Playwright не смог отрендерить %s: %s", url, exc)
            return ""

    # ------------------------------------------------------------------
    # Руководство
    # ------------------------------------------------------------------
    def _parse_leadership(self, doc: SourceDocument, soup: BeautifulSoup) -> None:
        table_parser = SvedenTableParser(doc.url)
        rows = table_parser.parse(soup)
        leaders: list[Leadership] = []

        email_counts: dict[str, int] = {}
        phone_counts: dict[str, int] = {}
        for row in rows:
            if row.get("email"):
                email_counts[row["email"]] = email_counts.get(row["email"], 0) + 1
            if row.get("phone"):
                phone_counts[row["phone"]] = phone_counts.get(row["phone"], 0) + 1

        for row in rows:
            name = strip_name(row.get("name", ""))
            position = normalize_whitespace(row.get("position", ""))
            if not _looks_like_table_name(name):
                continue
            if not position and not row.get("email") and not row.get("phone"):
                continue

            email = row.get("email", "")
            phone = row.get("phone", "")
            position_rank = _position_rank(position)
            personal_email = bool(email) and _email_matches_name(email, name)
            generic_top_email = (
                bool(email)
                and position_rank <= 1
                and _mailbox(email) in GENERIC_MAILBOXES
            )
            leader = Leadership(
                full_name=name,
                position=position,
                email=(
                    email
                    if email_counts.get(email, 0) == 1
                    and (personal_email or generic_top_email)
                    else None
                ),
                phone=phone if phone_counts.get(phone, 0) == 1 else None,
                address=row.get("address") or None,
                unit=row.get("unit") or None,
                source_url=doc.url,
                is_acting=bool(
                    re.search(r"\bи\.?\s*о\.?\b|исполняющ", position, re.IGNORECASE)
                ),
            )
            leaders.append(leader)

            detail_url = row.get("site") or row.get("name_link")
            if (
                detail_url
                and detail_url.startswith("http")
                and _is_bio_like_url(detail_url)
            ):
                self._detail_urls.setdefault(name, detail_url)

            owner, owner_kind = _owner_from_row(row, email_counts.get(email, 99))
            if email:
                self._add_contact(
                    Contact(
                        type="email",
                        value=email,
                        owner=owner,
                        owner_kind=owner_kind,  # type: ignore[arg-type]
                        source_url=doc.url,
                        context=row.get("unit", ""),
                    )
                )
            if phone:
                self._add_contact(
                    Contact(
                        type="phone",
                        value=phone,
                        owner=owner,
                        owner_kind=owner_kind,  # type: ignore[arg-type]
                        source_url=doc.url,
                        context=row.get("unit", ""),
                    )
                )

        if len(leaders) < 2:
            leaders.extend(self._leadership_from_text(doc))

        # Один человек может встречаться в нескольких органах управления.
        # Оставляем самую «старшую» должность.
        merged: dict[str, Leadership] = {}
        for leader in leaders:
            key = _name_key(leader.full_name)
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = leader
                continue
            if _position_rank(leader.position) < _position_rank(existing.position):
                # Сохраняем контакты и биографию более полной записи.
                leader.email = leader.email or existing.email
                leader.phone = leader.phone or existing.phone
                leader.office_phone = leader.office_phone or existing.office_phone
                leader.fax = leader.fax or existing.fax
                leader.office_number = leader.office_number or existing.office_number
                leader.office = leader.office or existing.office
                leader.bio = leader.bio or existing.bio
                if not leader.unit and existing.unit:
                    leader.unit = existing.unit
                merged[key] = leader
            else:
                existing.email = existing.email or leader.email
                existing.phone = existing.phone or leader.phone
                existing.office_phone = existing.office_phone or leader.office_phone
                existing.fax = existing.fax or leader.fax
                existing.office_number = (
                    existing.office_number or leader.office_number
                )
                existing.office = existing.office or leader.office
                existing.unit = existing.unit or leader.unit
                if not existing.source_url:
                    existing.source_url = leader.source_url

        self._leadership = sorted(
            merged.values(),
            key=lambda item: (_position_rank(item.position), item.full_name),
        )
        LOGGER.info(
            "Руководство: найдено %s записей (таблиц: %s)",
            len(self._leadership),
            len(rows),
        )

    def _leadership_from_text(self, doc: SourceDocument) -> list[Leadership]:
        """Fallback для сайтов без стандартной таблицы."""

        leaders: list[Leadership] = []
        blocks = doc.blocks
        for index, block in enumerate(blocks):
            text = block.text
            if not utils.contains_position(text):
                continue
            names = find_person_names(text)
            if names:
                position = truncate_text(text, 250)
                leaders.append(
                    Leadership(
                        full_name=names[0],
                        position=position,
                        source_url=doc.url,
                    )
                )
                continue
            # Должность в одной строке, ФИО — в соседней.
            neighbours = []
            if index > 0:
                neighbours.append(blocks[index - 1].text)
            if index + 1 < len(blocks):
                neighbours.append(blocks[index + 1].text)
            for neighbour in neighbours:
                names = find_person_names(neighbour)
                if names:
                    leaders.append(
                        Leadership(
                            full_name=names[0],
                            position=truncate_text(text, 250),
                            source_url=doc.url,
                        )
                    )
                    break
        return leaders

    # ------------------------------------------------------------------
    # Контакты
    # ------------------------------------------------------------------
    def _parse_contacts(self, doc: SourceDocument, soup: BeautifulSoup) -> None:
        # Структурированные контакты из фрагментов (email/phone + владелец).
        for fragment in doc.fragments:
            for hint in fragment.contact_hints:
                self._add_contact(
                    Contact(
                        type="email" if hint.field == "contact.email" else "phone",
                        value=hint.value,
                        owner=hint.owner.label or "ВУЗ",
                        owner_kind=hint.owner.kind,
                        source_url=doc.url,
                        context=fragment.heading,
                    )
                )

        # Контакты из таблиц (если страница контактов — таблица).
        for row in SvedenTableParser(doc.url).parse(soup):
            name = strip_name(row.get("name", ""))
            owner = name if _looks_like_table_name(name) else row.get("unit") or "ВУЗ"
            kind = "person" if _looks_like_table_name(name) else "department"
            if row.get("email"):
                self._add_contact(
                    Contact(
                        type="email",
                        value=row["email"],
                        owner=owner,
                        owner_kind=kind,  # type: ignore[arg-type]
                        source_url=doc.url,
                        context=row.get("position", ""),
                    )
                )
            if row.get("phone"):
                self._add_contact(
                    Contact(
                        type="phone",
                        value=row["phone"],
                        owner=owner,
                        owner_kind=kind,  # type: ignore[arg-type]
                        source_url=doc.url,
                        context=row.get("position", ""),
                    )
                )

        # Адреса: строки, начинающиеся с «Адрес» или содержащие город и улицу.
        for block in doc.blocks:
            text = block.text
            if len(text) < 20 or len(text) > 300:
                continue
            lowered = text.lower()
            if lowered.startswith(("адрес", "почтовый адрес", "место нахождения")) or (
                re.search(r"\b(?:г\.|город)\s+[А-ЯЁ]", text)
                and re.search(
                    r"(?:ул\.|улица|пр\.|проспект|наб\.|площадь|пер\.)(?=\s|,|$)",
                    lowered,
                )
            ):
                address = re.sub(
                    r"^(?:почтовый\s+)?адрес(?:\s+места\s+нахождения)?\s*[:\-–—]?\s*",
                    "",
                    text,
                    flags=re.IGNORECASE,
                )
                address = re.sub(
                    r"\+?7?\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
                    "",
                    address,
                ).strip(" ,;")
                address = re.sub(
                    r"^[^:]{3,90}:\s*(?=\d{6})", "", address
                ).strip(" ,;")
                if 8 <= len(address) <= 250:
                    self._add_contact(
                        Contact(
                            type="address",
                            value=address,
                            owner="ВУЗ",
                            owner_kind="organization",
                            source_url=doc.url,
                        )
                    )

    def _add_contact(self, contact: Contact) -> None:
        key = (contact.type, contact.value.lower())
        existing = self._contact_index.get(key)
        if existing is None:
            self._contact_index[key] = contact
            self._contacts.append(contact)
            return
        if OWNER_RANK.get(contact.owner_kind, 0) > OWNER_RANK.get(
            existing.owner_kind, 0
        ):
            existing.owner = contact.owner
            existing.owner_kind = contact.owner_kind
            existing.context = contact.context or existing.context
            existing.source_url = contact.source_url or existing.source_url

    # ------------------------------------------------------------------
    # Проекты, новости, мероприятия
    # ------------------------------------------------------------------
    def _parse_projects(
        self, doc: SourceDocument, soup: BeautifulSoup, section: SectionSpec
    ) -> None:
        items = self._extract_items(doc, soup, section)
        if section.require_keywords:
            items = [
                item
                for item in items
                if any(
                    keyword in f"{item['title']} {item['description']}".lower()
                    for keywords in PROJECT_TYPE_KEYWORDS.values()
                    for keyword in keywords
                )
            ]
        items = self._enrich_items_with_pages(items, "projects")
        for item in items:
            self._projects.append(self._project_from_item(item, doc))
        if not items:
            for item in self._items_from_blocks(doc, PROJECT_TYPE_KEYWORDS):
                self._projects.append(
                    self._project_from_item(item, doc, default_type="НИР")
                )
        self._projects = self._dedupe_projects(self._projects)
        LOGGER.info("Проекты: +%s (всего %s)", len(items), len(self._projects))

    def _parse_news(
        self, doc: SourceDocument, soup: BeautifulSoup, section: SectionSpec
    ) -> None:
        items = self._extract_items(doc, soup, section)
        # Пункт 8: новости старше 24 месяцев не нужны, если это не
        # информация о действующих проектах/партнёрствах.
        items = [item for item in items if not self._is_old_news(item["date"])]
        items = self._enrich_items_with_pages(items, "project_news")
        items = [item for item in items if not self._is_old_news(item["date"])]
        for item in items:
            haystack = f"{item['title']} {item['description']}".lower()
            if any(
                keyword in haystack
                for keywords in PROJECT_TYPE_KEYWORDS.values()
                for keyword in keywords
            ):
                self._projects.append(self._project_from_item(item, doc))
            title_haystack = item["title"].lower()
            if any(
                keyword in title_haystack
                for keywords in EVENT_TYPE_KEYWORDS.values()
                for keyword in keywords
            ) and item["date"]:
                # Событие из новости засчитываем только при наличии даты
                # и ключевого слова в заголовке: так отсекаются новости
                # и навигационные ссылки, не являющиеся мероприятиями.
                self._events.append(
                    Event(
                        title=item["title"],
                        type=_classify(haystack, EVENT_TYPE_KEYWORDS, "не определено"),
                        date=item["date"],
                        description=item["description"],
                        url=item["url"],
                        source_url=doc.url,
                    )
                )
        self._projects = self._dedupe_projects(self._projects)
        self._events = self._dedupe_events(self._events)
        LOGGER.info(
            "Новости: обработано %s, проектов %s, событий %s",
            len(items),
            len(self._projects),
            len(self._events),
        )

    def _parse_events(
        self, doc: SourceDocument, soup: BeautifulSoup, section: SectionSpec
    ) -> None:
        items = self._extract_items(doc, soup, section)
        items = self._enrich_items_with_pages(items, "events")
        for item in items:
            if not item["date"]:
                # Мероприятие без даты бесполезно для карточки и часто
                # оказывается пунктом меню.
                continue
            text = f"{item['title']} {item['description']}"
            self._events.append(
                Event(
                    title=item["title"],
                    type=_classify(text, EVENT_TYPE_KEYWORDS, "не определено"),
                    date=item["date"],
                    description=item["description"],
                    url=item["url"],
                    source_url=doc.url,
                )
            )
        if not items:
            for item in self._items_from_blocks(doc, EVENT_TYPE_KEYWORDS):
                if not item["date"]:
                    continue
                self._events.append(
                    Event(
                        title=item["title"],
                        type=_classify(
                            item["title"] + " " + item["description"],
                            EVENT_TYPE_KEYWORDS,
                            "не определено",
                        ),
                        date=item["date"],
                        description=item["description"],
                        url=item["url"],
                        source_url=doc.url,
                    )
                )
        self._events = self._dedupe_events(self._events)
        LOGGER.info("Мероприятия: +%s (всего %s)", len(items), len(self._events))

    def _parse_categorized(
        self, doc: SourceDocument, soup: BeautifulSoup, section: SectionSpec
    ) -> None:
        """Разбор научной деятельности, грантов, партнёров и достижений."""

        items = self._extract_items(doc, soup, section)
        items = self._enrich_items_with_pages(items, section.name)
        if section.name == "research":
            for item in items:
                text = f"{item['title']} {item['description']}"
                head_match = HEAD_RE.search(text)
                self._research.append(
                    ResearchItem(
                        name=item["title"],
                        description=item["description"],
                        head=(
                            normalize_whitespace(head_match.group(1)).strip(" ,;")
                            if head_match
                            else None
                        ),
                        source_quote=truncate_text(
                            item["description"] or item["title"], 300
                        ),
                        url=item["url"],
                        source_urls=dedupe_preserve(
                            [item["url"], doc.url]
                        ),
                    )
                )
            self._research = self._dedupe_named(self._research)
        elif section.name == "grants":
            for item in items:
                self._grants.append(self._grant_from_item(item, doc))
            self._grants = self._dedupe_named(self._grants)
        elif section.name == "partnerships":
            for item in items:
                self._partnerships.append(self._partnership_from_item(item, doc))
            self._partnerships = self._dedupe_named(self._partnerships, "organization")
        elif section.name == "achievements":
            for item in items:
                self._achievements.append(
                    Achievement(
                        title=item["title"],
                        result=truncate_text(item["description"], 500),
                        source_quote=truncate_text(
                            item["description"] or item["title"], 300
                        ),
                        date=item["date"] or None,
                        url=item["url"],
                        source_urls=dedupe_preserve([item["url"], doc.url]),
                    )
                )
            self._achievements = self._dedupe_named(self._achievements, "title")
        LOGGER.info(
            "%s: +%s (всего %s)",
            section.name,
            len(items),
            len(
                {
                    "research": self._research,
                    "grants": self._grants,
                    "partnerships": self._partnerships,
                    "achievements": self._achievements,
                }[section.name]
            ),
        )

    def _project_from_item(
        self,
        item: dict[str, str],
        doc: SourceDocument,
        default_type: str = "не определён",
    ) -> Project:
        text = f"{item['title']} {item['description']}"
        dates = re.findall(
            r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|"
            r"\b\d{1,2}\s+[а-яё]+\s+\d{4}\s*(?:г\.?)?",
            item["description"],
            flags=re.IGNORECASE,
        )
        parsed_dates = [parse_date(value) for value in dates]
        parsed_dates = [value for value in parsed_dates if value]
        partners = []
        partner_match = PARTNERS_RE.search(item["description"])
        if partner_match:
            partners = [
                part.strip(" ,;")
                for part in re.split(r"[,;]\s*", partner_match.group(1))
                if part.strip(" ,;")
            ]
        return Project(
            title=item["title"],
            type=_classify(text, PROJECT_TYPE_KEYWORDS, default_type),
            description=item["description"],
            partners=partners,
            source_quote=truncate_text(
                item["description"] or item["title"], 300
            ),
            start_date=parsed_dates[0] if parsed_dates else (item["date"] or None),
            end_date=parsed_dates[1] if len(parsed_dates) > 1 else None,
            date=item["date"] or None,
            url=item["url"],
            source_url=doc.url,
            source_urls=dedupe_preserve([item["url"], doc.url]),
        )

    def _grant_from_item(
        self, item: dict[str, str], doc: SourceDocument
    ) -> Grant:
        text = f"{item['title']} {item['description']}"
        amount_match = AMOUNT_RE.search(text)
        organizer_match = ORGANIZER_RE.search(text)
        winners_match = WINNERS_RE.search(text)
        dates = [
            value
            for value in (
                parse_date(match.group(0))
                for match in re.finditer(
                    r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|"
                    r"\b\d{1,2}\s+[а-яё]+\s+\d{4}\s*(?:г\.?)?",
                    text,
                    flags=re.IGNORECASE,
                )
            )
            if value
        ]
        winners: list[str] = []
        if winners_match:
            winners = [
                part.strip(" ,;")
                for part in re.split(r"[,;]\s*", winners_match.group(1))
                if part.strip(" ,;")
            ]
        return Grant(
            name=item["title"],
            organizer=(
                normalize_whitespace(organizer_match.group(1)).strip(" ,;")
                if organizer_match
                else None
            ),
            amount=(
                normalize_whitespace(amount_match.group(1)) if amount_match else None
            ),
            start_date=dates[0] if dates else item["date"] or None,
            end_date=dates[1] if len(dates) > 1 else None,
            winners=winners,
            description=item["description"],
            source_quote=truncate_text(item["description"] or item["title"], 300),
            url=item["url"],
            source_urls=dedupe_preserve([item["url"], doc.url]),
        )

    @staticmethod
    def _partnership_from_item(
        item: dict[str, str], doc: SourceDocument
    ) -> Partnership:
        text = f"{item['title']} {item['description']}"
        organization = item["title"]
        subject = item["description"]
        # «Партнёр: ...» / «Соглашение с ...» — уточняем организацию.
        match = re.search(
            r"(?:партн[её]р|соглашение с)\s*[:\-–—]?\s*([^.;\n]{3,160})",
            text,
            re.IGNORECASE,
        )
        if match:
            organization = normalize_whitespace(match.group(1)).strip(" ,;")
        return Partnership(
            organization=organization,
            subject=truncate_text(subject, 500),
            source_quote=truncate_text(subject or organization, 300),
            date=item["date"] or None,
            url=item["url"],
            source_urls=dedupe_preserve([item["url"], doc.url]),
        )

    def _enrich_items_with_pages(
        self, items: list[dict[str, str]], section_name: str
    ) -> list[dict[str, str]]:
        """Догружает страницы публикаций: дата, текст и документ для LLM."""

        if (
            not self.settings.fetch_item_pages
            or self.settings.max_item_documents <= 0
            or not self._base_url
        ):
            return items
        enriched: list[dict[str, str]] = []
        fetched = 0
        for item in items:
            if fetched >= self.settings.max_item_documents:
                enriched.append(item)
                continue
            item_url = item.get("url", "")
            if not item_url or not is_same_domain(item_url, self._base_url or item_url):
                enriched.append(item)
                continue
            if item_url.rstrip("/") == (self._base_url or "").rstrip("/"):
                enriched.append(item)
                continue
            doc, _ = self._get_page(item_url, section=section_name)
            fetched += 1
            if doc is None:
                enriched.append(item)
                continue
            if not item.get("date") and doc.published_at:
                item["date"] = doc.published_at.date().isoformat()
            if not doc.published_at and item.get("date"):
                try:
                    doc.published_at = datetime.fromisoformat(item["date"]).replace(
                        tzinfo=timezone.utc
                    )
                    if self._store is not None:
                        self._store.save_document(
                            doc, self._university_name, self._official_domain
                        )
                except ValueError:
                    pass
            if len(item.get("description", "")) < 200 and len(doc.text) > 200:
                item["description"] = truncate_text(doc.text, 700)
            enriched.append(item)
        return enriched

    def _is_old_news(self, value: str) -> bool:
        if not value or self.settings.news_window_months <= 0:
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        cutoff = utc_now() - timedelta(days=30 * self.settings.news_window_months)
        return parsed < cutoff

    @staticmethod
    def _dedupe_named(items: Iterable[object], attribute: str = "name") -> list[object]:
        result: list[object] = []
        seen: set[str] = set()
        for item in items:
            value = getattr(item, attribute, "")
            key = _title_key(str(value))
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _extract_items(
        self,
        doc: SourceDocument,
        soup: BeautifulSoup,
        section: SectionSpec,
    ) -> list[dict[str, str]]:
        """Извлекает карточки/ссылки со страницы списка."""

        content_soup = _make_soup(doc.main_html) if doc.main_html else soup
        selectors = section.item_selectors or (
            DEFAULT_ITEM_SELECTORS
            if section.name in {"projects", "news", "events"}
            else ()
        )
        items: list[dict[str, str]] = []

        containers: list[Tag] = []
        for selector in selectors:
            try:
                containers.extend(content_soup.select(selector))
            except Exception:  # noqa: BLE001
                continue

        if containers:
            for container in containers:
                item = self._item_from_container(container, doc.url)
                if item:
                    items.append(item)

        # Если карточки не дали результата (или их слишком мало), пробуем
        # ссылки напрямую — это устойчивее к незнакомой вёрстке.
        if len(items) < 3:
            for anchor in content_soup.select("a[href]"):
                item = self._item_from_anchor(anchor, doc.url)
                if item:
                    items.append(item)

        # Дедупликация и ограничение.
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            key = _title_key(item["title"])
            url_key = item.get("url", "").split("#")[0]
            if not key or key in seen:
                continue
            if url_key and url_key in seen:
                continue
            seen.add(key)
            if url_key:
                seen.add(url_key)
            deduped.append(item)

        # Убираем элементы навигации: слишком короткие/длинные заголовки.
        result = []
        for item in deduped:
            title = item["title"]
            if len(title) < 8 or len(title) > 300:
                continue
            if title.lower() in GENERIC_LINK_TITLES:
                continue
            if _is_navigation_like(title):
                continue
            if re.fullmatch(r"[\W\d_]+", title):
                continue
            result.append(item)
        return result[: self.settings.max_items_per_section]

    def _item_from_container(
        self, container: Tag, page_url: str
    ) -> dict[str, str] | None:
        heading = container.find(["h1", "h2", "h3", "h4", "h5"])
        anchors = [
            anchor
            for anchor in container.find_all("a", href=True)
            if is_probably_content_link(anchor.get("href", ""))
            and len(normalize_whitespace(anchor.get_text(" ", strip=True))) >= 8
        ]
        if not anchors:
            return None
        # Предпочитаем ссылку внутри заголовка.
        anchor = None
        if heading:
            anchor = heading.find("a", href=True)
        if anchor is None:
            anchor = max(
                anchors,
                key=lambda node: len(
                    normalize_whitespace(node.get_text(" ", strip=True))
                ),
            )
        title = normalize_whitespace(
            (heading.get_text(" ", strip=True) if heading else "")
            or anchor.get_text(" ", strip=True)
        )
        url = canonical_url(anchor.get("href", ""), page_url)
        if not is_same_domain(url, page_url):
            return None
        return self._build_item(title, container, url)

    def _item_from_anchor(
        self, anchor: Tag, page_url: str
    ) -> dict[str, str] | None:
        href = normalize_whitespace(anchor.get("href", ""))
        if not href or not is_probably_content_link(href):
            return None
        url = canonical_url(href, page_url)
        if not is_same_domain(url, page_url):
            return None
        title = normalize_whitespace(anchor.get_text(" ", strip=True))
        if len(title) < 8:
            return None
        container = self._closest_container(anchor)
        return self._build_item(title, container or anchor, url)

    def _build_item(
        self, title: str, container: Tag, url: str
    ) -> dict[str, str] | None:
        title = _strip_leading_date(normalize_whitespace(title)).strip(" |•·—–-")
        if not title or title.lower() in GENERIC_LINK_TITLES:
            return None

        container_text = normalize_whitespace(container.get_text(" ", strip=True))
        description = container_text
        if title in description:
            description = description.replace(title, " ", 1)
        # Убираем дату из описания, если она уже выделена.
        date_value = parse_date(container_text)
        if date_value:
            description = re.sub(
                r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", " ", description
            )
            description = re.sub(
                r"\b\d{1,2}\s+[а-яё]+\s+\d{4}\s*(?:г\.?)?", " ", description, flags=re.IGNORECASE
            )
        description = normalize_whitespace(description)
        description = truncate_text(description, 600)
        if description.lower() == title.lower():
            description = ""

        return {
            "title": title,
            "description": description,
            "date": date_value or "",
            "url": url,
        }

    @staticmethod
    def _closest_container(anchor: Tag) -> Tag | None:
        """Наименьший родитель, в котором есть текст помимо заголовка ссылки."""

        title_len = len(normalize_whitespace(anchor.get_text(" ", strip=True)))
        current = anchor.parent
        best = None
        while isinstance(current, Tag) and current.name not in {"body", "html"}:
            text = normalize_whitespace(current.get_text(" ", strip=True))
            if len(text) <= max(1200, title_len + 500):
                best = current
            else:
                break
            current = current.parent
        return best

    @staticmethod
    def _items_from_blocks(
        doc: SourceDocument, keywords: dict[str, tuple[str, ...]]
    ) -> list[dict[str, str]]:
        """Fallback: абзацы с ключевыми словами проекта/мероприятия."""

        all_keywords = [keyword for values in keywords.values() for keyword in values]
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for block in doc.blocks:
            text = block.text
            if len(text) < 30 or len(text) > 800:
                continue
            lowered = text.lower()
            if not any(keyword in lowered for keyword in all_keywords):
                continue
            if _is_navigation_like(text):
                continue
            title = _first_sentence(text, 160)
            key = _title_key(title)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "title": title,
                    "description": truncate_text(text, 600),
                    "date": parse_date(text) or "",
                    "url": doc.url,
                }
            )
        return items[:30]

    @staticmethod
    def _similar_title(left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right:
            return True
        return SequenceMatcher(None, left, right).ratio() >= 0.92

    @classmethod
    def _dedupe_projects(cls, projects: Iterable[Project]) -> list[Project]:
        result: list[Project] = []
        for project in projects:
            key = _title_key(project.title)
            if not key:
                continue
            duplicate = next(
                (
                    existing
                    for existing in result
                    if cls._similar_title(key, _title_key(existing.title))
                ),
                None,
            )
            if duplicate is None:
                result.append(project)
                continue
            # Оставляем более полную версию и объединяем источники.
            if len(project.description) > len(duplicate.description):
                project.partners = dedupe_preserve(
                    [*project.partners, *duplicate.partners]
                )
                project.source_urls = dedupe_preserve(
                    [*project.source_urls, *duplicate.source_urls]
                )
                result[result.index(duplicate)] = project
            else:
                duplicate.partners = dedupe_preserve(
                    [*duplicate.partners, *project.partners]
                )
                duplicate.source_urls = dedupe_preserve(
                    [*duplicate.source_urls, *project.source_urls]
                )
        result.sort(key=lambda item: (item.date or "", item.title), reverse=True)
        return result[:40]

    @classmethod
    def _dedupe_events(cls, events: Iterable[Event]) -> list[Event]:
        result: list[Event] = []
        for event in events:
            key = _title_key(event.title)
            if not key:
                continue
            duplicate = next(
                (
                    existing
                    for existing in result
                    if cls._similar_title(key, _title_key(existing.title))
                ),
                None,
            )
            if duplicate is None:
                result.append(event)
                continue
            if len(event.description) > len(duplicate.description):
                result[result.index(duplicate)] = event
        result.sort(key=lambda item: (item.date or "", item.title), reverse=True)
        return result[:40]

    # ------------------------------------------------------------------
    # «О ВУЗе» и биографии
    # ------------------------------------------------------------------
    def _parse_about(self, doc: SourceDocument, soup: BeautifulSoup | None = None) -> None:
        if self._about_doc is not None:
            return
        self._about_doc = doc

        # На страницах «Сведения об образовательной организации» основная
        # карточка организации — это блок с полным наименованием, ИНН/ОГРН
        # и адресом. Берём самый компактный такой блок: он информативнее
        # таблиц адресов филиалов.
        if soup is not None:
            candidates: list[tuple[int, Tag]] = []
            for node in soup.select("article, section, div, table"):
                text = normalize_whitespace(node.get_text(" ", strip=True))
                if not 200 <= len(text) <= 8000:
                    continue
                lowered = text.lower()
                markers = sum(
                    1
                    for marker in ("полное наименование", "инн", "огрн", "адрес")
                    if marker in lowered
                )
                if markers >= 2:
                    candidates.append((len(text), node))
            if candidates:
                _, best = min(candidates, key=lambda item: item[0])
                about_text = normalize_whitespace(best.get_text(" ", strip=True))
                if len(about_text) >= 100:
                    self._about = truncate_text(about_text, 1500)
                    self._add_org_contacts(doc, self._about)
                    return

        paragraphs = [
            block.text
            for block in doc.blocks
            if block.kind in {"paragraph", "text"}
            and len(block.text) >= 100
            and not extract_emails(block.text)
            and "@" not in block.text
            and not _is_navigation_like(block.text)
        ]
        if not paragraphs:
            paragraphs = [
                block.text
                for block in doc.blocks
                if len(block.text) >= 100 and not _is_navigation_like(block.text)
            ]
        self._about = truncate_text(" ".join(paragraphs[:5]), 1500)
        self._add_org_contacts(doc, self._about)

    def _add_org_contacts(self, doc: SourceDocument, text: str) -> None:
        """Добавляет общие контакты вуза со страницы «О вузе»."""

        for email in extract_emails(text)[:5]:
            self._add_contact(
                Contact(
                    type="email",
                    value=email,
                    owner="ВУЗ",
                    owner_kind="organization",
                    source_url=doc.url,
                )
            )
        for phone in extract_phones(text)[:10]:
            self._add_contact(
                Contact(
                    type="phone",
                    value=phone,
                    owner="ВУЗ",
                    owner_kind="organization",
                    source_url=doc.url,
                )
            )

    def _enrich_bios(self) -> None:
        if not self.settings.fetch_bios or self.settings.max_bios <= 0:
            return

        candidates = sorted(
            enumerate(self._leadership),
            key=lambda pair: (_position_rank(pair[1].position), pair[0]),
        )
        attempted = 0
        for _, leader in candidates:
            if attempted >= self.settings.max_bios:
                break
            if leader.bio:
                continue
            attempted += 1

            bio = ""
            # Общие страницы биографий (например, /rector/biografiya)
            # относятся только к первому лицу вуза. Проректорам их
            # приписывать нельзя.
            if self.profile and _position_rank(leader.position) <= 1:
                for bio_url in self.profile.bio_urls:
                    bio = self._bio_from_url(bio_url, leader)
                    if bio:
                        break

            if not bio:
                detail_url = self._detail_urls.get(leader.full_name)
                if detail_url:
                    bio = self._bio_from_url(detail_url, leader)

            if bio:
                leader.bio = bio
                leader.source_quote = truncate_text(bio, 300)

    def _bio_from_url(self, url: str, leader: Leadership) -> str:
        doc, _ = self._get_page(url, section="bio")
        if doc is None:
            return ""
        return self._extract_bio_text(doc, leader.full_name, leader)

    @staticmethod
    def _extract_bio_text(
        doc: SourceDocument,
        full_name: str,
        leader: Leadership | None = None,
    ) -> str:
        name_tokens = [
            token.lower()
            for token in re.findall(r"[А-ЯЁа-яёA-Za-z\-]{2,}", full_name)
            if len(token) >= 3
        ]
        blocks = [
            block.text
            for block in doc.blocks
            if block.kind in {"paragraph", "text", "quote"}
            and 80 <= len(block.text) <= 1200
            and not extract_emails(block.text)
            and "cookie" not in block.text.lower()
            and not _is_navigation_like(block.text)
        ]

        # Страница должна действительно относиться к этому человеку.
        # Иначе общая страница биографии ректора будет приписана проректору.
        doc_lowered = doc.text.lower()
        matched_tokens = sum(
            1 for token in set(name_tokens) if token in doc_lowered
        )
        if matched_tokens < min(2, len(set(name_tokens))):
            return ""

        # Контакты ищем на той же странице, но сохраняем их в поля
        # руководителя, а не в текст биографии.
        if leader is not None:
            UniversityParser._apply_bio_contacts(doc, leader)

        if not blocks:
            return ""

        # Биография должна либо упоминать человека, либо содержать
        # характерные для биографии маркеры. Иначе это текст раздела,
        # меню или новость — приписывать его руководителю нельзя.
        named = [
            block
            for block in blocks
            if any(token in block.lower() for token in name_tokens)
        ]
        topical = [
            block
            for block in blocks
            if any(keyword in block.lower() for keyword in BIO_TEXT_KEYWORDS)
        ]

        # Начинаем с абзаца «Родился/Родилась», чтобы биография не
        # обрезалась с середины (например, с записи об увольнении).
        start_index = next(
            (
                index
                for index, block in enumerate(blocks)
                if "родил" in block.lower()
            ),
            None,
        )
        if start_index is not None:
            selected = blocks[start_index : start_index + 3]
        else:
            relevant = [
                block
                for block in blocks
                if block in named or block in topical
            ]
            selected = relevant[:3]
        if not selected:
            return ""
        bio = truncate_text(" ".join(selected), 900)
        return bio if len(bio) >= 80 else ""

    @staticmethod
    def _apply_bio_contacts(doc: SourceDocument, leader: Leadership) -> None:
        """Переносит контакты со страницы биографии в поля руководителя."""

        merged: dict[str, str] = {}
        for chunk in _contact_chunks(doc):
            for key, value in _contacts_from_contact_text(chunk).items():
                merged.setdefault(key, value)

        # Резервный источник — структурированные подсказки очистителя.
        if not merged.get("office_phone"):
            for fragment in doc.fragments:
                for hint in fragment.contact_hints:
                    if hint.field != "contact.phone" or not hint.value:
                        continue
                    phone = clean_phone(hint.value)
                    if phone:
                        merged["office_phone"] = phone
                        break
                if merged.get("office_phone"):
                    break

        phone = merged.get("office_phone")
        if phone:
            leader.office_phone = phone
            leader.phone = phone

        fax = merged.get("fax")
        if fax:
            leader.fax = fax

        office_number = merged.get("office_number")
        if office_number:
            leader.office_number = office_number
            leader.office = leader.office or office_number

        email = merged.get("email")
        if (
            email
            and not _is_generic_email(email)
            and _email_matches_name(email, leader.full_name)
        ):
            leader.email = email
        elif leader.email and _is_generic_email(leader.email):
            # Служебный адрес (office@, info@) не должен выдаваться за личный.
            leader.email = None

        LOGGER.debug(
            "Контакты из биографии %s: телефон=%s, факс=%s, кабинет=%s, email=%s",
            leader.full_name,
            leader.office_phone,
            leader.fax,
            leader.office_number,
            leader.email,
        )

    # ------------------------------------------------------------------
    # Сборка карточки
    # ------------------------------------------------------------------
    def _build_card(
        self,
        base_url: str,
        name: str | None,
        profile: UniversityProfile | None,
        homepage_doc: SourceDocument | None,
    ) -> UniversityData:
        about_doc = self._about_doc
        if not self._about and homepage_doc is not None:
            paragraphs = [
                block.text
                for block in homepage_doc.blocks
                if block.kind in {"paragraph", "text"}
                and len(block.text) >= 100
                and not _is_navigation_like(block.text)
            ]
            self._about = truncate_text(" ".join(paragraphs[:4]), 1200)

        if not self._about and about_doc is not None:
            self._about = truncate_text(about_doc.text, 1200)

        card_name = first_non_empty(
            [
                name,
                profile.name if profile else "",
                homepage_doc.title if homepage_doc else "",
                self._title_from_url(base_url),
            ]
        )
        address = self._extract_field(r"адрес(?:\s+места\s+нахождения)?\s*[:\-–—]?\s*([^\n]{10,250})")
        address = re.split(
            r"\s+(?:режим|график работы|телефон|факс|e-?mail|электронная почта)\b",
            address,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ,;")
        if not address:
            address = next(
                (
                    contact.value
                    for contact in self._contacts
                    if contact.type == "address"
                ),
                "",
            )
        address = re.sub(
            r"^(?:мест[оа]?\s*нахождения(?:\s+образовательной\s+организации)?"
            r"|образовательной\s+организации)\s*[:\-–—]?\s*",
            "",
            address,
            flags=re.IGNORECASE,
        ).strip(" ,;")
        inn = self._extract_field(r"\bИНН\s*[:\-№]?\s*(\d{10,12})")
        ogrn = self._extract_field(r"\bОГРН\s*[:\-№]?\s*(\d{13,15})")
        org_email = self._org_contact("email")
        org_phone = self._org_contact("phone")

        university = UniversityInfo(
            name=card_name,
            url=base_url,
            official_domain=self._official_domain,
            scraped_at=utc_now(),
            inn=inn or None,
            ogrn=ogrn or None,
            address=address or None,
            region=self._extract_region(address) or None,
            activity_areas=self._extract_activity_areas(self._about),
            email=org_email or None,
            phone=org_phone or None,
        )

        if not self._leadership:
            self._warnings.append(
                "Руководство не найдено. Проверьте /sveden/struct/ или селекторы "
                "в config.py; возможно, контент рендерится JavaScript."
            )
        if not self._documents:
            self._errors.append("Не удалось получить ни одного документа источника")

        ordered_contacts = sorted(
            self._contacts,
            key=lambda contact: OWNER_RANK.get(contact.owner_kind, 0),
            reverse=True,
        )
        return UniversityData(
            university=university,
            about=self._about,
            leadership=self._leadership,
            contacts=ordered_contacts[:300],
            projects=self._projects,
            grants=sorted(
                self._grants,
                key=lambda item: (item.start_date or "", item.name),
                reverse=True,
            )[:40],
            research=self._research[:40],
            units=self._build_units(),
            partnerships=self._partnerships[:40],
            achievements=self._achievements[:40],
            events=self._events,
            documents=list(self._documents.values()),
            conflicts=self._collect_conflicts(),
            warnings=dedupe_preserve(self._warnings),
            errors=dedupe_preserve(self._errors),
            last_updated=utc_now(),
        )

    def _collect_conflicts(self) -> list[str]:
        """Фиксирует контакты, приписанные нескольким владельцам."""

        owners: dict[tuple[str, str], set[str]] = {}
        for contact in self._contacts:
            key = (contact.type, contact.value.lower())
            owners.setdefault(key, set()).add(contact.owner or "ВУЗ")
        conflicts: list[str] = []
        for (contact_type, value), labels in owners.items():
            if len(labels) <= 1:
                continue
            conflicts.append(
                f"{contact_type} {value} приписан нескольким владельцам: "
                + ", ".join(sorted(labels)[:5])
            )
        return conflicts[:50]

    def _build_units(self) -> list[Unit]:
        """Собирает подразделения из записей руководства."""

        groups: dict[str, list[Leadership]] = {}
        for leader in self._leadership:
            name = normalize_whitespace(leader.unit or "")
            if len(name) < 3:
                continue
            groups.setdefault(name, []).append(leader)
        units: list[Unit] = []
        for name, leaders in groups.items():
            head = min(leaders, key=lambda item: _position_rank(item.position))
            contacts = dedupe_preserve(
                [
                    value
                    for value in (head.email, head.phone, head.office_phone)
                    if value
                ]
                + [
                    contact.value
                    for contact in self._contacts
                    if contact.owner == name
                    or (contact.context and name.lower() in contact.context.lower())
                ]
            )
            units.append(
                Unit(
                    name=name,
                    profile=head.position,
                    head=head.full_name,
                    contacts=contacts,
                    source_urls=dedupe_preserve([head.source_url]),
                )
            )
        units.sort(key=lambda item: item.name)
        return units[:200]

    def _org_contact(self, contact_type: str) -> str:
        candidates = [
            contact for contact in self._contacts if contact.type == contact_type
        ]
        for kind in ("organization", "office", "department"):
            for contact in candidates:
                if contact.owner_kind == kind:
                    return contact.value
        return candidates[0].value if candidates else ""

    def _extract_field(self, pattern: str) -> str:
        haystack = self._about or ""
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match:
            return normalize_whitespace(match.group(1))
        return ""

    @staticmethod
    def _extract_region(address: str) -> str:
        value = normalize_whitespace(address)
        if not value:
            return ""
        lowered = value.lower()
        if "москв" in lowered:
            return "Москва"
        if "санкт-петербург" in lowered or "петербург" in lowered:
            return "Санкт-Петербург"
        match = re.search(
            r"(?:республик[а-яё]*|область|обл\.|край|регион)\s*[:\-–—]?\s*"
            r"([А-ЯЁ][А-ЯЁа-яё\- ]{2,60})",
            value,
        )
        if match:
            return normalize_whitespace(match.group(1)).strip(" ,;")
        match = re.search(
            r"([А-ЯЁ][А-ЯЁа-яё\-]{3,40})\s+область",
            value,
        )
        if match:
            return normalize_whitespace(match.group(1)).strip(" ,;")
        match = re.search(
            r"(?:\d{6}\s*,?\s*)?(?:г\.\s*)?([А-ЯЁ][А-ЯЁа-яё\-]{3,40})(?=\s*,|\s*$)",
            value,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _extract_activity_areas(about: str) -> list[str]:
        text = normalize_whitespace(about)
        if not text:
            return []
        match = re.search(
            r"направлени[а-яё]*\s+деятельности[^.]{0,400}[.:]\s*([^.]{10,500})",
            text,
            re.IGNORECASE,
        )
        if not match:
            return []
        parts = [
            part.strip(" ,;")
            for part in re.split(r"[;,]\s*", match.group(1))
            if len(part.strip(" ,;")) >= 4
        ]
        return dedupe_preserve(parts)[:10]

    @staticmethod
    def _title_from_url(url: str) -> str:
        parsed = utils.urlparse(url)
        host = parsed.netloc.replace("www.", "")
        return host or url


# ----------------------------------------------------------------------
# Сохранение, сравнение версий, CLI
# ----------------------------------------------------------------------
def diff_cards(previous: dict | None, current: UniversityData) -> ChangeReport | None:
    """Сравнивает текущую карточку с предыдущим JSON-снимком."""

    if not previous:
        return None
    previous_university = previous.get("university") or {}
    previous_scraped = previous_university.get("scraped_at")
    previous_dt = None
    if previous_scraped:
        try:
            previous_dt = datetime.fromisoformat(str(previous_scraped).replace("Z", "+00:00"))
        except ValueError:
            previous_dt = None

    previous_leaders = {
        _name_key(item.get("full_name", "")): item.get("position", "")
        for item in previous.get("leadership", [])
        if item.get("full_name")
    }
    current_leaders = {
        _name_key(item.full_name): item.position for item in current.leadership
    }
    previous_projects = {
        _title_key(item.get("title", "")) for item in previous.get("projects", [])
    }
    previous_grants = {
        _title_key(item.get("name", "")) for item in previous.get("grants", [])
    }
    previous_events = {
        _title_key(item.get("title", "")) for item in previous.get("events", [])
    }

    report = ChangeReport(
        previous_scraped_at=previous_dt,
        new_leadership=[
            item.full_name
            for item in current.leadership
            if _name_key(item.full_name) not in previous_leaders
        ],
        removed_leadership=[
            item.get("full_name", "")
            for item in previous.get("leadership", [])
            if _name_key(item.get("full_name", "")) not in current_leaders
        ],
        changed_positions=[
            item.full_name
            for item in current.leadership
            if _name_key(item.full_name) in previous_leaders
            and previous_leaders[_name_key(item.full_name)] != item.position
        ],
        new_projects=[
            item.title
            for item in current.projects
            if _title_key(item.title) not in previous_projects
        ],
        new_grants=[
            item.name
            for item in current.grants
            if _title_key(item.name) not in previous_grants
        ],
        new_events=[
            item.title
            for item in current.events
            if _title_key(item.title) not in previous_events
        ],
    )
    return report


def save_results(
    card: UniversityData,
    output_dir: Path,
    slug: str | None = None,
) -> dict[str, Path]:
    """Сохраняет карточку, предыдущую версию и отчёт об изменениях."""

    slug = slugify(slug or card.university.name)
    directory = Path(output_dir) / slug
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    latest_path = directory / f"{slug}_card.json"
    previous = utils.load_json(latest_path)
    change_report = diff_cards(previous, card)

    card_path = directory / f"{slug}_card_{timestamp}.json"
    documents_path = directory / f"{slug}_documents_{timestamp}.json"
    changes_path = directory / f"{slug}_changes_{timestamp}.json"

    utils.atomic_write_json(card_path, card.to_json_dict())
    university_name = card.university.name
    official_domain = card.university.official_domain
    document_records = [
        document.to_llm_record(university_name, official_domain)
        for document in card.documents
    ]
    utils.atomic_write_json(
        documents_path,
        {
            "schema_version": "0.3",
            "institution": {
                "id": slug,
                "name": university_name,
                "official_domain": official_domain,
                "url": card.university.url,
            },
            "task": (
                "Извлеки из документов подтверждённые сведения для «Карточки ВУЗа»: "
                "руководство, контакты, проекты, гранты, научные направления, "
                "партнёрства, достижения и новости. Каждое утверждение должно "
                "сопровождаться ссылкой на источник; при отсутствии данных верни null."
            ),
            "output_schema": {
                "university_name": "string",
                "leadership": ["full_name", "position", "source_url"],
                "contacts": ["type", "value", "owner", "source_url"],
                "projects": [
                    "name",
                    "description",
                    "partners",
                    "start_date",
                    "end_date",
                    "source_urls",
                ],
                "grants": [
                    "name",
                    "organizer",
                    "amount",
                    "start_date",
                    "end_date",
                    "winners",
                    "source_urls",
                ],
                "last_updated": "ISO-8601",
            },
            "documents": document_records,
        },
    )
    utils.atomic_write_json(latest_path, card.to_json_dict())

    paths = {
        "card": card_path,
        "latest": latest_path,
        "documents": documents_path,
    }
    if change_report is not None:
        utils.atomic_write_json(changes_path, change_report.model_dump(mode="json"))
        paths["changes"] = changes_path
        if change_report.has_changes:
            LOGGER.info(
                "Изменения: новые руководители=%s, ушли=%s, новые проекты=%s, "
                "новые гранты=%s, новые события=%s",
                len(change_report.new_leadership),
                len(change_report.removed_leadership),
                len(change_report.new_projects),
                len(change_report.new_grants),
                len(change_report.new_events),
            )
        else:
            LOGGER.info("Изменений относительно предыдущего снимка нет")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Парсер сайтов вузов для AI-агента «Карточка ВУЗа».",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python -m backend.parser.main --list-profiles\n"
            "  python -m backend.parser.main --profile spbu --with-bios\n"
            "  python -m backend.parser.main --url https://example.ru --name \"Пример\"\n"
            "  python -m backend.parser.main --all\n"
        ),
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--profile",
        action="append",
        choices=sorted(UNIVERSITY_PROFILES),
        help="Профиль вуза из config.py (можно повторять).",
    )
    target.add_argument("--all", action="store_true", help="Все профили из DEFAULT_BATCH_PROFILES.")
    target.add_argument("--url", help="URL сайта вуза без профиля.")
    target.add_argument(
        "--input-file",
        type=Path,
        default=None,
        help="Файл со списком вузов: URL, «Название, URL», CSV или JSON.",
    )
    parser.add_argument("--name", help="Название вуза (для --url).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Каталог результатов (по умолчанию data/output).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Пауза между запросами, секунды (минимум 2–3 рекомендуется).",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Не проверять robots.txt (не рекомендуется).",
    )
    parser.add_argument(
        "--with-bios",
        action="store_true",
        help="Загружать биографии руководителей (по умолчанию включено).",
    )
    parser.add_argument(
        "--no-bios",
        action="store_true",
        help="Не загружать биографии (быстрее и меньше запросов).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Максимум страниц на раздел (пагинация).",
    )
    parser.add_argument(
        "--max-item-docs",
        type=int,
        default=None,
        help="Сколько страниц публикаций догружать на раздел.",
    )
    parser.add_argument(
        "--news-months",
        type=int,
        default=None,
        help="Глубина новостей в месяцах (по умолчанию 24).",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Отключить кэш страниц между запусками."
    )
    parser.add_argument(
        "--no-sqlite", action="store_true", help="Не сохранять документы в SQLite."
    )
    parser.add_argument(
        "--no-item-pages",
        action="store_true",
        help="Не догружать страницы отдельных публикаций.",
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Разрешить браузерный рендеринг JS-страниц (если установлен Playwright).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Уровень логирования.",
    )
    parser.add_argument("--log-file", type=Path, default=None, help="Файл журнала.")
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Показать доступные профили и выйти.",
    )
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if args.output_dir:
        settings.output_dir = args.output_dir
    if args.delay is not None:
        settings.request_delay = max(1.0, args.delay)
    if args.no_robots:
        settings.respect_robots = False
    if args.with_bios:
        settings.fetch_bios = True
    if args.no_bios:
        settings.fetch_bios = False
    if args.max_pages is not None:
        settings.max_pages_per_section = max(1, args.max_pages)
    if args.max_item_docs is not None:
        settings.max_item_documents = max(0, args.max_item_docs)
    if args.news_months is not None:
        settings.news_window_months = max(0, args.news_months)
    if args.no_cache:
        settings.use_cache = False
    if args.no_sqlite:
        settings.use_sqlite = False
    if args.no_item_pages:
        settings.fetch_item_pages = False
    if args.playwright:
        settings.enable_playwright = True
    return settings


def load_university_list(path: Path) -> list[tuple[str | None, str | None, str | None]]:
    """Читает входной список вузов из JSON, CSV или текстового файла.

    Поддерживаются форматы:
    - ``https://university.ru`` — только URL;
    - ``Название, https://university.ru`` (CSV/TSV, разделитель ``|`` тоже);
    - JSON: список строк или объектов ``{"name": ..., "url": ...}``.
    """

    raw = path.read_text(encoding="utf-8-sig")
    targets: list[tuple[str | None, str | None, str | None]] = []
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Некорректный JSON в {path}: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("universities", [])
        for item in payload or []:
            if isinstance(item, str):
                targets.append((item.strip(), None, None))
            elif isinstance(item, dict):
                url = str(item.get("url") or item.get("site") or "").strip()
                name = str(item.get("name") or "").strip() or None
                if url:
                    targets.append((url, name, None))
        return [target for target in targets if target[0]]

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"\s*[|;\t]\s*|\s*,\s*", line) if part.strip()]
        if len(parts) >= 2 and parts[-1].startswith(("http://", "https://")):
            targets.append((parts[-1], ", ".join(parts[:-1]) or None, None))
        elif parts and parts[0].startswith(("http://", "https://")):
            targets.append((parts[0], None, None))
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        print("Доступные профили:")
        for key, profile in sorted(UNIVERSITY_PROFILES.items()):
            print(f"  {key:8s} {profile.name} — {profile.url}")
        return 0

    settings = _settings_from_args(args)
    log_file = args.log_file or (settings.log_dir / "parser.log")
    setup_logging(args.log_level, log_file)
    LOGGER.info("Журнал: %s", log_file)

    targets: list[tuple[str | None, str | None, str | None]] = []
    if args.all:
        for key in DEFAULT_BATCH_PROFILES:
            targets.append((None, None, key))
    elif args.input_file:
        targets.extend(load_university_list(args.input_file))
    elif args.profile:
        for key in args.profile:
            targets.append((args.url, args.name, key))
    elif args.url:
        targets.append((args.url, args.name, None))
    else:
        parser.print_help()
        print(
            "\nУкажите --profile, --all, --url или --input-file. "
            "Список профилей: --list-profiles"
        )
        return 2

    failures = 0
    for url, name, profile_key in targets:
        university_parser = UniversityParser(settings)
        try:
            card = university_parser.parse(url=url, name=name, profile=profile_key)
            slug = profile_key or slugify(card.university.name)
            paths = save_results(card, settings.output_dir, slug=slug)
            summary = card.summary()
            print("\n" + "=" * 70)
            print(f"ВУЗ: {summary['university']}")
            print(
                f"Руководство: {summary['leadership']} | Контакты: {summary['contacts']} | "
                f"Проекты: {summary['projects']} | Гранты: {summary['grants']} | "
                f"Наука: {summary['research']} | Партнёры: {summary['partnerships']} | "
                f"Достижения: {summary['achievements']} | Мероприятия: {summary['events']} | "
                f"Документы: {summary['documents']}"
            )
            print(f"Предупреждения: {summary['warnings']} | Ошибки: {summary['errors']}")
            print(f"Карточка: {paths['card']}")
            print(f"Документы для LLM: {paths['documents']}")
            if "changes" in paths:
                print(f"Изменения: {paths['changes']}")
            if card.warnings:
                print("Предупреждения:")
                for warning in card.warnings[:10]:
                    print(f"  - {warning}")
            if card.errors:
                failures += 1
                print("Ошибки:")
                for error in card.errors[:10]:
                    print(f"  - {error}")
        except KeyboardInterrupt:
            LOGGER.warning("Остановлено пользователем")
            return 130
        except Exception as exc:  # noqa: BLE001
            failures += 1
            LOGGER.exception("Не удалось обработать %s: %s", profile_key or url, exc)
            print(f"ОШИБКА: {profile_key or url}: {exc}", file=sys.stderr)
        finally:
            university_parser.close()

    return 1 if failures == len(targets) else 0


if __name__ == "__main__":
    raise SystemExit(main())
