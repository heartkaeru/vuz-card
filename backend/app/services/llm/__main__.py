"""Run from backend: python -m app.services.llm extract --help."""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .client import ConfigurationError, FixtureProvider, GigaChatProvider, GigaChatSettings, LLMError
from .models import ExtractionRequest, GenerationRequest
from .pipeline import AssistantPipeline, DraftValidationError
from .render import render_markdown


async def run(args):
    # Explicit path, no implicit search through parent directories. Existing env wins.
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if args.responses:
        provider = FixtureProvider.from_file(args.responses)
        print("OFFLINE FIXTURE: replaying saved responses; GigaChat is not called.", file=sys.stderr)
    else:
        provider = GigaChatProvider(GigaChatSettings.from_env())
    pipeline = AssistantPipeline(provider)
    raw = args.input.read_text(encoding="utf-8-sig")
    if args.operation == "extract":
        result = await pipeline.extract_facts(ExtractionRequest.model_validate_json(raw))
        code = 0 if result.complete else 2
    else:
        result = await pipeline.generate_materials(GenerationRequest.model_validate_json(raw))
        code = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
    print(f"Saved: {args.output}")
    return code


def main():
    parser = argparse.ArgumentParser(description="Extract leadership facts or draft meeting materials")
    parser.add_argument("operation", choices=["extract", "draft"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="Optional explicit .env path")
    parser.add_argument("--responses", type=Path, help="Offline JSON response list, for fixtures only")
    parser.add_argument("--markdown", type=Path, help="Readable draft output, only for the draft operation")
    args = parser.parse_args()
    if args.markdown and args.operation != "draft":
        parser.error("--markdown is only available for draft")
    try:
        code = asyncio.run(run(args))
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        code = 1
    except DraftValidationError as exc:
        print(f"Draft rejected: {exc}", file=sys.stderr)
        code = 2
    except LLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        code = 2
    except (ValidationError, ValueError, OSError):
        # ValidationError can include full source text; do not print it to shared logs.
        print("Invalid input/fixture JSON or file access error. Check paths and schema.", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
