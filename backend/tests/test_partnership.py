import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.services.llm.client import LLMError
from app.services.llm.models import EntitySubject, Evidence, Fact, FactField, MeetingContext
from app.services.llm.partnership import OPERATIONAL_TOPICS, match_partnership


class MatchProvider:
    def __init__(self, catalog_id=None, basis=None):
        self.catalog_id = catalog_id
        self.basis = basis
        self.catalog = {}

    async def complete(self, system, payload, schema):
        self.catalog = {entry["id"]: entry for entry in payload["catalog"]}
        allowed = schema["$defs"]["MatchProposal"]["properties"]["catalog_id"]["enum"]
        assert set(allowed) == self.catalog.keys()
        proposal = {
            "catalog_id": self.catalog_id, "basis_fact_ids": self.basis,
            "proposal": "Обсудить возможность содействия проекту.",
            "rationale": "Вуз уже реализует проект.",
            "discussion_question": "Какой формат подходит вузу?",
        }
        return json.dumps({"matches": [proposal] if self.catalog_id else []})


def fact(fact_id, field, value, quote=None):
    return Fact(
        fact_id=fact_id, institution_id="university",
        subject=EntitySubject(entity_id="university", entity_kind="institution", entity_name="Учебный университет"),
        field=field, value=value, validation_status="passed", verification_status="verified",
        evidence=[Evidence(
            source_id="source", quote=quote or value, url="https://university.example/project",
            retrieved_at=datetime(2026, 11, 26, tzinfo=timezone.utc), published_at=None,
            content_sha256="0" * 64, chunk_id="c1",
        )],
    )


@pytest.mark.parametrize("field,value,quote", [
    (FactField.STRUCTURE, "В 1980 году институт насчитывал 26 кафедр и 4 факультета.", None),
    (FactField.PROFILE, "Университет ведёт подготовку по специальности лечебное дело.", None),
    (FactField.PROFILE, "Вуз планирует медицинское направление и набор сотрудников на кафедру.", None),
    (FactField.PROJECT_DESCRIPTION, "Вуз планирует повысить заработную плату сотрудникам.", None),
    (FactField.PROJECT_DESCRIPTION, "Вуз не планирует обновление пропускной системы.", None),
    (FactField.PROJECT_DESCRIPTION, "Вуз завершил модернизацию пропускной системы.", None),
    (FactField.PROJECT_DESCRIPTION, "Вуз внедряет электронные пропуска.", "Вуз открыл новый корпус."),
])
def test_unrelated_or_inactive_facts_do_not_offer_operational_products(field, value, quote):
    provider = MatchProvider()
    meeting = MeetingContext(goal="Модернизировать пропуска и добавить медподдержку сотрудникам")

    matches = asyncio.run(match_partnership(provider, [fact("first", field, value, quote)], meeting))

    assert matches == []
    assert {"school-21", "sber-student-internships"} <= provider.catalog.keys()
    assert not OPERATIONAL_TOPICS.keys() & provider.catalog.keys()


@pytest.mark.parametrize("catalog_id,value", [
    ("sber-campus-card", "Вуз объявил проект модернизации системы пропусков в корпусах."),
    ("sber-payroll", "Бухгалтерия автоматизирует передачу электронных реестров для выплат сотрудникам."),
    ("sber-health-corporate", "Вуз планирует организовать медицинские консультации для сотрудников."),
])
def test_matching_product_is_allowed_only_with_its_own_fact(catalog_id, value):
    initiative = fact("initiative", FactField.PROJECT_DESCRIPTION, value)
    general = fact("general", FactField.STRUCTURE, "Вуз включает три корпуса и два филиала.")
    provider = MatchProvider(catalog_id, [initiative.fact_id])
    meeting = MeetingContext()

    result, = asyncio.run(match_partnership(provider, [initiative, general], meeting))

    assert result.basis_fact_ids == [initiative.fact_id]
    assert set(OPERATIONAL_TOPICS) & provider.catalog.keys() == {catalog_id}
    assert provider.catalog[catalog_id]["required_basis_fact_ids"] == [initiative.fact_id]
    assert result.rationale == (value if catalog_id != "sber-payroll" else "Вуз уже реализует проект.")

    provider.basis = [general.fact_id]
    with pytest.raises(LLMError, match="invalid_partnership_response"):
        asyncio.run(match_partnership(provider, [initiative, general], meeting))
