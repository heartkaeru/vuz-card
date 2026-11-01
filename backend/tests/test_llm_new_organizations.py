import json

import pytest

from app.services.llm.client import GigaChatSettings


def test_new_page_gold_has_explicit_scope_and_cannot_be_overwritten(tmp_path, monkeypatch):
    pytest.importorskip("bs4")
    from examples.llm import prepare_new_organizations as module

    names = ["Иванов Иван Иванович", "Петров Пётр Петрович", "Сидоров Иван Петрович",
             "Орлов Пётр Иванович", "Волков Иван Сергеевич"]
    cases = []
    for case, count in (("bgu", 5), ("brstu", 4)):
        folder = tmp_path / case
        folder.mkdir()
        blocks = []
        for name in names[:count]:
            tag = "li" if case == "bgu" else "tr"
            blocks.append(f'<{tag}><span itemprop="fio">{name}</span><span itemprop="post">Проректор</span></{tag}>')
        (folder / "source-0.html").write_text('<html><head><meta charset="utf-8"></head><body>' +
                                             "\n".join(blocks) + '</body></html>', encoding="utf-8")
        (folder / "source-0-metadata.json").write_text(json.dumps({
            "final_url": f"https://{case}.example/leadership", "retrieved_at": "2026-10-31T12:00:00Z"}), encoding="utf-8")
        cases.append({"id": case, "institution": {"id": case, "name": case, "card_version": 1},
                      "sources": [{"title": "Руководство"}]})
    (tmp_path / "manifest.json").write_text(json.dumps({"cases": cases}), encoding="utf-8")
    monkeypatch.setattr(module, "FROZEN_FILES", [])
    monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(GigaChatSettings, "from_env", classmethod(lambda cls:
                        GigaChatSettings(credentials="test", model="test", scope="test")))
    module.prepare(tmp_path)
    gold = json.loads((tmp_path / "gold.json").read_text(encoding="utf-8"))
    assert gold["fields"] == ["person.name", "appointment.position"]
    assert sum(map(len, gold["cases"].values())) == 18
    assert gold["not_holdout"] is False
    with pytest.raises(ValueError, match="Protocol already frozen"):
        module.prepare(tmp_path)
