"""Site-specific adapters for the saved three-page development corpus.

Ownership hints are parser configuration for this pilot, NOT independent human approval.
No network calls and no edits to the old corpus. The source HTML remains unchanged.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app.services.llm.fragment_models import FragmentExtractionRequest
from .pilot import save


def prepare(old_root: Path, root: Path):
    manifest = json.loads((old_root / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = "0.2"
    manifest["parser_scope_note"] = (
        "Ручная конфигурация структуры известных страниц. Контакты профиля трактуются "
        "как функциональные контакты должности ректора, не эксклюзивные личные контакты человека. "
        "Владельцы контактов АПЭТ определяются заголовками столбцов и rowspan."
    )
    contact_gold = {"scope": "Владельцы и значения контактов в настроенных фрагментах трёх страниц; не независимый контроль",
                    "fields": ["contact.email", "contact.phone"], "cases": {}}
    for case in manifest["cases"]:
        case_id = case["id"]
        raw = (old_root / case_id / "source-0.html").read_bytes()
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.select("script, style, noscript, template, .su-tooltip-content"):
            node.decompose()
        table = None
        if case_id == "apet":
            table = soup.select("table")[2]
            for row in table.select("tr")[4:]:
                row.decompose()
        text = soup.body.get_text("\n", strip=True)
        fragments = []
        expected_contacts = []

        def add(fragment_id, fragment_text, hints=None):
            start = text.index(fragment_text)
            item = {"fragment_id": fragment_id, "start": start, "end": start + len(fragment_text),
                    "contact_hints": hints or []}
            fragments.append(item)
            return item

        def hint(field, value, label, kind="office", context=None):
            owner = {"kind": kind, "label": label}
            item = {"field": field, "value": value, "owner": owner,
                    "context_fragment_ids": context or []}
            if not any(e["field"] == field and e["values"] == [value] and e["owner"] == owner
                       for e in expected_contacts):
                expected_contacts.append({"field": field, "values": [value], "owner": owner})
            return item

        if case_id in {"isu", "irnitu"}:
            selector = "div.textUnit_Text:has(h3)" if case_id == "isu" else "main.page-content"
            profile = soup.select_one(selector).get_text("\n", strip=True)
            profile_hints = [hint("contact.email", email, "Ректор")
                             for email in re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", profile)]
            if case_id == "isu":
                phone = re.search(r"тел\.\s*([^,\n]+)", profile).group(1).strip()
                profile_hints.append(hint("contact.phone", phone, "Ректор"))
            else:
                start = text.index("Приемная ректора:")
                end = text.index("\nФакс", start)
                reception = text[start:end]
                phone = reception.split("\n", 1)[1].strip()
                add("reception", reception, [hint("contact.phone", phone, "Приемная ректора")])
                card_phone = profile.splitlines()[-1]
                # The page's reception block grounds the role of the same telephone.
                profile_hints.append({"field": "contact.phone", "value": card_phone,
                    "owner": {"kind": "office", "label": "Приемная ректора"},
                    "context_fragment_ids": ["reception"]})
            add("profile", profile, profile_hints)
        else:
            rows = table.select("tr")
            add("table-header", rows[0].get_text("\n", strip=True))
            # First row's department cell spans four staff rows in the source HTML.
            department_cell = rows[1].find_all("td", recursive=False)[0]
            assert int(department_cell.get("rowspan", "1")) >= 3
            department = department_cell.get_text(" ", strip=True)
            for index, row in enumerate(rows[1:], 1):
                cells = row.find_all(["td", "th"], recursive=False)
                offset = 1 if index == 1 else 0
                email = cells[2 + offset].get_text(" ", strip=True)
                phone = cells[4 + offset].get_text(" ", strip=True)
                context = ["table-header"] + (["row-1"] if index != 1 else [])
                owner_hints = [hint("contact.email", email, department, "department", context),
                               hint("contact.phone", phone, department, "department", context)]
                add(f"row-{index}", row.get_text("\n", strip=True), owner_hints)
        metadata = json.loads((old_root / case_id / "source-0-metadata.json").read_text(encoding="utf-8"))
        request = FragmentExtractionRequest.model_validate({
            "schema_version": "0.2", "institution": case["institution"], "fields": manifest["fields"],
            "project_structured_contacts": True,
            "documents": [{"source_id": f"{case_id}-0", "url": metadata["final_url"],
                "title": case["sources"][0]["title"], "text": text,
                "retrieved_at": metadata["retrieved_at"], "published_at": None, "fragments": fragments}],
        })
        folder = root / case_id
        save(folder / "request.json", request.model_dump(mode="json"))
        save(folder / "source-metadata.json", {"original_html_sha256": hashlib.sha256(raw).hexdigest(),
             "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "contact_scope_note": manifest["parser_scope_note"], "retrieved_at": metadata["retrieved_at"]})
        (folder / "source.txt").write_text(text, encoding="utf-8")
        contact_gold["cases"][case_id] = expected_contacts
        print(case_id, len(fragments), "fragments,", len(expected_contacts), "unique scoped contacts")
    save(root / "manifest.json", manifest)
    (root / "gold.json").write_bytes((old_root / "gold.json").read_bytes())
    save(root / "contact-gold.json", contact_gold)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-dir", type=Path, default=Path("output/pilot"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/pilot-v2"))
    args = parser.parse_args()
    if args.pilot_dir.resolve() == args.output_dir.resolve():
        parser.error("Output must differ from the original corpus directory")
    prepare(args.pilot_dir, args.output_dir)
