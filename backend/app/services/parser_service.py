"""Мост между CRM-бэкендом и парсером.

Позволяет создавать карточку по названию вуза: ссылку на официальный сайт
искать не нужно, её находит парсер.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


def _parser_imports():
    try:
        from parser.config import Settings
        from parser.main import UniversityParser
        from parser.official_site import resolve_official_site
    except ImportError:  # запуск из корня репозитория
        from backend.parser.config import Settings
        from backend.parser.main import UniversityParser
        from backend.parser.official_site import resolve_official_site
    return Settings, UniversityParser, resolve_official_site


def resolve_site(name: str, short_name: str = "") -> str:
    """Найти официальный сайт по названию вуза без ручной ссылки."""

    _, _, resolve_official_site = _parser_imports()
    return resolve_official_site(name, short_name=short_name, timeout=25.0)


def build_card(
    name: str,
    city: str = "",
    website: str = "",
    short_name: str = "",
) -> Any:
    """Собрать карточку парсера по названию (URL необязателен)."""

    Settings, UniversityParser, _ = _parser_imports()
    settings = Settings(
        aggregators_only=True,
        use_sqlite=False,
        use_cache=False,
        fetch_bios=False,
        max_bios=0,
    )
    parser = UniversityParser(settings)
    try:
        # Если сайт уже найден — передаём его; иначе parser найдёт сам.
        return parser.parse(url=website or None, name=name)
    finally:
        parser.close()


def to_officials(card: Any, institution_id: str) -> list[dict[str, Any]]:
    """Преобразование Leadership парсера в формат CRM ``Official``."""

    officials: list[dict[str, Any]] = []
    for index, leader in enumerate(card.leadership, start=1):
        officials.append(
            {
                "id": f"{institution_id}-official-{index}",
                "fio": leader.full_name,
                "position": leader.position or "Руководитель",
                "department": leader.unit,
                "responsibilities": leader.source_quote or leader.bio,
                "email": leader.reception_email or leader.email,
                "phone": leader.reception_phone
                or leader.office_phone
                or leader.phone,
                "photo_url": leader.photo_url,
                "source_url": leader.source_url or None,
                "status": "ai_draft",
                "previous_value": None,
            }
        )
    return officials
