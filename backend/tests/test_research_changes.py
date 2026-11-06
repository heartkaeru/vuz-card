import asyncio
import copy
from datetime import datetime, timezone

import pytest

from app.services.llm.changes import compare_run, effective_facts
from app.services.llm.client import FixtureProvider
from app.services.llm.models import Institution
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch, WorkflowError
from database.demo1_store import DemoRepository
from parser.cleaner import HTMLCleaner
from parser.models import UniversityData, UniversityInfo

NAME = "Иванов Иван Иванович"
OLD = "+7 (495) 111-22-33"
NEW = "+7 (495) 111-22-44"


def collect(repo, card, key, name=NAME, phone=OLD, empty=False):
    html = "<main><p>Пустой результат сбора</p></main>" if empty else f"<main><h1>{name}</h1><p>{name} — Ректор. Рабочий телефон: {phone}</p></main>"
    doc = HTMLCleaner().to_document(html, url="https://university.example/managers", section="leadership")
    parsed = UniversityData(university=UniversityInfo(name=card["name"], url=card["website"]), documents=[doc])
    facts = [] if empty else [
        {"subject": {"person_name": name}, "field": "appointment.position", "value": "Ректор", "fragment_ids": ["f0002"]},
        {"subject": {"person_name": name}, "field": "contact.phone", "value": phone, "fragment_ids": ["f0002"],
         "contact_owner": {"kind": "person", "label": name}}]
    workflow = LLMWorkflow(repo, AssistantPipeline(FixtureProvider([{"facts": facts, "gaps": []}])))
    run, _ = workflow.create(Institution(id=card["id"], name=card["name"], card_version=card["version"]), parsed, key, modes=("0.2",))
    return workflow, asyncio.run(workflow.extract(run.run_id)), parsed


def review(workflow, run, ids=None, decision="accept", retire=()):
    return workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=[
        {"fact_id": fid, "decision": decision, "source_checked": True, "reason": "Synthetic source checked"}
        for fid in (ids if ids is not None else [f.fact_id for f in run.facts])], retirements=[
        {"fact_id": fid, "source_checked": True, "reason": "Explicit replacement of synthetic old value"} for fid in retire]), actor="test-reviewer")


@pytest.fixture
def initial(tmp_path):
    repo = DemoRepository(tmp_path / "registry.sqlite3")
    card = repo.create_card("Учебный университет", "Город N", "https://university.example")
    workflow, run, _ = collect(repo, card, "first")
    assert len(run.facts) == 3 and all(f.validation_status == "passed" for f in run.facts)
    return repo, card, review(workflow, run)


def test_phone_update_requires_explicit_replacement_and_keeps_history(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "second", phone=NEW)
    changes = compare_run(run)
    change = next(c for c in changes["entries"] if c["kind"] == "changed")
    assert change["before"][0].value == OLD and change["after"][0].value == NEW
    assert run.previous_run_id == old.run_id and len(run.baseline_facts) == 3
    assert {f.value for f in effective_facts(run) if f.field.value == "contact.phone"} == {OLD}
    with pytest.raises(WorkflowError, match="replacement_confirmation"):
        review(workflow, run)
    assert repo.get(run.run_id).revision == run.revision
    updated = review(workflow, run, retire=change["retirement_ids"])
    assert {f.value for f in effective_facts(updated) if f.field.value == "contact.phone"} == {NEW}
    assert repo.get(old.run_id).facts == old.facts
    assert any(record["decision"] == "retire" for record in repo.reviews(run.run_id))
    reopened = DemoRepository(repo.path).get(run.run_id)
    assert effective_facts(reopened) == effective_facts(updated)


def test_rejecting_new_phone_keeps_previously_approved_value(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "reject-update", phone=NEW)
    before = effective_facts(run)
    phone = next(f for f in run.facts if f.field.value == "contact.phone")
    updated = review(workflow, run, [phone.fact_id], "reject")
    assert effective_facts(updated) == before and not updated.retired_fact_ids


def test_empty_repeat_does_not_delete_or_recheck_old_facts_and_can_export_them(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "empty", empty=True)
    assert run.status == "failed" and effective_facts(run) == effective_facts(old)
    assert all(c["kind"] == "not_confirmed" and not c["retirement_ids"] for c in compare_run(run)["entries"])
    class DraftProvider:
        name, model = "fixture", "test"
        async def complete(self, system, payload, schema):
            import json
            statement = {"text": "; ".join(f["value"] for f in payload["facts"]), "fact_ids": [f["fact_id"] for f in payload["facts"]]}
            return json.dumps({"brief_sections": [{"title": "Сведения", "statements": [statement]}],
                "slides": [{"title": "Сведения", "theses": [statement]}], "questions": []})
    workflow.pipeline = AssistantPipeline(DraftProvider())
    material_id, draft = asyncio.run(workflow.generate(run.run_id, run.revision))
    assert repo.material_facts(material_id) == effective_facts(old)
    assert any("сбор не удался" in warning for warning in draft.warnings)


def test_unchanged_values_still_need_review_of_new_source(initial):
    repo, card, old = initial
    workflow, run, parsed = collect(repo, card, "unchanged")
    assert compare_run(run)["counts"]["unchanged"] == 3
    assert all(f.verification_status == "pending" for f in run.facts)
    assert effective_facts(run) == effective_facts(old)
    again, created = workflow.create(run.institution, parsed, "unchanged", modes=("0.2",))
    assert not created and again.run_id == run.run_id
    assert repo.card(card["id"])["latest_run"] == run.run_id


def test_possible_rector_change_archives_previous_profile_only_on_explicit_review(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "new-rector", name="Петров Пётр Петрович", phone=NEW)
    entry = next(c for c in compare_run(run)["entries"] if c["kind"] == "head_change")
    assert set(entry["retirement_ids"]) == {f.fact_id for f in old.facts}
    with pytest.raises(WorkflowError, match="replacement_confirmation"):
        review(workflow, run)
    run = review(workflow, run, retire=entry["retirement_ids"])
    assert {f.subject.person_name for f in effective_facts(run)} == {"Петров Пётр Петрович"}
    assert all(f.verification_status == "verified" for f in repo.get(old.run_id).facts)


def test_unrelated_retirement_and_superseded_review_are_blocked(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "changed", phone=NEW)
    unrelated = next(f.fact_id for f in old.facts if f.field.value == "person.name")
    with pytest.raises(WorkflowError, match="replacement_confirmation"):
        review(workflow, run, retire=[unrelated])
    with pytest.raises(WorkflowError, match="superseded"):
        review(workflow, old)


def test_branch_owner_and_metric_period_are_not_merged(initial):
    from test_dossier import metric_extract, METRIC
    _, _, old = initial
    run = old.model_copy(deep=True)
    run.previous_run_id, run.baseline_facts = old.run_id, copy.deepcopy(old.facts)
    for fact in run.facts:
        fact.fact_id += "-branch"
        fact.subject.department = "Филиал в городе N"
        fact.verification_status = "pending"
    assert not any(c["kind"] in {"changed", "head_change"} for c in compare_run(run)["entries"])
    metric = metric_extract(METRIC).facts[0]
    metric.verification_status = "verified"
    run.baseline_facts = [metric]
    newer = metric.model_copy(deep=True)
    newer.fact_id += "-2026"
    newer.metric_context.period = "2026 году"
    newer.verification_status = "pending"
    run.facts = [newer]
    assert compare_run(run)["counts"]["changed"] == 0
    assert compare_run(run)["counts"]["new"] == 1
    assert effective_facts(run) == [metric]


def test_ambiguous_new_head_cannot_bypass_explicit_replacement(initial):
    repo, card, old = initial
    workflow, run, _ = collect(repo, card, "ambiguous", name="Петров Пётр Петрович")
    def add_second(current, db):
        other = next(f for f in current.facts if f.field.value == "appointment.position").model_copy(deep=True)
        other.fact_id += "-other"
        other.subject.person_name = "Сидоров Сидор Сидорович"
        current.facts.append(other)
    run = repo.mutate(run.run_id, run.revision, add_second)
    head = next(f for f in run.facts if f.field.value == "appointment.position")
    with pytest.raises(WorkflowError, match="ambiguous_head_replacement"):
        review(workflow, run, [head.fact_id])
    assert effective_facts(repo.get(run.run_id)) == effective_facts(old)


def test_head_role_title_change_does_not_hide_person_replacement(initial):
    repo, card, old = initial
    _, run, _ = collect(repo, card, "director", name="Петров Пётр Петрович")
    for fact in run.facts:
        if fact.field.value == "appointment.position":
            fact.value = "Директор"
    entry = next(c for c in compare_run(run)["entries"] if c["kind"] == "head_change")
    assert set(entry["retirement_ids"]) == {f.fact_id for f in old.facts}


def test_reused_previous_ids_are_not_treated_as_fresh_evidence(initial):
    _, _, old = initial
    run = old.model_copy(deep=True)
    run.previous_run_id = old.run_id
    run.baseline_facts = copy.deepcopy(old.facts)
    changes = compare_run(run)
    assert changes["counts"]["not_confirmed"] == 3
    assert changes["approved_facts"] == effective_facts(old)


def test_offline_actualization_rehearsal_is_isolated_and_repeatable(tmp_path):
    from examples.llm.actualization_demo import seed, NEW_PHONE
    repo = DemoRepository(tmp_path / "fictional.sqlite3")
    card = asyncio.run(seed(repo))
    run = repo.get(card["latest_run"])
    changed = next(c for c in compare_run(run)["entries"] if c["kind"] == "changed")
    assert changed["after"][0].value == NEW_PHONE
    assert len(effective_facts(run)) == 3 and all(f.verification_status == "pending" for f in run.facts)
    assert all(result.run.provider == "fixture" for result in run.results.values())
    assert asyncio.run(seed(repo))["latest_run"] == run.run_id
