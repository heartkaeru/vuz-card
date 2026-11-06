"""Compare evidence-backed proposals against approved data. Absence never deletes."""

import hashlib
import json
import re


def normalized(value):
    return " ".join((value or "").lower().replace("ё", "е").split())


def value_key(fact):
    if fact.metric_context and re.fullmatch(r"[\d\s]+", fact.value):
        return re.sub(r"\s", "", fact.value)
    if fact.field.value == "contact.phone":
        digits = re.sub(r"\D", "", fact.value)
        return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits
    return normalized(fact.value)


MULTIPLE = {"person.education", "person.degree", "person.academic_title", "person.career", "person.interests",
            "appointment.responsibilities", "institution.profile", "institution.education_areas",
            "institution.education_levels", "institution.structure"}


def slot(fact):
    subject = fact.subject
    if subject.person_name:
        owner = ("person", normalized(subject.person_name), normalized(subject.department))
    elif fact.contact_owner:
        owner = (fact.contact_owner.kind, normalized(fact.contact_owner.label),
                 normalized(getattr(subject, "department", None)))
    elif getattr(subject, "entity_kind", None) == "institution":
        owner = ("institution", fact.institution_id)
    else:
        # Project/event occurrence identities must never merge just by a title.
        owner = ("entity", getattr(subject, "entity_id", fact.fact_id))
    qualifiers = tuple(normalized(value) for value in (
        fact.metric_context.period, fact.metric_context.scope, fact.metric_context.coverage,
        fact.metric_context.unit)) if fact.metric_context else ()
    return (owner, fact.field.value, qualifiers, value_key(fact) if fact.field.value in MULTIPLE else None)


def effective_facts(run):
    """Newest explicitly approved proof wins for equal values; no auto-acceptance."""
    result = {}
    excluded = set(run.retired_fact_ids) | {f.fact_id for f in run.facts if f.verification_status == "rejected"}
    for fact in [*run.baseline_facts, *run.facts]:
        if (fact.verification_status == "verified" and fact.validation_status == "passed"
                and not fact.issues and fact.fact_id not in excluded):
            result[(slot(fact), value_key(fact))] = fact
    return list(result.values())


def head_key(fact):
    if (fact.field.value != "appointment.position" or not fact.subject.person_name
            or fact.subject.department):
        return None
    value = normalized(fact.value)
    if re.search(r"филиал|заместител|помощник|советник|проректор", value):
        return None
    for role in ("ректор", "директор"):
        if re.search(rf"(?<!\w){role}(?:а)?(?!\w)", value):
            return role
    return None


def compare_run(run):
    # Legacy/first studies may already have reviewed facts without a parent snapshot.
    baseline = run.baseline_facts if run.previous_run_id else [f for f in run.facts if f.verification_status == "verified"]
    baseline = [f for f in baseline if f.validation_status == "passed" and not f.issues]
    current = [f for f in run.facts if f.validation_status == "passed" and not f.issues
               and f.verification_status in {"pending", "verified"}]
    active_ids = {f.fact_id for f in effective_facts(run)}
    entries, suppressed = [], set()

    def entry(kind, key, before, after, retire=None):
        label_fact = (after or before)[0]
        subject = label_fact.subject
        label = subject.person_name or (label_fact.contact_owner.label if label_fact.contact_owner else
                                        getattr(subject, "entity_name", "Учреждение"))
        scope = getattr(subject, "department", None)
        if scope:
            label += " · " + scope
        identity = json.dumps([key, [f.fact_id for f in after]], ensure_ascii=False, sort_keys=True)
        return {"change_id": hashlib.sha256(identity.encode()).hexdigest()[:24], "kind": kind,
                "field": "leadership.head" if kind == "head_change" else label_fact.field.value,
                "subject_label": label, "before": before, "after": after,
                "retirement_trigger_ids": [f.fact_id for f in after if kind != "head_change" or head_key(f)],
                "retirement_ids": [f.fact_id for f in (retire or []) if f.fact_id in active_ids]}

    baseline_ids = {f.fact_id for f in baseline}
    fresh = [f for f in current if f.fact_id not in baseline_ids]
    old_names = {normalized(f.subject.person_name) for f in baseline if head_key(f)}
    new_names = {normalized(f.subject.person_name) for f in fresh if head_key(f)}
    if len(old_names) == len(new_names) == 1 and old_names != new_names:
        before = [f for f in baseline if normalized(f.subject.person_name) in old_names and not getattr(f.subject, "department", None)]
        after = [f for f in current if normalized(f.subject.person_name) in new_names and not getattr(f.subject, "department", None)]
        entries.append(entry("head_change", ("head",), before, after, before))
        suppressed.update(f.fact_id for f in [*before, *after])
    old_groups, new_groups = {}, {}
    for fact in baseline:
        if fact.fact_id not in suppressed:
            old_groups.setdefault(slot(fact), []).append(fact)
    for fact in fresh if run.previous_run_id else current:
        if fact.fact_id not in suppressed:
            new_groups.setdefault(slot(fact), []).append(fact)
    for key in dict.fromkeys([*new_groups, *old_groups]):
        before, after = old_groups.get(key, []), new_groups.get(key, [])
        if not before:
            kind, retire = "new", []
        elif not after:
            kind, retire = "not_confirmed", []
        else:
            old_values, new_values = {value_key(f) for f in before}, {value_key(f) for f in after}
            kind = "unchanged" if old_values == new_values else "changed"
            retire = [f for f in before if value_key(f) not in new_values] if kind == "changed" else []
        entries.append(entry(kind, key, before, after, retire))
    counts = {kind: sum(e["kind"] == kind for e in entries) for kind in
              ("new", "changed", "head_change", "unchanged", "not_confirmed")}
    return {"run_id": run.run_id, "revision": run.revision, "previous_run_id": run.previous_run_id,
            "counts": counts, "entries": entries, "approved_facts": effective_facts(run),
            "warning": "Отсутствие пригодного кандидата в повторном сборе не доказывает удаление или увольнение. Прежние сведения сохранены."}
