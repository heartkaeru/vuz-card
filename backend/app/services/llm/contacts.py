"""Render approved contact-only statements without guessing their owners."""

import re
from collections import defaultdict

from .changes import normalized, slot, value_key
from .models import ContactOwner, Fact, FactField, NonPersonSubject


CONTACT_FIELDS = {FactField.EMAIL, FactField.PHONE}
NAMED_CONTACT_CHANNEL = re.compile(
    r"\b(?:контакт[- ]центр|колл[- ]центр|горячая линия|справочная служба|"
    r"справочная|приемная|канцелярия|служба поддержки|пресс[- ]служба|общие контакты)\b"
)
GENERIC_CONTACT_HEADING = re.compile(r"контактные (?:телефоны|данные)|рабочие телефоны")
CONTACT_ROW_OWNER = re.compile(
    r"^\s*(?P<label>справочная(?: служба)?|при[её]мная комиссия|"
    r"при[её]мная\s+(?:ректора|директора)|контакт[- ]центр|колл[- ]центр|канцелярия)\s*[:|–—]",
    re.I,
)


def refine_contact_hints(fragment, quote):
    """Prefer an explicit label on the contact row to a heading above the table."""
    row = CONTACT_ROW_OWNER.match(quote) if "\n" not in quote.strip() else None
    if not row:
        return
    for hint in fragment["contact_hints"]:
        owner = hint["owner"]
        if GENERIC_CONTACT_HEADING.fullmatch(normalized(owner["label"])):
            hint["owner"] = ContactOwner(kind="office", label=row["label"]).model_dump()
            hint["context_fragment_ids"] = []


def reconcile_contact_candidates(facts, documents):
    hints = {(document.source_id, fragment.fragment_id): fragment.contact_hints
             for document in documents for fragment in document.fragments if fragment.contact_hints}
    grouped = defaultdict(list)
    kept = {}
    for fact in facts:
        if fact.field in CONTACT_FIELDS and fact.validation_status == "passed" and fact.contact_owner:
            grouped[fact.institution_id, fact.field, value_key(fact)].append(fact)
        else:
            kept[fact.fact_id] = fact

    for group in grouped.values():
        owners = defaultdict(list)
        for fact in group:
            owners[slot(fact)].append(fact)
        ranked = []
        for candidates in owners.values():
            representative = candidates[0]
            evidence = unique_contact_evidence(candidates)
            owner = representative.contact_owner
            direct = any(
                not hint.context_fragment_ids and hint.field == representative.field
                and normalized(hint.value) == normalized(candidate.value)
                and hint.owner.kind == owner.kind and normalized(hint.owner.label) == normalized(owner.label)
                for candidate in candidates for proof in candidate.evidence
                for hint in hints.get((proof.source_id, proof.fragment_id), ())
            )
            strength = 2 if direct else int(bool(NAMED_CONTACT_CHANNEL.search(normalized(owner.label))))
            ranked.append((strength, representative.model_copy(update={"evidence": evidence})))

        strongest = max(strength for strength, _ in ranked)
        if len(ranked) > 1 and strongest == 0:
            first = group[0]
            kept[first.fact_id] = first.model_copy(update={
                "subject": NonPersonSubject(), "contact_owner": None,
                "evidence": unique_contact_evidence(group), "validation_status": "failed",
                "verification_status": "rejected", "issues": ["conflicting_contact_scopes"],
            })
        else:
            kept.update((fact.fact_id, fact) for strength, fact in ranked if strength == strongest)
    return [kept[fact.fact_id] for fact in facts if fact.fact_id in kept]


def unique_contact_evidence(candidates):
    return list({(proof.source_id, proof.fragment_id, proof.start_offset, proof.end_offset): proof
                 for fact in candidates for proof in fact.evidence}.values())


def contact_statement(facts: list[Fact]) -> str | None:
    if not facts or any(f.field not in CONTACT_FIELDS or not f.contact_owner for f in facts):
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
