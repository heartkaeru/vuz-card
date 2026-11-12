"""Three fixed customer-inspired slides; full source values remain in notes/brief."""

import re
import textwrap

from .llm.context_render import LABELS as CONTEXT_LABELS, context_value


def render_compact(draft, facts, head, *, page, put, notes, citations, sources, labels,
                   portrait=None, partnership_matches=(), add_portrait=None):
    def lines(value, width=49):
        return [line for part in value.splitlines() for line in textwrap.wrap(part, width=width, break_long_words=True)]

    def column(slide, entries, x, *, y=2.35, budget=18, width=5.7):
        omitted = []
        for text, ids, limit in entries:
            wrapped = lines(text + (" " + citations(ids) if ids else ""))
            height = len(wrapped) * .285 + .08
            if len(wrapped) > min(limit, budget) or y + height > 5.98:
                omitted.extend(ids)
                continue
            put(slide, "\n".join(wrapped), x, y, width, height, 17)
            y += height + .08
            budget -= len(wrapped) + 1
        return omitted

    def footer(slide, ids, *, omitted=0):
        # Sources stay clickable without adding a fourth slide. Full URL, quote,
        # PDF page, fact ID and omitted facts are preserved in speaker notes.
        selected_urls = list(dict.fromkeys(e.url for f in facts if f.fact_id in ids for e in f.evidence))
        label_for = {e.url: label for label, e in sources.values()}
        for index, url in enumerate(selected_urls[:3]):
            from urllib.parse import urlsplit
            frame = put(slide, f"[{label_for[url]}] {urlsplit(url).netloc}", .7 + index * 3.98, 6.50, 3.8, .22, 10, muted=True)
            frame.paragraphs[0].runs[0].hyperlink.address = url
        more = " · дополнительные ссылки — в заметках" if len(selected_urls) > 3 else ""
        message = "Полные сведения и цитаты — в справке и заметках" + more
        if omitted:
            message = "Показаны ключевые сведения; полные значения и остальные факты — в справке и заметках"
        put(slide, message, .7, 6.17, 11.9, .25, 11, muted=True)
        notes(slide, ids)
        if draft.run.provider == "fixture":
            put(slide, "Учебный пример · вымышленные данные", .7, 5.86, 11.9, .22, 10, muted=True)

    slide = page("Руководитель учреждения")
    if head:
        if portrait and add_portrait:
            add_portrait(slide, portrait)
        (name, _), items = head
        put(slide, name, .7, 1.58, 11.9, .6, 25, bold=True)
        primary = []
        for field in ("appointment.position", "appointment.status", "contact.phone", "contact.email",
                      "person.degree", "person.academic_title", "person.education", "appointment.since"):
            values = list(dict.fromkeys(f.value for f in items if f.field.value == field))
            for value in values[:1]:
                ids = [f.fact_id for f in items if f.field.value == field and f.value == value]
                shown = value
                # Extract only an explicit, literal qualification. Dissertation titles
                # stay complete in notes; no degree/title is inferred from publications.
                if field == "person.degree":
                    match = re.search(r"(?:доктор(?:а)?|кандидат(?:а)?)\s+[а-яё-]+(?:\s+[а-яё-]+)?\s+наук", value, re.I)
                    if match:
                        shown = match.group()
                elif field == "person.academic_title":
                    match = re.search(r"\b(?:профессор|доцент)\b", value, re.I)
                    if match:
                        shown = match.group()
                primary.append((labels.get(field, field) + ": " + shown, ids, 4))
        careers = [f for f in items if f.field.value == "person.career"]
        def year(fact):
            years = re.findall(r"\b(?:19|20)\d{2}\b", fact.value[:100])
            return max(map(int, years)) if years else 0
        careers.sort(key=year, reverse=True)
        secondary = [("Карьерный этап: " + f.value, [f.fact_id], 5) for f in careers[:2]]
        secondary.extend((labels[f.field.value] + ": " + f.value, [f.fact_id], 4) for f in items
                         if f.field.value in {"appointment.responsibilities", "person.interests"})
        if not secondary:
            secondary = [("Карьерная история и зоны ответственности не подтверждены в выбранных материалах.", [], 4)]
        if portrait:
            omitted = column(slide, primary, .7, width=4.05) + column(slide, secondary, 4.95, width=4.05)
        else:
            omitted = column(slide, primary, .7) + column(slide, secondary, 6.9)
        footer(slide, [f.fact_id for f in items], omitted=len(omitted) + max(0, len(careers) - 2))
    else:
        put(slide, "Руководитель головного учреждения не подтверждён в выбранных материалах", .7, 1.8, 11.9, 1.1, 23)
        footer(slide, [])

    slide = page("Справочная информация об учреждении")
    name_lines = lines(draft.institution.name, 93)
    put(slide, "\n".join(name_lines[:2]), .7, 1.55, 11.9, .73, 20, bold=True)
    institutional = [f for f in facts if f.field.value.startswith("institution.")]
    left, right = [], []
    for field in ("institution.students", "institution.type", "institution.location", "institution.founded",
                  "institution.education_areas", "institution.education_levels", "institution.structure", "institution.profile"):
        group = [f for f in institutional if f.field.value == field]
        target = left if field in {"institution.students", "institution.type", "institution.location", "institution.founded"} else right
        for f in group[:2]:
            # Never truncate a population/year/unit qualifier to fit a number.
            target.append((CONTEXT_LABELS[field] + ": " + context_value(f), [f.fact_id], 7 if f.metric_context else 5))
    if not left:
        left = [("Численность и основные показатели не подтверждены в выбранных материалах.", [], 4)]
    if not right:
        right = [("Направления подготовки и структура не подтверждены в выбранных материалах.", [], 4)]
    omitted = column(slide, left, .7) + column(slide, right, 6.9)
    footer(slide, [f.fact_id for f in institutional], omitted=len(omitted))

    slide = page("Повестка встречи")
    goal = draft.meeting.goal
    if len(lines(goal, 93)) > 2:
        goal = "Полная цель встречи приведена в справке"
    put(slide, "Цель: " + goal, .7, 1.55, 11.9, .74, 19)
    contextual = [f for f in facts if f.field.value.startswith(("project.", "event."))]
    entries = []
    for f in contextual:
        if f.field.value.endswith(".name"):
            group = [item for item in contextual if item.subject.entity_id == f.subject.entity_id
                     and item.subject.entity_kind == f.subject.entity_kind]
            qualifiers = [CONTEXT_LABELS[item.field.value] + ": " + item.value for item in group
                          if item.field.value in {"project.status", "project.role", "project.period", "event.status", "event.date"}]
            prefix = "Проект: " if f.field.value.startswith("project.") else "Мероприятие: "
            entries.append((prefix + f.value + ("\n" + "; ".join(qualifiers) if qualifiers else ""),
                            [item.fact_id for item in group], 7))
    if not entries:
        entries = [("Проекты и мероприятия не подтверждены в выбранных материалах.", [], 4)]
    questions = [("Вопрос: " + q.question, q.basis_fact_ids, 4) for q in draft.questions[:3]]
    if not questions:
        questions = [("Какие образовательные задачи приоритетны для учреждения?", [], 3),
                     ("В каких проектах и мероприятиях возможно участие учебного центра?", [], 3)]
    omitted = column(slide, entries, .7) + column(slide, questions, 6.9)
    # Include all facts (also other staff) so compact selection loses no provenance.
    footer(slide, [f.fact_id for f in facts], omitted=len(omitted))

    if partnership_matches:
        for index, match in enumerate(partnership_matches):
            slide = page("Предложения и партнерство со Сбером" + (f" · {index + 1}" if index else ""))
            put(slide, match["product_name"], .7, 1.55, 11.9, .62, 21, bold=True)
            put(slide, "Предложение: " + match["proposal"], .7, 2.35, 11.9, 1.05, 17)
            put(slide, "Основание: " + match["rationale"], .7, 3.65, 11.9, 1.05, 17)
            put(slide, "Вопрос для встречи: " + match["discussion_question"], .7, 4.95, 11.9, .85, 17)
            link = put(slide, "Источник в каталоге Сбера", .7, 6.05, 11.9, .3, 12, muted=True)
            link.paragraphs[0].runs[0].hyperlink.address = match["catalog_url"]
            footer(slide, match["basis_fact_ids"])
            slide.notes_slide.notes_text_frame.text += f"\n\nЗапись каталога: {match['catalog_url']}"
    else:
        slide = page("Предложения и партнерство со Сбером")
        put(slide, "Данные матчинга с каталогом продуктов Сбера не переданы. "
            "Предложения появятся после подключения каталога и сопоставления с принятыми фактами.",
            .7, 1.75, 11.9, 1.6, 17)
