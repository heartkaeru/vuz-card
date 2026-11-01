"""Prepare a new-page first-pass evaluation before viewing any LLM responses.

Only semantic block boundaries are supplied to the model, not expected names/roles.
References are read from official fio/post markup and must be reviewed before the run.
No prompts or extraction rules are tuned by this script.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from app.services.llm.client import GigaChatSettings
from app.services.llm.fragment_models import FragmentExtractionRequest
from .pilot import save

FROZEN_FILES = [
    "app/services/llm/pipeline.py", "app/services/llm/fragment_extraction.py",
    "app/services/llm/fragment_models.py", "app/services/llm/models.py",
    "app/services/llm/evidence.py", "app/services/llm/names.py",
    "app/services/llm/client.py", "app/services/llm/prompts/extract_fragments.txt",
]


def prepare(root: Path):
    if (root / "evaluation-protocol.json").exists():
        raise ValueError("Protocol already frozen; use a new folder rather than overwriting the first pass")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    gold = {"scope": "ФИО и текущие должности в выбранных блоках новых страниц; контакты вне оценки",
            "fields": ["person.name", "appointment.position"],
            "not_holdout": False, "cases": {}}
    unavailable, selected_count = [], 0
    for case in manifest["cases"]:
        folder = root / case["id"]
        raw_path = folder / "source-0.html"
        metadata_path = folder / "source-0-metadata.json"
        if not raw_path.exists() or not metadata_path.exists():
            unavailable.append(case["id"])
            continue
        if (folder / "extraction.json").exists():
            raise ValueError("Model results already exist; do not retrospectively prepare gold")
        raw = raw_path.read_bytes()
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.select("script, style, noscript, template"):
            node.decompose()
        text = soup.body.get_text("\n", strip=True)
        # Fixed headquarters scope before the branch section; first five staff in BGU,
        # rector and three deputies in BrSU. No names are passed as metadata/hints.
        limit = {"bgu": 5, "brstu": 4}[case["id"]]
        people = soup.select('[itemprop="fio"]')[:limit]
        if len(people) != limit:
            raise ValueError("Expected source scope changed; inspect it before calling the model")
        fragments, expected = [], []
        for index, person in enumerate(people, 1):
            block = person.find_parent("li") if case["id"] == "bgu" else person.find_parent("tr")
            block_text = block.get_text("\n", strip=True)
            name = person.get_text(" ", strip=True)
            position = block.select_one('[itemprop="post"]').get_text(" ", strip=True)
            start = text.index(block_text)
            fragments.append({"fragment_id": f"leader-{index}", "start": start,
                              "end": start + len(block_text), "contact_hints": []})
            expected.extend([
                {"person_name": name, "field": "person.name", "values": [name]},
                {"person_name": name, "field": "appointment.position", "values": [position]},
            ])
            print(case["id"], name, "|", position)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        request = FragmentExtractionRequest.model_validate({
            "schema_version": "0.2", "institution": case["institution"],
            "fields": ["person.name", "appointment.position"], "project_structured_contacts": False,
            "documents": [{"source_id": f"{case['id']}-0", "url": metadata["final_url"],
                "title": case["sources"][0]["title"], "retrieved_at": metadata["retrieved_at"],
                "text": text, "fragments": fragments}],
        })
        save(folder / "request.json", request.model_dump(mode="json"))
        (folder / "source.txt").write_text(text, encoding="utf-8")
        gold["cases"][case["id"]] = expected
        selected_count += len(expected)
    save(root / "gold.json", gold)
    load_dotenv(".env", override=False)
    settings = GigaChatSettings.from_env()
    save(root / "evaluation-protocol.json", {
        "frozen_at": datetime.now(timezone.utc).isoformat(), "model": settings.model,
        "first_pass_only": True, "gold_prepared_before_model_run": True,
        "headquarters_only": True, "selected_facts": selected_count,
        "unavailable_before_model_run": unavailable, "selected_organizations": len(manifest["cases"]),
        "evaluated_organizations": len(gold["cases"]),
        "limitations": ["small sample", "manually chosen semantic blocks", "one source reviewer",
                        "no contact metrics", "not an end-to-end discovery/chat test"],
        "sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in FROZEN_FILES},
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, nargs="?", default=Path("output/new-organizations"))
    prepare(parser.parse_args().folder)
