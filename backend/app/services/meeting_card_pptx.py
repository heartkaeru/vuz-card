"""Customer-inspired editable card layout, populated only from accepted fact snapshots."""

import json
import re
import textwrap
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from .llm.models import DraftResult, Fact
from .llm.context_render import LABELS as CONTEXT_LABELS, context_value

ROOT = Path(__file__).resolve().parents[2] / "templates"
LABELS = {
    "appointment.position": "Должность", "appointment.status": "Статус назначения",
    "appointment.department": "Подразделение", "appointment.responsibilities": "Ответственность",
    "person.education": "Образование", "person.degree": "Учёная степень", "person.career": "Биография",
    "person.academic_title": "Учёное звание", "person.interests": "Научные интересы",
    "appointment.since": "Назначение",
    "contact.phone": "Рабочий телефон", "contact.email": "Рабочая почта",
}
BIO = {"appointment.responsibilities", "person.education", "person.degree", "person.career",
       "person.academic_title", "person.interests"}


def build_card_presentation(draft: DraftResult, output_path: Path, facts, *, candidate_preview=False):
    draft = DraftResult.model_validate_json(draft.model_dump_json())
    facts = [Fact.model_validate_json(f.model_dump_json()) for f in facts]
    if (len({f.fact_id for f in facts}) != len(facts) or set(draft.sources) != {f.fact_id for f in facts}
            or any(f.verification_status not in ({"pending", "verified"} if candidate_preview else {"verified"})
                   or f.validation_status != "passed" or f.issues
                   or f.field.value == "institution.students" and f.metric_context is None
                   or f.institution_id != draft.institution.id for f in facts)):
        raise ValueError("Presentation requires the exact accepted fact snapshot")
    style = json.loads((ROOT / ("customer-compact.json" if draft.presentation_variant == "compact" else "customer-card.json")).read_text(encoding="utf-8"))
    presentation = Presentation()
    presentation.slide_width = Inches(style["width_inches"])
    presentation.slide_height = Inches(style["height_inches"])
    sources, ref_for, source_pages = OrderedDict(), {}, {}
    for fact in facts:
        ref_for[fact.fact_id] = []
        for evidence in fact.evidence:
            key = (evidence.url, evidence.file_sha256 or evidence.content_sha256, evidence.retrieved_at.isoformat())
            if key not in sources:
                sources[key] = (f"S{len(sources) + 1}", evidence)
            if evidence.page_number:
                source_pages.setdefault(key, set()).add(evidence.page_number)
            ref_for[fact.fact_id].append(sources[key][0])
    by_id = {f.fact_id: f for f in facts}

    def citations(ids):
        labels = dict.fromkeys(label for fid in ids for label in ref_for[fid])
        return " ".join(f"[{label}]" for label in labels)

    def put(page, value, x, y, width, height, size=18, bold=False, muted=False):
        frame = page.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height)).text_frame
        frame.word_wrap = True
        frame.margin_left = frame.margin_right = Inches(.02)
        frame.margin_top = frame.margin_bottom = Inches(.02)
        for i, line in enumerate(value.split("\n")):
            paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            paragraph.text = line
            paragraph.font.name = style["font"]
            paragraph.font.size = Pt(size)
            paragraph.font.bold = bold
            paragraph.font.color.rgb = RGBColor.from_string(style["muted_color"] if muted else style["text_color"])
            paragraph.space_after = Pt(0)
            paragraph.line_spacing = 1.15
        return frame

    def page(title):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(str(ROOT / style["header_image"]), 0, 0,
                                 width=presentation.slide_width, height=Inches(1.85))
        slide.shapes.add_picture(str(ROOT / style["logo_image"]), Inches(10.85), Inches(.4), width=Inches(1.65))
        put(slide, title if len(title) < 110 else "Материалы к встрече", .68, .48, 9.7, 1.05,
            style["title_pt"] if len(title) < 50 else 23)
        caption = ("ПРЕДПРОСМОТР · КАНДИДАТЫ НЕ ПРИНЯТЫ" if candidate_preview else "Черновик для проверки")
        put(slide, f"{caption} · версия карточки {draft.institution.card_version} · {len(presentation.slides)}",
            .7, 7.06, 11.9, .22, 10, muted=True)
        slide.notes_slide.notes_text_frame.text = f"Шаблон: {style['template_id']}\n" + "\n".join(draft.warnings)
        if candidate_preview:
            put(slide, "КАНДИДАТЫ ДЛЯ ПРОВЕРКИ", .7, 6.77, 11.9, .22, 10, bold=True)
        return slide

    def notes(slide, ids):
        parts = []
        for fid in dict.fromkeys(ids):
            fact = by_id[fid]
            parts.append(f"{fid} {citations([fid])}: {fact.field.value} = {fact.value}; проверка: {fact.verification_status}")
            if fact.metric_context:
                parts.append(f"Контекст показателя: {fact.metric_context.model_dump_json()}")
            for evidence in fact.evidence:
                parts.append(f"Цитата: {evidence.quote}")
                parts.append(f"{evidence.url}\nПолучено: {evidence.retrieved_at.isoformat()}\n"
                             f"Фрагмент: {evidence.fragment_id or evidence.chunk_id}; SHA256: {evidence.content_sha256}")
                if evidence.page_number:
                    parts.append(f"Страница PDF: {evidence.page_number}; SHA256 файла: {evidence.file_sha256}")
        slide.notes_slide.notes_text_frame.text += "\n\n" + "\n\n".join(parts)

    def pages(title, entries, emphasize_first=False, missing_note=None):
        current, used, page_index = [], 0, 0
        def flush(last=False):
            nonlocal current, used, page_index
            if not current:
                return
            slide = page(title + (" · продолжение" if page_index else ""))
            y, ids = 1.65, []
            for value, refs, lines, first in current:
                height = (lines + 1) * .31
                frame = put(slide, value, .7, y, 11.9, height, style["body_pt"])
                if first and emphasize_first:
                    frame.paragraphs[0].font.bold = True
                y += height + .15
                ids.extend(refs)
            notes(slide, ids)
            if last and missing_note:
                put(slide, missing_note, .7, 6.36, 11.9, .38, 11, muted=True)
            current, used, page_index = [], 0, page_index + 1
        for entry_index, (value, ids) in enumerate(entries):
            wrapped = [line for part in value.splitlines() for line in textwrap.wrap(part,
                       width=style["line_characters"], break_long_words=True)]
            for start in range(0, len(wrapped), 6):
                chunk = wrapped[start:start + 6]
                lines = len(chunk)
                if current and used + lines + 2 > (16 if missing_note else style["body_lines"]):
                    flush()
                current.append(("\n".join(chunk) + " " + citations(ids), ids, lines,
                                start == 0 and (emphasize_first != "first_entry" or entry_index == 0)))
                used += lines + 2
        flush(last=True)

    people, office = OrderedDict(), []
    for fact in facts:
        name = fact.subject.person_name
        if name:
            key = (name, getattr(fact.subject, "department", None))
            people.setdefault(key, []).append(fact)
        elif fact.contact_owner is not None:
            office.append(fact)
    head = None
    for role in (r"ректор(?:а)?", r"директор(?:а)?"):
        head = next((item for item in people.items() if item[0][1] is None and any(
            f.field.value == "appointment.position" and re.search(rf"(?<!\w){role}(?!\w)", f.value, re.I)
            and not re.search(r"филиал", f.value, re.I)
            and not re.search(r"заместител|помощник|советник|проректор", f.value, re.I)
            for f in item[1])), None)
        if head:
            break

    if draft.presentation_variant == "compact":
        from .compact_card import render_compact
        render_compact(draft, facts, head, page=page, put=put, notes=notes, citations=citations,
                       sources=sources, labels=LABELS)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        presentation.save(output_path)
        return output_path

    for section in style["sections"]:
        if section == "overview":
            slide = page("Карточка образовательной организации")
            put(slide, draft.institution.name, .7, 1.5, 11.9, 1.35, 28 if len(draft.institution.name) < 150 else 22, True)
            put(slide, "К встрече", 8.5, 3.1, 4.1, .4, 16, muted=True)
            goal = draft.meeting.goal
            short_goal = sum(max(1, len(textwrap.wrap(part, width=35))) for part in goal.splitlines()) <= 6
            put(slide, goal if short_goal else "Цель встречи приведена на следующем слайде", 8.5, 3.6, 4.1, 2.1, 18)
            if draft.meeting.date:
                put(slide, draft.meeting.date, 8.5, 6.0, 4.1, .65, 16, muted=True)
            if head:
                (name, _), items = head
                selected = [f for f in items if f.field.value in {"person.name", "appointment.position", "appointment.status"}]
                put(slide, "Руководитель", .7, 3.1, 7.3, .4, 16, muted=True)
                put(slide, name, .7, 3.6, 7.3, .75, 25, True)
                positions = [f.value for f in selected if f.field.value != "person.name"]
                role_text = "; ".join(positions)
                if len(textwrap.wrap(role_text, width=65)) > 5:
                    role_text = "Должности и статус — в разделе руководства"
                put(slide, role_text + " " + citations([f.fact_id for f in selected]),
                    .7, 4.55, 7.3, 1.6, 18)
                notes(slide, [f.fact_id for f in selected])
            elif sources:
                put(slide, "Источники для подготовки", .7, 3.1, 7.3, .4, 16, muted=True)
                put(slide, "\n".join(dict.fromkeys(urlsplit(e.url).netloc for _, e in sources.values())),
                    .7, 3.6, 7.3, 1.4, 18)
            if draft.run.provider == "fixture":
                put(slide, "Учебный пример: сохранённые ответы, без GigaChat", .7, 6.55, 11.9, .3, 11, muted=True)
            if not short_goal:
                pages("Цель встречи", [(goal, [])])
        elif section == "head":
            if head:
                (name, _), items = head
                selected = [f for f in items if f.field.value not in {"person.name", "person.career"}]
                entries = [(name, [f.fact_id for f in items if f.field.value == "person.name"])]
                grouped_values = OrderedDict()
                for fact in selected:
                    grouped_values.setdefault(fact.value, []).append(fact)
                for value, group in grouped_values.items():
                    label = " / ".join(dict.fromkeys(LABELS.get(f.field.value, f.field.value) for f in group))
                    entries.append((value if value.lower().startswith(label.lower() + ":") else label + ": " + value,
                                    [f.fact_id for f in group]))
                expected = {"person.education": "образование", "person.degree": "учёная степень",
                            "person.academic_title": "учёное звание", "person.career": "карьерная история",
                            "appointment.since": "дата назначения", "appointment.responsibilities": "зона ответственности"}
                missing = [label for field, label in expected.items() if not any(f.field.value == field for f in items)]
                # Keep related fields together; a short missing-data note should not
                # create a nearly empty continuation slide on its own.
                combined = [("\n".join(value for value, _ in entries), [fid for _, ids in entries for fid in ids])]
                note = "Не подтверждено в выбранных материалах: " + ", ".join(missing) + "." if missing else None
                pages("Руководитель учреждения", combined, emphasize_first="first_entry", missing_note=note)
        elif section == "institution":
            selected = [f for f in facts if f.field.value.startswith("institution.")]
            groups = OrderedDict()
            for fact in selected:
                # Counts remain separate so different periods/populations never merge.
                key = (fact.field.value, fact.fact_id if fact.metric_context else None)
                groups.setdefault(key, []).append(fact)
            entries = [(f"{CONTEXT_LABELS[field]}: " + "\n".join(dict.fromkeys(context_value(f) for f in group)),
                        [f.fact_id for f in group]) for (field, _), group in groups.items()]
            expected = {"institution.education_areas": "направления подготовки",
                        "institution.education_levels": "уровни образования",
                        "institution.students": "численность обучающихся с периодом и составом",
                        "institution.structure": "структура и филиалы"}
            missing = [label for field, label in expected.items() if not any(f.field.value == field for f in selected)]
            if missing:
                entries.append(("Не подтверждено в выбранных материалах: " + "; ".join(missing) + ".", []))
            pages("Справка об учреждении", entries)
        elif section == "leadership":
            entries = []
            for (name, department), items in people.items():
                if head and (name, department) == head[0]:
                    continue
                selected = [f for f in items if f.field.value not in BIO and f.field.value != "person.name"]
                labels = [f"{LABELS.get(f.field.value, f.field.value)}: {f.value}" for f in selected]
                scope = f"\nПодразделение: {department}" if department else ""
                entries.append((name + scope + ("\n" + "\n".join(labels) if labels else ""), [f.fact_id for f in items if f.field.value not in BIO]))
            for fact in office:
                entries.append((f"{fact.contact_owner.label}\n{LABELS[fact.field.value]}: {fact.value}", [fact.fact_id]))
            pages("Руководство и контакты", entries, emphasize_first=True)
        elif section == "biography":
            entries = []
            for (name, department), items in people.items():
                selected = [f for f in items if f.field.value in BIO and (
                    not head or (name, department) != head[0] or f.field.value == "person.career")]
                def biography_order(fact):
                    year = re.search(r"\b(?:19|20)\d{2}\b", fact.value[:80])
                    return int(year.group()) if fact.field.value == "person.career" and year else 9999
                selected.sort(key=biography_order)
                if selected:
                    scope = f"\nПодразделение: {department}" if department else ""
                    for index, fact in enumerate(selected):
                        prefix = name + scope + "\n" if index == 0 else ""
                        entries.append((prefix + LABELS[fact.field.value] + ": " + fact.value, [fact.fact_id]))
            pages("Биография и ответственность", entries)
        elif section == "questions":
            pages("Вопросы к встрече", [(q.question, q.basis_fact_ids) for q in draft.questions])
        elif section == "theses":
            # Leadership is already rendered structurally, without model wording changes.
            context_ids = {f.fact_id for f in facts if f.field.value.startswith(("project.", "event."))}
            for item in draft.slides:
                entries = [(s.text, s.fact_ids) for s in item.theses if set(s.fact_ids) & context_ids]
                pages(item.title, entries)
        elif section == "sources":
            source_items = list(sources.items())
            for start in range(0, len(source_items), 6):
                slide = page("Источники" + (" · продолжение" if start else ""))
                y = 1.55
                for key, (label, evidence) in source_items[start:start + 6]:
                    url = urlsplit(evidence.url)
                    short_path = "/".join(unquote(url.path).strip("/").split("/")[-2:])
                    if len(short_path) > 75:
                        short_path = "…/" + short_path.split("/")[-1][:65]
                    frame = put(slide, f"[{label}] {url.netloc}/{short_path}", .7, y, 11.9, .4, 16)
                    frame.paragraphs[0].runs[0].hyperlink.address = evidence.url
                    pages_text = "; PDF, страницы: " + ", ".join(map(str, sorted(source_pages[key]))) if key in source_pages else ""
                    put(slide, f"Получено: {evidence.retrieved_at.date().isoformat()}{pages_text}", .7, y + .4, 11.9, .3, 12, muted=True)
                    y += .85
                notes(slide, list(by_id))
        else:
            raise ValueError("Unknown presentation template section")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output_path)
    return output_path
