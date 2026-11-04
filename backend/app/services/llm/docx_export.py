"""Create a lightweight Word version of a generated meeting brief."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET

from .models import DraftResult

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("w", W)
ET.register_namespace("r", REL)


def _tag(name: str) -> str:
    return f"{{{W}}}{name}"


def _paragraph(body, text: str, *, size: int = 22, bold: bool = False, centered: bool = False):
    paragraph = ET.SubElement(body, _tag("p"))
    properties = ET.SubElement(paragraph, _tag("pPr"))
    ET.SubElement(properties, _tag("spacing"), {_tag("after"): "120", _tag("line"): "300"})
    if centered:
        ET.SubElement(properties, _tag("jc"), {_tag("val"): "center"})
    run = ET.SubElement(paragraph, _tag("r"))
    run_properties = ET.SubElement(run, _tag("rPr"))
    ET.SubElement(run_properties, _tag("rFonts"), {_tag("ascii"): "Arial", _tag("hAnsi"): "Arial"})
    if bold:
        ET.SubElement(run_properties, _tag("b"))
    ET.SubElement(run_properties, _tag("sz"), {_tag("val"): str(size)})
    ET.SubElement(run_properties, _tag("szCs"), {_tag("val"): str(size)})
    value = ET.SubElement(run, _tag("t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
    value.text = text
    return paragraph


def build_word_document(draft: DraftResult, output_path: Path) -> Path:
    """Write a DOCX brief using only content already present in the draft."""
    document = ET.Element(_tag("document"))
    body = ET.SubElement(document, _tag("body"))
    _paragraph(body, f"Материалы к встрече: {draft.institution.name}", size=34, bold=True, centered=True)
    _paragraph(body, "Черновик — требуется проверка сотрудником.", bold=True)
    _paragraph(body, f"Версия карточки: {draft.institution.card_version}")
    _paragraph(body, f"Подготовлено: {draft.run.created_at.isoformat()}")
    _paragraph(body, f"Цель встречи: {draft.meeting.goal}")
    if draft.meeting.date:
        _paragraph(body, f"Дата встречи: {draft.meeting.date}")
    if draft.run.provider == "fixture":
        _paragraph(body, "Демонстрация на сохранённых ответах. Реальных запросов к GigaChat нет.")

    refs = {fact_id: f"S{index}" for index, fact_id in enumerate(draft.sources, 1)}

    def citation(fact_ids):
        return " ".join(f"[{refs[fact_id]}]" for fact_id in dict.fromkeys(fact_ids))

    for section in draft.brief_sections:
        if not section.statements:
            continue
        _paragraph(body, section.title, size=28, bold=True)
        for statement in section.statements:
            _paragraph(body, f"• {statement.text} {citation(statement.fact_ids)}")

    _paragraph(body, "Тезисы презентации", size=28, bold=True)
    for slide in draft.slides:
        _paragraph(body, slide.title, size=24, bold=True)
        for thesis in slide.theses:
            _paragraph(body, f"• {thesis.text} {citation(thesis.fact_ids)}")

    if draft.questions:
        _paragraph(body, "Вопросы для обсуждения", size=28, bold=True)
        for question in draft.questions:
            _paragraph(body, f"• {question.question} {citation(question.basis_fact_ids)}")

    _paragraph(body, "Источники", size=28, bold=True)
    for fact_id, evidence_list in draft.sources.items():
        _paragraph(body, f"{refs[fact_id]} — {fact_id}", bold=True)
        for evidence in evidence_list:
            _paragraph(body, f"URL: {evidence.url}")
            _paragraph(body, f"Получено: {evidence.retrieved_at.isoformat()}")
            _paragraph(body, f"Опубликовано: {evidence.published_at.isoformat() if evidence.published_at else 'дата неизвестна'}")
            _paragraph(body, f"Цитата: {evidence.quote}")

    _paragraph(body, "Ограничения проверки", size=28, bold=True)
    for warning in draft.warnings:
        _paragraph(body, f"• {warning}")

    section = ET.SubElement(body, _tag("sectPr"))
    ET.SubElement(section, _tag("pgSz"), {_tag("w"): "11906", _tag("h"): "16838"})
    ET.SubElement(section, _tag("pgMar"), {
        _tag("top"): "1134", _tag("right"): "850", _tag("bottom"): "1134",
        _tag("left"): "850", _tag("header"): "708", _tag("footer"): "708", _tag("gutter"): "0",
    })

    types = ET.Element(f"{{{CT}}}Types")
    ET.SubElement(types, f"{{{CT}}}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(types, f"{{{CT}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(types, f"{{{CT}}}Override", {
        "PartName": "/word/document.xml",
        "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    })

    relationships = ET.Element(f"{{{REL}}}Relationships")
    ET.SubElement(relationships, f"{{{REL}}}Relationship", {
        "Id": "rId1",
        "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        "Target": "word/document.xml",
    })

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ET.tostring(types, encoding="utf-8", xml_declaration=True))
        archive.writestr("_rels/.rels", ET.tostring(relationships, encoding="utf-8", xml_declaration=True))
        archive.writestr("word/document.xml", ET.tostring(document, encoding="utf-8", xml_declaration=True))
    return output_path
