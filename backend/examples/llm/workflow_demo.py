"""Offline parser/LLM/registry integration on fictional HTML. No CRM identities invented."""

import argparse
import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

from database.llm_store import LLMRepository
from parser.cleaner import HTMLCleaner
from parser.models import UniversityData, UniversityInfo

from app.services.llm.client import FixtureProvider
from app.services.llm.models import Institution
from app.services.llm.pipeline import AssistantPipeline
from app.services.llm.render import render_markdown
from app.services.llm.workflow import LLMWorkflow
from app.services.llm.workflow_models import ReviewBatch
from .context_demo import example_request


def example_card():
    cleaner = HTMLCleaner()
    leadership = cleaner.to_document("<main><p>Иванов Иван Иванович — Ректор.</p></main>",
        url="https://university.example/leadership", section="leadership")
    leadership.category = "leadership"
    blocks = example_request().documents[0].text.split("\n\n")
    context = cleaner.to_document("<main>" + "".join(f"<p>{b}</p>" for b in blocks) + "</main>",
        url="https://university.example/projects", section="projects")
    context.category = "projects"
    return UniversityData(university=UniversityInfo(name="Университет Северный", url="https://university.example/"),
                          documents=[leadership, context])


def extraction_responses(card):
    leadership = {"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
        "field": "appointment.position", "value": "Ректор",
        "fragment_ids": [card.documents[0].fragments[0].fragment_id]}], "gaps": []}
    context = json.loads((Path(__file__).parent / "extract_context_responses.json").read_text(encoding="utf-8"))[0]
    refs = {f"f{i}": f.fragment_id for i, f in enumerate(card.documents[1].fragments, 1)}
    for fact in context["facts"]:
        fact["subject"]["entity_fragment_id"] = refs[fact["subject"]["entity_fragment_id"]]
        fact["fragment_ids"] = [refs[ref] for ref in fact["fragment_ids"]]
    return [leadership, context]


def draft_response(run):
    groups = {}
    for i, fact in enumerate(run.facts, 1):
        groups.setdefault(getattr(fact.subject, "entity_id", "leadership"), []).append((i, fact))
    sections, slides = [], []
    for group in groups.values():
        statement = {"text": "; ".join(f.value for _, f in group), "fact_ids": [f"fact_{i}" for i, _ in group]}
        sections.append({"title": "Сведения", "statements": [statement]})
        slides.append({"title": "Сведения", "theses": [copy.deepcopy(statement)]})
    return {"brief_sections": sections, "slides": slides, "questions": []}


async def run(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = (output_dir / f"fictional_demo_{uuid4().hex[:8]}.sqlite3").resolve()
    repository = LLMRepository(database_path)
    card = example_card()
    extraction = extraction_responses(card)
    workflow = LLMWorkflow(repository, AssistantPipeline(FixtureProvider(extraction)))
    research, _ = workflow.create(Institution(id="draft:fictional-workflow-demo", name=card.university.name,
        card_version=1), card, "fictional-demo")
    research = await workflow.extract(research.run_id)
    (output_dir / "parser_llm_card.json").write_text(card.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "pending_run.json").write_text(research.model_dump_json(indent=2), encoding="utf-8")
    if research.status != "needs_review":
        raise ValueError("Fictional fixture failed validation")
    # Only this fictional demonstration simulates review, visibly and under its own actor.
    research = workflow.review(research.run_id, ReviewBatch(expected_revision=research.revision, decisions=[
        {"fact_id": f.fact_id, "decision": "accept", "source_checked": True,
         "reason": "SIMULATED review of fictional demo fixture"} for f in research.facts
    ]), actor="demo-simulated-review")
    generation = draft_response(research)
    workflow.pipeline = AssistantPipeline(FixtureProvider([generation]))
    material_id, draft = await workflow.generate(research.run_id, research.revision)
    (output_dir / "reviewed_run.json").write_text(research.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "draft.json").write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "brief.md").write_text(render_markdown(draft), encoding="utf-8")
    for filename, value in (("extraction_responses.json", extraction), ("draft_responses.json", [generation]),
        ("summary.json", {"database": str(database_path), "run_id": research.run_id,
                          "revision": research.revision, "facts": len(research.facts), "material_id": material_id,
                          "provider": "fixture", "review": "SIMULATED; fictional sources only"})):
        (output_dir / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print("OFFLINE FIXTURE: fictional HTML, real cleaner/adapter and SQLite; no web crawl or GigaChat calls.")
    print("SIMULATED review: demo-simulated-review. No legal entity or confirmed CRM institution created.")
    print(f"Saved {len(research.facts)} candidates, review audit and draft: {output_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline LLM integration demonstration; fictional sources only")
    parser.add_argument("--output-dir", type=Path, default=Path("output/workflow-demo"))
    asyncio.run(run(parser.parse_args().output_dir))
