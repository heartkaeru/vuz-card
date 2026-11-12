import io
import json

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation

from app.api.v1.ai import get_collector, get_pipeline, get_repository
from app.main import app
from app.services.llm.pipeline import AssistantPipeline
from database.demo1_store import DemoRepository
from examples.llm.workflow_demo import example_card


class DemoProvider:
    name = "fixture"
    model = "offline-api-test"

    async def complete(self, system, payload, schema):
        if "fragments" in payload:
            return json.dumps({"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
                "field": "appointment.position", "value": "Ректор",
                "fragment_ids": [payload["fragments"][0]["fragment_id"]]}], "gaps": []})
        statement = {"text": "; ".join(f["value"] for f in payload["facts"]),
                     "fact_ids": [f["fact_id"] for f in payload["facts"]]}
        return json.dumps({"brief_sections": [{"title": "Руководство", "statements": [statement]}],
            "slides": [{"title": "Руководство и контакты", "theses": [statement]}], "questions": []})


@pytest.fixture
def api_client(tmp_path):
    repository = DemoRepository(tmp_path / "registry.sqlite3")
    calls = []
    def collect(website, name, leadership_url=None, **source_options):
        calls.append(website)
        return example_card()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(DemoProvider())
    app.dependency_overrides[get_collector] = lambda: collect
    with TestClient(app) as client:
        yield client, repository, calls
    app.dependency_overrides.clear()


def collected(client):
    response = client.post("/api/v1/ai/cards", json={"name": "Учебный пример", "city": "Город N",
                                                     "website": "https://university.example/"})
    assert response.status_code == 201, response.text
    card = response.json()
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "collect-one"})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/ai/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "completed", job
    run = client.get(f"/api/v1/ai/runs/{job['result']['run_id']}").json()
    return card, run


def review_payload(run, decision="accept"):
    return {"expected_revision": run["revision"], "reviewer": "test-reviewer",
            "decisions": [{"fact_id": f["fact_id"], "decision": decision, "source_checked": True,
                           "reason": "Synthetic fixture source checked"} for f in run["facts"]]}


def test_ui_api_full_path_and_pptx_download(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    assert len(run["facts"]) == 2
    assert all(f["verification_status"] == "pending" for f in run["facts"])
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "before-review"}).status_code == 409
    response = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run))
    assert response.status_code == 200, response.text
    run = response.json()
    response = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "material-one"})
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/ai/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "completed", job
    download = client.get(job["result"]["download_url"])
    assert download.status_code == 200, download.text if download.status_code != 200 else ""
    assert "presentationml" in download.headers["content-type"]
    presentation = Presentation(io.BytesIO(download.content))
    texts = "\n".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert "Иванов Иван Иванович" in texts
    assert "Учебный пример" in texts and "Черновик" in texts
    assert "Гордин" not in texts and "Смирнова" not in texts
    assert any("university.example" in slide.notes_slide.notes_text_frame.text for slide in presentation.slides)
    for slide in presentation.slides:
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0
            assert shape.left + shape.width <= presentation.slide_width
            assert shape.top + shape.height <= presentation.slide_height
    assert "Иванов" in client.get(job["result"]["brief_url"]).text
    restored = DemoRepository(repository.path)
    assert restored.card(card["id"])["latest_run"] == run["run_id"]
    assert len(restored.reviews(run["run_id"])) == 2
    assert len(restored.material_facts(job["result"]["material_id"])) == 2
    from database.schema import connect
    with connect(repository.path) as db:
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM llm_material_fact")
    reports = client.get("/api/v1/ai/materials").json()
    assert len(reports) == 1 and reports[0]["stale"] is False


def test_refresh_is_idempotent_and_sources_are_not_recollected(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    previous_count = len(calls)
    again = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "collect-one"})
    assert again.status_code == 202
    assert again.json()["result"]["run_id"] == run["run_id"]
    assert len(calls) == previous_count


def test_actualization_http_preserves_card_requires_replacement_and_keeps_history(api_client):
    from parser.cleaner import HTMLCleaner
    from parser.models import UniversityData, UniversityInfo
    client, repository, _ = api_client
    card, old = collected(client)
    old = client.post(f"/api/v1/ai/runs/{old['run_id']}/reviews", json=review_payload(old)).json()
    replacement = "Исполняющий обязанности ректора"
    def collector(website, name, *args, **kwargs):
        doc = HTMLCleaner().to_document(f"<main><p>Иванов Иван Иванович — {replacement}</p></main>",
            url="https://university.example/managers", section="leadership")
        return UniversityData(university=UniversityInfo(name=name, url=website), documents=[doc])
    class UpdatedProvider(DemoProvider):
        async def complete(self, system, payload, schema):
            if "fragments" not in payload:
                return await super().complete(system, payload, schema)
            return json.dumps({"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
                "field": "appointment.position", "value": replacement,
                "fragment_ids": [payload["fragments"][0]["fragment_id"]]}], "gaps": []})
    app.dependency_overrides[get_collector] = lambda: collector
    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(UpdatedProvider())
    def refresh(key):
        job = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": key}).json()
        return client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()
    job = refresh("new-position")
    assert job["status"] == "completed", job
    run = client.get(f"/api/v1/ai/runs/{job['result']['run_id']}").json()
    before = client.get(f"/api/v1/ai/cards/{card['id']}").json()["approved_facts"]
    assert before == old["facts"]
    change_set = client.get(f"/api/v1/ai/runs/{run['run_id']}/changes").json()
    change = next(c for c in change_set["entries"] if c["kind"] == "changed")
    assert change["before"][0]["value"] == "Ректор" and change["after"][0]["value"] == replacement
    payload = review_payload(run)
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=payload).status_code == 409
    payload["retirements"] = [{"fact_id": fid, "source_checked": True, "reason": "Synthetic replacement checked"}
                              for fid in change["retirement_ids"]]
    result = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=payload)
    assert result.status_code == 200, result.text
    active = client.get(f"/api/v1/ai/cards/{card['id']}").json()["approved_facts"]
    assert {f["value"] for f in active} == {"Иванов Иван Иванович", replacement}
    assert client.get(f"/api/v1/ai/runs/{old['run_id']}").json() == old
    assert client.get(f"/api/v1/ai/runs/{old['run_id']}/changes").status_code == 200
    def empty(website, name, *args, **kwargs):
        return UniversityData(university=UniversityInfo(name=name, url=website))
    app.dependency_overrides[get_collector] = lambda: empty
    failed = refresh("empty-after-review")
    assert failed["status"] == "failed"
    detail = client.get(f"/api/v1/ai/cards/{card['id']}").json()
    assert detail["run"]["status"] == "failed" and detail["approved_facts"] == active
    history = client.get(f"/api/v1/ai/cards/{card['id']}/history").json()
    assert len(history) == 3 and all(h["approved_count"] == 2 for h in history)
    export = client.post(f"/api/v1/ai/runs/{detail['run']['run_id']}/materials", json={
        "expected_revision": detail["run"]["revision"], "request_key": "export-preserved"}).json()
    export = client.get(f"/api/v1/ai/jobs/{export['job_id']}").json()
    assert export["status"] == "completed", export
    assert "Последний сбор не удался" in client.get(export["result"]["brief_url"]).text


def test_meeting_context_reaches_provider_persistent_material_and_both_exports(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    seen = []

    class MeetingProvider(DemoProvider):
        async def complete(self, system, payload, schema):
            seen.append(payload["meeting"])
            return await super().complete(system, payload, schema)

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(MeetingProvider())
    meeting = {"goal": "Обсудить стажировки студентов", "date": "2026-11-07"}
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "meeting-context", "meeting": meeting}).json()
    result = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()["result"]
    assert seen == [meeting]
    restored = DemoRepository(repository.path)
    draft, stale = restored.material(result["material_id"])
    assert not stale and draft.meeting.model_dump() == meeting
    assert restored.card_job(card["id"])["identity"]["meeting"] == meeting
    brief = client.get(result["brief_url"]).text
    presentation = Presentation(io.BytesIO(client.get(result["download_url"]).content))
    text = "\n".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert all(value in brief and value in text for value in meeting.values())


def test_source_check_and_version_are_required(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    payload = review_payload(run)
    payload["decisions"][0]["source_checked"] = False
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=payload).status_code == 409
    payload = review_payload(run)
    payload["expected_revision"] -= 1
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=payload).status_code == 409
    assert repository.reviews(run["run_id"]) == []


def test_new_collection_does_not_allow_reviewing_obsolete_run(api_client):
    client, repository, calls = api_client
    card, old_run = collected(client)
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "collect-two"})
    assert response.status_code == 202
    assert client.post(f"/api/v1/ai/runs/{old_run['run_id']}/reviews", json=review_payload(old_run)).status_code == 409
    assert repository.get(old_run["run_id"]).facts


def test_pptx_is_not_current_after_rejection(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "export"}).json()
    result = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()["result"]
    client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run, "reject"))
    assert client.get(result["download_url"]).status_code == 409
    assert client.get("/api/v1/ai/materials").json()[0]["stale"] is True


def test_missing_credentials_do_not_silently_enable_fixture_mode(api_client, monkeypatch):
    client, repository, calls = api_client
    app.dependency_overrides.pop(get_pipeline)
    for name in ("GIGACHAT_CREDENTIALS", "GIGACHAT_MODEL", "GIGACHAT_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    card = client.post("/api/v1/ai/cards", json={"name": "Test", "website": "https://university.example/"}).json()
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "no-key"})
    assert response.status_code == 503
    assert not calls and repository.card_job(card["id"]) is None


def test_explicit_source_stays_on_the_official_domain(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1,
        "request_key": "wrong-domain", "leadership_url": "https://other.example/leadership"})
    assert response.status_code == 422


def test_interrupted_job_can_be_retried_after_server_restart(api_client):
    client, repository, calls = api_client
    card = repository.create_card("Test", "N", "https://university.example")
    job, _ = repository.new_job(card["id"], "interrupted", "collect", {"version": 1})
    repository.update_job(job["job_id"], status="running")
    repository.fail_interrupted_jobs()
    assert repository.job(job["job_id"])["status"] == "failed"
    _, created = repository.new_job(card["id"], "retry-after-restart", "collect", {"version": 1})
    assert created


def test_long_biography_paginates_without_dropping_words(api_client, tmp_path):
    from app.services.meeting_pptx import build_meeting_presentation
    from app.services.llm.models import DraftResult
    from app.services.llm.render import render_markdown
    client, repository, calls = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "long-test"}).json()
    result = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()["result"]
    draft, _ = repository.material(result["material_id"])
    draft.slides[0].theses[0].text = "Начало биографии. " + "Описание этапа карьеры. " * 200 + "Конец биографии."
    path = build_meeting_presentation(draft, tmp_path / "long.pptx")
    presentation = Presentation(path)
    assert len(presentation.slides) > 3
    texts = " ".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert "Начало биографии" in texts and "Конец биографии" in texts
    for word in ("Описание", "этапа", "карьеры."):
        assert texts.split().count(word) == 200
    assert "продолжение" in texts
    for slide in presentation.slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= presentation.slide_height


def test_customer_card_uses_typed_facts_and_reuses_only_brand_assets(api_client, tmp_path):
    from app.services.meeting_pptx import build_meeting_presentation
    client, repository, calls = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "typed-card"}).json()
    result = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()["result"]
    draft, _ = repository.material(result["material_id"])
    facts = repository.material_facts(result["material_id"])
    fact = facts[0].model_copy(deep=True)
    fact.fact_id = "fact_biography_fixture"
    from app.services.llm.models import FactField
    fact.field = FactField.CAREER
    fact.value = "Начало. " + "Описание этапа карьеры. " * 100 + "Окончание."
    from app.services.llm.models import Fact
    fact = Fact.model_validate_json(fact.model_dump_json())
    draft.sources[fact.fact_id] = fact.evidence
    path = build_meeting_presentation(draft, tmp_path / "customer-card.pptx", [*facts, fact])
    presentation = Presentation(path)
    text = " ".join(shape.text for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert "Карточка образовательной организации" in text
    assert "Биография и ответственность" in text
    assert text.split().count("Описание") == 100 and "Окончание." in text
    assert "Корняков" not in text and "Кулинова" not in text and "1308" not in text
    assert all(sum(shape.shape_type == 13 for shape in slide.shapes) == 2 for slide in presentation.slides)
    assert any("customer-card-v1" in slide.notes_slide.notes_text_frame.text for slide in presentation.slides)
    for slide in presentation.slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= presentation.slide_height


@pytest.mark.parametrize("department,position", [
    ("Филиал в городе N", "Директор филиала"),
    (None, "Заместитель директора"),
    (None, "Советник ректора"),
])
def test_branch_or_support_role_is_not_promoted_to_university_head(api_client, tmp_path, department, position):
    from app.services.meeting_pptx import build_meeting_presentation
    client, repository, calls = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "branch-layout"}).json()
    result = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()["result"]
    draft, _ = repository.material(result["material_id"])
    facts = repository.material_facts(result["material_id"])
    # Rendering fixture: source/review behavior is covered by workflow scope tests.
    for fact in facts:
        fact.subject.department = department
        if fact.field.value == "appointment.position":
            fact.value = position
    path = build_meeting_presentation(draft, tmp_path / "branch.pptx", facts)
    presentation = Presentation(path)
    first = "\n".join(s.text for s in presentation.slides[0].shapes if s.has_text_frame)
    rest = "\n".join(s.text for slide in list(presentation.slides)[1:] for s in slide.shapes if s.has_text_frame)
    assert "Руководитель" not in first and "Иванов Иван Иванович" not in first
    if department:
        assert "Подразделение: Филиал в городе N" in rest
    assert position in rest


def test_refresh_supports_biography_and_institution_context_without_autoaccept(api_client):
    from parser.cleaner import HTMLCleaner
    client, repository, calls = api_client
    options = []
    def collect(website, name, leadership_url=None, **source_options):
        options.append(source_options)
        card = example_card()
        card.documents = card.documents[:1] + [HTMLCleaner().to_document(
            "<main><p>Университет Северный. В 2025 году студентов очной формы — 1200 человек.</p></main>",
            url="https://university.example/about", section="about")]
        return card
    class DossierProvider(DemoProvider):
        async def complete(self, system, payload, schema):
            if "fields" in payload:
                return json.dumps({"facts": [{"subject": {"entity_kind": "institution", "entity_name": "Университет Северный",
                    "entity_fragment_id": payload["fragments"][0]["fragment_id"]}, "field": "institution.students", "value": "1200",
                    "metric_context": {"period": "2025 году", "scope": "студентов очной формы", "unit": "человек"},
                    "fragment_ids": [payload["fragments"][0]["fragment_id"]]}], "gaps": []})
            return await super().complete(system, payload, schema)
    app.dependency_overrides[get_collector] = lambda: collect
    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(DossierProvider())
    card = client.post("/api/v1/ai/cards", json={"name": "Университет Северный", "website": "https://university.example/"}).json()
    payload = {"version": 1, "request_key": "dossier", "biography_url": "https://university.example/rector",
               "institution_urls": ["https://university.example/about"]}
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json=payload)
    assert response.status_code == 202, response.text
    job = client.get(f"/api/v1/ai/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "completed", job
    run = client.get(f"/api/v1/ai/runs/{job['result']['run_id']}").json()
    assert set(run["results"]) == {"0.2", "0.3"}
    assert all(fact["verification_status"] == "pending" for fact in run["facts"])
    assert next(f for f in run["facts"] if f["field"] == "institution.students")["metric_context"]["period"] == "2025 году"
    assert options[0]["include_institution"] is True and options[0]["biography_url"] == payload["biography_url"]
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "unreviewed"}).status_code == 409


@pytest.mark.parametrize("extra", [{"biography_url": "https://foreign.example/rector"},
                                    {"institution_urls": ["https://foreign.example/about"]}])
def test_dossier_sources_must_use_official_domain(api_client, extra):
    client, _, _ = api_client
    card = client.post("/api/v1/ai/cards", json={"name": "Вуз", "website": "https://university.example/"}).json()
    response = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "foreign", **extra})
    assert response.status_code == 422


def test_pending_preview_is_marked_and_does_not_accept_facts(api_client):
    client, repository, _ = api_client
    _, run = collected(client)
    response = client.get(f"/api/v1/ai/runs/{run['run_id']}/preview?expected_revision={run['revision']}")
    assert response.status_code == 200
    presentation = Presentation(io.BytesIO(response.content))
    for slide in presentation.slides:
        text = "\n".join(s.text for s in slide.shapes if s.has_text_frame)
        assert "КАНДИДАТЫ НЕ ПРИНЯТЫ" in text
    current = repository.get(run["run_id"])
    assert current.revision == run["revision"] and all(f.verification_status == "pending" for f in current.facts)
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "after-preview"}).status_code == 409
    assert client.get(f"/api/v1/ai/runs/{run['run_id']}/preview?expected_revision=999").status_code == 409


@pytest.mark.parametrize("variant", ["compact", "detailed"])
def test_presentation_variant_is_persisted_and_downloaded_without_changing_facts(api_client, variant):
    client, repository, _ = api_client
    card, run = collected(client)
    run = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run)).json()
    reviews_before = repository.reviews(run["run_id"])
    job = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "variant-test", "presentation_variant": variant}).json()
    job = client.get(f"/api/v1/ai/jobs/{job['job_id']}").json()
    assert job["status"] == "completed", job
    result = job["result"]
    draft, stale = repository.material(result["material_id"])
    assert draft.presentation_variant == variant and not stale
    assert job["identity"]["presentation_variant"] == result["presentation_variant"] == variant
    deck = Presentation(io.BytesIO(client.get(result["download_url"]).content))
    if variant == "compact":
        assert len(deck.slides) == 4
        assert "Руководитель учреждения" in deck.slides[0].shapes[2].text
        assert "Справочная информация" in deck.slides[1].shapes[2].text
        assert "Повестка встречи" in deck.slides[2].shapes[2].text
        assert "Предложения и партнерство со Сбером" in deck.slides[3].shapes[2].text
    for slide in deck.slides:
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0
            assert shape.left + shape.width <= deck.slide_width
            assert shape.top + shape.height <= deck.slide_height
    notes = "\n".join(s.notes_slide.notes_text_frame.text for s in deck.slides)
    for fact in run["facts"]:
        assert fact["fact_id"] in notes and fact["value"] in notes
        assert fact["evidence"][0]["quote"] in notes
    assert "Полные значения выбранных фактов" in client.get(result["brief_url"]).text
    assert client.get("/api/v1/ai/materials").json()[0]["presentation_variant"] == variant
    assert repository.reviews(run["run_id"]) == reviews_before
    assert client.get(f"/api/v1/ai/runs/{run['run_id']}").json() == run
    mismatch = client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "variant-test",
        "presentation_variant": "detailed" if variant == "compact" else "compact"})
    assert mismatch.status_code == 409


def test_compact_preview_stays_unapproved_and_unknown_variant_is_rejected(api_client):
    client, repository, _ = api_client
    card, run = collected(client)
    url = f"/api/v1/ai/runs/{run['run_id']}/preview?expected_revision={run['revision']}"
    response = client.get(url + "&presentation_variant=compact")
    assert response.status_code == 200
    deck = Presentation(io.BytesIO(response.content))
    assert len(deck.slides) == 4
    assert all("КАНДИДАТЫ НЕ ПРИНЯТЫ" in " ".join(s.text for s in slide.shapes if s.has_text_frame) for slide in deck.slides)
    assert client.get(url + "&presentation_variant=unknown").status_code == 422
    assert repository.reviews(run["run_id"]) == []
    assert client.post(f"/api/v1/ai/runs/{run['run_id']}/materials", json={
        "expected_revision": run["revision"], "request_key": "no-review", "presentation_variant": "compact"}).status_code == 409


@pytest.mark.parametrize("source", [
    {"url": "https://foreign.example/report.pdf", "pages": [1]},
    {"url": "https://university.example/report.pdf", "pages": [0]},
    {"url": "https://university.example/report.pdf", "pages": [True]},
    {"url": "https://university.example/report.pdf", "pages": list(range(1, 22))},
])
def test_pdf_api_validates_domain_and_pages(api_client, source):
    client, _, _ = api_client
    card = client.post("/api/v1/ai/cards", json={"name": "Вуз", "website": "https://university.example/"}).json()
    assert client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={
        "version": 1, "request_key": "pdf-source", "pdf_sources": [source]}).status_code == 422
