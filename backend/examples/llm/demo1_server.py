"""Explicit OFFLINE UI rehearsal. Isolated fictional records; never the default server."""

import argparse
import json
from pathlib import Path

import uvicorn

from app.api.v1.ai import get_collector, get_pipeline, get_repository
from app.main import app
from app.services.llm.pipeline import AssistantPipeline
from database.demo1_store import DemoRepository
from parser.cleaner import HTMLCleaner
from .workflow_demo import example_card


DEMO_BIO = {
    "person.education": "Высшее инженерное образование",
    "person.career": "С 2020 года руководит учебным университетом",
    "appointment.responsibilities": "Развитие образовательных и научных программ",
}


def fictional_card():
    card = example_card()
    text = "Иванов Иван Иванович — Ректор. " + ". ".join(DEMO_BIO.values()) + "."
    document = HTMLCleaner().to_document(f"<main><p>{text}</p></main>",
        url="https://university.example/leadership", section="leadership")
    document.category = "leadership"
    card.documents = [document]
    return card


class OfflineDemoProvider:
    name = "fixture"
    model = "demo1-offline-replay"

    async def complete(self, system, payload, schema):
        if "fragments" in payload:
            values = {"appointment.position": "Ректор", **DEMO_BIO}
            return json.dumps({"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
                "field": field, "value": value,
                "fragment_ids": [payload["fragments"][0]["fragment_id"]]} for field, value in values.items()], "gaps": []})
        statement = {"text": "; ".join(f["value"] for f in payload["facts"]),
                     "fact_ids": [f["fact_id"] for f in payload["facts"]]}
        return json.dumps({"brief_sections": [{"title": "Руководство", "statements": [statement]}],
            "slides": [{"title": "Руководство и контакты", "theses": [statement]}], "questions": []})


def main():
    parser = argparse.ArgumentParser(description="OFFLINE fictional UI rehearsal; no GigaChat or live parsing")
    parser.add_argument("--db", type=Path, default=Path("output/demo1-offline/registry.sqlite3"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    repository = DemoRepository(args.db)
    repository.fail_interrupted_jobs()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_pipeline] = lambda: AssistantPipeline(OfflineDemoProvider())
    app.dependency_overrides[get_collector] = lambda: lambda website, name, leadership_url=None: fictional_card()
    print("OFFLINE FICTIONAL REHEARSAL: no network sources, no GigaChat; separate database.")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
