"""Dossier grounding: separate biography paragraphs and qualified student counts."""

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from pptx import Presentation

from app.services.llm.client import FixtureProvider
from app.services.llm.context_models import ContextExtractionRequest
from app.services.llm.fragment_models import FragmentExtractionRequest
from app.services.llm.models import DraftResult, GenerationRequest
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError
from app.services.meeting_pptx import build_meeting_presentation
from parser.biography import mark_biography_subject
from parser.cleaner import HTMLCleaner
from parser.llm_adapter import to_llm_document
from parser.institution_context import mark_institution_context

NAME = "Иванов Иван Иванович"
INSTITUTION = {"id": "draft:dossier-test", "name": "Учебный университет", "card_version": 1}


def biography_request():
    html = f"""<main><h1>{NAME}</h1><p>Ректор</p>
    <p>В 2001 году окончил Учебный университет по специальности «Экономика».</p>
    <h2>Карьера</h2><p>В 2010–2020 годах работал проректором.</p>
    <h2>Петров Пётр Петрович</h2><p>В 2002 году окончил Технический университет.</p></main>"""
    doc = HTMLCleaner().to_document(html, url="https://university.example/rector", section="bio")
    assert mark_biography_subject(doc, NAME)
    return FragmentExtractionRequest(institution=INSTITUTION, documents=[to_llm_document(doc)])


def bio_extract(request, field="person.education", name=NAME, index=2, **kwargs):
    fragment = request.documents[0].fragments[index]
    value = request.documents[0].text[fragment.start:fragment.end]
    response = {"facts": [{"subject": {"person_name": name}, "field": field, "value": value,
                           "fragment_ids": [fragment.fragment_id]}], "gaps": []}
    return asyncio.run(AssistantPipeline(FixtureProvider([response]), **kwargs).extract_facts(request))


def test_biography_keeps_literal_body_and_named_heading_in_evidence():
    request = biography_request()
    result = bio_extract(request)
    fact = result.facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert len(fact.evidence) == 2
    assert fact.evidence[1].quote == NAME
    assert fact.value.startswith("В 2001 году")
    for evidence in fact.evidence:
        assert evidence.quote == request.documents[0].text[evidence.start_offset:evidence.end_offset]


@pytest.mark.parametrize("field,name,index", [
    ("appointment.position", NAME, 1),  # A profile scope is not proof of a current role.
    ("person.education", "Сидоров Сидор Сидорович", 2),
    ("person.education", NAME, 6),  # A different person's heading ends the profile.
])
def test_biography_scope_cannot_change_person_or_make_current_role(field, name, index):
    result = bio_extract(biography_request(), field, name, index)
    assert result.facts[0].verification_status == "rejected"


def test_invalid_biography_anchor_is_rejected_before_llm():
    raw = biography_request().model_dump(mode="json")
    raw["documents"][0]["fragments"][2]["biography_subject"]["anchor_fragment_id"] = "missing"
    with pytest.raises(ValidationError):
        FragmentExtractionRequest.model_validate(raw)


def test_biography_selector_uses_exact_source_without_retyping_or_new_dates():
    request = biography_request()
    fragment = request.documents[0].fragments[2]
    response = {"facts": [{"subject": {"person_name": NAME}, "field": "person.education",
        "value_fragment_id": fragment.fragment_id, "fragment_ids": [fragment.fragment_id]}], "gaps": []}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request)).facts[0]
    assert fact.validation_status == "passed" and fact.value == request.documents[0].text[fragment.start:fragment.end]
    response["facts"][0]["field"] = "person.interests"
    fact = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request)).facts[0]
    assert "research_interests_require_explicit_statement" in fact.issues


def test_historical_year_is_not_the_institution_founding_date():
    request = metric_request()
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.founded", "value": "2025", "fragment_ids": ["current"]}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request)).facts[0]
    assert "institution_founding_context_required" in fact.issues


def test_education_word_does_not_mean_institution_was_founded():
    request = metric_request()
    request.documents[0].text = request.documents[0].text.replace("численность", "образования")
    # Equal-length change preserves original source offsets.
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.founded", "value_fragment_id": "current", "fragment_ids": ["current"]}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request)).facts[0]
    assert "institution_founding_context_required" in fact.issues


def metric_request():
    texts = ["Учебный университет. В 2025 году численность студентов очной формы головного вуза — 1200 человек.",
             "Другая статья. В 2024 году в филиале обучалось 300 человек."]
    return ContextExtractionRequest(institution=INSTITUTION, documents=[{
        "source_id": "stats", "url": "https://university.example/about", "text": "\n\n".join(texts),
        "retrieved_at": datetime.now(timezone.utc), "fragments": [
            {"fragment_id": "current", "start": 0, "end": len(texts[0])},
            {"fragment_id": "other", "start": len(texts[0]) + 2, "end": sum(map(len, texts)) + 2}]}])


def metric_extract(context=None, refs=None):
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.students", "value": "1200", "fragment_ids": refs or ["current"],
            "metric_context": context}
    return asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(metric_request()))


METRIC = {"period": "2025 году", "scope": "студентов очной формы головного вуза", "unit": "человек"}


def test_qualified_metric_remains_candidate_and_preserves_population():
    fact = metric_extract(METRIC).facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert fact.metric_context.scope == METRIC["scope"]


@pytest.mark.parametrize("metric,refs,issue", [
    (None, None, "student_metric_context_required"),
    ({**METRIC, "period": "сегодня"}, None, "student_metric_period_required"),
    ({**METRIC, "scope": "всех студентов, включая филиалы"}, None, "student_metric_context_not_in_scope"),
    ({**METRIC, "period": "2024 году", "scope": "в филиале"}, ["current", "other"], "student_metric_context_not_in_scope"),
])
def test_unqualified_or_foreign_metric_cannot_be_accepted(metric, refs, issue):
    fact = metric_extract(metric, refs).facts[0]
    assert issue in fact.issues and fact.verification_status == "rejected"


def test_metric_presentation_has_period_population_and_sources(tmp_path):
    result = metric_extract(METRIC)
    fact = result.facts[0]
    now = datetime.now(timezone.utc)
    fact.verification_status, fact.reviewed_by = "verified", "synthetic-test-reviewer"
    fact.reviewed_at = fact.last_checked_at = now
    fact.freshness_status = "within_window"
    statement = {"text": "1200", "fact_ids": [fact.fact_id]}
    draft = DraftResult(institution=result.institution, run=result.run, meeting={}, warnings=[],
        brief_sections=[{"title": "Профиль", "statements": [statement]}],
        slides=[{"title": "Профиль", "theses": [statement]}], questions=[], sources={fact.fact_id: fact.evidence})
    pptx = Presentation(build_meeting_presentation(draft, tmp_path / "metric.pptx", [fact]))
    text = "\n".join(s.text for page in pptx.slides for s in page.shapes if s.has_text_frame)
    assert "Справка об учреждении" in text and "1200" in text
    assert METRIC["period"] in text and METRIC["scope"] in text
    assert "Не подтверждено" in text and "направления подготовки" in text
    for slide in pptx.slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= pptx.slide_height
    fact.metric_context = None
    with pytest.raises(DraftValidationError, match="student_metric_context_required"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(
            GenerationRequest(schema_version="0.3", institution=result.institution, facts=[fact])))


def test_reference_anchor_can_cross_batches_but_cannot_anchor_project_facts():
    doc = HTMLCleaner().to_document("<main><h1>Учебный университет</h1><p>" + "Справка. " * 40 +
        "</p><p>Бакалавриат и магистратура.</p></main>", url="https://university.example/about", section="about")
    assert mark_institution_context(doc, "Учебный университет")
    request = ContextExtractionRequest(institution=INSTITUTION, documents=[to_llm_document(doc, include_contact_hints=False)])
    fragment = request.documents[0].fragments[-1]
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "f0001"},
            "field": "institution.education_levels", "value": "Бакалавриат и магистратура", "fragment_ids": [fragment.fragment_id]}
    pipeline = AssistantPipeline(FixtureProvider([{"facts": [], "gaps": []}, {"facts": [item], "gaps": []}]), chunk_size=400, overlap=0)
    result = asyncio.run(pipeline.extract_facts(request))
    assert result.complete and result.facts[0].validation_status == "passed"
    assert len(result.facts[0].evidence) == 2
    item["subject"]["entity_kind"], item["field"] = "project", "project.description"
    result = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request))
    assert "context_entity_value_scope_mismatch" in result.facts[0].issues
    doc.section = "news"
    assert not mark_institution_context(doc, "Учебный университет")
