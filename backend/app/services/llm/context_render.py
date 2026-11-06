"""Render reviewed context values without upgrading plans, roles or event status."""

from .models import EntitySubject, Fact

LABELS = {
    "institution.profile": "Профиль",
    "institution.type": "Тип учреждения",
    "institution.location": "Местонахождение",
    "institution.founded": "Основание",
    "institution.education_areas": "Направления подготовки",
    "institution.education_levels": "Уровни образования",
    "institution.students": "Обучающиеся",
    "institution.structure": "Структура и филиалы",
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
        key = (fact.field, fact.value, fact.metric_context.model_dump_json() if fact.metric_context else None)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{LABELS[fact.field.value]}: {context_value(fact)}")
    return ". ".join(parts) + "."


def context_value(fact: Fact) -> str:
    value = " ".join(fact.value.rstrip(". ").split())
    if fact.metric_context:
        metric = fact.metric_context
        value += f" (период: {metric.period}; состав: {metric.scope}; единица: {metric.unit})"
        if metric.coverage:
            value += f"; охват: {metric.coverage}"
    return value


def context_title(facts: list[Fact]) -> str | None:
    kinds = {f.subject.entity_kind for f in facts if isinstance(f.subject, EntitySubject)}
    if not kinds:
        return None
    if any(not isinstance(f.subject, EntitySubject) for f in facts):
        return "Материалы к встрече"
    return " / ".join(label for kind, label in (
        ("institution", "Профиль учреждения"), ("project", "Проекты"), ("event", "События"),
    ) if kind in kinds)
