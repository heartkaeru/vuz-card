"""Text PDFs with page-level provenance; no OCR or inferred report dates."""

import hashlib
import io
import re

from pypdf import PdfReader

from .models import DocumentFragment, SourceDocument, utc_now


class PDFSourceError(ValueError):
    """Safe diagnostic code for the API and parser report."""


def semantic_spans(text):
    for paragraph in re.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", text):
        value = paragraph.group()
        boundaries = [paragraph.start()]
        for match in re.finditer(r"(?<=[а-яёА-ЯЁA-Za-z)])\.\s+(?=[А-ЯЁA-Z])", value):
            word = re.search(r"([\w]+)\.$", value[:match.start() + 1])
            if word and len(word.group(1)) <= 3:
                continue  # г., им., initials, etc. are not sentence boundaries.
            boundaries.append(paragraph.start() + match.end())
        boundaries.extend(paragraph.start() + match.end() for match in re.finditer(
            r"\n[ \t]*(?=[•–−]\s)", value))
        boundaries.append(paragraph.end())
        boundaries = sorted(set(boundaries))
        yield from zip(boundaries, boundaries[1:])


def pdf_document(content, *, url, pages=None, retrieved_at=None, max_chars=120000,
                 max_bytes=5_000_000, max_pages=300, max_selected_pages=20):
    if len(content) > max_bytes:
        raise PDFSourceError("pdf_file_too_large")
    if not content.startswith(b"%PDF-"):
        raise PDFSourceError("invalid_pdf_signature")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise PDFSourceError("encrypted_pdf_not_supported")
        count = len(reader.pages)
        if count > max_pages:
            raise PDFSourceError("pdf_page_limit_exceeded")
        selected = sorted(set(pages if pages is not None else range(1, min(count, max_selected_pages) + 1)))
        if not selected or len(selected) > max_selected_pages or any(
            isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= count for number in selected
        ):
            raise PDFSourceError("invalid_pdf_page_selection")
        parts, fragments, offset, blank_pages = [], [], 0, []
        for number in selected:
            page = reader.pages[number - 1]
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > 5_000_000:
                raise PDFSourceError("pdf_page_content_too_large")
            text = (page.extract_text(extraction_mode="layout", layout_mode_strip_rotated=False) or "").strip()
            if not text:
                blank_pages.append(number)
                continue
            if offset + len(text) + (2 if parts else 0) > max_chars:
                raise PDFSourceError("pdf_text_limit_exceeded_select_fewer_pages")
            if parts:
                offset += 2
            base = offset
            parts.append(text)
            # Retain line alignment inside tables and original extracted offsets.
            for start, end in semantic_spans(text):
                while start < end:
                    stop = min(end, start + 1800)
                    if stop < end:
                        split = text.rfind("\n", start, stop)
                        stop = split if split > start else stop
                    trimmed_end = stop
                    while trimmed_end > start and text[trimmed_end - 1].isspace():
                        trimmed_end -= 1
                    if trimmed_end > start:
                        fragments.append(DocumentFragment(fragment_id=f"p{number:04d}_f{len(fragments) + 1:04d}",
                            start=base + start, end=base + trimmed_end, page_number=number))
                    start = stop
                    while start < end and text[start].isspace():
                        start += 1
            offset = base + len(text)
        if not parts:
            raise PDFSourceError("pdf_text_not_found_ocr_required")
        text = "\n\n".join(parts)
        digest = hashlib.sha256(content).hexdigest()
        identity = url + "|" + ",".join(map(str, selected))
        return SourceDocument(source_id="pdf_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            url=url, title=(str(reader.metadata.title) if reader.metadata and reader.metadata.title else "Отчёт учреждения (PDF)"),
            section="about", category="organization", content_type="application/pdf", text=text,
            retrieved_at=retrieved_at or utc_now(), text_hash=hashlib.sha256(text.encode()).hexdigest(),
            file_sha256=digest, fragments=fragments, metadata={
                "pdf_page_count": str(count), "pdf_selected_pages": ",".join(map(str, selected)),
                "partial_pdf": str(len(selected) < count).lower(),
                "pdf_pages_without_text": ",".join(map(str, blank_pages)),
                "pdf_layout_review_required": "true",
            })
    except PDFSourceError:
        raise
    except Exception as exc:
        # Corrupt PDFs raise different exceptions across parser versions.
        raise PDFSourceError("pdf_text_extraction_failed") from exc
