"""Keep appointment wording literal when a statement contains only identity/role facts."""

from .models import Fact, FactField


def leadership_statement(facts: list[Fact]) -> str | None:
    labels = {
        FactField.NAME: "ФИО", FactField.POSITION: "Должность",
        FactField.STATUS: "Статус назначения", FactField.DEPARTMENT: "Подразделение",
        FactField.APPOINTED_AT: "Назначение", FactField.EDUCATION: "Образование",
        FactField.DEGREE: "Учёная степень", FactField.TITLE: "Учёное звание",
        FactField.CAREER: "Карьера", FactField.INTERESTS: "Научные интересы",
        FactField.RESPONSIBILITIES: "Зона ответственности",
    }
    if not facts or any(f.field not in labels or not f.subject.person_name for f in facts):
        return None
    groups = {}
    for fact in facts:
        key = (fact.subject.person_name, fact.subject.department)
        groups.setdefault(key, []).append(fact)
    parts = []
    for (name, department), group in groups.items():
        fields = list(dict.fromkeys(f"{labels[f.field]}: {f.value}" for f in group if f.field != FactField.NAME))
        if department and not any(f.field == FactField.DEPARTMENT and f.value == department for f in group):
            fields.insert(0, f"Подразделение: {department}")
        parts.append(". ".join([name, *fields]))
    return ". ".join(parts) + "."
