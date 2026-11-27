"""Three fixed customer-inspired slides; full source values remain in notes/brief."""

import re
import textwrap

from .llm.context_render import LABELS as CONTEXT_LABELS, context_value


def render_compact(draft, facts, head, *, page, put, notes, citations, sources, labels,
                   portrait=None, portrait_url=None, partnership_matches=(), add_portrait=None):
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

    def footer(slide, ids, *, omitted=0, extra_link=None):
        # Source and catalog links stay clickable; full provenance is in notes.
        selected_urls = list(dict.fromkeys(e.url for f in facts if f.fact_id in ids for e in f.evidence))
        display_urls = selected_urls[:3]
        if extra_link:
            display_urls = [url for url in selected_urls if url != extra_link[0]][:2] + [extra_link[0]]
        label_for = {e.url: label for label, e in sources.values()}
        for index, url in enumerate(display_urls):
            from urllib.parse import urlsplit
            label = label_for.get(url, extra_link[1] if extra_link and url == extra_link[0] else "Источник")
            frame = put(slide, f"[{label}] {urlsplit(url).netloc}", .7 + index * 3.98, 6.50, 3.8, .22, 10, muted=True)
            frame.paragraphs[0].runs[0].hyperlink.address = url
        more = " · дополнительные ссылки — в заметках" if len(selected_urls) > len(display_urls) - bool(extra_link) else ""
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
        put(slide, name, .7, 1.58, 8.55 if portrait else 11.9, .6, 25, bold=True)
        primary = []
        for field in ("appointment.position", "appointment.status", "person.degree", "person.academic_title",
                      "person.education", "person.birth_date", "person.birth_place", "person.experience",
                      "appointment.since", "contact.phone", "contact.email"):
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
                         if f.field.value in {"person.honors", "appointment.responsibilities", "person.interests"})
        if not secondary:
            secondary = [("Карьерная история и зоны ответственности не подтверждены в выбранных материалах.", [], 4)]
        if portrait:
            omitted = column(slide, primary, .7, width=4.05) + column(slide, secondary, 4.95, width=4.05)
        else:
            omitted = column(slide, primary, .7) + column(slide, secondary, 6.9)
        footer(slide, [f.fact_id for f in items], omitted=len(omitted) + max(0, len(careers) - 2))
        if portrait and portrait_url:
            slide.notes_slide.notes_text_frame.text += f"\n\nПортрет руководителя: {portrait_url}"
    else:
        put(slide, "Руководитель головного учреждения не подтверждён в выбранных материалах", .7, 1.8, 11.9, 1.1, 23)
        footer(slide, [])

    slide = page("Справочная информация об учреждении")
    name_lines = lines(draft.institution.name, 93)
    put(slide, "\n".join(name_lines[:2]), .7, 1.55, 11.9, .73, 20, bold=True)
    institutional = [f for f in facts if f.field.value.startswith("institution.")]
    left, right = [], []
    metrics = ("institution.students", "institution.budget_students", "institution.teaching_staff",
               "institution.dormitory_capacity", "institution.dormitory_occupancy")
    for field in (*metrics, "institution.type", "institution.location", "institution.founded",
                  "institution.education_levels", "institution.structure", "institution.education_areas", "institution.profile"):
        group = [f for f in institutional if f.field.value == field]
        target = left if field in metrics else right
        limit = {"institution.education_areas": 5, "institution.education_levels": 2}.get(field, 1)
        for f in group[:limit]:
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
                          if item.field.value in {"project.program", "project.partner", "project.status",
                                                  "event.status", "event.date"}]
            prefix = "Проект: " if f.field.value.startswith("project.") else "Мероприятие: "
            entries.append((prefix + f.value + ("\n" + "; ".join(qualifiers) if qualifiers else ""),
                            [item.fact_id for item in group], 7))
    if not entries:
        entries = [("Проекты и мероприятия не подтверждены в выбранных материалах.", [], 4)]
    questions = [("Вопрос: " + q.question, q.basis_fact_ids, 4) for q in draft.questions[:3]]
    if not questions:
        questions = [("В каких федеральных проектах участвует учреждение?", [], 3),
                     ("Какие проекты идут с банками и крупными работодателями региона?", [], 3),
                     ("Что уже сделано со Сбером и что можно обсудить дальше?", [], 3)]
    omitted = column(slide, entries, .7) + column(slide, questions, 6.9)
    # Include all facts (also other staff) so compact selection loses no provenance.
    footer(slide, [f.fact_id for f in facts], omitted=len(omitted))

    if partnership_matches:
        for index, match in enumerate(partnership_matches):
            external = match.get("kind") == "external_course"
            title = "Вариант обучения · внешний курс" if external else "Предложения и партнерство со Сбером"
            slide = page(title + (f" · {index + 1}" if index else ""))
            put(slide, match["product_name"], .7, 1.55, 11.9, .62, 21, bold=True)
            put(slide, "Гипотеза для обсуждения: " + match["proposal"], .7, 2.35, 11.9, 1.05, 17)
            put(slide, "Основание: " + match["rationale"] + " " + citations(match["basis_fact_ids"]),
                .7, 3.65, 11.9, 1.05, 17)
            put(slide, "Вопрос для встречи: " + match["discussion_question"], .7, 4.75, 11.9, .85, 17)
            event = match.get("event_start")
            if event:
                put(slide, f"Дата начала по анонсу: {event}", .7, 5.63, 11.9, .28, 12)
            case = match.get("regional_case")
            if case:
                frame = put(slide, f"Похожий опыт · {case['location']} · {case['title']}", .7, 5.63, 11.9, .42, 12)
                frame.paragraphs[0].runs[0].hyperlink.address = case["url"]
            footer(slide, match["basis_fact_ids"], extra_link=(match["catalog_url"],
                   "Страница курса" if external else "Анонс Сбера" if event else "Каталог Сбера"))
            slide.notes_slide.notes_text_frame.text += (
                f"\n\nЗапись каталога: {match['catalog_url']}"
                "\nСовпадение — гипотеза для обсуждения, не подтверждение партнёрства или доступности продукта."
            )
            if case:
                slide.notes_slide.notes_text_frame.text += f"\nОпыт другого региона: {case['url']}"
    else:
        slide = page("Предложения и партнерство со Сбером")
        put(slide, "Предложения из каталога в эту презентацию не включены.",
            .7, 1.75, 11.9, 1.6, 17)
        footer(slide, [])
