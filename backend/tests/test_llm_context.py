import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.llm import AssistantPipeline
from app.services.llm.client import FixtureProvider
from app.services.llm.context_models import ContextExtractionRequest, ContextField
from app.services.llm.fragment_models import parse_extraction_request
from app.services.llm.models import EntitySubject, ExtractionRequest, GenerationRequest
from app.services.llm.pipeline import DraftValidationError
from examples.llm.context_demo import example_request

EXAMPLES = Path(__file__).parents[1] / "examples" / "llm"


def responses():
    return json.loads((EXAMPLES / "extract_context_responses.json").read_text(encoding="utf-8"))


def extract(items=None, request=None, **kwargs):
    output = responses()[0] if items is None else {"facts": items, "gaps": []}
    return asyncio.run(AssistantPipeline(FixtureProvider([output]), **kwargs).extract_facts(
        request or example_request()))


def proposal(field="project.status", value="подготовка заявки", anchor="f2", refs=None):
    return {"subject": {"entity_kind": "project", "entity_name": "Лаборатория робототехники",
                        "entity_fragment_id": anchor},
            "field": field, "value": value, "fragment_ids": refs or [anchor]}


def reviewed_request():
    result = extract()
    now = datetime.now(timezone.utc)
    for fact in result.facts:
        fact.verification_status = "verified"
        fact.reviewed_by = "test-reviewer"
        fact.reviewed_at = fact.last_checked_at = now
        fact.freshness_status = "within_window"
    return GenerationRequest(schema_version="0.3", institution=result.institution, facts=result.facts)


def test_context_keeps_candidate_status_and_exact_source_slices():
    request = example_request()
    result = extract(request=request)
    assert result.complete and result.schema_version == "0.3"
    assert len(result.facts) == 9
    assert all(f.verification_status == "pending" for f in result.facts)
    assert all(isinstance(f.subject, EntitySubject) and f.subject.person_name is None for f in result.facts)
    assert {f.field.value for f in result.facts} >= {"institution.profile", "project.role", "event.date"}
    assert next(f for f in result.facts if f.field.value == "project.role").value == "планирует подать заявку"
    for fact in result.facts:
        for evidence in fact.evidence:
            assert evidence.quote == request.documents[0].text[evidence.start_offset:evidence.end_offset]
            assert evidence.url == str(request.documents[0].url)
            assert len(evidence.content_sha256) == 64


def test_context_parser_is_explicit_and_legacy_fields_stay_separate():
    request = example_request()
    assert isinstance(parse_extraction_request(request.model_dump_json()), ContextExtractionRequest)
    raw = json.loads((EXAMPLES / "extract_request.json").read_text(encoding="utf-8"))
    raw["fields"] = ["project.name"]
    with pytest.raises(ValidationError):
        ExtractionRequest.model_validate(raw)


@pytest.mark.parametrize("field,value,issue", [
    ("project.status", "победитель конкурса", "value_not_in_fragment"),
    ("project.name", "робототехники", "context_name_mismatch"),
    ("event.status", "подготовка заявки", "context_entity_field_mismatch"),
    ("project.role", "готовка", "value_not_in_fragment"),
])
def test_unsupported_value_or_kind_rejected(field, value, issue):
    result = extract([proposal(field, value)])
    assert not result.complete
    assert issue in result.facts[0].issues
    assert result.facts[0].verification_status == "rejected"


@pytest.mark.parametrize("changes,issue", [
    ({"fragment_ids": ["f3"]}, "value_not_in_fragment"),
    ({"fragment_ids": ["invented"]}, "unknown_fragment_id"),
    ({"subject": {"entity_kind": "project", "entity_name": "Выдуманный проект",
                  "entity_fragment_id": "f2"}}, "context_entity_not_in_anchor"),
])
def test_name_and_value_require_source_support(changes, issue):
    item = proposal() | changes
    result = extract([item])
    assert issue in result.facts[0].issues


def test_field_scope_does_not_accept_a_neighbouring_events_date():
    item = proposal("project.period", "12 ноября 2026 года", refs=["f3"])
    result = extract([item])
    assert "context_entity_value_scope_mismatch" in result.facts[0].issues


def test_title_and_body_can_share_parser_scope():
    raw = example_request().model_dump(mode="json")
    document = raw["documents"][0]
    project = document["fragments"][1]
    split = document["text"].index(" Университет Северный планирует")
    body = dict(project, fragment_id="project-body", start=split + 1)
    project["end"] = split
    document["fragments"].append(body)
    request = ContextExtractionRequest.model_validate(raw)
    result = extract([proposal(refs=["project-body"])], request)
    assert result.complete
    assert len(result.facts[0].evidence) == 2
    body["entity_scope_id"] = "another-project"
    result = extract([proposal(refs=["project-body"])], ContextExtractionRequest.model_validate(raw))
    assert "context_entity_value_scope_mismatch" in result.facts[0].issues


def test_repeated_event_title_from_other_article_does_not_merge_occurrences():
    request = example_request()
    raw = request.model_dump(mode="json")
    document = raw["documents"][0]
    block = "Карьерный форум «Старт». Дата: 12 ноября 2025 года."
    start = len(document["text"]) + 2
    document["text"] += "\n\n" + block
    document["fragments"].append({"fragment_id": "old-event", "start": start,
                                  "end": len(document["text"]), "entity_scope_id": "old-article"})
    old = {"subject": {"entity_kind": "event", "entity_name": "Карьерный форум «Старт»",
                       "entity_fragment_id": "old-event"},
           "field": "event.date", "value": "12 ноября 2025 года", "fragment_ids": ["old-event"]}
    new = responses()[0]["facts"][7]
    result = extract([new, old], ContextExtractionRequest.model_validate(raw))
    assert result.complete
    assert result.facts[0].subject.entity_id != result.facts[1].subject.entity_id
    old["subject"]["entity_fragment_id"] = "f3"
    result = extract([old], ContextExtractionRequest.model_validate(raw))
    assert "context_entity_value_scope_mismatch" in result.facts[0].issues


def test_publication_date_is_not_accepted_as_event_date_without_text_evidence():
    item = copy.deepcopy(responses()[0]["facts"][7])
    item["value"] = "2026-10-30"
    result = extract([item])
    assert "value_not_in_fragment" in result.facts[0].issues


def test_occurrence_id_does_not_merge_changed_snapshots_of_same_url():
    original = extract([proposal()])
    request = example_request()
    # Equal length change keeps offsets valid; this is another immutable snapshot.
    request.documents[0].text = request.documents[0].text.replace("2026", "2027")
    changed = extract([proposal()], request)
    assert original.facts[0].subject.entity_id != changed.facts[0].subject.entity_id


def test_unknown_or_model_supplied_quote_rejects_whole_model_response():
    item = proposal() | {"quote": "Иная цитата"}
    result = extract([item])
    assert not result.facts and result.errors[0].code == "invalid_context_json"


def test_context_field_filter_and_duplicate_outputs():
    request = example_request()
    request.fields = [ContextField.PROJECT_NAME]
    request = ContextExtractionRequest.model_validate_json(request.model_dump_json())
    result = extract([proposal("project.name", "Лаборатория робототехники")] * 2, request)
    assert len(result.facts) == 1
    result = extract([proposal()], request)
    assert "field_not_requested" in result.facts[0].issues


def test_incompatible_values_flag_conflict_without_auto_selection():
    items = [proposal(), proposal(value="планирует подать заявку")]
    result = extract(items)
    assert all(f.verification_status == "conflict" for f in result.facts)
    assert all("conflicting_context_values" in f.issues for f in result.facts)


def test_batch_limits_and_unavailable_provider_are_not_empty_success():
    result = asyncio.run(AssistantPipeline(FixtureProvider([])).extract_facts(example_request()))
    assert not result.complete and result.errors[0].code == "fixture_exhausted"
    result = extract([], chunk_size=100, overlap=10)
    assert any(e.code == "context_scope_too_large" for e in result.errors)
    result = extract([], chunk_size=250, overlap=10, max_chunks=1)
    assert any(e.code == "context_batch_limit_reached" for e in result.errors)


def test_mutated_fragment_bounds_checked_again_before_llm_call():
    request = example_request()
    request.documents[0].fragments[0].end = 99999
    with pytest.raises(ValidationError):
        extract([], request)


def test_pending_or_stale_context_facts_cannot_generate_materials():
    request = reviewed_request()
    request.facts[0].verification_status = "pending"
    with pytest.raises(DraftValidationError, match="only_reviewed"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(request))
    request = reviewed_request()
    request.facts[0].last_checked_at -= timedelta(days=8)
    with pytest.raises(DraftValidationError, match="freshness_check"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(request))


def test_context_generation_requires_opt_in_and_matching_entity_kind():
    request = reviewed_request()
    request.schema_version = "0.1"
    with pytest.raises(DraftValidationError, match="require_schema_0_3"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(request))
    request.schema_version = "0.3"
    request.facts[0].subject.entity_kind = "project"
    with pytest.raises(DraftValidationError, match="context_subject_field_mismatch"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(request))


def test_context_generation_sends_entity_values_but_no_unreviewed_quote_details():
    class RecordingProvider(FixtureProvider):
        async def complete(self, system, payload, schema):
            assert "draft-context-v2" in system
            assert payload["facts"][1]["entity"]["name"] == "Лаборатория робототехники"
            assert "quote" not in json.dumps(payload)
            assert "fictional-context" not in json.dumps(payload)
            return await super().complete(system, payload, schema)

    request = reviewed_request()
    draft = asyncio.run(AssistantPipeline(RecordingProvider([responses()[1]])).generate_materials(request))
    assert draft.status == "draft" and draft.run.prompt_version == "draft-context-v2"
    assert draft.schema_version == "0.3"
    assert set(draft.sources) == {f.fact_id for f in request.facts}


def test_context_generation_rejects_invented_numbers_or_references():
    request = reviewed_request()
    output = responses()[1]
    output["slides"][0]["theses"][0]["text"] += " Бюджет 900000 рублей."
    with pytest.raises(DraftValidationError, match="unsupported_number"):
        asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))
    output = responses()[1]
    output["slides"][0]["theses"][0]["fact_ids"] = ["unknown-fact"]
    with pytest.raises(DraftValidationError, match="unknown_fact_reference"):
        asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))


def test_context_empty_text_result_is_a_gap_not_university_has_no_projects():
    result = extract([])
    assert result.complete and not result.facts
    assert "переданных фрагментах" in result.gaps[0]


def test_context_statements_and_titles_are_built_from_exact_reviewed_values():
    request = reviewed_request()
    output = responses()[1]
    output["brief_sections"][1]["title"] = "Успешно поддержанные проекты"
    output["brief_sections"][1]["statements"][0]["text"] = "Вуз победил в конкурсе и получил поддержку."
    draft = asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))
    section = draft.brief_sections[1]
    assert section.title == "Проекты"
    assert "Роль учреждения: планирует подать заявку" in section.statements[0].text
    assert "Статус проекта: подготовка заявки" in section.statements[0].text
    assert "победил" not in section.statements[0].text


def test_one_context_statement_cannot_mix_entities():
    request = reviewed_request()
    output = responses()[1]
    output["slides"][0]["theses"][0] = {"text": "Сводные сведения.", "fact_ids": ["fact_2", "fact_6"]}
    with pytest.raises(DraftValidationError, match="context_statement_requires_single_entity"):
        asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))


def test_brief_limit_also_applies_after_context_template_expansion():
    request = reviewed_request()
    request.facts[0].value = "исследования " * 801
    with pytest.raises(DraftValidationError, match="brief_too_long"):
        asyncio.run(AssistantPipeline(FixtureProvider([responses()[1]])).generate_materials(request))


def test_equivalent_context_theses_are_not_duplicated_after_rendering():
    request = reviewed_request()
    output = responses()[1]
    output["slides"][0] = {"title": "Профиль", "theses": [
        {"text": "Ключевой профиль — инженерное образование.", "fact_ids": ["fact_1"]},
        {"text": "Важное место занимают прикладные исследования.", "fact_ids": ["fact_1"]},
    ]}
    draft = asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))
    assert len(draft.slides[0].theses) == 1
    assert "ключевой" not in draft.slides[0].theses[0].text.casefold()


def test_mixed_brief_accepts_separate_leadership_and_context_statements():
    request = reviewed_request()
    leadership = parse_extraction_request((EXAMPLES / "extract_request.json").read_text(encoding="utf-8"))
    leadership.institution = request.institution
    saved = json.loads((EXAMPLES / "extract_responses.json").read_text(encoding="utf-8"))
    people = asyncio.run(AssistantPipeline(FixtureProvider(saved)).extract_facts(leadership)).facts
    now = datetime.now(timezone.utc)
    for fact in people:
        fact.verification_status = "verified"
        fact.reviewed_by = "test-reviewer"
        fact.reviewed_at = fact.last_checked_at = now
        fact.freshness_status = "within_window"
    request.facts.extend(people)
    output = responses()[1]
    statement = {"text": "; ".join(f.value for f in people),
                 "fact_ids": [f"fact_{index}" for index in range(10, 10 + len(people))]}
    output["brief_sections"].append({"title": "Руководство", "statements": [statement]})
    output["slides"].append({"title": "Руководство", "theses": [copy.deepcopy(statement)]})
    draft = asyncio.run(AssistantPipeline(FixtureProvider([output])).generate_materials(request))
    assert draft.brief_sections[-1].title == "Руководство"
    assert len(draft.sources) == 9 + len(people)
    assert draft.brief_sections[1].title == "Проекты"
