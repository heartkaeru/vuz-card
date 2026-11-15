import asyncio

import pytest

from app.services.llm.client import FixtureProvider
from app.services.llm.fragment_models import FragmentExtractionRequest
from app.services.llm.pipeline import AssistantPipeline
from parser.cleaner import HTMLCleaner
from parser.llm_adapter import to_llm_request
from parser.models import UniversityData, UniversityInfo


def document(body):
    return HTMLCleaner().to_document(f"<main>{body}</main>", url="https://university.example/managers",
                                     section="leadership")


def request(doc):
    card = UniversityData(university=UniversityInfo(name="Учебный вуз", url="https://university.example"), documents=[doc])
    return FragmentExtractionRequest.model_validate(to_llm_request(card, institution_id="draft:test"))


def profile(name="Иванов Иван Иванович", email="ivanov@example.org", extra="", container="li", prop="rucovodstvo"):
    return (f'<{container} itemprop="{prop}"><p><span itemprop="fio">{name}</span></p>'
            f'<p><span itemprop="post">Ректор</span></p><p>Email: <span itemprop="email">{email}</span></p>'
            f'{extra}</{container}>')


def hints(doc):
    return [hint for fragment in doc.fragments for hint in fragment.contact_hints]


def test_explicit_profiles_override_generic_heading_without_cross_person_contacts():
    doc = document('<h2>Руководитель и заместители руководителя</h2>' + profile() +
                   profile("Петров Пётр Петрович", "petrov@example.org", prop="rucovodstvoZam"))
    req = request(doc)
    assert [(h.value, h.owner.kind, h.owner.label) for h in hints(doc)] == [
        ("ivanov@example.org", "person", "Иванов Иван Иванович"),
        ("petrov@example.org", "person", "Петров Пётр Петрович")]
    for f in doc.fragments:
        if f.contact_hints:
            assert f.contact_hints[0].owner.label in doc.text[f.start:f.end]
    assert len(req.documents[0].fragments) == 3


@pytest.mark.parametrize("body", [
    '<h2>Руководство</h2><p>Иванов Иван Иванович. ivanov@example.org</p>',
    profile(extra='<span itemprop="fio">Петров Пётр Петрович</span>'),
    profile(container="tr"),
])
def test_ambiguous_or_table_scopes_do_not_invent_personal_ownership(body):
    doc = document(body)
    assert not hints(doc)
    assert "ivanov@example.org" in doc.text
    request(doc)


def test_reception_inside_person_profile_is_not_a_personal_contact():
    doc = document(profile(extra='<p>Приёмная ректора: <span itemprop="telephone">+7 (495) 111-22-33</span></p>'))
    assert [h.field for h in hints(doc)] == ["contact.email"]
    assert "+7 (495) 111-22-33" in doc.text


def test_branch_has_its_own_complete_fragment_and_organization_contact():
    doc = document('<h2>Руководители филиалов</h2><div itemprop="rucovodstvoFil">'
                   '<p><span itemprop="nameFil">Филиал в городе N</span></p>'
                   '<p><span itemprop="fio">Иванов Иван Иванович</span></p>'
                   '<p><span itemprop="post">Директор филиала</span></p>'
                   '<p><span itemprop="email">branch@example.org</span></p></div>')
    assert hints(doc)[0].owner.model_dump() == {"kind": "department", "label": "Филиал в городе N"}
    f = next(f for f in doc.fragments if f.contact_hints)
    assert all(value in doc.text[f.start:f.end] for value in ("Директор филиала", "Иванов Иван Иванович"))
    request(doc)


def test_extension_is_preserved_and_correct_owner_candidate_stays_pending():
    phone = "+7(3952)50-00-08, вн.тел. 102"
    doc = document(profile(extra=f'<p><span itemprop="telephone">{phone}</span></p>'))
    req = request(doc)
    hint = next(h for h in hints(doc) if h.field == "contact.phone")
    assert hint.value == phone
    fragment = next(f for f in doc.fragments if f.contact_hints)
    response = {"facts": [{"subject": {"person_name": "Иванов Иван Иванович"},
        "field": hint.field, "value": hint.value, "contact_owner": hint.owner.model_dump(),
        "fragment_ids": [fragment.fragment_id]}], "gaps": []}
    result = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(req))
    fact = result.facts[0]
    assert fact.validation_status == "passed" and fact.verification_status == "pending"
    assert fact.evidence[0].quote == doc.text[fragment.start:fragment.end]


def test_nested_profiles_do_not_transfer_contact_between_owners():
    inner = profile("Петров Пётр Петрович", "petrov@example.org", container="div", prop="rucovodstvoZam")
    doc = document(profile(extra=inner))
    assert [(h.value, h.owner.label) for h in hints(doc)] == [
        ("ivanov@example.org", "Иванов Иван Иванович"), ("petrov@example.org", "Петров Пётр Петрович")]
    for f in doc.fragments:
        assert not all(name in doc.text[f.start:f.end] for name in ("Иванов Иван Иванович", "Петров Пётр Петрович"))
    request(doc)


def test_shared_email_is_not_projected_and_explicit_deputy_keeps_local_phone():
    doc = document(
        profile(email="common@example.org", extra='<span itemprop="telephone">552-07-26</span>') +
        '<div itemprop="teachingStaff"><span itemprop="fio">Петров Пётр Петрович</span>'
        '<span itemprop="post">Зам.директора по учебной работе</span>'
        '<span itemprop="telephone">297-10-35</span><span itemprop="email">COMMON@example.org</span></div>'
    )
    req = request(doc)
    req.project_structured_contacts = True
    assert [(h.field, h.value, h.owner.label) for h in hints(doc)] == [
        ("contact.phone", "552-07-26", "Иванов Иван Иванович"),
        ("contact.phone", "297-10-35", "Петров Пётр Петрович"),
    ]
    assert "common@example.org" in doc.text
    response = {"facts": [{
        "subject": {"person_name": "Иванов Иван Иванович"}, "field": "contact.email",
        "value": "common@example.org", "fragment_ids": [doc.fragments[0].fragment_id],
        "contact_owner": {"kind": "person", "label": "Иванов Иван Иванович"},
    }], "gaps": []}
    result = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(req))
    email, = [f for f in result.facts if f.field.value == "contact.email"]
    assert email.issues == ["contact_owner_requires_source_scope"]
    assert [f.value for f in result.facts if f.field.value == "contact.phone"] == ["552-07-26", "297-10-35"]


def test_institution_email_in_footer_is_not_assigned_to_single_leader():
    doc = HTMLCleaner().to_document(
        "<main>" + profile(email="common@example.org") + "</main>"
        '<footer><a href="mailto:common@example.org">Почта колледжа</a></footer>',
        url="https://university.example/managers", section="leadership",
    )
    assert not hints(doc)
    request(doc)


def test_repeated_main_phone_is_not_personal_but_extensions_are_distinct():
    def phone_profile(name, email, phone):
        return profile(name, email, extra=f'<span itemprop="telephone">{phone}</span>')

    doc = document("".join([
        phone_profile("Иванов Иван Иванович", "ivanov@example.org", "+7 (495) 111-22-33"),
        phone_profile("Петров Пётр Петрович", "petrov@example.org", "8 (495) 111-22-33"),
        phone_profile("Сидоров Сидор Сидорович", "sidorov@example.org", "+7 (495) 111-22-33 доб. 12"),
    ]))
    assert [h.value for h in hints(doc) if h.field == "contact.phone"] == ["+7 (495) 111-22-33 доб. 12"]
    request(doc)


def test_structured_position_recovers_paraphrase_from_explicit_profile_only_once():
    doc = document(profile().replace("Ректор", "И. о. ректора"))
    fragment = doc.fragments[0]
    req = request(doc)
    response = {"facts": [{
        "subject": {"person_name": "Иванов Иван Иванович"}, "field": "appointment.position",
        "value": "Исполняющий обязанности ректора", "fragment_ids": [fragment.fragment_id],
    }], "gaps": []}
    result = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(req))
    valid = [f for f in result.facts if f.field.value == "appointment.position" and f.validation_status == "passed"]
    assert [(f.value, f.origin, f.verification_status) for f in valid] == [
        ("И. о. ректора", "parser_projection", "pending"),
    ]
    assert valid[0].evidence[0].quote == doc.text
    response["facts"][0]["value"] = "И. о. ректора"
    result = asyncio.run(AssistantPipeline(FixtureProvider([response])).extract_facts(req))
    assert [(f.value, f.origin) for f in result.facts if f.field.value == "appointment.position"] == [
        ("И. о. ректора", "llm"),
    ]
