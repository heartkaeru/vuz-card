from datetime import datetime, timezone

from pptx import Presentation

from app.services.llm.models import DraftResult
from app.services.meeting_card_pptx import build_card_presentation
from test_dossier import metric_extract, METRIC


def test_compact_never_truncates_metric_qualifiers_and_keeps_omitted_fact_in_notes(tmp_path):
    fact = metric_extract(METRIC).facts[0]
    fact.verification_status = "verified"
    fact.metric_context.scope = "Длинное описание состава контингента " * 40
    draft = DraftResult(schema_version="0.3", presentation_variant="compact", institution={
        "id": fact.institution_id, "name": "Учебный университет", "card_version": 1},
        run={"provider": "fixture", "model": "layout-test", "prompt_version": "test", "created_at": datetime.now(timezone.utc)},
        meeting={"goal": "Проверка макета"},
        brief_sections=[{"title": "Сведения", "statements": [{"text": fact.value, "fact_ids": [fact.fact_id]}]}],
        slides=[{"title": "Сведения", "theses": [{"text": fact.value, "fact_ids": [fact.fact_id]}]}], questions=[],
        warnings=[], sources={fact.fact_id: fact.evidence})
    path = build_card_presentation(draft, tmp_path / "compact.pptx", [fact])
    deck = Presentation(path)
    assert len(deck.slides) == 4
    assert "Предложения и партнерство со Сбером" in " ".join(
        shape.text for shape in deck.slides[3].shapes if shape.has_text_frame)
    content = "\n".join(s.text for slide in deck.slides for s in slide.shapes if s.has_text_frame)
    assert "1200" not in content  # A count without its full qualifier must never appear.
    notes = "\n".join(slide.notes_slide.notes_text_frame.text for slide in deck.slides)
    assert fact.value in notes and fact.fact_id in notes and fact.metric_context.scope.strip() in notes
    assert "полные значения" in content.lower()
    assert all("Учебный пример" in " ".join(s.text for s in slide.shapes if s.has_text_frame) for slide in deck.slides)
