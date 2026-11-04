"""Адаптер парсера к контрактам LLM-модуля ``app.services.llm``.

Парсер отдаёт ``UniversityData``/``SourceDocument``. LLM-модуль принимает
``FragmentDocument`` (schema 0.2) и ``ContextDocument`` (schema 0.3).
Этот модуль переводит одно в другое без импорта ``app`` — достаточно
передать результат в API/пайплайн LLM.

Пример::

    from parser.llm_adapter import to_llm_request

    request = to_llm_request(card, institution_id="hse", card_version=1)
    # request можно передать в FragmentExtractionRequest.model_validate(request)
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Iterable, Literal

from .models import Leadership, SourceDocument, UniversityData


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def to_llm_document(
    document: SourceDocument,
    *,
    entity_scope_id: str | None = None,
    include_fragments: bool = True,
    include_contact_hints: bool = True,
) -> dict[str, object]:
    """Документ в формате ``SourceDocument``/``FragmentDocument`` LLM-модуля."""

    payload: dict[str, object] = {
        "source_id": document.source_id,
        "url": document.url,
        "title": document.title,
        "text": document.text,
        "retrieved_at": _iso(document.retrieved_at),
        "published_at": _iso(document.published_at),
    }
    if include_fragments:
        fragments = []
        for fragment in document.fragments:
            item: dict[str, object] = {
                "fragment_id": fragment.fragment_id,
                "start": fragment.start,
                "end": fragment.end,
            }
            if entity_scope_id is not None:
                item["entity_scope_id"] = entity_scope_id or document.source_id
            elif not include_contact_hints and fragment.entity_scope_id is not None:
                item["entity_scope_id"] = fragment.entity_scope_id
            if include_contact_hints:
                item["contact_hints"] = [
                    hint.model_dump(mode="json")
                    for hint in fragment.contact_hints
                ]
            fragments.append(item)
        payload["fragments"] = fragments
    return payload


def to_llm_documents(
    documents: Iterable[SourceDocument],
    *,
    entity_scope_id: str | None = None,
) -> list[dict[str, object]]:
    return [
        to_llm_document(document, entity_scope_id=entity_scope_id)
        for document in documents
    ]


def to_llm_request(
    card: UniversityData,
    *,
    institution_id: str,
    card_version: int = 1,
    schema_version: Literal["0.2", "0.3"] = "0.2",
    fields: list[str] | None = None,
) -> dict[str, object]:
    """Запрос на извлечение фактов для LLM-пайплайна.

    ``schema_version="0.2"`` — руководство/контакты с фрагментами.
    ``schema_version="0.3"`` — профиль, проекты и события (ContextDocument).
    """

    if schema_version == "0.3":
        documents = [
            to_llm_document(
                document,
                include_fragments=True,
                include_contact_hints=False,
            )
            for document in card.documents
        ]
    else:
        documents = to_llm_documents(card.documents)

    request: dict[str, object] = {
        "schema_version": schema_version,
        "institution": {
            "id": institution_id,
            "name": card.university.name,
            "card_version": max(1, card_version),
        },
        "documents": documents,
    }
    if schema_version == "0.2":
        request["project_structured_contacts"] = False
    if fields:
        request["fields"] = list(dict.fromkeys(fields))
    return request


def to_officials(card: UniversityData, *, institution_id: str) -> list[dict[str, object]]:
    """Руководство в формате ``app.schemas.university.Official``."""

    officials: list[dict[str, object]] = []
    for index, leader in enumerate(card.leadership, start=1):
        officials.append(
            {
                "id": f"{institution_id}-official-{index}",
                "fio": leader.full_name,
                "position": leader.position,
                "department": leader.unit,
                "responsibilities": leader.source_quote,
                "email": leader.email,
                "phone": leader.office_phone or leader.phone,
                "photo_url": None,
                "source_url": leader.source_url or None,
                "status": "ai_draft",
            }
        )
    return officials


def to_university_detail(
    card: UniversityData,
    *,
    institution_id: str,
    university_id: int = 1,
    short_name: str | None = None,
) -> dict[str, object]:
    """Карточка в формате ``app.schemas.university.UniversityDetail``."""

    return {
        "id": university_id,
        "name": card.university.name,
        "short_name": short_name or card.university.name,
        "inn": card.university.inn or "",
        "website": card.university.url,
        "region": card.university.region or "",
        "city": "",
        "rector_fio": _rector_name(card.leadership),
        "version": 1,
        "updated_at": _iso(card.last_updated),
        "officials": to_officials(card, institution_id=institution_id),
        "fot_volume": None,
        "contracts_count": 0,
        "active_projects": [project.name for project in card.projects[:20]],
    }


def _rector_name(leaders: Iterable[Leadership]) -> str | None:
    for leader in leaders:
        position = (leader.position or "").lower()
        if re.search(r"\bректор(?:а|ом|у|е)?\b", position):
            return leader.full_name
    return None
