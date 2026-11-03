"""Local integration CLI. Review is explicit; fixture mode never approves real data."""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from database.llm_store import LLMRepository
from parser.models import UniversityData

from .client import ConfigurationError, FixtureProvider, GigaChatProvider, GigaChatSettings, LLMError
from .models import Institution, MeetingContext
from .pipeline import AssistantPipeline, DraftValidationError
from .render import render_markdown
from .workflow import LLMWorkflow
from .workflow_models import ReviewBatch, WorkflowError


def provider(args):
    if args.responses:
        print("OFFLINE FIXTURE: GigaChat is not called.", file=sys.stderr)
        return FixtureProvider.from_file(args.responses)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    return GigaChatProvider(GigaChatSettings.from_env())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


async def run(args):
    repository = LLMRepository(args.db)
    if args.operation == "extract":
        raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict) or "documents" not in raw:
            raise WorkflowError("lossless_llm_card_snapshot_required")
        card = UniversityData.model_validate(raw)
        institution = Institution(id=args.institution_id, name=args.name or card.university.name,
                                  card_version=args.card_version)
        workflow = LLMWorkflow(repository)
        result, created = workflow.create(institution, card, args.request_key, modes=args.mode or ("0.2", "0.3"))
        if result.status == "queued":
            workflow.pipeline = AssistantPipeline(provider(args))
            result = await workflow.extract(result.run_id)
        print(f"run_id={result.run_id} status={result.status} revision={result.revision} facts={len(result.facts)}")
        if args.output:
            save(args.output, result.model_dump_json(indent=2))
        return 2 if result.status in {"partial", "failed"} else 0
    if args.operation == "show":
        result = repository.get(args.run_id)
        save(args.output, result.model_dump_json(indent=2))
        if args.review_template:
            save(args.review_template, json.dumps({
                "expected_revision": result.revision,
                "decisions": [{"fact_id": fact.fact_id, "decision": None,
                               "source_checked": False, "reason": ""}
                              for fact in result.facts if fact.verification_status in {"pending", "conflict"}],
            }, ensure_ascii=False, indent=2))
        print(f"Saved research: {args.output}; status={result.status} revision={result.revision}")
        return 0
    if args.operation == "review":
        batch = ReviewBatch.model_validate_json(args.decisions.read_text(encoding="utf-8-sig"))
        result = LLMWorkflow(repository).review(args.run_id, batch, actor=args.actor)
        print(f"Review recorded: run_id={result.run_id} revision={result.revision}")
        return 0
    workflow = LLMWorkflow(repository, AssistantPipeline(provider(args)))
    material_id, draft = await workflow.generate(args.run_id, args.revision, fact_ids=args.fact_id,
                                                meeting=MeetingContext(goal=args.goal, date=args.date))
    save(args.output_dir / f"{material_id}.json", draft.model_dump_json(indent=2))
    save(args.output_dir / f"{material_id}.md", render_markdown(draft))
    print(f"Draft saved: material_id={material_id}; fact_revision={args.revision}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Parser/LLM/SQLite integration; explicit local employee review")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[3] / "output/mvp/registry.sqlite3")
    sub = parser.add_subparsers(dest="operation", required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("--input", required=True, type=Path, help="Lossless parser *_llm_card_*.json")
    extract.add_argument("--institution-id", required=True, help="Registry ID, or explicit draft:<local-id>")
    extract.add_argument("--name")
    extract.add_argument("--card-version", type=int, default=1)
    extract.add_argument("--request-key", required=True)
    extract.add_argument("--mode", action="append", choices=["0.2", "0.3"])
    extract.add_argument("--output", type=Path)
    show = sub.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--output", type=Path, required=True)
    show.add_argument("--review-template", type=Path)
    review = sub.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("--decisions", type=Path, required=True)
    review.add_argument("--actor", required=True, help="Local reviewer identity; API must use authenticated user")
    draft = sub.add_parser("draft")
    draft.add_argument("run_id")
    draft.add_argument("--revision", type=int, required=True)
    draft.add_argument("--fact-id", action="append")
    draft.add_argument("--goal", default="Цель встречи не задана")
    draft.add_argument("--date")
    draft.add_argument("--output-dir", type=Path, required=True)
    for command in (extract, draft):
        command.add_argument("--env-file", type=Path)
        command.add_argument("--responses", type=Path, help="Explicit offline responses, for testing only")
    args = parser.parse_args()
    try:
        code = asyncio.run(run(args))
    except (WorkflowError, ConfigurationError, LLMError, DraftValidationError) as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        code = 2
    except (ValidationError, ValueError, OSError, sqlite3.Error):
        print("Invalid input or database/file access error; source and credential details omitted.", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
