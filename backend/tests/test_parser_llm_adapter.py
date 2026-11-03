from datetime import datetime, timezone

from parser.llm_adapter import to_llm_request, to_officials
from parser.models import (
    ContactHint,
    ContactOwner,
    DocumentFragment,
    Leadership,
    SourceDocument,
    UniversityData,
    UniversityInfo,
)


def _card() -> UniversityData:
    text = "Иванов Иван Иванович\n\nТелефон: +7 (495) 123-45-67"
    document = SourceDocument(
        source_id="s1",
        url="https://example.ru/staff/ivanov/",
        title="Иванов",
        text=text,
        retrieved_at=datetime.now(timezone.utc),
        fragments=[
            DocumentFragment(
                fragment_id="f1",
                start=0,
                end=len(text),
                contact_hints=[
                    ContactHint(
                        field="contact.phone",
                        value="+7 (495) 123-45-67",
                        owner=ContactOwner(
                            kind="person", label="Иванов Иван Иванович"
                        ),
                    )
                ],
            )
        ],
    )
    return UniversityData(
        university=UniversityInfo(
            name="Тест",
            url="https://example.ru/",
            official_domain="example.ru",
        ),
        leadership=[Leadership(full_name="Иванов Иван Иванович", position="Ректор")],
        documents=[document],
    )


def test_fragment_request_contract() -> None:
    request = to_llm_request(_card(), institution_id="test")

    assert request["schema_version"] == "0.2"
    document = request["documents"][0]
    assert document["fragments"][0]["contact_hints"][0]["value"] == (
        "+7 (495) 123-45-67"
    )
    assert "entity_scope_id" not in document["fragments"][0]


def test_context_request_contract() -> None:
    request = to_llm_request(
        _card(), institution_id="test", schema_version="0.3"
    )

    fragment = request["documents"][0]["fragments"][0]
    assert "entity_scope_id" not in fragment
    assert "contact_hints" not in fragment


def test_officials_projection() -> None:
    officials = to_officials(_card(), institution_id="test")

    assert officials[0]["fio"] == "Иванов Иван Иванович"
    assert officials[0]["status"] == "ai_draft"
