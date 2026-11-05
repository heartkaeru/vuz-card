"""A typed, explicitly unapproved preview; never writes review decisions/material records."""

from datetime import datetime, timezone

from .llm.models import DraftResult, GroundedStatement, MeetingContext, RunInfo
from .meeting_card_pptx import build_card_presentation


def build_dossier_preview(institution, facts, output_path, *, meeting=None, presentation_variant="detailed"):
    facts = [fact.model_copy(deep=True) for fact in facts if fact.validation_status == "passed"
             and not fact.issues and fact.verification_status in {"pending", "verified"}]
    if not facts:
        raise ValueError("no_previewable_facts")
    statement = GroundedStatement(text="Кандидаты для содержательной проверки сотрудником.",
                                  fact_ids=[fact.fact_id for fact in facts])
    draft = DraftResult(schema_version="0.3", institution=institution, presentation_variant=presentation_variant,
        meeting=meeting or MeetingContext(),
        run=RunInfo(provider="candidate-preview", model="typed-layout-without-llm",
                    prompt_version="candidate-preview-v1", created_at=datetime.now(timezone.utc)),
        brief_sections=[{"title": "Предпросмотр кандидатов", "statements": [statement]}],
        slides=[{"title": "Предпросмотр кандидатов", "theses": [statement]}], questions=[],
        warnings=["Предпросмотр: кандидаты не приняты сотрудником. Автоматическая проверка цитат не подтверждает смысл, актуальность и полноту.",
                  "Проверяйте период и состав показателей, текущие должности и принадлежность контактов."],
        sources={fact.fact_id: fact.evidence for fact in facts})
    return build_card_presentation(draft, output_path, facts, candidate_preview=True)
