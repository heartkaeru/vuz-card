"""Validate a proposed single-field edit against the active card."""

import re
from typing import Literal

from pydantic import EmailStr, Field, TypeAdapter, ValidationError

from .changes import effective_facts, normalized, slot, value_key
from .models import StrictModel
from .workflow_models import WorkflowError


class EditCard(StrictModel):
    field: Literal["contact.email", "contact.phone", "appointment.position",
                   "institution.profile", "institution.education_areas",
                   "project.description", "event.description"]
    owner: str = Field(default="", max_length=160, description="ФИО, должность, подразделение или владелец контакта.")
    current_value: str = Field(default="", max_length=500)
    new_value: str = Field(min_length=1, max_length=500)


EDIT_TOOL = {
    "name": "propose_card_edit",
    "description": "Готовит предпросмотр замены одного существующего принятого значения по прямой просьбе сотрудника.",
    "parameters": EditCard.model_json_schema(),
}
EMAIL = TypeAdapter(EmailStr)
FIELD_LABELS = {
    "contact.email": "Рабочая почта", "contact.phone": "Рабочий телефон",
    "appointment.position": "Должность", "institution.profile": "Профиль",
    "institution.education_areas": "Направление подготовки",
    "project.description": "Описание проекта", "event.description": "Описание события",
}


def labels(fact):
    return [label for label in (
        fact.subject.person_name, getattr(fact.subject, "position", None),
        getattr(fact.subject, "department", None), getattr(fact.subject, "entity_name", None),
        fact.contact_owner.label if fact.contact_owner else None,
        fact.value if fact.field.value == "appointment.position" else None,
    ) if label]


def edited_fact(fact, value, active):
    if fact.field.value.startswith("contact.") and (
        not fact.contact_owner or fact.contact_owner.kind == "unknown"
    ):
        raise WorkflowError("chat_edit_unknown_owner")
    if fact.field.value == "contact.email":
        try:
            value = EMAIL.validate_python(value)
        except ValidationError:
            raise WorkflowError("chat_edit_invalid_value") from None
    if fact.field.value == "contact.phone":
        if not re.fullmatch(r"\+?[\d\s().-]+(?:\s*(?:доб\.?|вн\.?|#)\s*\d{1,8})?", value, re.IGNORECASE):
            raise WorkflowError("chat_edit_invalid_value")
        main = re.split(r"доб|вн|#", value, maxsplit=1, flags=re.IGNORECASE)[0]
        if not 7 <= sum(char.isdecimal() for char in main) <= 15:
            raise WorkflowError("chat_edit_invalid_value")
    replacement = fact.model_copy(update={"value": value})
    if any(slot(replacement) == slot(item) and value_key(replacement) == value_key(item) for item in active):
        raise WorkflowError("chat_edit_unchanged")
    return replacement


def preview_edit(run, args, question):
    # The replacement must come from the employee's current message.
    if " ".join(args.new_value.split()) not in " ".join(question.split()):
        raise WorkflowError("chat_edit_value_required")
    active = effective_facts(run)
    owner = normalized(args.owner)
    owner = {"ректора": "ректор", "директора": "директор"}.get(owner, owner)
    matching = [fact for fact in active if fact.field == args.field
                and (not args.current_value or normalized(fact.value) == normalized(args.current_value))]
    if owner:
        direct = [fact for fact in matching if any(
            set(owner.split()) <= set(normalized(label).split()) for label in labels(fact)
        )]
        if not direct and args.field.startswith("contact."):
            people = {fact.subject.person_name for fact in active
                      if fact.field.value == "appointment.position" and normalized(fact.value) == owner}
            direct = [fact for fact in matching if fact.subject.person_name in people and fact.subject.person_name]
        matching = direct
    if len(matching) != 1:
        raise WorkflowError("chat_edit_target_ambiguous" if matching else "chat_edit_target_not_found")
    fact = matching[0]
    replacement = edited_fact(fact, args.new_value, active)
    return {
        "fact_id": fact.fact_id, "field": FIELD_LABELS[args.field],
        "owner": " · ".join(dict.fromkeys(labels(fact)[:2])),
        "before": fact.value, "after": replacement.value, "status": "pending",
    }
