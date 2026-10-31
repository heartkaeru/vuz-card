import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parents[1]


def test_offline_cli_runs_without_credentials(tmp_path):
    target = tmp_path / "facts.json"
    result = subprocess.run([
        sys.executable, "-m", "app.services.llm", "extract",
        "--input", "examples/llm/extract_request.json",
        "--responses", "examples/llm/extract_responses.json", "--output", str(target),
    ], cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "OFFLINE FIXTURE" in result.stderr
    facts = json.loads(target.read_text(encoding="utf-8"))
    assert facts["run"]["provider"] == "fixture"
    assert all(f["verification_status"] == "pending" for f in facts["facts"])


def test_demo_covers_extract_and_draft(tmp_path):
    result = subprocess.run([
        sys.executable, "-m", "examples.llm.demo", "--output-dir", str(tmp_path),
    ], cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "SIMULATED review" in result.stdout
    facts = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    draft = json.loads((tmp_path / "draft.json").read_text(encoding="utf-8"))
    assert draft["status"] == "draft"
    assert all(f["verification_status"] == "pending" for f in facts["facts"])
    assert set(draft["sources"]) == {f["fact_id"] for f in facts["facts"]}
    markdown = (tmp_path / "brief.md").read_text(encoding="utf-8")
    assert "Демонстрация" in markdown
    assert "https://university.example/leadership" in markdown
    assert "[S1]" in markdown
