"""Fictional context example; review is SIMULATED and never written to CRM."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.llm.client import FixtureProvider
from app.services.llm.context_models import ContextExtractionRequest
from app.services.llm.models import GenerationRequest
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.render import render_markdown

EXAMPLES = Path(__file__).parent


def example_request():
    blocks = [
        "Университет Северный: профиль — инженерное образование и прикладные исследования.",
        "Проект «Лаборатория робототехники». Университет Северный планирует подать заявку "
        "в программу «Приоритет-2030». Статус проекта: подготовка заявки.",
        "Карьерный форум «Старт». Университет Северный организует мероприятие. "
        "Дата события: 12 ноября 2026 года. Статус: анонс.",
    ]
    fragments, start = [], 0
    for index, block in enumerate(blocks, 1):
        fragments.append({"fragment_id": f"f{index}", "start": start, "end": start + len(block),
                          "entity_scope_id": f"article-{index}"})
        start += len(block) + 2
    return ContextExtractionRequest.model_validate({
        "schema_version": "0.3",
        "institution": {"id": "context-demo", "name": "Университет Северный", "card_version": 1},
        "documents": [{"source_id": "fictional-context", "url": "https://university.example/news",
                       "title": "Вымышленный пример профиля, проекта и события",
                       "text": "\n\n".join(blocks), "retrieved_at": "2026-10-31T12:00:00+05:00",
                       "published_at": "2026-10-30T12:00:00+05:00", "fragments": fragments}],
    })


async def run(output_dir: Path, prepare_only: bool = False):
    output_dir.mkdir(parents=True, exist_ok=True)
    request = example_request()
    (output_dir / "request.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    if prepare_only:
        print(f"Fictional request saved: {output_dir / 'request.json'}")
        return
    responses = json.loads((EXAMPLES / "extract_context_responses.json").read_text(encoding="utf-8"))
    pipeline = AssistantPipeline(FixtureProvider(responses))
    result = await pipeline.extract_facts(request)
    (output_dir / "facts.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    if not result.complete or not result.facts:
        raise ValueError("Offline context example failed validation")
    print("OFFLINE FIXTURE: fictional sources; GigaChat is not called.")
    print("SIMULATED review in this example only; original facts remain pending; no CRM writes.")
    reviewed = [fact.model_copy(deep=True) for fact in result.facts]
    now = datetime.now(timezone.utc)
    for fact in reviewed:
        fact.verification_status = "verified"
        fact.reviewed_by = "demo-simulated-review"
        fact.reviewed_at = fact.last_checked_at = now
        fact.freshness_status = "within_window"
    generation = GenerationRequest(schema_version="0.3", institution=request.institution, facts=reviewed)
    (output_dir / "reviewed_request.json").write_text(generation.model_dump_json(indent=2), encoding="utf-8")
    draft = await pipeline.generate_materials(generation)
    (output_dir / "draft.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "brief.md").write_text(render_markdown(draft), encoding="utf-8")
    print(f"Draft and source registry saved: {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline context demo; fictional facts and simulated review")
    parser.add_argument("--output-dir", type=Path, default=Path("output/context-demo"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.output_dir, args.prepare_only))
