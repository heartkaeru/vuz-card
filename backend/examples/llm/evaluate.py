"""Evaluate a saved pilot against an explicitly scoped, pre-annotated gold file."""

import argparse
import json
from pathlib import Path

from app.services.llm.names import folded
from app.services.llm.pipeline import normalize
from app.services.llm.fragment_extraction import value_key


def canonical(value):
    return folded(normalize(value)).rstrip(".")


def score(facts, expected, fields):
    matched, wrong, duplicates = set(), [], []
    for fact in facts:
        if fact["field"] not in fields:
            continue
        def same_owner(item):
            if "owner" in item:
                owner = fact.get("contact_owner") or {}
                return (owner.get("kind") == item["owner"]["kind"]
                        and (not fact["subject"].get("person_name") if owner.get("kind") != "person"
                             else canonical(fact["subject"].get("person_name") or "") == canonical(owner.get("label", "")))
                        and canonical(owner.get("label", "")) == canonical(item["owner"]["label"]))
            return canonical(fact["subject"].get("person_name") or "") == canonical(item["person_name"])

        matches = [index for index, item in enumerate(expected)
                   if same_owner(item) and fact["field"] == item["field"]
                   and value_key(fact["field"], fact["value"]) in {
                       value_key(item["field"], v) for v in item["values"]}]
        available = [index for index in matches if index not in matched]
        if available:
            matched.add(available[0])
        elif matches:
            duplicates.append(fact["fact_id"])
        else:
            wrong.append(fact["fact_id"])
    tp, fp, fn = len(matched), len(wrong) + len(duplicates), len(expected) - len(matched)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
            "f1": f1, "wrong_fact_ids": wrong, "duplicate_fact_ids": duplicates,
            "missing": [item for index, item in enumerate(expected) if index not in matched]}


def evaluate(folder: Path, gold_name: str = "gold.json"):
    gold = json.loads((folder / gold_name).read_text(encoding="utf-8"))
    report = {"scope": gold["scope"], "not_holdout": gold.get("not_holdout", True), "cases": {}}
    for case_id, expected in gold["cases"].items():
        result = json.loads((folder / case_id / "extraction.json").read_text(encoding="utf-8"))
        facts = result["facts"]
        report["cases"][case_id] = {
            "run": result["run"], "all_facts": len(facts), "errors": result["errors"],
            "status_counts": {status: sum(f["verification_status"] == status for f in facts)
                              for status in ("pending", "conflict", "rejected")},
            "raw": score(facts, expected, gold["fields"]),
            "automatically_valid": score([f for f in facts if f["validation_status"] == "passed"],
                                          expected, gold["fields"]),
        }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--gold", default="gold.json", help="Scoped gold file relative to the corpus folder")
    parser.add_argument("--output", default="metrics.json", help="Report filename relative to the corpus folder")
    args = parser.parse_args()
    report = evaluate(args.folder, args.gold)
    (args.folder / args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case_id, item in report["cases"].items():
        print(case_id, item["status_counts"], "raw TP/FP/FN:",
              *(item["raw"][key] for key in ("tp", "fp", "fn")),
              "valid TP/FP/FN:", *(item["automatically_valid"][key] for key in ("tp", "fp", "fn")))
