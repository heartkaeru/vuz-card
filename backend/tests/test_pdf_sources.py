import asyncio
import hashlib
import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.services.llm.client import FixtureProvider
from app.services.llm.context_models import ContextExtractionRequest
from app.services.llm.pipeline import AssistantPipeline
from parser.config import Settings
from parser.institution_context import mark_institution_context
from parser.llm_adapter import to_llm_document
from parser.pdf_source import PDFSourceError, pdf_document
from parser.utils import HttpClient


def sample_pdf(*, blank=False, encrypted=False):
    writer = PdfWriter()
    for text in ["Test University report for 2025", "Students, all forms including branches: 1200 persons"]:
        page = writer.add_blank_page(600, 800)
        if not blank:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 740 Td ({text}) Tj ET".encode())
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("test-only-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_pages_hashes_offsets_and_native_contract():
    content = sample_pdf()
    document = pdf_document(content, url="https://university.example/report.pdf", pages=[2, 1, 2])
    assert document.file_sha256 == hashlib.sha256(content).hexdigest()
    assert document.metadata["pdf_selected_pages"] == "1,2"
    assert {f.page_number for f in document.fragments} == {1, 2}
    assert mark_institution_context(document, "Test University")
    request = ContextExtractionRequest(institution={"id": "draft:pdf-test", "name": "Test University", "card_version": 1},
        documents=[to_llm_document(document, include_contact_hints=False)], fields=["institution.students"])
    response = {"facts": [{"subject": {"entity_kind": "institution", "entity_name": "Test University",
        "entity_fragment_id": document.fragments[0].fragment_id}, "field": "institution.students", "value": "1200",
        "fragment_ids": [f.fragment_id for f in document.fragments],
        "metric_context": {"period": "2025", "scope": "all forms including branches", "unit": "persons"}}], "gaps": []}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request)).facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert {e.page_number for e in fact.evidence} == {1, 2}
    for evidence in fact.evidence:
        assert evidence.quote == document.text[evidence.start_offset:evidence.end_offset]
        assert evidence.file_sha256 == document.file_sha256


@pytest.mark.parametrize("pages", [[], [0], [3], [True], list(range(1, 22))])
def test_invalid_pdf_pages_rejected(pages):
    with pytest.raises(PDFSourceError, match="invalid_pdf_page_selection"):
        pdf_document(sample_pdf(), url="https://university.example/report.pdf", pages=pages)


@pytest.mark.parametrize("options,error", [
    ({"content": b"<html>not a PDF</html>"}, "invalid_pdf_signature"),
    ({"content": b"%PDF-broken"}, "pdf_text_extraction_failed"),
    ({"content": sample_pdf(blank=True)}, "pdf_text_not_found_ocr_required"),
    ({"content": sample_pdf(encrypted=True)}, "encrypted_pdf_not_supported"),
    ({"max_bytes": 10}, "pdf_file_too_large"),
    ({"max_chars": 5}, "pdf_text_limit_exceeded_select_fewer_pages"),
    ({"max_pages": 1}, "pdf_page_limit_exceeded"),
])
def test_pdf_failures_are_explicit(options, error):
    with pytest.raises(PDFSourceError, match=error):
        pdf_document(**({"content": sample_pdf(), "url": "https://university.example/report.pdf"} | options))


def test_pdf_partial_selection_keeps_absolute_page_number():
    document = pdf_document(sample_pdf(), url="https://university.example/report.pdf", pages=[2])
    assert document.metadata["partial_pdf"] == "true"
    assert all(f.page_number == 2 for f in document.fragments)


def test_literal_value_and_selector_can_coexist_without_accepting_an_invention():
    from test_dossier import metric_request, METRIC
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
        "field": "institution.students", "value": "1200", "value_fragment_id": "current",
        "fragment_ids": ["current"], "metric_context": METRIC}
    def extract(items):
        return asyncio.run(AssistantPipeline(FixtureProvider([{"facts": items, "gaps": []}])).extract_facts(metric_request()))
    assert extract([item]).facts[0].validation_status == "passed"
    wrong = dict(item, value="9999")
    assert "value_not_in_selected_fragment" in extract([wrong]).facts[0].issues


def test_equal_metric_values_from_different_periods_are_not_silently_collapsed():
    from test_dossier import metric_extract, METRIC
    from app.services.llm.context_render import context_statement
    fact = metric_extract(METRIC).facts[0]
    older = fact.model_copy(deep=True)
    older.metric_context.period = "2024 году"
    text = context_statement([fact, older])
    assert "2024 году" in text and "2025 году" in text


def test_education_section_heading_is_not_an_education_level():
    from test_dossier import metric_request
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.education_levels", "value_fragment_id": "current", "fragment_ids": ["current"]}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(metric_request())).facts[0]
    assert "education_levels_require_explicit_statement" in fact.issues


class Response:
    def __init__(self, blocks, content_type, encoding=None):
        self.blocks, self.headers, self.encoding = blocks, {"Content-Type": content_type}, encoding
        self.url, self.status_code, self.closed, self.read_count = "https://university.example/source", 200, False, 0
    def iter_content(self, chunk_size):
        for block in self.blocks:
            self.read_count += 1
            yield block
    def close(self):
        self.closed = True


def client_for(response):
    client = HttpClient(Settings(respect_robots=False, max_retries=1, max_response_bytes=1000))
    client.session.get = lambda *args, **kwargs: response
    client.limiter.wait = lambda *args: None
    return client


def test_http_download_stops_on_byte_limit_and_closes_response():
    response = Response([b"x" * 800, b"x" * 800, b"should not be downloaded"], "application/pdf")
    client = client_for(response)
    assert client.get(response.url) is None
    assert response.read_count == 2 and response.closed


def test_http_preserves_pdf_bytes_without_decoding():
    response = Response([b"%PDF-", b"raw\xff\x00bytes"], "application/pdf")
    result = client_for(response).get(response.url)
    assert result.content == b"%PDF-raw\xff\x00bytes" and result.text == "" and response.closed


def test_streaming_keeps_legacy_cyrillic_html_decoding():
    html = "<html><body>Руководство университета</body></html>"
    response = Response([html.encode("cp1251")], "text/html", "cp1251")
    result = client_for(response).get(response.url)
    assert result.text == html and result.content == b"" and response.closed
