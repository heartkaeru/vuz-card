"""Small live pilot on explicitly selected public pages, not a production crawler.

Keep the manifest and snapshots in ignored output/, never send the source workbook.
"""

import argparse
import asyncio
import hashlib
import json
import ssl
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.services.llm import AssistantPipeline
from app.services.llm.client import GigaChatProvider, GigaChatSettings, JSONProvider, LLMError, FixtureProvider
from app.services.llm.pipeline import DraftValidationError
from app.services.llm.models import ExtractionRequest, GenerationRequest
from app.services.llm.fragment_models import parse_extraction_request
from app.services.llm.render import render_markdown


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class RecordingProvider:
    """Save only model message content, never SDK headers, credentials or tokens."""

    def __init__(self, provider: JSONProvider, folder: Path, stage: str):
        self.provider, self.folder, self.stage = provider, folder, stage
        self.name, self.model = provider.name, provider.model
        self.index = 0

    async def complete(self, system, payload, schema):
        raw = await self.provider.complete(system, payload, schema)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{self.stage}-response-{self.index}.json").write_text(raw, encoding="utf-8")
        self.index += 1
        return raw


async def run(args):
    load_dotenv(args.env_file, override=False)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    root = args.manifest.parent
    if args.stage == "fetch":
        # Parsing dependency is optional and is not required by the production module.
        from bs4 import BeautifulSoup

        settings = GigaChatSettings.from_env()
        context = ssl.create_default_context(cafile=settings.ca_bundle_file)
        async with httpx.AsyncClient(verify=context, timeout=45, follow_redirects=True,
                                     headers={"User-Agent": "VuzCardPilot/0.1 (manual small sample)"}) as client:
            for case in manifest["cases"]:
                case_dir = root / case["id"]
                case_dir.mkdir(parents=True, exist_ok=True)
                documents = []
                for index, source in enumerate(case["sources"]):
                    raw_path = case_dir / f"source-{index}.html"
                    metadata_path = case_dir / f"source-{index}-metadata.json"
                    cached = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
                    if (raw_path.exists() and not args.refresh
                            and cached.get("requested_url") == source["url"]):
                        raw = raw_path.read_bytes()
                        final_url, retrieved_at = cached["final_url"], cached["retrieved_at"]
                    else:
                        try:
                            response = await client.get(source["url"])
                            response.raise_for_status()
                        except httpx.HTTPError as exc:
                            code = (f"http_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
                                    else type(exc).__name__)
                            save(case_dir / "fetch-error.json", {"url": source["url"], "code": code,
                                 "attempted_at": datetime.now(timezone.utc).isoformat()})
                            print(case["id"], "source unavailable:", code)
                            documents = []
                            break
                        raw = response.content
                        raw_path.write_bytes(raw)
                        final_url = str(response.url)
                        retrieved_at = datetime.now(timezone.utc).isoformat()
                    soup = BeautifulSoup(raw, "html.parser")
                    for node in soup.select(
                        "script, style, noscript, template" +
                        (", " + ", ".join(source["exclude_selectors"]) if source.get("exclude_selectors") else "")
                    ):
                        node.decompose()
                    selections = soup.select(source.get("selector", "body"))
                    selected_index = source.get("selector_index", 0)
                    if selected_index >= len(selections):
                        raise ValueError(f"Missing HTML selector for {case['id']}")
                    selected = selections[selected_index]
                    if source.get("table_row_limit"):
                        for row in selected.select("tr")[source["table_row_limit"]:]:
                            row.decompose()
                    text = selected.get_text("\n", strip=True)
                    start, end = 0, len(text)
                    if source.get("start"):
                        start = text.index(source["start"])
                    if source.get("end"):
                        end = text.index(source["end"], start + 1)
                    excerpt = text[start:end].strip()
                    (case_dir / f"source-{index}.txt").write_text(excerpt, encoding="utf-8")
                    documents.append({
                        "source_id": f"{case['id']}-{index}", "url": final_url,
                        "title": source["title"], "text": excerpt,
                        "retrieved_at": retrieved_at, "published_at": None,
                    })
                    save(case_dir / f"source-{index}-metadata.json", {
                        "requested_url": source["url"], "final_url": final_url,
                        "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "selection": source, "start": start, "end": end,
                        "text_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                        "retrieved_at": documents[-1]["retrieved_at"],
                    })
                if not documents:
                    continue
                request = ExtractionRequest.model_validate({"institution": case["institution"],
                                                             "fields": manifest.get("fields"),
                                                             "documents": documents})
                save(case_dir / "request.json", request.model_dump(mode="json"))
                print(case["id"], "downloaded", [len(d["text"]) for d in documents])
        return
    provider = GigaChatProvider(replace(GigaChatSettings.from_env(), max_output_tokens=8192,
                                       timeout=180, max_retries=0))
    # Sequential calls respect the Freemium single-stream limit.
    for case in manifest["cases"]:
        if args.case and args.case != case["id"]:
            continue
        folder = root / case["id"]
        request_path = folder / ("request.json" if args.stage == "extract" else "reviewed-request.json")
        if not request_path.exists():
            save(folder / f"{args.stage}-error.json", {"code": "source_snapshot_missing" if args.stage == "extract"
                                                       else "reviewed_request_missing"})
            print(case["id"], "skipped:", "source snapshot missing" if args.stage == "extract" else "reviewed request missing")
            continue
        if args.replay:
            saved = [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted(folder.glob(f"{args.stage}-response-*.json"))]
            replay = FixtureProvider(saved)
            replay.name, replay.model = "gigachat-recording", provider.model
            pipeline = AssistantPipeline(replay)
            print(case["id"], "reprocessing saved API response; no new API call")
        else:
            pipeline = AssistantPipeline(RecordingProvider(provider, folder, args.stage))
        if args.stage == "extract":
            request = parse_extraction_request(request_path.read_text(encoding="utf-8"))
            result = await pipeline.extract_facts(request)
            save(folder / "extraction.json", result.model_dump(mode="json"))
            counts = {state: sum(f.verification_status == state for f in result.facts)
                      for state in ("pending", "conflict", "rejected")}
            print(case["id"], counts, "errors", [error.code for error in result.errors])
        else:
            request = GenerationRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
            result = await pipeline.generate_materials(request)
            result.warnings.append(
                "Технический пилот: факты сверены ассистентом по сохранённым страницам; "
                "принятие сотрудником CRM имитируется и должно быть выполнено отдельно."
            )
            save(folder / "draft.json", result.model_dump(mode="json"))
            (folder / "brief.md").write_text(render_markdown(result), encoding="utf-8")
            print(case["id"], "draft generated", len(result.slides), "slides")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["fetch", "extract", "draft"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--case", help="Process only this case during extraction or drafting")
    parser.add_argument("--refresh", action="store_true", help="Re-download even when a snapshot exists")
    parser.add_argument("--replay", action="store_true", help="Reprocess existing real API responses without a new request")
    args = parser.parse_args()
    # Suppress SDK details in terminal logs; the extraction pipeline records safe codes.
    try:
        asyncio.run(run(args))
    except (LLMError, DraftValidationError) as exc:
        print("Pilot stopped:", str(exc))
        raise SystemExit(2) from None
    except Exception as exc:
        print("Pilot stopped:", type(exc).__name__, "(check local input and safe error codes)")
        raise SystemExit(2) from None
