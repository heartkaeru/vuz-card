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
from .utils import looks_like_person_name


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
    if document.file_sha256:
        payload["file_sha256"] = document.file_sha256
    if include_fragments:
        fragments = []
        for fragment in document.fragments:
            item: dict[str, object] = {
                "fragment_id": fragment.fragment_id,
                "start": fragment.start,
                "end": fragment.end,
            }
            if fragment.page_number is not None:
                item["page_number"] = fragment.page_number
            if entity_scope_id is not None:
                item["entity_scope_id"] = entity_scope_id or document.source_id
            elif not include_contact_hints and fragment.entity_scope_id is not None:
                item["entity_scope_id"] = fragment.entity_scope_id
            if include_contact_hints:
                if fragment.biography_subject:
                    item["biography_subject"] = fragment.biography_subject.model_dump(mode="json")
                if fragment.subject_department:
                    item["subject_department"] = fragment.subject_department
                if fragment.leadership_position:
                    item["leadership_position"] = fragment.leadership_position.model_dump(mode="json")
                item["contact_hints"] = [
                    hint.model_dump(mode="json")
                    for hint in fragment.contact_hints
                ]
            elif fragment.institution_anchor_id:
                item["institution_anchor_id"] = fragment.institution_anchor_id
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
        documents = []
        for document in card.documents:
            item = to_llm_document(
                document,
                include_fragments=True,
                include_contact_hints=False,
            )
            item["section"] = document.section
            item["category"] = document.category
            documents.append(item)
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


def to_officials(
    card: UniversityData, *, institution_id: str, require_person_name: bool = False
) -> list[dict[str, object]]:
    """Преобразует сведения о руководстве в API-представление.

    ``require_person_name`` отбрасывает записи, где вместо ФИО оказался
    абзац или заголовок страницы (для автоматического заполнения CRM).
    """

    officials: list[dict[str, object]] = []
    for leader in card.leadership:
        if require_person_name and not looks_like_person_name(leader.full_name or ""):
            continue
        position = (leader.position or "").strip() or "Руководитель"
        if len(position) > 160:
            position = position[:157].rstrip() + "..."
        officials.append(
            {
                "id": f"{institution_id}-official-{len(officials) + 1}",
                "fio": leader.full_name,
                "position": position,
                "department": leader.unit,
                "responsibilities": leader.source_quote or leader.bio,
                "email": leader.reception_email or leader.email,
                "phone": leader.reception_phone or leader.office_phone or leader.phone,
                "photo_url": leader.photo_url,
                "source_url": leader.source_url or None,
                "status": "ai_draft",
                "previous_value": None,
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
    """Преобразует собранные сведения в API-представление карточки."""

    return {
        "id": university_id,
        "name": card.university.name,
        "short_name": short_name or card.university.name,
        "inn": card.university.inn or "",
        "website": card.university.url,
        "region": card.university.region or "",
        "city": card.university.city or "",
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
        if re.search(r"\bректор(?:а|ом|у|е)?\b", position) or re.search(
            r"исполняющ[а-яё]*\s+обязанност[а-яё]*\s+ректор|врио\s+ректор", position
        ):
            return leader.full_name
    return None
