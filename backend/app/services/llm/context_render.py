"""Render reviewed context values without upgrading plans, roles or event status."""

from .models import EntitySubject, Fact

LABELS = {
    "institution.profile": "Профиль",
    "project.program": "Программа",
    "project.role": "Роль учреждения",
    "project.status": "Статус проекта",
    "project.period": "Период проекта",
    "project.description": "Описание проекта",
    "event.type": "Вид события",
    "event.date": "Дата события",
    "event.status": "Статус события",
    "event.description": "Описание события",
}


def context_statement(facts: list[Fact]) -> str | None:
    if not facts or not all(isinstance(f.subject, EntitySubject) for f in facts):
        return None
    if len({(f.subject.entity_id, f.subject.entity_kind, f.subject.entity_name) for f in facts}) != 1:
        return None
    parts = [facts[0].subject.entity_name.rstrip(". ")]
    seen = set()
    for fact in facts:
        if fact.field.value.endswith(".name"):
            continue
        key = (fact.field, fact.value)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{LABELS[fact.field.value]}: {fact.value.rstrip('. ')}")
    return ". ".join(parts) + "."


def context_title(facts: list[Fact]) -> str | None:
    kinds = {f.subject.entity_kind for f in facts if isinstance(f.subject, EntitySubject)}
    if not kinds:
        return None
    if any(not isinstance(f.subject, EntitySubject) for f in facts):
        return "Материалы к встрече"
    return " / ".join(label for kind, label in (
        ("institution", "Профиль учреждения"), ("project", "Проекты"), ("event", "События"),
    ) if kind in kinds)
