"""Readable output; source registry is built by code, not invented by the LLM."""

from .models import DraftResult


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
