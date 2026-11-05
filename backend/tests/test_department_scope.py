import asyncio

import pytest
from pydantic import ValidationError

from app.services.llm.client import FixtureProvider
from app.services.llm.pipeline import AssistantPipeline
from test_contact_profile_scope import document, request, hints


def branch_document():
    return document('<div itemprop="rucovodstvoFil"><p itemprop="nameFil">Филиал в городе N</p>'
                    '<p itemprop="fio">Иванов Иван Иванович</p><p itemprop="post">Директор филиала</p></div>')


def extract_branch(department=None):
    req = request(branch_document())
    item = {"subject": {"person_name": "Иванов Иван Иванович", "department": department},
            "field": "appointment.position", "value": "Директор филиала", "fragment_ids": ["f0001"]}
    return asyncio.run(AssistantPipeline(FixtureProvider([{"facts": [item], "gaps": []}])).extract_facts(req))


def test_branch_scope_is_preserved_even_if_model_omits_it():
    result = extract_branch()
    assert len(result.facts) == 2  # role plus derived atomic name
    assert all(f.subject.department == "Филиал в городе N" and f.verification_status == "pending" for f in result.facts)


def test_model_cannot_reassign_a_branch_director_to_another_unit():
    result = extract_branch("Головной вуз")
    assert result.facts[0].verification_status == "rejected"
    assert "subject_department_scope_mismatch" in result.facts[0].issues


def test_scope_must_be_grounded_before_the_model_is_called():
    req = request(branch_document())
    req.documents[0].fragments[0].subject_department = "Другой филиал"
    with pytest.raises(ValidationError, match="Subject department is absent"):
        asyncio.run(AssistantPipeline(FixtureProvider([])).extract_facts(req))


def test_plain_person_microdata_table_preserves_working_phone_and_annotation_boundary():
    doc = document('<table><tr><th>Ф.И.О.</th><th>Телефон</th></tr><tr itemprop="rucovodstvo">'
                   '<td itemprop="fio">Иванов Иван Иванович</td>'
                   '<td itemprop="telephone">8 (3953) 344-011 (телефон/факс)</td></tr></table>')
    assert hints(doc)[0].value == "8 (3953) 344-011"
    assert hints(doc)[0].owner.kind == "person"
    request(doc)


def test_unit_rowspan_contacts_do_not_become_personal_or_leak_into_next_unit():
    doc = document('<table><tr><th>Наименование структурного подразделения</th>'
        '<th>ФИО, должность</th><th>Адреса электронной почты подразделений</th>'
        '<th>Номера телефонов справочных служб организации</th><th>Положения</th></tr>'
        '<tr><td rowspan="2">Учебная часть</td><td>Иванов Иван Иванович</td><td>one@example.org</td>'
        '<td>8 (3953) 111-22-33</td><td>Документ подписан: Директор Петров Пётр Петрович</td></tr>'
        '<tr><td>Сидоров Семён Семёнович</td><td>two@example.org</td><td>8 (3953) 111-22-34</td><td>—</td></tr>'
        '<tr><td>Бухгалтерия</td><td>Петров Пётр Петрович</td><td>three@example.org</td>'
        '<td>8 (3953) 111-22-35</td><td>—</td></tr></table>')
    assert [(h.value, h.owner.kind, h.owner.label) for h in hints(doc)] == [
        ("one@example.org", "department", "Учебная часть"),
        ("two@example.org", "department", "Учебная часть"),
        ("three@example.org", "department", "Бухгалтерия")]
    assert [f.subject_department for f in doc.fragments[1:]] == ["Учебная часть", "Учебная часть", "Бухгалтерия"]
    assert "Документ подписан" not in doc.text
    request(doc)


def test_ambiguous_colspan_falls_back_without_inventing_unit_contact_scope():
    doc = document('<h2>Руководство</h2><table><tr><th>Наименование структурного подразделения</th>'
        '<th>Электронная почта подразделения</th></tr><tr><td colspan="2">Общие сведения</td></tr>'
        '<tr><td>Учебная часть</td><td>unit@example.org</td></tr></table>')
    assert not hints(doc)
    assert "unit@example.org" in doc.text
    request(doc)
