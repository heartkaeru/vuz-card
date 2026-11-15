import io
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from gigachat.models import Choices, Messages
from pptx import Presentation

from app.api.v1.ai import get_collector, get_pipeline, get_repository
from app.auth.security import create_access_token
from app.main import app
from app.services.llm.models import EntitySubject, FactField
from app.services.llm.pipeline import AssistantPipeline
from database.demo1_store import DemoRepository
from database.llm_store import stamp
from examples.llm.workflow_demo import example_card
from parser.models import UniversityData


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
    assert client.get("/api/v1/ai/materials").json() == []
    saved = client.put(f"/api/v1/ai/materials/{job['result']['material_id']}/report")
    assert saved.status_code == 200 and saved.json() == {"saved": True}
    reports = client.get("/api/v1/ai/materials").json()
    assert len(reports) == 1 and reports[0]["stale"] is False


def test_pptx_recheck_renews_inherited_facts_after_explicit_source_confirmation(api_client):
    client, repository, _ = api_client
    card, run = collected(client)
    reviewed = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run))
    assert reviewed.status_code == 200, reviewed.text

    def age_facts(current, _):
        for fact in current.facts:
            fact.last_checked_at = stamp() - timedelta(days=8)

    repository.mutate(run["run_id"], reviewed.json()["revision"], age_facts)
    snapshot = UniversityData.model_validate(repository.source_card(run["run_id"]))
    app.dependency_overrides[get_collector] = lambda: lambda *args, **kwargs: snapshot
    refreshed = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={
        "version": card["version"], "request_key": "recheck-collection",
    })
    assert refreshed.status_code == 202, refreshed.text
    job = repository.job(refreshed.json()["job_id"])
    current = repository.get(job["result"]["run_id"])
    path = f"/api/v1/ai/runs/{current.run_id}"
    ids = [fact.fact_id for fact in current.baseline_facts]
    assert len(ids) == 2 and all(fact.verification_status == "pending" for fact in current.facts)
    assert current.reconfirmed_facts == {}
    assert client.get(f"{path}/changes").json()["recheck_fact_ids"] == ids
    payload = {"expected_revision": current.revision, "request_key": "recheck-material"}
    blocked = client.post(f"{path}/materials", json=payload)
    assert blocked.status_code == 409 and blocked.json()["detail"] == "facts_require_freshness_check"
    assert repository.card_job(card["id"])["job_id"] == job["job_id"]

    recheck = {"expected_revision": current.revision, "fact_ids": ids, "reviewer": "test-reviewer"}
    assert client.post(f"{path}/recheck", json={**recheck, "source_checked": False}).status_code == 422
    pending = client.post(f"{path}/recheck", json={
        **recheck, "fact_ids": [current.facts[0].fact_id], "source_checked": True,
    })
    assert pending.status_code == 409
    renewed = client.post(f"{path}/recheck", json={**recheck, "source_checked": True})
    assert renewed.status_code == 200, renewed.text
    assert client.post(f"{path}/recheck", json={**recheck, "source_checked": True}).status_code == 409
    assert client.get(f"{path}/changes").json()["recheck_fact_ids"] == []
    for entry, old in zip(repository.reviews(current.run_id), current.baseline_facts, strict=True):
        assert entry["decision"] == "recheck" and entry["actor"] == "test-reviewer"
        assert json.loads(entry["before_json"])["value"] == json.loads(entry["after_json"])["value"] == old.value

    payload["expected_revision"] = renewed.json()["revision"]
    response = client.post(f"{path}/materials", json=payload)
    assert response.status_code == 202, response.text
    result = repository.job(response.json()["job_id"])
    assert result["status"] == "completed", result
    assert client.get(result["result"]["download_url"]).status_code == 200


def test_repeat_collection_refreshes_outdated_facts_and_exports_without_new_human_review(api_client):
    client, repository, _ = api_client
    card, run = collected(client)
    response = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run))
    assert response.status_code == 200, response.text

    def age_facts(current, _):
        for fact in current.facts:
            fact.last_checked_at -= timedelta(days=8)

    previous = repository.mutate(run["run_id"], response.json()["revision"], age_facts)
    refresh = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={
        "version": card["version"], "request_key": "refresh-confirmed-facts",
    })
    job = repository.job(refresh.json()["job_id"])
    current = repository.get(job["result"]["run_id"])
    path = f"/api/v1/ai/runs/{current.run_id}"
    changes = client.get(f"{path}/changes").json()
    assert changes["recheck_fact_ids"] == []
    assert set(changes["reconfirmed_facts"]) == {f.fact_id for f in previous.facts}
    assert all(f["reviewed_by"] == "test-reviewer" for f in changes["approved_facts"])
    duplicate = client.post(f"{path}/reviews", json=review_payload(current.model_dump(mode="json")))
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "fact_already_reconfirmed"

    response = client.post(f"{path}/materials", json={"expected_revision": current.revision, "request_key": "export-refreshed"})
    assert response.status_code == 202, response.text
    material = repository.job(response.json()["job_id"])
    assert material["status"] == "completed", material
    assert client.get(material["result"]["download_url"]).status_code == 200


def test_educational_match_uses_reviewed_profile_and_reaches_pptx(api_client):
    client, repository, _ = api_client
    _, run = collected(client)
    first_revision = run["revision"]

    def add_profile(research, _):
        fact = research.facts[0].model_copy(update={
            "fact_id": "education-fact",
            "field": FactField.EDUCATION_AREAS,
            "subject": EntitySubject(entity_kind="institution", entity_id=research.institution.id,
                                     entity_name=research.institution.name),
            "value": "Прикладной анализ данных",
        })
        fact.evidence = [fact.evidence[0].model_copy(update={"quote": fact.value})]
        research.facts.append(fact)

    updated = repository.mutate(run["run_id"], first_revision, add_profile)
    run = updated.model_dump(mode="json")
    base = f"/api/v1/ai/runs/{run['run_id']}"
    calls = []
    proposal = {
        "catalog_id": "netology-data-viz", "basis_fact_ids": ["education-fact"],
        "proposal": "Обсудить вводный курс по визуализации для преподавателей.",
        "rationale": "Вуз ведёт подготовку по прикладному анализу данных.",
        "discussion_question": "Какие инструменты визуализации используются на занятиях?",
    }

    class MatchProvider(DemoProvider):
        async def complete(self, system, payload, schema):
            if "catalog" not in payload:
                statement = {"text": "Прикладной анализ данных.", "fact_ids": ["education-fact"]}
                return json.dumps({
                    "brief_sections": [{"title": "Направления", "statements": [statement]}],
                    "slides": [{"title": "Направления", "theses": [statement]}],
                    "questions": [],
                })
            calls.append(payload)
            return json.dumps({"matches": [proposal]})

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(MatchProvider())
    params = {"expected_revision": run["revision"], "meeting": {"goal": "Обучение преподавателей"}}
    assert client.post(f"{base}/partnership-matches", json=params).json() == []
    assert calls == []

    run = client.post(f"{base}/reviews", json=review_payload(run)).json()
    assert client.post(f"{base}/partnership-matches", json=params).status_code == 409
    params["expected_revision"] = run["revision"]
    response = client.post(f"{base}/partnership-matches", json=params)
    assert response.status_code == 200, response.text
    matches = response.json()
    assert [fact["fact_id"] for fact in calls[-1]["facts"]] == ["education-fact"]
    assert calls[-1]["meeting"]["goal"] == "Обучение преподавателей"
    assert {"edutoria", "sber-payroll", "sber-campus-card", "sber-health-corporate"}.isdisjoint(
        item["id"] for item in calls[-1]["catalog"]
    )
    assert matches[0]["kind"] == "external_course"
    assert matches[0]["catalog_url"] == "https://netology.ru/programs/data-viz"

    result = client.post(f"{base}/materials", json={
        **params, "request_key": "with-educational-proposal", "partnership_matches": matches,
    })
    assert result.status_code == 202, result.text
    job = client.get(f"/api/v1/ai/jobs/{result.json()['job_id']}").json()
    assert job["status"] == "completed", job["result"]
    presentation = Presentation(io.BytesIO(client.get(job["result"]["download_url"]).content))
    assert any("Вариант обучения · внешний курс" in shape.text
               for slide in presentation.slides for shape in slide.shapes if shape.has_text_frame)
    assert any("netology.ru/programs/data-viz" in slide.notes_slide.notes_text_frame.text
               for slide in presentation.slides)

    for invalid in (
        {"catalog_id": "invented-course"},
        {"catalog_id": "edutoria"},
        {"catalog_id": "sber-payroll"},
        {"basis_fact_ids": ["unknown-fact"]},
        {"basis_fact_ids": [run["facts"][0]["fact_id"]]},
    ):
        original = proposal.copy()
        proposal.update(invalid)
        response = client.post(f"{base}/partnership-matches", json=params)
        assert response.status_code == 502
        assert response.json()["detail"] == "invalid_partnership_response"
        proposal.update(original)

    def add_project(research, _):
        profile = next(fact for fact in research.facts if fact.fact_id == "education-fact")
        value = "Вуз внедряет электронные реестры для расчётов с сотрудниками."
        research.facts.append(profile.model_copy(update={
            "fact_id": "payroll-fact", "field": FactField.PROJECT_DESCRIPTION,
            "subject": EntitySubject(entity_kind="project", entity_id="payments", entity_name="Расчёты"),
            "value": value, "evidence": [profile.evidence[0].model_copy(update={"quote": value})],
        }))

    updated = repository.mutate(run["run_id"], run["revision"], add_project)
    params["expected_revision"] = updated.revision
    proposal.update(catalog_id="sber-payroll", basis_fact_ids=["payroll-fact"],
                    rationale="Вуз внедряет электронные реестры выплат.",
                    proposal="Обсудить интеграцию зарплатного проекта с реестрами вуза.",
                    discussion_question="Как бухгалтерия передаёт реестры выплат сейчас?")
    response = client.post(f"{base}/partnership-matches", json=params)
    assert response.status_code == 200, response.text
    assert response.json()[0]["product_name"] == "Зарплатный проект Сбера"
    proposal["basis_fact_ids"] = ["education-fact"]
    assert client.post(f"{base}/partnership-matches", json=params).status_code == 502


def test_card_chat_reads_current_accepted_facts_and_isolates_history(api_client, monkeypatch):
    client, repository, _ = api_client
    monkeypatch.setenv("JWT_SECRET_KEY", "test-chat-key-used-only-for-this-test")
    headers = {"Authorization": f"Bearer {create_access_token('employee-1')[0]}"}
    other_user = {"Authorization": f"Bearer {create_access_token('employee-2')[0]}"}
    card, run = collected(client)
    other_card = repository.create_card("Другой вуз", "", "https://other.example")
    path = f"/api/v1/ai/cards/{card['id']}/chat"
    calls = []
    invalid_reply = False

    class ChatProvider(DemoProvider):
        async def chat(self, messages, **options):
            calls.append(messages.copy())
            question = next(message["content"] for message in reversed(messages) if message["role"] == "user")
            follow_up = question == "А как с ним связаться?"
            if messages[-1]["role"] == "user":
                if follow_up:
                    assert "Иванов" in messages[-2]["content"]
                message = Messages(role="assistant", functions_state_id="state-id", function_call={
                    "name": "read_card",
                    "arguments": {"section": "contacts", "person": "Иванов"},
                })
                return Choices(index=0, finish_reason="function_call", message=message)

            assert messages[-2]["functions_state_id"] == "state-id"
            assert options["function_call"] == "none"
            facts = json.loads(messages[-1]["content"])["facts"]
            position = next(fact for fact in facts if fact["field"] == "appointment.position")
            text = "В карточке нет подтверждённого контакта ректора." if follow_up else "Ректор — Иванов Иван Иванович."
            if follow_up:
                assert not any(fact["field"].startswith("contact.") for fact in facts)
            return Choices(index=0, finish_reason="stop", message=Messages(role="assistant", content=json.dumps({
                "text": text, "fact_ids": ["invented"] if invalid_reply else [position["id"]],
            })))

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(ChatProvider())
    assert client.get(path).status_code == 401
    assert client.post(path, json={"message": "Кто руководит вузом?"}).status_code == 401
    response = client.post(path, headers=headers, json={"message": "Кто руководит вузом?"})
    assert response.status_code == 200, response.text
    assert not response.json()["answer"]["sources"]
    assert "нет принятой записи о руководителе" in response.json()["answer"]["text"]
    assert not calls

    reviewed = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run))
    assert reviewed.status_code == 200, reviewed.text
    response = client.post(path, headers=headers, json={"message": "Кто руководит вузом"})
    assert response.status_code == 200, response.text
    assert "Иванов Иван Иванович — Ректор" in response.json()["answer"]["text"]
    assert response.json()["answer"]["sources"][0]["url"].startswith("https://")
    assert not calls
    response = client.post(path, headers=headers, json={"message": "А как с ним связаться?"})
    assert response.status_code == 200, response.text
    assert response.json()["answer"]["sources"][0]["url"].startswith("https://")
    assert len(calls) == 2
    assert len(client.get(path, headers=headers).json()) == 3
    assert client.get(path, headers=other_user).json() == []
    assert client.get(f"/api/v1/ai/cards/{other_card['id']}/chat", headers=headers).json() == []
    assert len(DemoRepository(repository.path).chat_snapshot(card["id"], "employee-1", 20)[2]) == 3
    invalid_reply = True
    response = client.post(path, headers=headers, json={"message": "Какие рабочие контакты есть?"})
    assert response.status_code == 502
    assert response.json()["detail"] == "invalid_chat_reply"
    assert len(client.get(path, headers=headers).json()) == 3


def test_refresh_is_idempotent_and_sources_are_not_recollected(api_client):
    client, repository, calls = api_client
    card, run = collected(client)
    previous_count = len(calls)
    again = client.post(f"/api/v1/ai/cards/{card['id']}/refresh", json={"version": 1, "request_key": "collect-one"})
    assert again.status_code == 202
    assert again.json()["result"]["run_id"] == run["run_id"]
    assert len(calls) == previous_count


def test_chat_edit_requires_owner_confirmation_and_current_revision(api_client, monkeypatch):
    client, repository, _ = api_client
    monkeypatch.setenv("JWT_SECRET_KEY", "test-chat-key-used-only-for-this-test")
    headers = {"Authorization": f"Bearer {create_access_token('editor')[0]}"}
    other = {"Authorization": f"Bearer {create_access_token('other')[0]}"}
    card, run = collected(client)
    review = client.post(f"/api/v1/ai/runs/{run['run_id']}/reviews", json=review_payload(run))
    assert review.status_code == 200, review.text
    run = review.json()

    class EditProvider(DemoProvider):
        async def chat(self, messages, **options):
            return Choices(index=0, finish_reason="function_call", message=Messages(
                role="assistant", function_call={
                    "name": "propose_card_edit",
                    "arguments": {"field": "appointment.position", "owner": "Иванов",
                                  "new_value": "Исполняющий обязанности ректора"},
                }))

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(EditProvider())
    path = f"/api/v1/ai/cards/{card['id']}/chat"
    question = {"message": "Замени должность Иванова на Исполняющий обязанности ректора"}
    invalid = client.post(path, headers=headers, json={"message": "Исправь должность Иванова"})
    assert invalid.json()["detail"] == "chat_edit_value_required"
    first = client.post(path, headers=headers, json=question)
    assert first.status_code == 200, first.text
    edit = first.json()["answer"]["edit"]
    assert edit["before"] == "Ректор" and edit["status"] == "pending"
    assert repository.get(run["run_id"]).revision == run["revision"]
    second = client.post(path, headers=headers, json=question).json()
    confirm = f"{path}/{first.json()['id']}/confirm"
    assert client.post(confirm, headers=other).status_code == 404
    assert client.post(confirm).status_code == 401
    assert client.post(confirm, headers=headers).status_code == 200
    assert client.post(f"{path}/{second['id']}/confirm", headers=headers).status_code == 409
    assert client.post(confirm, headers=headers).status_code == 409

    detail = client.get(f"/api/v1/ai/cards/{card['id']}").json()
    positions = [fact for fact in detail["approved_facts"] if fact["field"] == "appointment.position"]
    assert [fact["value"] for fact in positions] == [edit["after"]]
    assert positions[0]["origin"] == "manual_edit" and positions[0]["evidence"] == []
    audit = repository.reviews(run["run_id"])[-1]
    assert audit["decision"] == "manual_edit" and audit["actor"] == "editor"
    assert json.loads(audit["before_json"])["value"] == edit["before"]
    assert json.loads(audit["after_json"])["value"] == edit["after"]
    assert client.get(path, headers=headers).json()[0]["answer"]["edit"]["status"] == "applied"


def test_general_chat_keeps_its_own_context_and_starts_collection(api_client, monkeypatch):
    client, repository, calls = api_client
    monkeypatch.setenv("JWT_SECRET_KEY", "test-chat-key-used-only-for-this-test")
    headers = {"Authorization": f"Bearer {create_access_token('employee')[0]}"}
    other = {"Authorization": f"Bearer {create_access_token('other')[0]}"}
    card = repository.create_card("Первый вуз", "Иркутск", "https://first.example")
    question = "Какие вузы есть в Иркутске?"

    class GeneralProvider(DemoProvider):
        async def chat(self, messages, **options):
            assert {tool["name"] for tool in options["functions"]} == {"find_cards", "collect_card"}
            current = messages[-1]["content"]
            if current == question:
                call = {"name": "find_cards", "arguments": {"query": "Иркутск"}}
            elif current.startswith("Создай"):
                assert messages[1]["content"] == question
                call = {"name": "collect_card", "arguments": {
                    "name": "Второй вуз", "website": "https://second.example",
                }}
            else:
                assert messages[1]["content"] == question
                assert "Первый вуз (Иркутск)" in messages[2]["content"]
                return Choices(index=0, finish_reason="stop", message=Messages(
                    role="assistant", content="Откройте карточку нужного вуза.",
                ))
            return Choices(index=0, finish_reason="function_call",
                           message=Messages(role="assistant", function_call=call))

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(GeneralProvider())
    path = "/api/v1/ai/chat/general"
    assert client.get(path).status_code == 401
    assert client.post(path, json={"message": question}).status_code == 401
    response = client.post(path, headers=headers, json={"message": question})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["answer"]["cards"]] == [card["id"]]
    follow_up = client.post(path, headers=headers, json={"message": "А кто им руководит?"})
    assert follow_up.json()["answer"]["text"] == "Откройте карточку нужного вуза."
    response = client.post(path, headers=headers, json={"message": "Создай Второй вуз https://second.example"})
    assert response.status_code == 200, response.text
    new_card, = response.json()["answer"]["cards"]
    assert repository.card_job(new_card["id"])["status"] == "completed"
    assert calls == ["https://second.example"]
    assert len(client.get(path, headers=headers).json()) == 3
    assert client.get(path, headers=other).json() == []
    assert client.get(f"/api/v1/ai/cards/{card['id']}/chat", headers=headers).json() == []


def test_chat_collects_new_card_and_reuses_it_on_retry(api_client, monkeypatch):
    client, repository, calls = api_client
    monkeypatch.setenv("JWT_SECRET_KEY", "test-chat-key-used-only-for-this-test")
    headers = {"Authorization": f"Bearer {create_access_token('employee')[0]}"}
    arguments = {"name": "Учебный университет", "website": "https://university.example"}

    class IntakeProvider(DemoProvider):
        async def chat(self, messages, **options):
            assert any(tool["name"] == "collect_card" for tool in options["functions"])
            return Choices(index=0, finish_reason="function_call", message=Messages(
                role="assistant", function_call={"name": "collect_card", "arguments": arguments},
            ))

    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(IntakeProvider())
    path = "/api/v1/ai/chat/intake"
    payload = {"message": "Собери карточку Учебный университет: https://university.example", "request_key": "one"}
    assert client.post(path, json=payload).status_code == 401
    assert "message" in client.post(path, json={"message": "Собери Учебный университет"}, headers=headers).json()
    assert repository.cards() == []
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    collection = response.json()["collection"]
    card_id, job_id = collection["card"]["id"], collection["job"]["job_id"]
    job = client.get(f"/api/v1/ai/jobs/{job_id}").json()
    assert job["status"] == "completed" and job["identity"]["public_site_only"]
    detail = client.get(f"/api/v1/ai/cards/{card_id}").json()
    assert detail["run"]["facts"] and not detail["approved_facts"]
    retry = client.post(f"/api/v1/ai/cards/{card_id}/chat", json=payload, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["collection"]["job"]["job_id"] == job_id
    assert len(repository.cards()) == 1 and len(calls) == 1

    repository.update_job(job_id, status="running")
    active = client.post(path, json={**payload, "request_key": "while-running"}, headers=headers)
    assert active.json()["collection"]["job"]["job_id"] == job_id
    assert len(calls) == 1
    repository.update_job(job_id, status="completed")

    arguments["website"] = "https://invented.example"
    invalid = client.post(path, json=payload, headers=headers)
    assert invalid.status_code == 422 and invalid.json()["detail"] == "chat_collection_url_required"
    arguments["website"] = "http://127.0.0.1"
    invalid = client.post(path, headers=headers, json={"message": "Собери Учебный университет http://127.0.0.1"})
    assert invalid.status_code == 422 and invalid.json()["detail"] == "public_website_required"
    assert len(repository.cards()) == 1 and len(calls) == 1


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
    assert client.put(f"/api/v1/ai/materials/{result['material_id']}/report").status_code == 200
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
    assert client.put(f"/api/v1/ai/materials/{result['material_id']}/report").status_code == 200
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
