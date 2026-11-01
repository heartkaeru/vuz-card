"""Render approved contact-only statements without guessing their owners."""

from .models import Fact, FactField


def contact_statement(facts: list[Fact]) -> str | None:
    if not facts or any(f.field not in {FactField.EMAIL, FactField.PHONE} or not f.contact_owner for f in facts):
        return None
    groups = {}
    for fact in facts:
        owner = fact.contact_owner
        if owner.kind == "unknown":
            return None
        fields = groups.setdefault((owner.kind, owner.label), {})
        fields.setdefault(fact.field, []).append(fact.value)
    parts = []
    for (kind, label), fields in groups.items():
        title = {
            "person": f"Рабочие контакты {label}",
            "office": f"Служебные контакты «{label}»",
            "department": f"Контакты подразделения «{label}»",
            "organization": f"Общие контакты организации «{label}»",
        }[kind]
        values = []
        for field, caption in ((FactField.EMAIL, "электронная почта"), (FactField.PHONE, "телефон")):
            if field in fields:
                values.append(f"{caption}: {', '.join(dict.fromkeys(fields[field]))}")
        parts.append(f"{title}: {'; '.join(values)}")
    return ". ".join(parts) + "."
