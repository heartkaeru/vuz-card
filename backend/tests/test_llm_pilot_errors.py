import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services.llm.client import GigaChatSettings
from examples.llm.pilot import run


def args_for(tmp_path, stage):
    manifest = {"cases": [{"id": "new", "institution": {"id": "draft-new", "name": "Новый вуз", "card_version": 1},
                           "sources": [{"url": "https://university.example/leadership", "title": "Руководство"}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return SimpleNamespace(stage=stage, manifest=path, env_file=tmp_path / "missing.env",
                           refresh=False, replay=False, case=None)


def configure(monkeypatch):
    settings = GigaChatSettings(credentials="test", model="test", scope="test")
    monkeypatch.setattr(GigaChatSettings, "from_env", classmethod(lambda cls: settings))


def test_fetch_failure_is_saved_and_not_treated_as_empty_success(tmp_path, monkeypatch):
    pytest.importorskip("bs4")  # Optional pilot dependency, not needed for the LLM runtime.
    configure(monkeypatch)

    class UnavailableSource:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return httpx.Response(504, request=httpx.Request("GET", url))

    monkeypatch.setattr("examples.llm.pilot.httpx.AsyncClient", UnavailableSource)
    asyncio.run(run(args_for(tmp_path, "fetch")))
    error = json.loads((tmp_path / "new" / "fetch-error.json").read_text(encoding="utf-8"))
    assert error["code"] == "http_504"
    assert not (tmp_path / "new" / "request.json").exists()


def test_missing_snapshot_is_not_sent_to_llm_or_replaced_with_facts(tmp_path, monkeypatch):
    configure(monkeypatch)

    async def forbidden_request(*args, **kwargs):
        raise AssertionError("Do not call an LLM without a source snapshot")

    monkeypatch.setattr("examples.llm.pilot.GigaChatProvider.complete", forbidden_request)
    asyncio.run(run(args_for(tmp_path, "extract")))
    error = json.loads((tmp_path / "new" / "extract-error.json").read_text(encoding="utf-8"))
    assert error["code"] == "source_snapshot_missing"
    assert not (tmp_path / "new" / "extraction.json").exists()
