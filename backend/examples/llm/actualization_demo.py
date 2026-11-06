"""Explicit fictional before/after rehearsal in its own DB; never calls GigaChat."""

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn

from app.api.v1.ai import get_collector, get_pipeline, get_repository
from app.main import app
from app.services.llm.models import Institution
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch
from database.demo1_store import DemoRepository
from parser.cleaner import HTMLCleaner
from parser.models import UniversityData, UniversityInfo
from .demo1_server import OfflineDemoProvider

NAME = "Иванов Иван Иванович"
OLD_PHONE, NEW_PHONE = "+7 (495) 111-22-33", "+7 (495) 111-22-44"
CARD_NAME = "Учебный пример обновления (вымышленные данные)"


def fictional_card(phone=NEW_PHONE):
    document = HTMLCleaner().to_document(
        f"<main><h1>{NAME}</h1><p>{NAME} — Ректор. Рабочий телефон: {phone}</p></main>",
        url="https://university.example/leadership", section="leadership")
    return UniversityData(university=UniversityInfo(name=CARD_NAME, url="https://university.example"), documents=[document])


class ActualizationProvider(OfflineDemoProvider):
    model = "actualization-offline-replay"

    async def complete(self, system, payload, schema):
        if "fragments" not in payload:
            return await super().complete(system, payload, schema)
        fragment = next(f for f in payload["fragments"] if OLD_PHONE in json.dumps(f, ensure_ascii=False)
                        or NEW_PHONE in json.dumps(f, ensure_ascii=False))
        phone = NEW_PHONE if NEW_PHONE in json.dumps(fragment, ensure_ascii=False) else OLD_PHONE
        return json.dumps({"facts": [
            {"subject": {"person_name": NAME}, "field": "appointment.position", "value": "Ректор",
             "fragment_ids": [fragment["fragment_id"]]},
            {"subject": {"person_name": NAME}, "field": "contact.phone", "value": phone,
             "contact_owner": {"kind": "person", "label": NAME}, "fragment_ids": [fragment["fragment_id"]]}
        ], "gaps": []}, ensure_ascii=False)


async def seed(repository):
    card = repository.create_card(CARD_NAME, "Вымышленный город", "https://university.example")
    if card["latest_run"] and any(run.baseline_facts for run in repository.history(card["id"])):
        return card
    workflow = LLMWorkflow(repository, AssistantPipeline(ActualizationProvider()))
    institution = Institution(id=card["id"], name=card["name"], card_version=card["version"])
    old, _ = workflow.create(institution, fictional_card(OLD_PHONE), "fictional-before-v2", modes=("0.2",))
    old = await workflow.extract(old.run_id)
    # Synthetic test decisions, never attributed to the user or a real institution.
    workflow.review(old.run_id, ReviewBatch(expected_revision=old.revision, decisions=[
        {"fact_id": f.fact_id, "decision": "accept", "source_checked": True,
         "reason": "Synthetic fixture baseline for offline UI rehearsal; not real university data"}
        for f in old.facts]), actor="offline-synthetic-reviewer")
    current, _ = workflow.create(institution, fictional_card(), "fictional-after", modes=("0.2",))
    await workflow.extract(current.run_id)
    return repository.card(card["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("output/actualization-offline/registry.sqlite3"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    repository = DemoRepository(args.db)
    repository.fail_interrupted_jobs()
    asyncio.run(seed(repository))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(ActualizationProvider())
    app.dependency_overrides[get_collector] = lambda: lambda *args, **kwargs: fictional_card()
    print("OFFLINE FICTIONAL ACTUALIZATION: isolated DB, no network sources or GigaChat.")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
