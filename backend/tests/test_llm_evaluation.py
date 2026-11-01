from examples.llm.evaluate import score


def test_scoring_counts_wrong_owner_duplicates_and_missing_values():
    expected = [
        {"person_name": "Иванов Иван Иванович", "field": "person.name", "values": ["Иванов Иван Иванович"]},
        {"person_name": "Иванов Иван Иванович", "field": "appointment.position", "values": ["Ректор"]},
    ]
    fact = {"fact_id": "one", "field": "person.name", "value": "Иванов Иван Иванович",
            "subject": {"person_name": "Иванов Иван Иванович"}}
    result = score([fact, {**fact, "fact_id": "duplicate"},
                    {**fact, "fact_id": "wrong", "subject": {"person_name": "Петров Пётр Петрович"}}],
                   expected, ["person.name", "appointment.position"])
    assert (result["tp"], result["fp"], result["fn"]) == (1, 2, 1)
    assert result["duplicate_fact_ids"] == ["duplicate"]
    assert result["wrong_fact_ids"] == ["wrong"]


def test_scoped_contact_is_not_a_person_contact_even_with_same_value():
    owner = {"kind": "department", "label": "Учебная часть"}
    expected = [{"field": "contact.email", "values": ["study@example.org"], "owner": owner}]
    fact = {"fact_id": "one", "field": "contact.email", "value": "study@example.org",
            "subject": {"person_name": None}, "contact_owner": owner}
    assert score([fact], expected, ["contact.email"])["tp"] == 1
    fact["subject"]["person_name"] = "Иванов Иван Иванович"
    fact["contact_owner"] = {"kind": "person", "label": "Иванов Иван Иванович"}
    result = score([fact], expected, ["contact.email"])
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 1)


def test_phone_scoring_recognizes_russian_country_prefix_without_double_counting():
    owner = {"kind": "office", "label": "Приёмная"}
    expected = [{"field": "contact.phone", "values": ["+7 (3952) 111-22-33"], "owner": owner}]
    fact = {"fact_id": "one", "field": "contact.phone", "value": "8 3952 111-22-33",
            "subject": {"person_name": None}, "contact_owner": owner}
    result = score([fact, {**fact, "fact_id": "duplicate", "value": "+7 (3952) 111-22-33"}],
                   expected, ["contact.phone"])
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 0)
