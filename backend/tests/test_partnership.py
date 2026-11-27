import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from app.services.llm.client import LLMError
from app.services.llm.models import EntitySubject, Evidence, Fact, FactField, MeetingContext
from app.services.llm.partnership import OPERATIONAL_TOPICS, match_partnership


class MatchProvider:
    def __init__(self, catalog_id=None, basis=None, regional_case_id=None):
        self.catalog_id = catalog_id
        self.basis = basis
        self.catalog = {}
        self.regional_case_id = regional_case_id

    async def complete(self, system, payload, schema):
        self.catalog = {entry["id"]: entry for entry in payload["catalog"]}
        allowed = schema["$defs"]["MatchProposal"]["properties"]["catalog_id"]["enum"]
        assert set(allowed) == self.catalog.keys()
        proposal = {
            "catalog_id": self.catalog_id, "basis_fact_ids": self.basis,
            "proposal": "Обсудить возможность содействия проекту.",
            "rationale": "Вуз уже реализует проект.",
            "discussion_question": "Какой формат подходит вузу?",
            "regional_case_id": self.regional_case_id,
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


def test_event_requires_a_relevant_fact_and_a_future_date():
    event = "sber-ai-transformation-2026-12"
    topic = fact("specific", FactField.PROJECT_DESCRIPTION, "Вуз внедряет искусственный интеллект в работу преподавателей.")
    program = fact("program", FactField.EDUCATION_AREAS, "Прикладная информатика")
    provider = MatchProvider(event, [topic.fact_id])
    today = date(2026, 11, 27)
    result, = asyncio.run(match_partnership(provider, [topic, program], MeetingContext(), today=today))
    assert result.event_start == date(2026, 12, 2)
    provider.catalog_id = None
    for facts, meeting in (([program], MeetingContext()), ([topic], MeetingContext(date="2026-12-02"))):
        assert not asyncio.run(match_partnership(provider, facts, meeting, today=today))
        assert event not in provider.catalog


def test_regional_case_must_belong_to_the_selected_product():
    program = fact("program", FactField.EDUCATION_AREAS, "Цифровые технологии в клинической практике")
    provider = MatchProvider("school-21", [program.fact_id], "saratov-medical-school-21")
    result, = asyncio.run(match_partnership(provider, [program], MeetingContext()))
    assert result.regional_case.location == "Саратов"
    assert result.regional_case.url.host == "21-school.ru"
    provider.regional_case_id = "siberia-university-workshops"
    with pytest.raises(LLMError, match="invalid_partnership_response"):
        asyncio.run(match_partnership(provider, [program], MeetingContext()))


def test_banking_practice_needs_a_matching_spo_program():
    banking = "sber-spo-banking-practice"
    program = fact("banking", FactField.EDUCATION_AREAS, "38.02.07 Банковское дело")
    provider = MatchProvider(banking, [program.fact_id], "novosibirsk-banking-spo")
    result, = asyncio.run(match_partnership(provider, [program], MeetingContext()))
    assert result.regional_case.location == "Новосибирск"
    provider.catalog_id = None
    unrelated = fact("other", FactField.EDUCATION_AREAS, "Экономика предприятия")
    assert not asyncio.run(match_partnership(provider, [unrelated], MeetingContext()))
    assert banking not in provider.catalog


@pytest.mark.parametrize("retry", [False, True])
def test_invalid_proposal_does_not_discard_a_verified_match(retry):
    program = fact("program", FactField.EDUCATION_AREAS, "Информатика и вычислительная техника")

    class MixedProvider(MatchProvider):
        calls = 0

        async def complete(self, system, payload, schema):
            self.calls += 1
            valid, = json.loads(await super().complete(system, payload, schema))["matches"]
            wrong_reference = {**valid, "basis_fact_ids": ["unknown"]}
            wrong_shape = {**valid, "rationale": ""}
            if self.calls == 1:
                matches = [wrong_reference, wrong_shape]
                if not retry:
                    matches.append(valid)
                return json.dumps({"matches": matches})
            assert "Предыдущий ответ не прошёл проверку" in system
            return json.dumps({"matches": [valid]})

    provider = MixedProvider("sber-student-internships", [program.fact_id])
    result, = asyncio.run(match_partnership(provider, [program], MeetingContext()))
    assert result.basis_fact_ids == [program.fact_id]
    assert provider.calls == (2 if retry else 1)
