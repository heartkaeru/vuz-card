import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.llm.client import FixtureProvider
from app.services.llm.models import Institution
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch, WorkflowError
from database.llm_store import LLMRepository, stamp
from database.schema import connect, initialize
from examples.llm.context_demo import example_request
from parser.cleaner import HTMLCleaner
from parser.main import save_results
from parser.models import UniversityData, UniversityInfo

EXAMPLES = Path(__file__).parents[1] / "examples/llm"


def parser_card():
    cleaner = HTMLCleaner()
    leadership = cleaner.to_document(
        "<main><p>Иванов Иван Иванович — Ректор.</p></main>",
        url="https://university.example/leadership", section="leadership")
    leadership.category = "leadership"
    blocks = example_request().documents[0].text.split("\n\n")
    context = cleaner.to_document("<main>" + "".join(f"<p>{b}</p>" for b in blocks) + "</main>",
                                  url="https://university.example/projects", section="projects")
    context.category = "projects"
    return UniversityData(university=UniversityInfo(name="Университет Северный", url="https://university.example/"),
                          documents=[leadership, context])


def extraction_responses(card):
    leader, context = card.documents
    first = {"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
                        "field": "appointment.position", "value": "Ректор",
                        "fragment_ids": [leader.fragments[0].fragment_id]}], "gaps": []}
    second = json.loads((EXAMPLES / "extract_context_responses.json").read_text(encoding="utf-8"))[0]
    mapping = {f"f{i}": fragment.fragment_id for i, fragment in enumerate(context.fragments, 1)}
    for fact in second["facts"]:
        fact["subject"]["entity_fragment_id"] = mapping[fact["subject"]["entity_fragment_id"]]
        fact["fragment_ids"] = [mapping[fid] for fid in fact["fragment_ids"]]
    return [first, second]


def draft_response(run):
    groups = {}
    for index, fact in enumerate(run.facts, 1):
        if fact.verification_status != "verified":
            continue
        key = getattr(fact.subject, "entity_id", "leadership")
        groups.setdefault(key, []).append((index, fact))
    sections, slides = [], []
    for group in groups.values():
        statement = {"text": "; ".join(f.value for _, f in group),
                     "fact_ids": [f"fact_{i}" for i, _ in group]}
        sections.append({"title": "Сведения", "statements": [statement]})
        slides.append({"title": "Сведения", "theses": [copy.deepcopy(statement)]})
    return {"brief_sections": sections, "slides": slides, "questions": []}


@pytest.fixture
def setup(tmp_path):
    repository = LLMRepository(tmp_path / "registry.sqlite3")
    card = parser_card()
    workflow = LLMWorkflow(repository, AssistantPipeline(FixtureProvider(extraction_responses(card))))
    institution = Institution(id="draft:integration-example", name=card.university.name, card_version=1)
    run, created = workflow.create(institution, card, "test-request")
    assert created
    return repository, workflow, run, card


def extracted(setup):
    repository, workflow, run, card = setup
    result = asyncio.run(workflow.extract(run.run_id))
    assert result.status == "needs_review", result.model_dump()
    return repository, workflow, result, card


def approve(workflow, run):
    return workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=[
        {"fact_id": f.fact_id, "decision": "accept", "source_checked": True,
         "reason": "Проверка вымышленной тестовой фикстуры"} for f in run.facts
    ]), actor="test-reviewer")


def test_parser_llm_database_review_and_draft_roundtrip(setup):
    repository, workflow, run, card = extracted(setup)
    assert len(run.facts) == 11
    assert all(f.verification_status == "pending" for f in run.facts)
    assert all(f.verification_status == "pending" for r in run.results.values() for f in r.facts)
    reopened = LLMRepository(repository.path)
    assert reopened.source_card(run.run_id)["documents"][0]["text"] == card.documents[0].text
    with pytest.raises(WorkflowError, match="no_accepted_facts"):
        asyncio.run(workflow.generate(run.run_id, run.revision))
    run = approve(workflow, run)
    workflow.pipeline = AssistantPipeline(FixtureProvider([draft_response(run)]))
    material_id, draft = asyncio.run(workflow.generate(run.run_id, run.revision))
    assert draft.status == "draft" and len(draft.sources) == 11
    assert draft.institution.id == run.institution.id
    assert any("личность" in warning for warning in draft.warnings)
    assert reopened.material(material_id)[0] == draft
    assert not reopened.material(material_id)[1]
    assert len(reopened.reviews(run.run_id)) == 11
    assert reopened.get(run.run_id).results["0.2"].facts[0].verification_status == "pending"
    with connect(repository.path) as db:
        assert db.execute("SELECT COUNT(*) FROM legal_entity").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM institution").fetchone()[0] == 0
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        initialize(db)


def test_idempotent_request_does_not_run_llm_again(setup):
    repository, workflow, run, card = extracted(setup)
    again, created = workflow.create(run.institution, card, "test-request")
    assert not created and again.run_id == run.run_id
    with pytest.raises(WorkflowError, match="already_started"):
        asyncio.run(workflow.extract(run.run_id))
    card.warnings.append("Another input")
    with pytest.raises(WorkflowError, match="request_key_payload_mismatch"):
        workflow.create(run.institution, card, "test-request")


@pytest.mark.parametrize("changes,code", [
    ({"source_checked": False}, "source_check_required"),
    ({"fact_id": "foreign-fact"}, "unknown_review_fact_id"),
])
def test_invalid_review_has_no_partial_writes(setup, changes, code):
    repository, workflow, run, card = extracted(setup)
    decisions = [{"fact_id": f.fact_id, "decision": "accept", "source_checked": True, "reason": "test"}
                 for f in run.facts[:2]]
    decisions[1].update(changes)
    with pytest.raises(WorkflowError, match=code):
        workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=decisions), actor="test")
    assert not repository.reviews(run.run_id)
    assert repository.get(run.run_id).revision == run.revision
    assert all(f.verification_status == "pending" for f in repository.get(run.run_id).facts)


def test_stale_review_and_duplicate_decisions_rejected(setup):
    repository, workflow, run, card = extracted(setup)
    old_revision = run.revision
    current = approve(workflow, run)
    decision = {"fact_id": run.facts[0].fact_id, "decision": "reject", "reason": "test"}
    with pytest.raises(WorkflowError, match="stale_run_revision"):
        workflow.review(run.run_id, ReviewBatch(expected_revision=old_revision, decisions=[decision]), actor="test")
    with pytest.raises(WorkflowError, match="duplicate_review_fact_id"):
        workflow.review(run.run_id, ReviewBatch(expected_revision=current.revision, decisions=[decision] * 2), actor="test")


def test_rejected_and_stale_facts_are_not_used_for_generation(setup):
    repository, workflow, run, card = extracted(setup)
    run = approve(workflow, run)
    rejected_id = run.facts[0].fact_id
    run = workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=[
        {"fact_id": rejected_id, "decision": "reject", "reason": "No longer valid"}]), actor="test")
    with pytest.raises(DraftValidationError, match="only_reviewed"):
        asyncio.run(workflow.generate(run.run_id, run.revision, fact_ids=[rejected_id]))
    def age(current, db):
        current.facts[1].last_checked_at = stamp() - timedelta(days=8)
    run = repository.mutate(run.run_id, run.revision, age)
    with pytest.raises(DraftValidationError, match="freshness_check"):
        asyncio.run(workflow.generate(run.run_id, run.revision, fact_ids=[run.facts[1].fact_id]))


def test_review_audit_and_parser_snapshot_are_immutable(setup):
    repository, workflow, run, card = extracted(setup)
    approve(workflow, run)
    with connect(repository.path) as db:
        for statement in ("DELETE FROM llm_review", "UPDATE llm_review SET actor='other'",
                          "DELETE FROM llm_run_input", "UPDATE llm_run_input SET payload='{}'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)


def test_approval_during_generation_invalidates_result(setup):
    repository, workflow, run, card = extracted(setup)
    run = approve(workflow, run)
    class ConcurrentProvider(FixtureProvider):
        async def complete(self, system, payload, schema):
            workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=[
                {"fact_id": run.facts[0].fact_id, "decision": "reject", "reason": "Concurrent rejection"}]), actor="test")
            return await super().complete(system, payload, schema)
    workflow.pipeline = AssistantPipeline(ConcurrentProvider([draft_response(run)]))
    with pytest.raises(WorkflowError, match="facts_changed_during_generation"):
        asyncio.run(workflow.generate(run.run_id, run.revision))
    with connect(repository.path) as db:
        assert db.execute("SELECT COUNT(*) FROM llm_material").fetchone()[0] == 0


def test_old_material_is_marked_stale_after_review(setup):
    repository, workflow, run, card = extracted(setup)
    run = approve(workflow, run)
    workflow.pipeline = AssistantPipeline(FixtureProvider([draft_response(run)]))
    material_id, _ = asyncio.run(workflow.generate(run.run_id, run.revision))
    workflow.review(run.run_id, ReviewBatch(expected_revision=run.revision, decisions=[
        {"fact_id": run.facts[0].fact_id, "decision": "reject", "reason": "Correction"}]), actor="test")
    assert repository.material(material_id)[1]


def test_unavailable_model_is_persisted_as_failure(setup):
    repository, workflow, run, card = setup
    workflow.pipeline = AssistantPipeline(FixtureProvider([]))
    failed = asyncio.run(workflow.extract(run.run_id))
    assert failed.status == "failed"
    assert failed.results["0.2"].errors[0].code == "fixture_exhausted"
    assert not failed.facts
    assert repository.get(run.run_id) == failed


def test_parser_native_snapshot_keeps_full_llm_input(tmp_path):
    card = parser_card()
    paths = save_results(card, tmp_path)
    raw = json.loads(paths["llm_card"].read_text(encoding="utf-8"))
    restored = UniversityData.model_validate(raw)
    assert restored.model_dump(mode="json") == card.model_dump(mode="json")
    assert raw["documents"][0]["source_id"] == card.documents[0].source_id
    assert raw["documents"][0]["retrieved_at"] == card.model_dump(mode="json")["documents"][0]["retrieved_at"]


def test_registered_card_version_is_checked_again_before_generation(setup):
    repository, workflow, old_run, card = setup
    with connect(repository.path) as db:
        db.execute("INSERT INTO legal_entity(id,inn,official_name,verified_at) VALUES (?,?,?,?)",
                   ("synthetic-legal", "1234567894", "Synthetic fixture", stamp().isoformat()))
        db.execute("""INSERT INTO institution(id,legal_entity_id,unit_code,display_name,education_type,unit_type)
                      VALUES (?,?,?,?,?,?)""",
                   ("registered-test", "synthetic-legal", "head", card.university.name, "university", "head"))
    run, _ = workflow.create(Institution(id="registered-test", name=card.university.name, card_version=1), card, "registered")
    run = approve(workflow, asyncio.run(workflow.extract(run.run_id)))
    with connect(repository.path) as db:
        db.execute("UPDATE institution SET version=version+1 WHERE id='registered-test'")
    with pytest.raises(WorkflowError, match="stale_card_version"):
        asyncio.run(workflow.generate(run.run_id, run.revision))


def test_missing_registry_identity_never_creates_a_fake_legal_entity(setup):
    repository, workflow, run, card = setup
    with pytest.raises(WorkflowError, match="unknown_institution"):
        workflow.create(Institution(id="unknown", name=card.university.name, card_version=1), card, "unknown")
    with connect(repository.path) as db:
        assert db.execute("SELECT COUNT(*) FROM legal_entity").fetchone()[0] == 0


def test_partial_parser_failure_is_visible_and_does_not_approve_candidates(setup):
    repository, workflow, run, card = setup
    card.errors.append("Fixture: news section unavailable")
    run, _ = workflow.create(run.institution, card, "partial-parser")
    run = asyncio.run(workflow.extract(run.run_id))
    assert run.status == "partial"
    assert run.parser_errors == ["Fixture: news section unavailable"]
    assert all(f.verification_status == "pending" for f in run.facts)


def test_conflicts_cannot_be_accepted_by_setting_source_checked(setup):
    repository, workflow, run, card = extracted(setup)
    def conflict(current, db):
        current.facts[0].verification_status = "conflict"
        current.facts[0].issues.append("multiple_head_candidates")
    run = repository.mutate(run.run_id, run.revision, conflict)
    with pytest.raises(WorkflowError, match="resolve_conflict_or_validation"):
        approve(workflow, run)
    assert not repository.reviews(run.run_id)


@pytest.mark.parametrize("selected", [[], ["foreign-fact"], ["repeat", "repeat"]])
def test_selected_fact_ids_cannot_bypass_stored_run(setup, selected):
    repository, workflow, run, card = extracted(setup)
    with pytest.raises(WorkflowError, match="invalid_selected_fact_ids"):
        asyncio.run(workflow.generate(run.run_id, run.revision, fact_ids=selected))


def test_cancelled_extraction_does_not_remain_running(setup):
    repository, workflow, run, card = setup
    class CancelProvider(FixtureProvider):
        async def complete(self, *args):
            raise asyncio.CancelledError
    workflow.pipeline = AssistantPipeline(CancelProvider([]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow.extract(run.run_id))
    result = repository.get(run.run_id)
    assert result.status == "failed" and result.errors == ["extraction_cancelled"]


def test_offline_workflow_demo_and_independent_review_cli(tmp_path):
    backend = Path(__file__).parents[1]
    def cli(*args):
        completed = subprocess.run([sys.executable, *args], cwd=backend, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        return completed
    cli("-m", "examples.llm.workflow_demo", "--output-dir", str(tmp_path))
    pending = json.loads((tmp_path / "pending_run.json").read_text(encoding="utf-8"))
    assert all(f["verification_status"] == "pending" for f in pending["facts"])
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["review"].startswith("SIMULATED")
    database = tmp_path / "separate-cli.sqlite3"
    base = ["-m", "app.services.llm.workflow_cli", "--db", str(database)]
    cli(*base, "extract", "--input", str(tmp_path / "parser_llm_card.json"), "--institution-id", "draft:cli",
        "--request-key", "one", "--responses", str(tmp_path / "extraction_responses.json"),
        "--output", str(tmp_path / "cli-run.json"))
    run = json.loads((tmp_path / "cli-run.json").read_text(encoding="utf-8"))
    # Identical request works without credentials or fixtures and makes no new model call.
    cli(*base, "extract", "--input", str(tmp_path / "parser_llm_card.json"),
        "--institution-id", "draft:cli", "--request-key", "one")
    cli(*base, "show", run["run_id"], "--output", str(tmp_path / "shown.json"),
        "--review-template", str(tmp_path / "decisions.json"))
    decisions = json.loads((tmp_path / "decisions.json").read_text(encoding="utf-8"))
    assert all(d["decision"] is None and not d["source_checked"] for d in decisions["decisions"])
    for decision in decisions["decisions"]:
        decision.update(decision="accept", source_checked=True, reason="Explicit synthetic test review")
    (tmp_path / "decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    cli(*base, "review", run["run_id"], "--decisions", str(tmp_path / "decisions.json"), "--actor", "test-reviewer")
    cli(*base, "draft", run["run_id"], "--revision", str(run["revision"] + 1),
        "--responses", str(tmp_path / "draft_responses.json"), "--output-dir", str(tmp_path / "materials"))
    assert len(list((tmp_path / "materials").glob("*.md"))) == 1
