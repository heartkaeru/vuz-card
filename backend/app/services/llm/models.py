"""Versioned contracts. Model output cannot assign review status or source URLs."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Institution(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    card_version: int = Field(ge=1)


class SourceDocument(StrictModel):
    source_id: str = Field(min_length=1)
    url: HttpUrl
    title: str = ""
    # Preserve exact parser text for hashes and source offsets.
    text: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1)]
    retrieved_at: datetime
    published_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("Source text must not be blank")
        return value

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Source dates must include a timezone")
        return value


class ExtractionRequest(StrictModel):
    schema_version: Literal["0.1"] = "0.1"
    institution: Institution
    documents: list[SourceDocument] = Field(min_length=1)
    fields: list[str] | None = Field(default=None, min_length=1)

    @field_validator("fields")
    @classmethod
    def supported_fields(cls, fields):
        allowed = {f.value for f in FactField if not f.value.startswith(("project.", "event.", "institution."))}
        if fields is not None and any(field not in allowed for field in fields):
            raise ValueError("Unsupported extraction field")
        return list(dict.fromkeys(fields)) if fields is not None else None

    @field_validator("documents")
    @classmethod
    def unique_sources(cls, documents):
        ids = [document.source_id for document in documents]
        if len(ids) != len(set(ids)):
            raise ValueError("source_id must be unique within a request")
        return documents


class FactField(str, Enum):
    NAME = "person.name"
    POSITION = "appointment.position"
    STATUS = "appointment.status"
    DEPARTMENT = "appointment.department"
    RESPONSIBILITIES = "appointment.responsibilities"
    EMAIL = "contact.email"
    PHONE = "contact.phone"
    EDUCATION = "person.education"
    DEGREE = "person.degree"
    CAREER = "person.career"
    PROFILE = "institution.profile"
    PROJECT_NAME = "project.name"
    PROJECT_PROGRAM = "project.program"
    PROJECT_ROLE = "project.role"
    PROJECT_STATUS = "project.status"
    PROJECT_PERIOD = "project.period"
    PROJECT_DESCRIPTION = "project.description"
    EVENT_NAME = "event.name"
    EVENT_TYPE = "event.type"
    EVENT_DATE = "event.date"
    EVENT_STATUS = "event.status"
    EVENT_DESCRIPTION = "event.description"


class Subject(StrictModel):
    # This is a candidate identity, not a globally unique person record.
    person_name: str = Field(min_length=1)
    position: str | None = None
    department: str | None = None


class NonPersonSubject(StrictModel):
    person_name: None = None
    position: None = None
    department: str | None = None


class EntitySubject(StrictModel):
    # Local source occurrence, never an automatic merge across articles or years.
    person_name: None = None
    entity_id: str = Field(min_length=1)
    entity_kind: Literal["institution", "project", "event"]
    entity_name: str = Field(min_length=1)


class ContactOwner(StrictModel):
    kind: Literal["person", "office", "department", "organization", "unknown"]
    label: str = Field(min_length=1)


class EvidenceProposal(StrictModel):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class FactProposal(StrictModel):
    subject: Subject
    field: FactField
    value: str = Field(min_length=1)
    evidence: list[EvidenceProposal] = Field(min_length=1)

    @field_validator("field")
    @classmethod
    def leadership_fields_only(cls, value):
        if value.value.startswith(("institution.", "project.", "event.")):
            raise ValueError("Context facts require schema 0.3")
        return value


class ExtractionOutput(StrictModel):
    facts: list[FactProposal]
    gaps: list[str]


class Evidence(EvidenceProposal):
    url: str
    retrieved_at: datetime
    published_at: datetime | None
    content_sha256: str
    chunk_id: str
    fragment_id: str | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)


class Fact(StrictModel):
    fact_id: str
    institution_id: str
    subject: Subject | NonPersonSubject | EntitySubject
    field: FactField
    value: str
    origin: Literal["llm", "subject_projection", "parser_projection"] = "llm"
    contact_owner: ContactOwner | None = None
    evidence: list[Evidence]
    validation_status: Literal["passed", "failed"]
    verification_status: Literal["pending", "verified", "conflict", "rejected"]
    freshness_status: Literal["unknown", "within_window", "needs_recheck"] = "unknown"
    issues: list[str] = Field(default_factory=list)
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    last_checked_at: datetime | None = None


class PipelineError(StrictModel):
    source_id: str
    chunk_id: str
    code: str


class RunInfo(StrictModel):
    provider: str
    model: str
    prompt_version: str
    created_at: datetime


class ExtractionResult(StrictModel):
    schema_version: Literal["0.1", "0.2", "0.3"] = "0.1"
    institution: Institution
    run: RunInfo
    facts: list[Fact]
    gaps: list[str]
    errors: list[PipelineError]
    complete: bool


class MeetingContext(StrictModel):
    goal: str = "Цель встречи не задана"
    date: str | None = None


class GenerationRequest(StrictModel):
    schema_version: Literal["0.1", "0.2", "0.3"] = "0.1"
    institution: Institution
    facts: list[Fact] = Field(min_length=1)
    meeting: MeetingContext = Field(default_factory=MeetingContext)


class GroundedStatement(StrictModel):
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)


class BriefSection(StrictModel):
    title: str = Field(min_length=1)
    statements: list[GroundedStatement]


class SlideTheses(StrictModel):
    title: str = Field(min_length=1)
    theses: list[GroundedStatement] = Field(max_length=5)


class MeetingQuestion(StrictModel):
    question: str = Field(min_length=1)
    basis_fact_ids: list[str] = Field(min_length=1)


class DraftOutput(StrictModel):
    brief_sections: list[BriefSection] = Field(min_length=1)
    slides: list[SlideTheses] = Field(min_length=1)
    questions: list[MeetingQuestion] = Field(max_length=5)


class DraftResult(DraftOutput):
    schema_version: Literal["0.1", "0.2", "0.3"] = "0.1"
    institution: Institution
    run: RunInfo
    meeting: MeetingContext
    status: Literal["draft"] = "draft"
    warnings: list[str]
    sources: dict[str, list[Evidence]]
