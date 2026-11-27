"""Readable output; source registry is built by code, not invented by the LLM."""

from datetime import datetime, timezone

from .contacts import contact_statement
from .context_render import context_statement, context_title
from .leadership_render import leadership_statement
from .models import DraftResult, GenerationRequest, GroundedStatement, RunInfo


def render_accepted_facts_draft(request: GenerationRequest) -> DraftResult:
    """Build a template draft from facts already validated by the drafting pipeline."""
    sections = {}
    for fact in request.facts:
        context = context_title([fact])
        contact = contact_statement([fact])
        title = context or ("Контакты" if contact else "Руководство и биография")
        text = context_statement([fact]) or contact or leadership_statement([fact]) or fact.value
        sections.setdefault(title, []).append(GroundedStatement(text=text, fact_ids=[fact.fact_id]))

    brief, slides, remaining_words = [], [], 800
    for title, statements in sections.items():
        summary = []
        for statement in statements:
            cost = len(statement.text.split())
            if cost <= remaining_words:
                summary.append(statement)
                remaining_words -= cost
        if summary:
            brief.append({"title": title, "statements": summary})
        slides.extend({"title": title, "theses": statements[start:start + 5]}
                      for start in range(0, len(statements), 5))

    if not brief:
        brief.append({"title": "Справка к встрече", "statements": [{
            "text": "Полные сведения приведены в тезисах и источниках.", "fact_ids": [request.facts[0].fact_id],
        }]})
    return DraftResult(
        schema_version=request.schema_version, institution=request.institution, meeting=request.meeting,
        run=RunInfo(provider="accepted-facts", model="template", prompt_version="accepted-facts-v1",
                    created_at=datetime.now(timezone.utc)),
        brief_sections=brief, slides=slides, questions=[],
        sources={fact.fact_id: fact.evidence for fact in request.facts},
        warnings=["Черновик собран по шаблону из принятых фактов после проверки чисел в ответе модели. "
                  "Перед встречей проверьте материал."],
    )


def render_markdown(draft: DraftResult, facts=None) -> str:
    refs = {fid: f"S{index}" for index, fid in enumerate(draft.sources, 1)}

    def citations(ids):
        return " ".join(f"[{refs[fid]}]" for fid in dict.fromkeys(ids))

    lines = [f"# Справка: {draft.institution.name}", "",
             "**Черновик — требуется проверка сотрудником.**", "",
             f"Версия карточки: {draft.institution.card_version}",
             f"Подготовлено: {draft.run.created_at.isoformat()}",
             f"Цель: {draft.meeting.goal}", ""]
    if draft.run.provider == "fixture":
        lines += ["**Демонстрация на сохранённых ответах. Реальных запросов к GigaChat нет.**", ""]
    if draft.meeting.date:
        lines += [f"Дата встречи: {draft.meeting.date}", ""]
    for section in draft.brief_sections:
        if not section.statements:
            continue
        lines += [f"## {section.title}", ""]
        lines += [f"- {s.text} {citations(s.fact_ids)}" for s in section.statements]
        lines.append("")
    lines += ["## Тезисы презентации", ""]
    for slide in draft.slides:
        lines += [f"### {slide.title}", ""]
        lines += [f"- {s.text} {citations(s.fact_ids)}" for s in slide.theses]
        lines.append("")
    if draft.questions:
        lines += ["## Вопросы для обсуждения", ""]
        lines += [f"- {q.question} {citations(q.basis_fact_ids)}" for q in draft.questions]
        lines.append("")
    lines += ["## Источники", ""]
    for fid, evidence_list in draft.sources.items():
        lines += [f"### {refs[fid]} — {fid}", ""]
        if not evidence_list:
            lines += ["Подтверждено сотрудником в чате.", ""]
        for evidence in evidence_list:
            lines += [f"- URL: {evidence.url}",
                      f"- Получено: {evidence.retrieved_at.isoformat()}",
                      f"- Опубликовано: {evidence.published_at.isoformat() if evidence.published_at else 'дата неизвестна'}",
                      f"- Фрагмент: {evidence.chunk_id}",
                      f"- Цитата: {evidence.quote}", ""]
            if evidence.page_number:
                lines += [f"- Страница PDF: {evidence.page_number}", f"- SHA256 файла: {evidence.file_sha256}", ""]
            if evidence.fragment_id is not None:
                lines += [f"- Границы цитаты: {evidence.start_offset}–{evidence.end_offset}; "
                          f"фрагмент {evidence.fragment_id}", ""]
    if facts:
        from .context_render import context_value
        lines += ["## Полные значения выбранных фактов", ""]
        for fact in facts:
            owner = fact.subject.person_name or getattr(fact.subject, "entity_name", None) or (fact.contact_owner.label if fact.contact_owner else "Учреждение")
            department = getattr(fact.subject, "department", None)
            if department:
                owner += " · " + department
            lines += [f"- **{owner}** · {fact.field.value}: {context_value(fact)} {citations([fact.fact_id])}", ""]
    lines += ["## Ограничения проверки", ""]
    lines += [f"- {warning}" for warning in draft.warnings]
    return "\n".join(lines) + "\n"
