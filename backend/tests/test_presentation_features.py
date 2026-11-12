from datetime import datetime, timezone
from io import BytesIO

from PIL import Image
from pptx import Presentation
from pydantic import ValidationError
import pytest

from app.services.llm.models import DraftResult, Evidence, Fact, FactField, Subject
from app.services.meeting_card_pptx import build_card_presentation
from app.services.presentation_assets import download_official_portrait


def _portrait_png():
    stream = BytesIO()
    Image.new("RGB", (120, 180), "green").save(stream, format="PNG")
    return stream.getvalue()


def test_portrait_download_is_limited_to_evidence_host_and_supported_images(monkeypatch):
    import app.services.presentation_assets as assets

    content = _portrait_png()

    class Headers:
        def get_content_type(self):
            return "image/png"

        def get(self, key):
            return str(len(content)) if key == "Content-Length" else None

    class Response(BytesIO):
        headers = Headers()

    class Opener:
        def open(self, request, timeout):
            assert timeout == 5
            return Response(content)

    monkeypatch.setattr(assets, "build_opener", lambda handler: Opener())
    assert download_official_portrait("https://university.example/rector.png",
        {"university.example"}) == (content, (120, 180))
    assert download_official_portrait("https://elsewhere.example/rector.png",
        {"university.example"}) is None


def test_material_request_accepts_only_https_photo_and_catalog_urls():
    from app.api.v1.ai import MaterialRequest

    payload = MaterialRequest(expected_revision=1, request_key="material-1",
        rector_photo_url="https://university.example/rector.png",
        partnership_matches=[{"product_name": "Курс", "proposal": "Пилотный запуск",
            "rationale": "Есть профильная кафедра", "discussion_question": "Какие сроки подходят?",
            "catalog_url": "https://sber.example/catalog/course", "basis_fact_ids": ["fact-1"]}])
    assert payload.rector_photo_url is not None
    assert payload.partnership_matches[0].basis_fact_ids == ["fact-1"]

    with pytest.raises(ValidationError):
        MaterialRequest(expected_revision=1, request_key="material-2",
            rector_photo_url="http://university.example/rector.png")
    with pytest.raises(ValidationError):
        MaterialRequest(expected_revision=1, request_key="material-3",
            partnership_matches=[{"product_name": "Курс", "proposal": "Пилотный запуск",
                "rationale": "Есть профильная кафедра", "discussion_question": "Какие сроки подходят?",
                "catalog_url": "http://sber.example/catalog/course", "basis_fact_ids": ["fact-1"]}])


def _rector_fixture(variant):
    evidence = Evidence(source_id="official", quote="Иванов Иван Иванович — ректор.",
        url="https://university.example/leadership", retrieved_at=datetime.now(timezone.utc),
        published_at=None, content_sha256="a" * 64, chunk_id="c0001")
    subject = Subject(person_name="Иванов Иван Иванович")
    facts = [
        Fact(fact_id="name", institution_id="university", subject=subject, field=FactField.NAME,
             value="Иванов Иван Иванович", evidence=[evidence], validation_status="passed",
             verification_status="verified"),
        Fact(fact_id="position", institution_id="university", subject=subject, field=FactField.POSITION,
             value="Ректор", evidence=[evidence], validation_status="passed",
             verification_status="verified"),
    ]
    draft = DraftResult(schema_version="0.3", presentation_variant=variant,
        institution={"id": "university", "name": "Учебный университет", "card_version": 1},
        run={"provider": "fixture", "model": "test", "prompt_version": "test",
             "created_at": datetime.now(timezone.utc)},
        meeting={"goal": "Обсуждение сотрудничества"},
        brief_sections=[{"title": "Руководство", "statements": [
            {"text": "Иванов Иван Иванович — ректор.", "fact_ids": ["name", "position"]}]}],
        slides=[{"title": "Руководство", "theses": [
            {"text": "Иванов Иван Иванович — ректор.", "fact_ids": ["name", "position"]}]}],
        questions=[], warnings=[], sources={fact.fact_id: fact.evidence for fact in facts})
    match = {"product_name": "Образовательная программа", "proposal": "Обсудить пилотный курс.",
        "rationale": "Вуз готовит ИТ-специалистов.", "discussion_question": "Какие направления приоритетны?",
        "catalog_url": "https://sber.example/catalog/course", "basis_fact_ids": ["position"]}
    return draft, facts, match


@pytest.mark.parametrize("variant", ["compact", "detailed"])
def test_presentation_places_rectors_photo_and_renders_grounded_match(tmp_path, monkeypatch, variant):
    import app.services.meeting_card_pptx as renderer

    draft, facts, match = _rector_fixture(variant)
    monkeypatch.setattr(renderer, "download_official_portrait",
        lambda url, hosts: (_portrait_png(), (120, 180)))
    path = build_card_presentation(draft, tmp_path / f"{variant}.pptx", facts,
        rector_photo_url="https://university.example/images/rector.png", partnership_matches=[match])
    deck = Presentation(path)

    head_slide = next(slide for slide in deck.slides if any(
        shape.has_text_frame and "Руководитель учреждения" in shape.text for shape in slide.shapes))
    assert sum(shape.shape_type == 13 for shape in head_slide.shapes) == 3
    partnership = [slide for slide in deck.slides if any(
        shape.has_text_frame and "Предложения и партнерство со Сбером" in shape.text
        for shape in slide.shapes)]
    assert len(partnership) == 1
    text = "\n".join(shape.text for shape in partnership[0].shapes if shape.has_text_frame)
    assert "Образовательная программа" in text
    assert "Какие направления приоритетны?" in text
    assert "position" in partnership[0].notes_slide.notes_text_frame.text
    assert all(shape.top + shape.height <= deck.slide_height for slide in deck.slides for shape in slide.shapes)
