"""Synthetic offline demonstration. Review below is simulated ONLY for this example."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.llm.client import FixtureProvider
from app.services.llm.models import ExtractionRequest, GenerationRequest
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.render import render_markdown


async def demo(output_dir: Path):
    folder = Path(__file__).parent
    request = ExtractionRequest.model_validate_json((folder / "extract_request.json").read_text(encoding="utf-8"))
    pipeline = AssistantPipeline(FixtureProvider.from_file(folder / "extract_responses.json"))
    extraction = await pipeline.extract_facts(request)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "facts.json").write_text(extraction.model_dump_json(indent=2), encoding="utf-8")

    # Do NOT move this review simulation to application/CRM code. Only synthetic data.
    now = datetime.now(timezone.utc)
    reviewed = [fact.model_copy(update={
        "verification_status": "verified", "reviewed_by": "SIMULATED-DEMO-REVIEW",
        "reviewed_at": now, "last_checked_at": now, "freshness_status": "within_window",
    }) for fact in extraction.facts]
    generation = GenerationRequest(institution=extraction.institution, facts=reviewed)
    statement = {
        "text": "Иванов Иван Иванович — исполняющий обязанности ректора.",
        "fact_ids": [fact.fact_id for fact in reviewed],
    }
    replay = {"brief_sections": [{"title": "Руководитель", "statements": [statement]}],
              "slides": [{"title": "Руководство", "theses": [statement]}], "questions": []}
    pipeline = AssistantPipeline(FixtureProvider([replay]))
    draft = await pipeline.generate_materials(generation)
    (output_dir / "draft.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "brief.md").write_text(render_markdown(draft), encoding="utf-8")
    print("OFFLINE DEMO: synthetic data, replayed model responses, SIMULATED review.")
    print(f"Facts, draft JSON and Markdown saved in {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output/demo"))
    asyncio.run(demo(parser.parse_args().output_dir))
