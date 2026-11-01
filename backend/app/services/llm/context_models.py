"""Opt-in project/event contract; identities and exact quotes come from the service."""

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .models import Institution, SourceDocument, StrictModel


class ContextField(str, Enum):
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


class ContextFragment(StrictModel):
    fragment_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    # Parser-defined scope: one article/project/event, not a whole university website.
    entity_scope_id: str | None = Field(default=None, min_length=1)


class ContextDocument(SourceDocument):
    fragments: list[ContextFragment] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_fragments(self):
        previous_end, ids = 0, set()
        for fragment in sorted(self.fragments, key=lambda f: f.start):
            if fragment.fragment_id in ids:
                raise ValueError("Duplicate fragment_id")
            ids.add(fragment.fragment_id)
            if not previous_end <= fragment.start < fragment.end <= len(self.text):
                raise ValueError("Fragments must be in bounds and non-overlapping")
            if not self.text[fragment.start:fragment.end].strip():
                raise ValueError("Blank fragment")
            previous_end = fragment.end
        return self


class ContextExtractionRequest(StrictModel):
    schema_version: Literal["0.3"] = "0.3"
    institution: Institution
    documents: list[ContextDocument] = Field(min_length=1, max_length=50)
    fields: list[ContextField] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def unique_sources_and_fields(self):
        ids = [d.source_id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source_id")
        if self.fields is not None:
            self.fields = list(dict.fromkeys(self.fields))
        return self


class ContextSubjectProposal(StrictModel):
    entity_kind: Literal["institution", "project", "event"]
    entity_name: str = Field(min_length=1)
    entity_fragment_id: str = Field(min_length=1)


class ContextFactProposal(StrictModel):
    subject: ContextSubjectProposal
    field: ContextField
    # A literal passage; paraphrasing belongs to the reviewed drafting stage.
    value: str = Field(min_length=1)
    fragment_ids: list[str] = Field(min_length=1)


class ContextExtractionOutput(StrictModel):
    facts: list[ContextFactProposal]
    gaps: list[str]
