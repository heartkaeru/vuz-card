from app.services.llm.contacts import contact_statement
from app.services.llm.models import Fact, ContactOwner, NonPersonSubject, Subject


def fact(kind="department", label="Учебная часть", field="contact.email", value="study@example.org"):
    return Fact(fact_id="f", institution_id="u", subject=NonPersonSubject(), field=field, value=value,
                contact_owner=ContactOwner(kind=kind, label=label), evidence=[],
                validation_status="passed", verification_status="pending")


def test_department_label_and_values_are_rendered_without_personal_assignment():
    facts = [fact(), fact(field="contact.phone", value="8(3955)-671830")]
    text = contact_statement(facts)
    assert text == "Контакты подразделения «Учебная часть»: электронная почта: study@example.org; телефон: 8(3955)-671830."


def test_mixed_person_and_contact_statements_are_left_for_manual_semantic_review():
    person = Fact(fact_id="person", institution_id="u", subject=Subject(person_name="Иванов Иван Иванович"),
                  field="person.name", value="Иванов Иван Иванович", evidence=[],
                  validation_status="passed", verification_status="pending")
    assert contact_statement([fact(), person]) is None


def test_multiple_owners_remain_separate_and_repeated_values_are_not_duplicated():
    office = fact("office", "Приёмная", "contact.phone", "+7 (3952) 111-22-33")
    text = contact_statement([fact(), fact(), office])
    assert text.count("study@example.org") == 1
    assert "Служебные контакты «Приёмная»" in text
