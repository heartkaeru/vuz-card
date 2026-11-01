import asyncio
import copy
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.services.llm import AssistantPipeline
from app.services.llm.client import FixtureProvider
from app.services.llm.fragment_models import FragmentExtractionRequest, parse_extraction_request
from app.services.llm.models import GenerationRequest
from app.services.llm.pipeline import DraftValidationError


def request():
    blocks = [
        "Ректор\nИванов Иван Иванович\nПерсональная рабочая почта: ivanov@example.org",
        "Приёмная ректора: +7 (3952) 111-22-33",
        "Учебная часть. Адрес электронной почты подразделения.",
        "Петрова Анна Ивановна — заведующий учебной частью. study@example.org",
    ]
    text = "\n\n".join(blocks)
    fragments, start = [], 0
    for index, block in enumerate(blocks, 1):
        fragments.append({"fragment_id": f"f{index}", "start": start, "end": start + len(block)})
        start += len(block) + 2
    fragments[0]["contact_hints"] = [{"field": "contact.email", "value": "ivanov@example.org",
        "owner": {"kind": "person", "label": "Иванов Иван Иванович"}}]
    fragments[1]["contact_hints"] = [{"field": "contact.phone", "value": "+7 (3952) 111-22-33",
        "owner": {"kind": "office", "label": "Приёмная ректора"}}]
    fragments[3]["contact_hints"] = [{"field": "contact.email", "value": "study@example.org",
        "owner": {"kind": "department", "label": "Учебная часть"}, "context_fragment_ids": ["f3"]}]
    return FragmentExtractionRequest.model_validate({
        "schema_version": "0.2", "institution": {"id": "test", "name": "Университет", "card_version": 1},
        "documents": [{"source_id": "source", "url": "https://university.example/leadership",
                       "text": text, "retrieved_at": "2026-10-31T12:00:00+05:00", "fragments": fragments}],
    })


def proposal(field="appointment.position", value="Ректор", refs=None, owner=None, person="Иванов Иван Иванович"):
    return {"subject": {"person_name": person}, "field": field, "value": value,
            "fragment_ids": refs or ["f1"], "contact_owner": owner}


def extract(items, req=None):
    return asyncio.run(AssistantPipeline(FixtureProvider([{"facts": items, "gaps": []}]))
                       .extract_facts(req or request()))


def test_server_builds_exact_quotes_with_offsets_and_hash():
    req = request()
    result = extract([proposal()], req)
    assert result.schema_version == "0.2"
    assert result.facts[0].verification_status == "pending"
    evidence = result.facts[0].evidence[0]
    assert evidence.quote == req.documents[0].text[evidence.start_offset:evidence.end_offset]
    assert evidence.fragment_id == "f1"
    assert len(evidence.content_sha256) == 64
    assert result.facts[1].field.value == "person.name"


def test_model_cannot_supply_or_rewrite_a_quote():
    item = proposal()
    item["quote"] = "Иванов — директор другой организации"
    result = extract([item])
    assert not result.facts
    assert result.errors[0].code == "invalid_extraction_json"


def test_unknown_fragment_is_rejected():
    result = extract([proposal(refs=["invented"])])
    assert result.facts[0].verification_status == "rejected"
    assert "unknown_fragment_id" in result.facts[0].issues


def test_value_and_person_must_share_a_semantic_fragment():
    result = extract([proposal(value="заведующий учебной частью", refs=["f1", "f4"])])
    assert "person_value_not_in_same_fragment" in result.facts[0].issues


@pytest.mark.parametrize("fragment_id,field,value,owner,person", [
    ("f1", "contact.email", "ivanov@example.org", {"kind": "person", "label": "Иванов Иван Иванович"}, "Иванов Иван Иванович"),
    ("f2", "contact.phone", "+7 (3952) 111-22-33", {"kind": "office", "label": "Приёмная ректора"}, None),
    ("f4", "contact.email", "study@example.org", {"kind": "department", "label": "Учебная часть"}, None),
])
def test_explicit_contact_owners_are_preserved(fragment_id, field, value, owner, person):
    result = extract([proposal(field, value, [fragment_id], owner, person)])
    fact = result.facts[0]
    assert fact.validation_status == "passed", fact.issues
    assert fact.contact_owner.model_dump() == owner
    if person is None:
        assert len(result.facts) == 1  # Offices and departments must not become people.
        assert fact.subject.person_name is None
    if owner["kind"] == "department":
        assert {e.fragment_id for e in fact.evidence} == {"f3", "f4"}


def test_department_contact_cannot_be_assigned_to_nearby_person():
    result = extract([proposal("contact.email", "study@example.org", ["f4"],
        {"kind": "person", "label": "Петрова Анна Ивановна"}, "Петрова Анна Ивановна")])
    assert result.facts[0].verification_status == "rejected"
    assert "contact_owner_scope_mismatch" in result.facts[0].issues


def test_office_contact_cannot_retain_person_subject():
    result = extract([proposal("contact.phone", "+7 (3952) 111-22-33", ["f2"],
        {"kind": "office", "label": "Приёмная ректора"})])
    assert "non_person_contact_has_person_subject" in result.facts[0].issues


def test_contact_without_parser_scope_is_not_automatically_accepted():
    req = request()
    req.documents[0].fragments[0].contact_hints = []
    result = extract([proposal("contact.email", "ivanov@example.org", ["f1"],
        {"kind": "person", "label": "Иванов Иван Иванович"})], req)
    assert "contact_owner_requires_source_scope" in result.facts[0].issues


def test_structured_contacts_are_opt_in_deduplicated_and_remain_pending():
    req = request()
    req.project_structured_contacts = True
    # A repeated source hint is not another independent contact.
    req.documents[0].fragments[1].contact_hints *= 2
    result = extract([], req)
    contacts = [f for f in result.facts if f.field.value.startswith("contact.")]
    assert len(contacts) == 3
    assert all(f.origin == "parser_projection" and f.verification_status == "pending" for f in contacts)
    assert all(e.quote == req.documents[0].text[e.start_offset:e.end_offset] for f in contacts for e in f.evidence)


def test_structured_projection_does_not_hide_a_model_ownership_error():
    req = request()
    req.project_structured_contacts = True
    result = extract([proposal("contact.email", "study@example.org", ["f4"],
        {"kind": "person", "label": "Петрова Анна Ивановна"}, "Петрова Анна Ивановна")], req)
    assert result.facts[0].verification_status == "rejected"
    scoped = [f for f in result.facts if f.value == "study@example.org" and f.validation_status == "passed"]
    assert len(scoped) == 1 and scoped[0].subject.person_name is None
    assert scoped[0].contact_owner.kind == "department"


def test_structured_projection_respects_requested_fields():
    req = request()
    req.project_structured_contacts = True
    req.fields = ["person.name"]
    assert extract([], req).facts == []


def test_projection_never_includes_unprocessed_fragments():
    req = request()
    req.project_structured_contacts = True
    pipeline = AssistantPipeline(FixtureProvider([{"facts": [], "gaps": []}]),
                                 chunk_size=100, overlap=0, max_chunks=1)
    result = asyncio.run(pipeline.extract_facts(req))
    assert not result.complete
    projected_refs = {e.fragment_id for f in result.facts for e in f.evidence}
    assert "f4" not in projected_refs


def test_mutated_fragment_offsets_are_rechecked_before_calling_model():
    req = request()
    req.documents[0].fragments[0].end = 999999
    with pytest.raises(ValidationError):
        extract([], req)


def test_nonliteral_fact_value_is_not_accepted_just_because_name_exists():
    result = extract([proposal(field="person.degree", value="доктор медицинских наук")])
    assert "person_value_not_in_same_fragment" in result.facts[0].issues


def test_rector_must_not_match_inside_vice_rector_word():
    req = request()
    raw = req.model_dump(mode="json")
    raw["documents"][0]["text"] = raw["documents"][0]["text"].replace("Ректор", "Проректор", 1)
    # Preserve valid new offsets for this altered source.
    for fragment in raw["documents"][0]["fragments"]:
        if fragment["start"] > 0:
            fragment["start"] += 3
        fragment["end"] += 3
    result = extract([proposal()], FragmentExtractionRequest.model_validate(raw))
    assert "person_value_not_in_same_fragment" in result.facts[0].issues


def test_partial_email_hint_is_not_allowed_to_match_a_different_address():
    raw = request().model_dump(mode="json")
    raw["documents"][0]["fragments"][0]["contact_hints"][0]["value"] = "vanov@example.org"
    with pytest.raises(ValidationError):
        FragmentExtractionRequest.model_validate(raw)


@pytest.mark.parametrize("change", [
    lambda d: d["fragments"][0].update(end=99999),
    lambda d: d["fragments"][1].update(start=0),
    lambda d: d["fragments"][1].update(fragment_id="f1"),
    lambda d: d["fragments"][0]["contact_hints"][0].update(value="not-in-source@example.org"),
    lambda d: d["fragments"][0]["contact_hints"][0]["owner"].update(label="Другой владелец"),
    lambda d: d["fragments"][0]["contact_hints"][0].update(context_fragment_ids=["missing"]),
])
def test_invalid_parser_fragments_fail_before_model_call(change):
    raw = request().model_dump(mode="json")
    change(raw["documents"][0])
    with pytest.raises(ValidationError):
        FragmentExtractionRequest.model_validate(raw)


def test_multiple_duties_for_one_person_are_not_false_conflicts():
    req = request()
    text = req.documents[0].text
    req.documents[0].text += "\nИванов Иван Иванович — председатель учёного совета."
    # Use a second source block with a clearly identified owner.
    from app.services.llm.fragment_models import SourceFragment
    req.documents[0].fragments.append(SourceFragment(fragment_id="f5", start=len(text) + 1,
                                                      end=len(req.documents[0].text)))
    result = extract([proposal(), proposal(value="председатель учёного совета", refs=["f5"])], req)
    assert all(f.verification_status == "pending" for f in result.facts)


def test_acting_and_permanent_head_candidates_are_flagged_for_review():
    from app.services.llm.fragment_models import SourceFragment
    req = request()
    previous_length = len(req.documents[0].text)
    req.documents[0].text += "\nПетров Пётр Петрович — и. о. ректора."
    req.documents[0].fragments.append(SourceFragment(fragment_id="acting", start=previous_length + 1,
                                                       end=len(req.documents[0].text)))
    result = extract([proposal(), proposal(value="и. о. ректора", refs=["acting"],
                                          person="Петров Пётр Петрович")], req)
    positions = [f for f in result.facts if f.field.value == "appointment.position"]
    assert len(positions) == 2
    assert all(f.verification_status == "conflict" for f in positions)


def test_v02_parser_and_v01_default_are_explicit():
    raw = request().model_dump_json()
    assert isinstance(parse_extraction_request(raw), FragmentExtractionRequest)
    value = json.loads(raw)
    value["schema_version"] = "0.3"
    with pytest.raises(ValidationError):
        parse_extraction_request(json.dumps(value))


def test_generation_keeps_office_owner_and_does_not_invent_person():
    result = extract([proposal("contact.phone", "+7 (3952) 111-22-33", ["f2"],
        {"kind": "office", "label": "Приёмная ректора"}, None)])
    now = datetime.now(timezone.utc)
    fact = result.facts[0].model_copy(update={"verification_status": "verified", "reviewed_by": "test",
        "reviewed_at": now, "last_checked_at": now, "freshness_status": "within_window"})
    req = GenerationRequest(schema_version="0.2", institution=result.institution, facts=[fact])

    class CaptureProvider(FixtureProvider):
        async def complete(self, system, payload, schema):
            assert payload["facts"][0]["person_name"] is None
            assert payload["facts"][0]["contact_owner"]["kind"] == "office"
            statement = {"text": "Приёмная ректора: +7 (3952) 111-22-33.", "fact_ids": ["fact_1"]}
            return json.dumps({"brief_sections": [{"title": "Контакты", "statements": [statement]}],
                               "slides": [{"title": "Контакты", "theses": [statement]}], "questions": []})

    draft = asyncio.run(AssistantPipeline(CaptureProvider([])).generate_materials(req))
    assert draft.schema_version == "0.2"
    assert "Служебные контакты «Приёмная ректора»" in draft.brief_sections[0].statements[0].text
    req.facts[0].contact_owner = None
    req.schema_version = "0.1"  # Changing the envelope must not bypass fragment-contract checks.
    with pytest.raises(DraftValidationError, match="contact_owner_required"):
        asyncio.run(AssistantPipeline(CaptureProvider([])).generate_materials(req))
