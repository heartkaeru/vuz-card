import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parents[1]


def test_interpret_cli_does_not_require_database_or_credentials(tmp_path):
    target = tmp_path / "request.json"
    result = subprocess.run([
        sys.executable, "-m", "app.services.llm", "interpret",
        "--input", "examples/llm/user_request.json",
        "--responses", "examples/llm/user_request_responses.json", "--output", str(target),
    ], cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    interpreted = json.loads(target.read_text(encoding="utf-8"))
    assert interpreted["next_step"] == "resolve_institution"
    assert interpreted["request"]["outputs"] == ["brief", "presentation"]


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


def test_context_demo_and_cli_cover_candidates_and_draft(tmp_path):
    result = subprocess.run([
        sys.executable, "-m", "examples.llm.context_demo", "--output-dir", str(tmp_path),
    ], cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "SIMULATED review" in result.stdout
    facts = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    draft = json.loads((tmp_path / "draft.json").read_text(encoding="utf-8"))
    assert facts["schema_version"] == "0.3"
    assert all(f["verification_status"] == "pending" for f in facts["facts"])
    assert draft["schema_version"] == "0.3" and draft["status"] == "draft"
    markdown = (tmp_path / "brief.md").read_text(encoding="utf-8")
    assert "Приоритет-2030" in markdown and "12 ноября 2026" in markdown
    target = tmp_path / "cli-facts.json"
    result = subprocess.run([
        sys.executable, "-m", "app.services.llm", "extract",
        "--input", str(tmp_path / "request.json"),
        "--responses", "examples/llm/extract_context_responses.json", "--output", str(target),
    ], cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(target.read_text(encoding="utf-8"))["facts"]) == 9
