import asyncio
from datetime import datetime, timezone

from app.services.llm.client import FixtureProvider
from app.services.llm.models import Fact, Subject, GenerationRequest
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.leadership_render import leadership_statement
from test_contact_profile_scope import document, request


def test_drafting_cannot_add_acting_status_to_a_regular_appointment():
    doc = document('<p>Иванов Иван Иванович — Проректор по учебной работе.</p>')
    response = {"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
        "field": "appointment.position", "value": "Проректор по учебной работе", "fragment_ids": ["f0001"]}], "gaps": []}
    result = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request(doc)))
    now = datetime.now(timezone.utc)
    for fact in result.facts:
        fact.verification_status = "verified"
        fact.reviewed_by = "fixture-reviewer"
        fact.reviewed_at = fact.last_checked_at = now
        fact.freshness_status = "within_window"
    statement = {"text": "Иванов Иван Иванович назначен исполняющим обязанности проректора.",
                 "fact_ids": [f"fact_{i}" for i in range(1, len(result.facts) + 1)]}
    output = {"brief_sections": [{"title": "Руководство", "statements": [statement]}],
              "slides": [{"title": "Руководство", "theses": [statement]}], "questions": []}
    req = GenerationRequest(schema_version="0.2", institution=result.institution, facts=result.facts)
    draft = asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(req))
    texts = [draft.brief_sections[0].statements[0].text, draft.slides[0].theses[0].text]
    assert all("Проректор по учебной работе" in text for text in texts)
    assert all("назначен" not in text and "исполняющим" not in text for text in texts)


def role(value):
    return Fact(fact_id="r", institution_id="u", subject=Subject(person_name="Иванов Иван Иванович"),
        field="appointment.position", value=value, evidence=[], validation_status="passed", verification_status="pending")


def test_explicit_acting_status_is_preserved():
    value = "исполняющий обязанности ректора"
    assert value in leadership_statement([role(value)])


def test_mixed_contact_statements_are_not_rewritten_as_appointment_only():
    contact = role("one@example.org")
    contact.field = "contact.email"
    assert leadership_statement([role("Ректор"), contact]) is None
