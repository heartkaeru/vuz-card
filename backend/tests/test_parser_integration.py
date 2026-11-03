from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.llm.context_extraction import batches
from app.services.llm.context_models import ContextExtractionRequest
from app.services.llm.fragment_models import FragmentExtractionRequest
from parser.cleaner import HTMLCleaner
from parser.config import Settings
from parser.llm_adapter import to_llm_request, to_university_detail
from parser.main import UniversityParser
from parser.models import Leadership, UniversityData, UniversityInfo
from parser.store import CrawlCache


def card_for(document):
    return UniversityData(
        university=UniversityInfo(name="Тестовый вуз", url="https://example.ru/"),
        documents=[document],
    )


@pytest.mark.parametrize("fixture", ["sveden_struct.html", "news_list.html", None])
def test_cleaned_html_passes_actual_llm_contracts(fixture):
    html = ((Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
            if fixture else "<main><p>Email: office@example.ru</p></main>")
    doc = HTMLCleaner().to_document(html, url="https://example.ru/contacts", section="contacts")
    for version, model in [("0.2", FragmentExtractionRequest), ("0.3", ContextExtractionRequest)]:
        request = model.model_validate(to_llm_request(card_for(doc), institution_id="test", schema_version=version))
        assert request.documents[0].text == doc.text


def test_context_does_not_merge_distinct_articles_or_make_a_page_one_large_batch():
    html = "<main>" + "".join(
        f"<article><h2>Проект {i}</h2><p>{str(i) + ' описание ' * 100}</p></article>"
        for i in range(20)
    ) + "</main>"
    doc = HTMLCleaner().to_document(html, url="https://example.ru/projects")
    request = ContextExtractionRequest.model_validate(
        to_llm_request(card_for(doc), institution_id="test", schema_version="0.3")
    )
    fragments = request.documents[0].fragments
    assert all(f.entity_scope_id is None for f in fragments)
    assert len(list(batches(request.documents[0], 12000))) > 1
    assert all(sum(f.end - f.start for f in group) <= 12000
               for group in batches(request.documents[0], 12000))


def test_explicit_semantic_scope_survives_adapter():
    doc = HTMLCleaner().to_document(
        "<main><h2>Проект Альфа</h2><p>Проект действует.</p></main>",
        url="https://example.ru/project",
    )
    for fragment in doc.fragments:
        fragment.entity_scope_id = "project-alpha"
    request = ContextExtractionRequest.model_validate(
        to_llm_request(card_for(doc), institution_id="test", schema_version="0.3")
    )
    assert {f.entity_scope_id for f in request.documents[0].fragments} == {"project-alpha"}


@pytest.mark.parametrize("position, expected", [("Проректор", None), ("Директор колледжа", None),
                                                ("И. о. ректора", "Иванов Иван Иванович")])
def test_rector_projection_requires_a_rector_position(position, expected):
    card = UniversityData(
        university=UniversityInfo(name="Тест", url="https://example.ru/"),
        leadership=[Leadership(full_name="Иванов Иван Иванович", position=position)],
    )
    assert to_university_detail(card, institution_id="test")["rector_fio"] == expected


@pytest.mark.parametrize("age_days", [0, 100])
def test_cached_html_keeps_original_retrieval_date(tmp_path, age_days):
    from datetime import timedelta
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days, minutes=1)
    url = "https://example.ru/leadership"
    cache = CrawlCache(tmp_path / "pages.json", 24)
    cache.set_page(url, "<main><h1>Ректор</h1><p>Иванов Иван Иванович</p></main>", url, "text/html")
    cache._data[url]["retrieved_at"] = stamp.isoformat()
    client = Mock(last_error="timeout")
    client.get.return_value = None
    parser = UniversityParser(Settings(use_sqlite=False, enable_playwright=False), client=client)
    parser._cache = cache
    try:
        doc, _ = parser._get_page(url, "leadership")
        assert doc.retrieved_at == stamp
        result = FragmentExtractionRequest.model_validate(to_llm_request(card_for(doc), institution_id="test"))
        assert result.documents[0].retrieved_at == stamp
        assert bool(parser._warnings) == bool(age_days)
    finally:
        parser.close()
