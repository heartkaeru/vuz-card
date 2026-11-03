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
    def collect(website, name, leadership_url=None):
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
