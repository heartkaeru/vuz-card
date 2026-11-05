"""Pydantic-модели данных парсера «Карточка ВУЗа».

Модели описывают два контура обмена:

1. ``UniversityData`` — итоговая карточка вуза в формате, пригодном для
   сохранения в JSON/CRM и для передачи нейросети.
2. ``SourceDocument`` — очищенный документ источника с метаданными,
   фрагментами и контактными подсказками. Документы можно экспортировать
   в контракт ``{university, document}`` (раздел 4 требований).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OwnerKind = Literal["person", "office", "department", "organization", "unknown"]
ContactType = Literal["email", "phone", "address"]


def utc_now() -> datetime:
    """Текущее время в UTC (единый формат для всех метаданных)."""

    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _strip_value(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.replace("\xa0", " ").split())
    return value


class UniversityInfo(BaseModel):
    """Базовые сведения об организации."""

    model_config = ConfigDict(extra="ignore")

    name: str
    url: str
    official_domain: str = ""
    scraped_at: datetime = Field(default_factory=utc_now)
    inn: Optional[str] = None
    ogrn: Optional[str] = None
    address: Optional[str] = None
    region: Optional[str] = None
    activity_areas: list[str] = Field(default_factory=list)
    email: Optional[str] = None
    phone: Optional[str] = None


class Leadership(BaseModel):
    """Руководитель: ректор, проректор, директор подразделения."""

    model_config = ConfigDict(extra="ignore")

    full_name: str
    position: str = ""
    bio: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    office_phone: str | None = None
    fax: str | None = None
    office: str | None = None
    office_number: str | None = None
    address: Optional[str] = None
    unit: Optional[str] = None
    source_url: str = ""
    source_quote: Optional[str] = None
    is_acting: bool = False

    @field_validator(
        "full_name",
        "position",
        "bio",
        "unit",
        "email",
        "phone",
        "office_phone",
        "fax",
        "office",
        "office_number",
        "address",
        "source_quote",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return _strip_value(value)


class Contact(BaseModel):
    """Контакт с указанием владельца (человек/должность/подразделение)."""

    model_config = ConfigDict(extra="ignore")

    type: ContactType = "email"
    value: str
    owner: str = "ВУЗ"
    owner_kind: OwnerKind = "organization"
    source_url: str = ""
    context: str = ""

    @field_validator("value", "owner", "context", mode="before")
    @classmethod
    def _strip_contact(cls, value: object) -> object:
        return _strip_value(value)


class Project(BaseModel):
    """Проект/НИР/разработка."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    name: str = ""
    type: str = "не определён"
    description: str = ""
    partners: list[str] = Field(default_factory=list)
    source_quote: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    date: Optional[str] = None
    url: str = ""
    source_url: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("title", "name", "description", "type", mode="before")
    @classmethod
    def _strip_project(cls, value: object) -> object:
        return _strip_value(value)

    @model_validator(mode="after")
    def _sync_name(self) -> "Project":
        if not self.name and self.title:
            self.name = self.title
        if not self.title and self.name:
            self.title = self.name
        if not self.source_urls:
            self.source_urls = [
                value for value in (self.url, self.source_url) if value
            ]
        return self


class Grant(BaseModel):
    """Грант/конкурс финансирования."""

    model_config = ConfigDict(extra="ignore")

    name: str
    organizer: Optional[str] = None
    amount: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    winners: list[str] = Field(default_factory=list)
    description: str = ""
    source_quote: Optional[str] = None
    url: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("name", "organizer", "amount", "description", mode="before")
    @classmethod
    def _strip_grant(cls, value: object) -> object:
        return _strip_value(value)


class ResearchItem(BaseModel):
    """Научное направление, лаборатория или исследовательский центр."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    head: Optional[str] = None
    source_quote: Optional[str] = None
    url: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("name", "description", "head", mode="before")
    @classmethod
    def _strip_research(cls, value: object) -> object:
        return _strip_value(value)


class Unit(BaseModel):
    """Подразделение вуза: профиль, руководитель и контакты."""

    model_config = ConfigDict(extra="ignore")

    name: str
    profile: str = ""
    head: Optional[str] = None
    contacts: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("name", "profile", "head", mode="before")
    @classmethod
    def _strip_unit(cls, value: object) -> object:
        return _strip_value(value)


class Partnership(BaseModel):
    """Сотрудничество с организацией-партнёром."""

    model_config = ConfigDict(extra="ignore")

    organization: str
    subject: str = ""
    source_quote: Optional[str] = None
    date: Optional[str] = None
    url: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("organization", "subject", mode="before")
    @classmethod
    def _strip_partnership(cls, value: object) -> object:
        return _strip_value(value)


class Achievement(BaseModel):
    """Конкурс, награда или достижение."""

    model_config = ConfigDict(extra="ignore")

    title: str
    result: str = ""
    source_quote: Optional[str] = None
    date: Optional[str] = None
    url: str = ""
    source_urls: list[str] = Field(default_factory=list)

    @field_validator("title", "result", mode="before")
    @classmethod
    def _strip_achievement(cls, value: object) -> object:
        return _strip_value(value)


class Event(BaseModel):
    """Мероприятие: студенческое, образовательное, научное и т. п."""

    model_config = ConfigDict(extra="ignore")

    title: str
    type: str = "не определено"
    date: Optional[str] = None
    description: str = ""
    url: str = ""
    source_url: str = ""

    @field_validator("title", "description", "type", mode="before")
    @classmethod
    def _strip_event(cls, value: object) -> object:
        return _strip_value(value)


class TextBlock(BaseModel):
    """Структурный блок очищенного текста (не сериализуется в JSON)."""

    model_config = ConfigDict(extra="ignore")

    kind: str = "text"
    text: str = ""


class ContactOwner(BaseModel):
    """Владелец контакта для передачи в LLM-модуль."""

    model_config = ConfigDict(extra="ignore")

    kind: OwnerKind = "unknown"
    label: str = ""


class ContactHint(BaseModel):
    """Структурированный контакт, привязанный к фрагменту документа."""

    model_config = ConfigDict(extra="ignore")

    field: Literal["contact.email", "contact.phone"] = "contact.email"
    value: str
    owner: ContactOwner = Field(default_factory=ContactOwner)
    context_fragment_ids: list[str] = Field(default_factory=list)


class BiographySubject(BaseModel):
    person_name: str
    anchor_fragment_id: str


class DocumentFragment(BaseModel):
    """Фрагмент очищенного текста с Unicode-смещениями.

    ``start`` включается, ``end`` не включается. Смещения относятся к
    ``SourceDocument.text``, а не к исходному HTML.
    """

    model_config = ConfigDict(extra="ignore")

    fragment_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    heading: str = ""
    subject_department: str | None = None
    biography_subject: BiographySubject | None = None
    entity_scope_id: str | None = Field(default=None, min_length=1)
    institution_anchor_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    contact_hints: list[ContactHint] = Field(default_factory=list)


class SourceDocument(BaseModel):
    """Очищенный документ источника для нейросети."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    url: str
    title: str = ""
    section: str = ""
    category: str = "other"
    content_type: str = "text/html"
    language: str = "ru"
    text: str = ""
    retrieved_at: datetime = Field(default_factory=utc_now)
    published_at: Optional[datetime] = None
    text_hash: str = ""
    file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fragments: list[DocumentFragment] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    # Служебные поля для извлечения сущностей. В JSON не попадают:
    # потребителю (LLM/CRM) нужны только text, fragments и метаданные.
    blocks: list[TextBlock] = Field(default_factory=list, exclude=True)
    main_html: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _validate_fragments(self) -> "SourceDocument":
        previous_end = 0
        for fragment in self.fragments:
            if fragment.end <= fragment.start:
                raise ValueError(
                    f"Пустой фрагмент {fragment.fragment_id}: "
                    f"{fragment.start}..{fragment.end}"
                )
            if fragment.end > len(self.text):
                raise ValueError(
                    f"Фрагмент {fragment.fragment_id} выходит за границы текста "
                    f"({fragment.end} > {len(self.text)})"
                )
            if fragment.start < previous_end:
                raise ValueError(
                    f"Фрагменты пересекаются: {fragment.fragment_id} начинается "
                    f"раньше предыдущего ({fragment.start} < {previous_end})"
                )
            previous_end = fragment.end
        return self

    def llm_fragments(
        self, max_chars: int = 3500, overlap: int = 250
    ) -> list[dict[str, object]]:
        """Смысловые фрагменты 500–1000 токенов (≈3500 символов).

        Перекрытие используется только в экспорте для нейросети, поэтому
        внутренние фрагменты с контактными подсказками остаются
        непересекающимися и валидными для смещений.
        """

        text = self.text or ""
        if not text:
            return []
        max_chars = max(500, max_chars)
        overlap = max(0, min(overlap, max_chars // 2))
        chunks: list[dict[str, object]] = []
        start = 0
        index = 1
        while start < len(text):
            end = min(len(text), start + max_chars)
            if end < len(text):
                boundary = text.rfind("\n\n", start + max_chars // 2, end)
                if boundary > start:
                    end = boundary
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(
                    {
                        "fragment_id": f"c{index:04d}",
                        "start": start,
                        "end": end,
                        "text": chunk,
                    }
                )
                index += 1
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def to_llm_record(
        self,
        university_name: str,
        official_domain: str,
    ) -> dict[str, object]:
        """Одна запись контракта «документ для нейросети» (раздел 4)."""

        published = (
            self.published_at.date().isoformat() if self.published_at else None
        )
        fragments = []
        for fragment in self.llm_fragments():
            fragments.append(
                {
                    **fragment,
                    "university": {"name": university_name},
                    "category": self.category or "other",
                    "title": self.title,
                    "url": self.url,
                    "published_at": published,
                }
            )
        return {
            "university": {
                "name": university_name,
                "official_domain": official_domain,
            },
            "document": {
                "category": self.category or "other",
                "title": self.title,
                "url": self.url,
                "published_at": published,
                "collected_at": _iso(self.retrieved_at),
                "language": self.language or "ru",
                "content": self.text,
                "content_hash": self.text_hash,
                "content_type": self.content_type,
                "fragments": fragments,
            },
        }


class ChangeReport(BaseModel):
    """Изменения относительно предыдущего снимка карточки."""

    model_config = ConfigDict(extra="ignore")

    previous_scraped_at: Optional[datetime] = None
    new_leadership: list[str] = Field(default_factory=list)
    removed_leadership: list[str] = Field(default_factory=list)
    changed_positions: list[str] = Field(default_factory=list)
    new_projects: list[str] = Field(default_factory=list)
    new_grants: list[str] = Field(default_factory=list)
    new_events: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_leadership
            or self.removed_leadership
            or self.changed_positions
            or self.new_projects
            or self.new_grants
            or self.new_events
        )


class UniversityData(BaseModel):
    """Итоговая карточка вуза + документы для LLM."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "2.0"
    university: UniversityInfo
    about: str = ""
    leadership: list[Leadership] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    grants: list[Grant] = Field(default_factory=list)
    research: list[ResearchItem] = Field(default_factory=list)
    units: list[Unit] = Field(default_factory=list)
    partnerships: list[Partnership] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    documents: list[SourceDocument] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=utc_now)

    def summary(self) -> dict[str, int | str]:
        """Краткая сводка для консоли и журнала."""

        return {
            "university": self.university.name,
            "leadership": len(self.leadership),
            "contacts": len(self.contacts),
            "projects": len(self.projects),
            "grants": len(self.grants),
            "research": len(self.research),
            "units": len(self.units),
            "partnerships": len(self.partnerships),
            "achievements": len(self.achievements),
            "events": len(self.events),
            "documents": len(self.documents),
            "warnings": len(self.warnings),
            "errors": len(self.errors),
        }

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый словарь.

        Содержит как целевые поля раздела 5 (``university_name``,
        ``grants``, ``last_updated``), так и расширенные данные парсера.
        """

        payload = self.model_dump(mode="json", exclude_none=False)
        payload.pop("documents", None)
        payload["documents_count"] = len(self.documents)
        payload["university_name"] = self.university.name
        payload["official_domain"] = self.university.official_domain
        payload["last_updated"] = _iso(self.last_updated)
        return payload
