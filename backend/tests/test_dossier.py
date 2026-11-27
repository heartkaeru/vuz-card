"""Dossier grounding: separate biography paragraphs and qualified student counts."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pptx import Presentation

from app.services.llm.client import FixtureProvider
from app.services.llm.context_extraction import select_report_measures
from app.services.llm.context_models import ContextDocument, ContextExtractionRequest, ContextField
from app.services.llm.fragment_models import FragmentExtractionRequest
from app.services.llm.models import DraftResult, GenerationRequest, Institution
from app.services.llm.pipeline import AssistantPipeline, DraftValidationError
from app.services.llm.official_sources import (
    official_basic_candidates, official_catalog_candidates, official_program_candidates,
    official_reference_document, official_self_study_url,
)
from app.services.meeting_pptx import build_meeting_presentation
from parser.biography import mark_biography_subject
from parser.cleaner import HTMLCleaner
from parser.llm_adapter import to_llm_document
from parser.institution_context import mark_institution_context
from parser.models import UniversityData

NAME = "Иванов Иван Иванович"
INSTITUTION = {"id": "draft:dossier-test", "name": "Учебный университет", "card_version": 1}


def test_official_catalog_needs_exact_code_and_name_on_the_same_official_row():
    url = "https://programs.university.example/catalog/"
    quote = "Математика | 01.03.01 Математика | Естественные науки | бакалавриат"
    document = HTMLCleaner().to_document(f"<main><p>{quote}</p><p>09.03.04 Соседнее направление</p></main>",
                                         url=url, section="programs")
    card = UniversityData.model_validate({"university": {"name": INSTITUTION["name"], "url": "https://university.example"},
        "documents": [document], "specialties": [
            {"code": "01.03.01", "name": "Математика", "level": "бакалавриат", "source_url": url},
            {"code": "09.03.04", "name": "Математика", "level": "магистратура", "source_url": url},
            {"code": "01.03.01", "name": "Математика", "source_url": "https://elsewhere.example/catalog/"},
        ]})
    institution = Institution.model_validate(INSTITUTION)
    facts = list(official_catalog_candidates(card, institution, [], {"institution.education_areas", "institution.education_levels"}))
    assert {f.value for f in facts} == {"01.03.01 Математика", "бакалавриат"}
    assert all(f.verification_status == "pending" and f.evidence[0].quote == quote and f.evidence[0].url == url for f in facts)


def test_official_common_fields_use_disclosed_name_and_values():
    url = "https://university.example/sveden/common/"
    owner = "Федеральное образовательное учреждение высшего образования «Учебный университет»"
    page = SimpleNamespace(final_url=url, text=f"""<main><p itemprop="fullName">{owner}</p>
        <p>Дата создания образовательной организации: <span itemprop="regDate">10 июля 1930 г.</span></p>
        <p>Адрес: <span itemprop="address">г. Москва, ул. Ленина, 11</span></p></main>""")
    institution = Institution.model_validate(INSTITUTION)
    doc = official_reference_document(SimpleNamespace(get=lambda _: page), url, url, institution.name, max_chars=4000)
    candidates = {f.field.value: f for f in official_basic_candidates([doc], institution, [])}
    assert {key: f.value for key, f in candidates.items()} == {"institution.founded": "10 июля 1930 г.",
        "institution.location": "г. Москва, ул. Ленина, 11", "institution.type": "образовательное учреждение высшего образования"}
    assert "Дата создания образовательной организации" in candidates["institution.founded"].evidence[0].quote
    assert all(f.verification_status == "pending" and any(owner in e.quote for e in f.evidence) for f in candidates.values())


def test_self_study_discovery_prefers_the_latest_head_office_report_on_official_site():
    page = SimpleNamespace(final_url="https://university.example/sveden/document/", text=r"""
        <div><a href="\reports\head.pdf">Скачать .PDF</a><a href="\reports\head.pdf.sig">ЭП</a>
        Отчет о результатах самообследования за 2025 год (головной вуз)</div>
        <a href="/branch.pdf">Отчет о результатах самообследования за 2026 год (филиал)</a>
        <a href="https://elsewhere.example/latest.pdf">Отчет о результатах самообследования за 2026 год</a>
        <a href="/older.pdf">Отчет о самообследовании за 2024 год</a>
    """)
    assert official_self_study_url(SimpleNamespace(get=lambda _: page), "https://university.example") == (
        "https://university.example/reports/head.pdf")


def test_pdf_measure_routing_retains_title_anchor_and_late_page():
    blocks = ["Учебный университет. Отчёт за 2025 год", "Этапы развития цифровой среды",
              "Контингент обучающихся по программам высшего образования составляет 1200 чел."]
    text = "\n\n".join(blocks)
    fragments = []
    for index, block in enumerate(blocks):
        start = text.index(block)
        fragments.append({"fragment_id": f"f{index}", "start": start, "end": start + len(block),
                          "page_number": (1, 4, 18)[index], "institution_anchor_id": "f0"})
    doc = ContextDocument.model_validate({"source_id": "report", "url": "https://university.example/report.pdf",
        "text": text, "retrieved_at": "2026-11-28T00:00:00Z", "file_sha256": "a" * 64, "fragments": fragments})

    focused = select_report_measures(doc, [ContextField.STUDENTS])

    assert focused.text == text and [f.page_number for f in focused.fragments] == [1, 18]
    assert select_report_measures(doc, [ContextField.STUDENTS, ContextField.PROFILE]) is doc


def test_official_program_reference_keeps_subdomain_and_literal_title_anchor():
    url = "https://admission.university.example/about/program"
    page = SimpleNamespace(final_url=url, text="""<html><head>
        <title>09.03.04. Программная инженерия | Приёмная комиссия. Учебный университет</title>
        <link rel="canonical" href="/about/program">
        </head><body><main><h1>Программная инженерия</h1><p>Бакалавриат</p></main></body></html>""")
    client = SimpleNamespace(get=lambda _: page)
    document = official_reference_document(client, url, "https://university.example",
                                           "Учебный университет", max_chars=4000)
    assert document.url == url
    anchor = next(fragment for fragment in document.fragments
                  if "Приёмная комиссия. Учебный университет" in document.text[fragment.start:fragment.end])
    assert any(fragment.institution_anchor_id == anchor.fragment_id
               and "Бакалавриат" in document.text[fragment.start:fragment.end] for fragment in document.fragments)
    institution = Institution.model_validate(INSTITUTION)
    fact, = official_program_candidates([document], institution, [])
    assert fact.value == "09.03.04. Программная инженерия" and fact.verification_status == "pending"
    assert fact.evidence[0].url == url
    assert document.text[anchor.start:anchor.end] == fact.evidence[0].quote
    assert not list(official_program_candidates([document], institution, [fact]))


def test_program_disclosure_keeps_query_and_links_literal_heading_to_publisher():
    url = "https://university.example/sveden/education/program?f=2026_09.03.03"
    value = "09.03.03 Прикладная информатика: Системы искусственного интеллекта"
    owner = "Учебный университет"
    page = SimpleNamespace(final_url=url, text=f"""<html><head>
        <title>Информация о реализуемой образовательной программе</title>
        <link rel="canonical" href="/sveden/education/program">
        </head><body><main><h4>{value}</h4>
        <span itemprop="eduCode">09.03.03</span><span itemprop="eduName">Прикладная информатика</span>
        </main><footer><p>© 2026 {owner}</p></footer></body></html>""")
    client = SimpleNamespace(get=lambda _: page)
    institution = Institution.model_validate(INSTITUTION)

    document = official_reference_document(client, url, "https://university.example",
                                           institution.name, max_chars=4000)
    assert document.url == url
    fact, = official_program_candidates([document], institution, [])
    assert fact.value == value and fact.verification_status == "pending"
    assert [proof.quote for proof in fact.evidence] == [value, owner]
    for proof in fact.evidence:
        assert document.text[proof.start_offset:proof.end_offset] == proof.quote
        assert proof.url == url

    page.text = page.text.replace(owner, "Другой университет")
    other = official_reference_document(client, url, "https://university.example",
                                        institution.name, max_chars=4000)
    assert not list(official_program_candidates([other], institution, []))


def biography_request():
    html = f"""<main><h1>{NAME}</h1><p>Ректор</p>
    <p>В 2001 году окончил Учебный университет по специальности «Экономика».</p>
    <h2>Карьера</h2><p>В 2010–2020 годах работал проректором.</p>
    <h2>Петров Пётр Петрович</h2><p>В 2002 году окончил Технический университет.</p></main>"""
    doc = HTMLCleaner().to_document(html, url="https://university.example/rector", section="bio")
    assert mark_biography_subject(doc, NAME)
    return FragmentExtractionRequest(institution=INSTITUTION, documents=[to_llm_document(doc)])


def bio_extract(request, field="person.education", name=NAME, index=2, **kwargs):
    fragment = request.documents[0].fragments[index]
    value = request.documents[0].text[fragment.start:fragment.end]
    response = {"facts": [{"subject": {"person_name": name}, "field": field, "value": value,
                           "fragment_ids": [fragment.fragment_id]}], "gaps": []}
    return asyncio.run(AssistantPipeline(FixtureProvider([response]), **kwargs).extract_facts(request))


def test_biography_keeps_literal_body_and_named_heading_in_evidence():
    request = biography_request()
    result = bio_extract(request)
    fact = result.facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert len(fact.evidence) == 2
    assert fact.evidence[1].quote == NAME
    assert fact.value.startswith("В 2001 году")
    for evidence in fact.evidence:
        assert evidence.quote == request.documents[0].text[evidence.start_offset:evidence.end_offset]


@pytest.mark.parametrize("field,name,index", [
    ("appointment.position", NAME, 1),  # A profile scope is not proof of a current role.
    ("person.education", "Сидоров Сидор Сидорович", 2),
    ("person.education", NAME, 6),  # A different person's heading ends the profile.
])
def test_biography_scope_cannot_change_person_or_make_current_role(field, name, index):
    result = bio_extract(biography_request(), field, name, index)
    assert result.facts[0].verification_status == "rejected"


def test_invalid_biography_anchor_is_rejected_before_llm():
    raw = biography_request().model_dump(mode="json")
    raw["documents"][0]["fragments"][2]["biography_subject"]["anchor_fragment_id"] = "missing"
    with pytest.raises(ValidationError):
        FragmentExtractionRequest.model_validate(raw)


def test_biography_selector_uses_exact_source_without_retyping_or_new_dates():
    request = biography_request()
    fragment = request.documents[0].fragments[2]
    response = {"facts": [{"subject": {"person_name": NAME}, "field": "person.education",
        "value_fragment_id": fragment.fragment_id, "fragment_ids": [fragment.fragment_id]}], "gaps": []}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request)).facts[0]
    assert fact.validation_status == "passed" and fact.value == request.documents[0].text[fragment.start:fragment.end]
    response["facts"][0]["field"] = "person.interests"
    fact = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(request)).facts[0]
    assert "research_interests_require_explicit_statement" in fact.issues


def test_historical_year_is_not_the_institution_founding_date():
    request = metric_request()
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.founded", "value": "2025", "fragment_ids": ["current"]}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request)).facts[0]
    assert "institution_founding_context_required" in fact.issues


def test_education_word_does_not_mean_institution_was_founded():
    request = metric_request()
    request.documents[0].text = request.documents[0].text.replace("численность", "образования")
    # Equal-length change preserves original source offsets.
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.founded", "value_fragment_id": "current", "fragment_ids": ["current"]}
    fact = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request)).facts[0]
    assert "institution_founding_context_required" in fact.issues


def metric_request():
    texts = ["Учебный университет. В 2025 году численность студентов очной формы головного вуза — 1200 человек.",
             "Другая статья. В 2024 году в филиале обучалось 300 человек."]
    return ContextExtractionRequest(institution=INSTITUTION, documents=[{
        "source_id": "stats", "url": "https://university.example/about", "text": "\n\n".join(texts),
        "retrieved_at": datetime.now(timezone.utc), "fragments": [
            {"fragment_id": "current", "start": 0, "end": len(texts[0])},
            {"fragment_id": "other", "start": len(texts[0]) + 2, "end": sum(map(len, texts)) + 2}]}])


def metric_extract(context=None, refs=None):
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"},
            "field": "institution.students", "value": "1200", "fragment_ids": refs or ["current"],
            "metric_context": context}
    return asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(metric_request()))


METRIC = {"period": "2025 году", "scope": "студентов очной формы головного вуза", "unit": "человек"}


def test_profile_dormitory_capacity_keeps_its_unit_and_period():
    request = metric_request()
    text = "Учебный университет. В 2026 году вместимость общежитий — 274 места."
    request.documents[0].text = text
    request.documents[0].fragments = [request.documents[0].fragments[0].model_copy(update={"end": len(text)})]
    request.fields = [ContextField.DORMITORY_CAPACITY, ContextField.PROJECT_NAME]
    payload = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет",
                           "entity_fragment_id": "current"},
               "field": "institution.dormitory_capacity", "value": "274", "value_fragment_id": "current",
               "fragment_ids": ["current"], "metric_context": {
                   "period": "2026 году", "scope": "вместимость общежитий", "unit": "места"}}
    result, = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [payload], "gaps": []}])).extract_facts(request)).facts
    assert result.verification_status == "pending" and result.metric_context.unit == "места"
    payload["metric_context"]["unit"] = "человек"
    result, = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [payload], "gaps": []}])).extract_facts(request)).facts
    assert "institution_metric_unit_mismatch" in result.issues


def test_qualified_metric_remains_candidate_and_preserves_population():
    fact = metric_extract(METRIC).facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert fact.metric_context.scope == METRIC["scope"]


def test_reference_selection_preserves_source_text_when_students_are_also_requested():
    request = metric_request()
    request.fields = [ContextField.PROFILE, ContextField.STUDENTS]
    subject = {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "current"}
    items = [
        {"subject": subject, "field": "institution.profile", "value": "Пересказ модели",
         "value_fragment_id": "current", "fragment_ids": ["current"]},
        {"subject": subject, "field": "institution.students", "value_fragment_id": "current",
         "fragment_ids": ["current"], "metric_context": METRIC},
    ]

    class Selector(FixtureProvider):
        async def complete(self, system, payload, schema):
            definition = schema["$defs"]["ContextFactProposal"]
            assert "value_fragment_id" in definition["required"]
            return await super().complete(system, payload, schema)

    result = asyncio.run(AssistantPipeline(Selector([{"facts": items, "gaps": []}])).extract_facts(request))
    profile, count = result.facts
    assert profile.validation_status == "passed" and profile.verification_status == "pending"
    assert profile.value == profile.evidence[0].quote and "Пересказ" not in profile.value
    assert count.verification_status == "rejected" and "student_metric_number_required" in count.issues


@pytest.mark.parametrize("text,passed", [
    ("Дата создания образовательной организации | 1\nсентября\n1987 г.", True),
    ("В 1930 году был создан Сибирский финансово-экономический институт.", True),
    ("Университет был основан в 1930 году.", True),
    ("В 1967 г. в институте было создано заочное отделение.", False),
    ("Дата создания филиала | 21 января 1988 г.", False),
])
def test_founding_date_requires_creation_of_the_institution(text, passed):
    doc = HTMLCleaner().to_document(f"<main><h1>Учебный университет</h1><p>{text}</p></main>",
                                    url="https://university.example/about", section="about")
    assert mark_institution_context(doc, "Учебный университет")
    title, value = doc.fragments
    request = ContextExtractionRequest(
        institution=INSTITUTION, documents=[to_llm_document(doc, include_contact_hints=False)],
    )
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет",
                        "entity_fragment_id": title.fragment_id},
            "field": "institution.founded", "value_fragment_id": value.fragment_id,
            "fragment_ids": [value.fragment_id]}
    result = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request))
    fact, = result.facts
    assert (fact.validation_status == "passed") is passed
    if not passed:
        assert "institution_founding_context_required" in fact.issues


@pytest.mark.parametrize("metric,refs,issue", [
    (None, None, "student_metric_context_required"),
    ({**METRIC, "period": "сегодня"}, None, "student_metric_period_required"),
    ({**METRIC, "scope": "всех студентов, включая филиалы"}, None, "student_metric_context_not_in_scope"),
    ({**METRIC, "period": "2024 году", "scope": "в филиале"}, ["current", "other"], "student_metric_context_not_in_scope"),
])
def test_unqualified_or_foreign_metric_cannot_be_accepted(metric, refs, issue):
    fact = metric_extract(metric, refs).facts[0]
    assert issue in fact.issues and fact.verification_status == "rejected"


def test_metric_presentation_has_period_population_and_sources(tmp_path):
    result = metric_extract(METRIC)
    fact = result.facts[0]
    now = datetime.now(timezone.utc)
    fact.verification_status, fact.reviewed_by = "verified", "synthetic-test-reviewer"
    fact.reviewed_at = fact.last_checked_at = now
    fact.freshness_status = "within_window"
    statement = {"text": "1200", "fact_ids": [fact.fact_id]}
    draft = DraftResult(institution=result.institution, run=result.run, meeting={}, warnings=[],
        brief_sections=[{"title": "Профиль", "statements": [statement]}],
        slides=[{"title": "Профиль", "theses": [statement]}], questions=[], sources={fact.fact_id: fact.evidence})
    pptx = Presentation(build_meeting_presentation(draft, tmp_path / "metric.pptx", [fact]))
    text = "\n".join(s.text for page in pptx.slides for s in page.shapes if s.has_text_frame)
    assert "Справка об учреждении" in text and "1200" in text
    assert METRIC["period"] in text and METRIC["scope"] in text
    assert "Не подтверждено" in text and "направления подготовки" in text
    for slide in pptx.slides:
        for shape in slide.shapes:
            assert shape.top + shape.height <= pptx.slide_height
    fact.metric_context = None
    with pytest.raises(DraftValidationError, match="student_metric_context_required"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).generate_materials(
            GenerationRequest(schema_version="0.3", institution=result.institution, facts=[fact])))


def test_reference_anchor_can_cross_batches_but_cannot_anchor_project_facts():
    doc = HTMLCleaner().to_document("<main><h1>Учебный университет</h1><p>" + "Справка. " * 40 +
        "</p><p>Бакалавриат и магистратура.</p></main>", url="https://university.example/about", section="about")
    assert mark_institution_context(doc, "Учебный университет")
    request = ContextExtractionRequest(institution=INSTITUTION, documents=[to_llm_document(doc, include_contact_hints=False)])
    fragment = request.documents[0].fragments[-1]
    item = {"subject": {"entity_kind": "institution", "entity_name": "Учебный университет", "entity_fragment_id": "f0001"},
            "field": "institution.education_levels", "value": "Бакалавриат и магистратура", "fragment_ids": [fragment.fragment_id]}
    pipeline = AssistantPipeline(FixtureProvider([{"facts": [], "gaps": []}, {"facts": [item], "gaps": []}]), chunk_size=400, overlap=0)
    result = asyncio.run(pipeline.extract_facts(request))
    assert result.complete and result.facts[0].validation_status == "passed"
    assert len(result.facts[0].evidence) == 2
    item["subject"]["entity_kind"], item["field"] = "project", "project.description"
    result = asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(request))
    assert "context_entity_value_scope_mismatch" in result.facts[0].issues
    doc.section = "news"
    assert not mark_institution_context(doc, "Учебный университет")
