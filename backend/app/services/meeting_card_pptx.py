"""Customer-inspired editable card layout, populated only from accepted fact snapshots."""

import json
import re
import textwrap
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from .llm.models import DraftResult, Fact

ROOT = Path(__file__).resolve().parents[2] / "templates"
LABELS = {
    "appointment.position": "Должность", "appointment.status": "Статус назначения",
    "appointment.department": "Подразделение", "appointment.responsibilities": "Ответственность",
    "person.education": "Образование", "person.degree": "Учёная степень", "person.career": "Биография",
    "contact.phone": "Рабочий телефон", "contact.email": "Рабочая почта",
}
BIO = {"appointment.responsibilities", "person.education", "person.degree", "person.career"}


def build_card_presentation(draft: DraftResult, output_path: Path, facts):
    draft = DraftResult.model_validate_json(draft.model_dump_json())
    facts = [Fact.model_validate_json(f.model_dump_json()) for f in facts]
    if (len({f.fact_id for f in facts}) != len(facts) or set(draft.sources) != {f.fact_id for f in facts}
            or any(f.verification_status != "verified" or f.validation_status != "passed" or f.issues
                   or f.institution_id != draft.institution.id for f in facts)):
        raise ValueError("Presentation requires the exact accepted fact snapshot")
    style = json.loads((ROOT / "customer-card.json").read_text(encoding="utf-8"))
    presentation = Presentation()
    presentation.slide_width = Inches(style["width_inches"])
    presentation.slide_height = Inches(style["height_inches"])
    sources, ref_for = OrderedDict(), {}
    for fact in facts:
        ref_for[fact.fact_id] = []
        for evidence in fact.evidence:
            key = (evidence.url, evidence.content_sha256, evidence.retrieved_at.isoformat())
            if key not in sources:
                sources[key] = (f"S{len(sources) + 1}", evidence)
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
        put(slide, f"Черновик для проверки · версия карточки {draft.institution.card_version} · {len(presentation.slides)}",
            .7, 7.06, 11.9, .22, 10, muted=True)
        slide.notes_slide.notes_text_frame.text = f"Шаблон: {style['template_id']}\n" + "\n".join(draft.warnings)
        return slide

    def notes(slide, ids):
        parts = []
        for fid in dict.fromkeys(ids):
            fact = by_id[fid]
            parts.append(f"{fid}: {fact.field.value} = {fact.value}")
            for evidence in fact.evidence:
                parts.append(f"{evidence.url}\nПолучено: {evidence.retrieved_at.isoformat()}\n"
                             f"Фрагмент: {evidence.fragment_id or evidence.chunk_id}; SHA256: {evidence.content_sha256}")
        slide.notes_slide.notes_text_frame.text += "\n\n" + "\n\n".join(parts)

    def pages(title, entries, emphasize_first=False):
        current, used, page_index = [], 0, 0
        def flush():
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
            current, used, page_index = [], 0, page_index + 1
        for value, ids in entries:
            wrapped = [line for part in value.splitlines() for line in textwrap.wrap(part,
                       width=style["line_characters"], break_long_words=True)]
            for start in range(0, len(wrapped), 6):
                chunk = wrapped[start:start + 6]
                lines = len(chunk)
                if current and used + lines + 2 > style["body_lines"]:
                    flush()
                current.append(("\n".join(chunk) + " " + citations(ids), ids, lines, start == 0))
                used += lines + 2
        flush()

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
            for f in item[1])), None)
        if head:
            break
    head = head or next(iter(people.items()), None)

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
        elif section == "leadership":
            entries = []
            for (name, _), items in people.items():
                selected = [f for f in items if f.field.value not in BIO and f.field.value != "person.name"]
                labels = [f"{LABELS.get(f.field.value, f.field.value)}: {f.value}" for f in selected]
                entries.append((name + ("\n" + "\n".join(labels) if labels else ""), [f.fact_id for f in items if f.field.value not in BIO]))
            for fact in office:
                entries.append((f"{fact.contact_owner.label}\n{LABELS[fact.field.value]}: {fact.value}", [fact.fact_id]))
            pages("Руководство и контакты", entries, emphasize_first=True)
        elif section == "biography":
            entries = []
            for (name, _), items in people.items():
                selected = [f for f in items if f.field.value in BIO]
                if selected:
                    entries.append((name + "\n" + "\n".join(LABELS[f.field.value] + ": " + f.value for f in selected),
                                    [f.fact_id for f in selected]))
            pages("Биография и ответственность", entries, emphasize_first=True)
        elif section == "questions":
            pages("Вопросы к встрече", [(q.question, q.basis_fact_ids) for q in draft.questions])
        elif section == "theses":
            # Leadership is already rendered structurally, without model wording changes.
            context_ids = {f.fact_id for f in facts if f.field.value.startswith(("project.", "event.", "institution."))}
            for item in draft.slides:
                entries = [(s.text, s.fact_ids) for s in item.theses if set(s.fact_ids) & context_ids]
                pages(item.title, entries)
        elif section == "sources":
            source_items = list(sources.values())
            for start in range(0, len(source_items), 8):
                slide = page("Источники" + (" · продолжение" if start else ""))
                y = 1.55
                for label, evidence in source_items[start:start + 8]:
                    frame = put(slide, f"[{label}] {urlsplit(evidence.url).netloc} · {evidence.retrieved_at.date().isoformat()}",
                                .7, y, 11.9, .45, 17)
                    frame.paragraphs[0].runs[0].hyperlink.address = evidence.url
                    y += .58
                notes(slide, list(by_id))
        else:
            raise ValueError("Unknown presentation template section")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output_path)
    return output_path
