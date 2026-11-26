"""Мост между CRM-бэкендом и парсером.

Позволяет создавать карточку по названию вуза: ссылку на официальный сайт
искать не нужно, её находит парсер.
"""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)


def _parser_imports():
    try:
        from parser import llm_adapter, utils
        from parser.config import Settings
        from parser.main import UniversityParser, _is_rector_position
        from parser.official_site import resolve_official_site
    except ImportError:  # запуск из корня репозитория
        from backend.parser import llm_adapter, utils
        from backend.parser.config import Settings
        from backend.parser.main import UniversityParser, _is_rector_position
        from backend.parser.official_site import resolve_official_site
    return Settings, UniversityParser, resolve_official_site, llm_adapter, utils, _is_rector_position


def resolve_site(name: str, short_name: str = "") -> str:
    """Найти официальный сайт по названию вуза без ручной ссылки."""

    resolve_official_site = _parser_imports()[2]
    return resolve_official_site(name, short_name=short_name, timeout=25.0)


def build_card(
    name: str,
    city: str = "",
    website: str = "",
    short_name: str = "",
) -> Any:
    """Собрать карточку парсера по названию (URL необязателен).

    Руководство и контакты берутся с официального сайта, паспорт — из
    агрегаторов, сотрудничество со Сбером — из новостей. Кэш выключен:
    карточка должна отражать текущее состояние сайта.
    """

    Settings, UniversityParser, *_ = _parser_imports()
    settings = Settings(
        enable_playwright=True,
        use_sqlite=False,
        use_cache=False,
        fetch_bios=False,
        fetch_item_pages=False,
        max_bios=0,
        max_retries=1,
        timeout=20.0,
        max_total_requests=40,
    )
    parser = UniversityParser(settings)
    try:
        # Если сайт уже найден — передаём его; иначе parser найдёт сам.
        # Без сайта парсер не обходит пустой адрес, а собирает данные
        # агрегаторов и предупреждает об этом в card.warnings.
        return parser.parse(
            url=website or None,
            name=name,
            short_name=short_name or None,
            allowed_sections=("about", "leadership", "contacts", "programs"),
        )
    finally:
        parser.close()


# ФИО полностью («Иванов Иван Иванович», «ИВАНОВ Иван Иванович») или
# с инициалами («Иванов И. И.»).
FIO_RE = re.compile(
    r"^[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-]+"
    r"(?:\s+(?:[А-ЯЁA-Z][а-яёa-z\-]+|[А-ЯЁA-Z]{2,})(?:\s+(?:[А-ЯЁA-Z][а-яёa-z\-]+|[А-ЯЁA-Z]{2,})){0,2}"
    r"|\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?)$"
)


def _is_person_name(value: str) -> bool:
    utils = _parser_imports()[4]
    value = (value or "").strip()
    return bool(FIO_RE.match(value)) and utils.looks_like_person_name(value)


def pick_rector(card: Any) -> str | None:
    """Выбрать ректора из руководства, отбрасывая мусорные «должности»."""

    is_rector_position = _parser_imports()[5]
    for leader in card.leadership:
        position = (leader.position or "").strip()
        lowered = position.lower()
        if not (
            is_rector_position(position)
            or re.match(r"(?:врио|временно\s+исполняющ[а-яё]*|исполняющ[а-яё]*\s+обязанност[а-яё]*)\s+ректор", lowered)
        ):
            continue
        if not _is_person_name(leader.full_name or ""):
            continue
        return leader.full_name
    return None


def to_officials(card: Any, institution_id: str) -> list[dict[str, Any]]:
    """Преобразование Leadership парсера в формат CRM ``Official``.

    Одна реализация с ``parser.llm_adapter.to_officials``; здесь
    дополнительно отбрасываются записи без ФИО (абзацы устава, заголовки).
    """

    llm_adapter = _parser_imports()[3]
    return llm_adapter.to_officials(card, institution_id=institution_id, require_person_name=True)
