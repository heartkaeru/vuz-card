"""Editable PPTX from a reviewed draft and a fixed, versioned layout template."""

import json
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from .llm.models import DraftResult

TEMPLATE = Path(__file__).resolve().parents[2] / "templates/meeting.json"


def build_meeting_presentation(draft: DraftResult, output_path: Path, facts=None):
    if facts:
        from .meeting_card_pptx import build_card_presentation
        return build_card_presentation(draft, output_path, facts)
    draft = DraftResult.model_validate_json(draft.model_dump_json())
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    presentation = Presentation()
    presentation.slide_width = Inches(template["width_inches"])
    presentation.slide_height = Inches(template["height_inches"])
    refs = {fid: f"S{i}" for i, fid in enumerate(draft.sources, 1)}

    def text(slide, value, x, y, width, height, size, color, bold=False):
        frame = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(width), Inches(height)).text_frame
        frame.word_wrap = True
        for i, line in enumerate(value.split("\n")):
            paragraph = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
            paragraph.text = line
            paragraph.font.name = template["font"]
            paragraph.font.size = Pt(size)
            paragraph.font.bold = bold
            paragraph.font.color.rgb = RGBColor.from_string(color)
            paragraph.space_after = Pt(0)
            paragraph.line_spacing = 1.1
        return frame

    def slide(title):
        page = presentation.slides.add_slide(presentation.slide_layouts[6])
        text(page, title if len(title) <= 100 else "Материалы к встрече", .7, .4, 11.9, 1.05,
             template["title_pt"] if len(title) < 65 else 24, template["accent"], True)
        text(page, f"Черновик для проверки · версия карточки {draft.institution.card_version} · {len(presentation.slides)}",
             .7, 7.02, 11.8, .25, 10, template["muted"])
        return page

    cover = slide(template["title"])
    text(cover, draft.institution.name, .7, 1.9, 11.8, 2.1,
         32 if len(draft.institution.name) < 150 else 22, template["text"], True)
    text(cover, f"Цель: {draft.meeting.goal}", .7, 4.5, 11.8, 1.1, 18, template["text"])
    if draft.meeting.date:
        text(cover, f"Дата встречи: {draft.meeting.date}", .7, 5.7, 11.8, .5, 16, template["muted"])
    if draft.run.provider == "fixture":
        text(cover, "Учебный пример: сохранённые ответы модели, без вызова GigaChat", .7, 6.4, 11.8, .35,
             12, template["muted"])
    cover.notes_slide.notes_text_frame.text = "\n".join([
        "Шаблон: " + template["template_id"],
        "Принятые факты использованы для черновика; итоговый текст требует проверки.",
        *draft.warnings,
    ])

    for section in draft.slides:
        pages, current, lines = [], [], 0
        for statement in section.theses:
            if not set(statement.fact_ids) <= draft.sources.keys():
                raise ValueError("Unknown source reference in presentation")
            label = " ".join(f"[{refs[fid]}]" for fid in dict.fromkeys(statement.fact_ids))
            # Split long biographies into readable continuations; never silently cut text.
            wrapped = textwrap.wrap(statement.text, width=template["line_characters"], break_long_words=True)
            for start in range(0, len(wrapped), 6):
                chunk = wrapped[start:start + 6]
                value = "\n".join(chunk) + " " + label
                count = len(chunk) + 2
                if current and lines + count > template["body_lines"]:
                    pages.append(current)
                    current, lines = [], 0
                current.append((value, statement.fact_ids))
                lines += count
        if current:
            pages.append(current)
        for index, items in enumerate(pages):
            page = slide(section.title + (" · продолжение" if index else ""))
            y = 1.55
            notes = []
            for value, ids in items:
                line_count = len(value.splitlines())
                height = (line_count + 1) * .31
                text(page, value, .7, y, 11.9, height, template["body_pt"], template["text"])
                y += height + .12
                for fid in dict.fromkeys(ids):
                    for evidence in draft.sources[fid]:
                        notes.append(f"[{refs[fid]}] {evidence.url}\nПолучено: {evidence.retrieved_at.isoformat()}\n"
                                     f"Фрагмент: {evidence.fragment_id or evidence.chunk_id}; SHA256: {evidence.content_sha256}")
            page.notes_slide.notes_text_frame.text = "\n\n".join(dict.fromkeys(notes))

    sources = [(refs[fid], evidence) for fid, items in draft.sources.items() for evidence in items]
    for start in range(0, len(sources), 8):
        page = slide("Источники" + (" · продолжение" if start else ""))
        y = 1.5
        for label, evidence in sources[start:start + 8]:
            domain = urlsplit(evidence.url).netloc
            frame = text(page, f"[{label}] {domain} · {evidence.retrieved_at.date().isoformat()}",
                         .7, y, 11.8, .5, 17, template["text"])
            frame.paragraphs[0].runs[0].hyperlink.address = evidence.url
            y += .58
        page.notes_slide.notes_text_frame.text = "\n".join(
            f"[{label}] {evidence.url}" for label, evidence in sources[start:start + 8])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output_path)
    return output_path
