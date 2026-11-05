"""Opt-in 0.2 contract. Fragment boundaries and ownership hints come from the parser."""

from typing import Literal
import json
import re

from pydantic import Field, field_validator, model_validator

from .models import (
    ContactOwner, ExtractionRequest, FactField, NonPersonSubject, SourceDocument,
    StrictModel, Subject,
)
from .names import person_name_in_quote
from .evidence import literal_value_in_text


class ContactHint(StrictModel):
    field: Literal["contact.email", "contact.phone"]
    value: str = Field(min_length=1)
    owner: ContactOwner
    context_fragment_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_contact_format(self):
        if self.field == "contact.email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.value):
            raise ValueError("Invalid email in parser hint")
        if self.field == "contact.phone" and not 7 <= len(re.sub(r"\D", "", self.value)) <= 15:
            raise ValueError("Invalid phone in parser hint")
        return self


class BiographySubject(StrictModel):
    person_name: str = Field(min_length=1)
    anchor_fragment_id: str = Field(min_length=1)


class SourceFragment(StrictModel):
    fragment_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    contact_hints: list[ContactHint] = Field(default_factory=list)
    subject_department: str | None = Field(default=None, min_length=1)
    biography_subject: BiographySubject | None = None
    page_number: int | None = Field(default=None, ge=1)


class FragmentDocument(SourceDocument):
    # Non-overlapping semantic blocks: a person profile, table header or table row.
    fragments: list[SourceFragment] = Field(min_length=1)

    @model_validator(mode="after")
    def check_fragments(self):
        ordered = sorted(self.fragments, key=lambda f: f.start)
        by_id = {f.fragment_id: f for f in ordered}
        if len(by_id) != len(ordered):
            raise ValueError("Fragment IDs must be unique within a source")
        previous_end = 0
        for fragment in ordered:
            if not previous_end <= fragment.start < fragment.end <= len(self.text):
                raise ValueError("Fragments must be in bounds and non-overlapping")
            previous_end = fragment.end
            content = self.text[fragment.start:fragment.end]
            if not content.strip():
                raise ValueError("Empty source fragment")
            if fragment.subject_department and not literal_value_in_text(fragment.subject_department, content):
                raise ValueError("Subject department is absent from its source fragment")
            if fragment.biography_subject:
                bio = fragment.biography_subject
                anchor = by_id.get(bio.anchor_fragment_id)
                if anchor is None or not person_name_in_quote(bio.person_name, self.text[anchor.start:anchor.end]):
                    raise ValueError("Biography subject is absent from its source anchor")
            for hint in fragment.contact_hints:
                if any(fid not in by_id for fid in hint.context_fragment_ids):
                    raise ValueError("Unknown contact context fragment")
                if not literal_value_in_text(hint.value, content, email=hint.field == "contact.email"):
                    raise ValueError("Contact hint value is absent from its fragment")
                context = content + " " + " ".join(
                    self.text[by_id[fid].start:by_id[fid].end] for fid in hint.context_fragment_ids
                )
                grounded_owner = (person_name_in_quote(hint.owner.label, context) if hint.owner.kind == "person"
                                  else literal_value_in_text(hint.owner.label, context))
                if not grounded_owner:
                    raise ValueError("Contact owner label is absent from source context")
        return self


class FragmentExtractionRequest(ExtractionRequest):
    schema_version: Literal["0.2"] = "0.2"
    documents: list[FragmentDocument] = Field(min_length=1)
    project_structured_contacts: bool = False


class FragmentFactProposal(StrictModel):
    subject: Subject | NonPersonSubject
    field: FactField
    value: str | None = Field(default=None, min_length=1)
    # Biography can select a whole source sentence instead of retyping it.
    value_fragment_id: str | None = Field(default=None, min_length=1)
    fragment_ids: list[str] = Field(min_length=1)
    contact_owner: ContactOwner | None = None

    @model_validator(mode="after")
    def one_value_representation(self):
        if (self.value is None) == (self.value_fragment_id is None):
            raise ValueError("Provide value OR value_fragment_id")
        return self

    @field_validator("field")
    @classmethod
    def leadership_fields_only(cls, value):
        if value.value.startswith(("institution.", "project.", "event.")):
            raise ValueError("Context facts require schema 0.3")
        return value


class FragmentExtractionOutput(StrictModel):
    facts: list[FragmentFactProposal]
    gaps: list[str]


def parse_extraction_request(raw: str):
    value = json.loads(raw)
    if value.get("schema_version") == "0.3":
        from .context_models import ContextExtractionRequest
        return ContextExtractionRequest.model_validate(value)
    if value.get("schema_version", "0.1") == "0.2":
        return FragmentExtractionRequest.model_validate(value)
    return ExtractionRequest.model_validate(value)
