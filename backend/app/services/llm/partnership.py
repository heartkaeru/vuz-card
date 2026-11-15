"""Propose meeting opportunities from the reviewed card and a fixed catalog."""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, field_validator

from .changes import effective_facts
from .client import JSONProvider, LLMError
from .models import Fact, FactField, MeetingContext, StrictModel
from .workflow_models import ResearchRun


CatalogKind = Literal["sber_product", "external_course"]


class MatchArgument(StrictModel):
    proposal: str = Field(min_length=1, max_length=220)
    rationale: str = Field(min_length=1, max_length=220)
    discussion_question: str = Field(min_length=1, max_length=180)
    basis_fact_ids: list[str] = Field(min_length=1, max_length=10)


class PartnershipMatch(MatchArgument):
    product_name: str = Field(min_length=1, max_length=100)
    catalog_url: HttpUrl
    kind: CatalogKind = "sber_product"

    @field_validator("catalog_url")
    @classmethod
    def catalog_must_use_https(cls, value):
        if value.scheme != "https":
            raise ValueError("Catalog URLs must use HTTPS")
        return value


class CatalogEntry(StrictModel):
    id: str
    kind: CatalogKind
    name: str
    audience: str
    terms: str
    url: HttpUrl
    matching_guidance: str = ""
    available_for_matching: bool = True
    requires_operational_context: bool = False
    checked_on: date = date(2026, 11, 13)


CATALOG = {entry.id: entry for entry in (
    CatalogEntry(
        id="sber-university-academic-center", kind="sber_product",
        name="Академический центр СберУниверситета",
        audience="Преподаватели, руководители и команды развития университетов.",
        terms="Открытые и адаптированные программы. Тему, формат и участие группы нужно обсудить с центром.",
        url="https://sberuniversity.ru/learning/academic-center/",
    ),
    CatalogEntry(
        id="school-21", kind="sber_product", name="Школа 21",
        audience="Совершеннолетние участники, заинтересованные в практическом обучении цифровым технологиям.",
        terms="Бесплатное обучение с отбором. Участникам нужно выбрать кампус и выделять время на обучение.",
        url="https://21-school.ru/admission",
    ),
    CatalogEntry(
        id="sber-student-internships", kind="sber_product", name="СберСтудент · стажировки в Сбере",
        audience="Студенты очной формы российского вуза, начиная со второго курса бакалавриата или специалитета, а также магистранты и аспиранты.",
        terms="Зависят от города, доступных направлений и отбора. Работа в офисе или в гибридном формате.",
        url="https://student.sber.ru/internship",
    ),
    CatalogEntry(
        id="netology-data-viz", kind="external_course", name="Нетология · Основы визуализации данных",
        audience="Начинающие, которым нужна визуализация данных для рабочих или учебных задач.",
        terms="Вводный онлайн-курс по визуализации данных. Условия доступа и стоимость нужно уточнить у провайдера.",
        url="https://netology.ru/programs/data-viz",
    ),
    CatalogEntry(
        id="sber-campus-card", kind="sber_product", name="Кампусная карта Сбера",
        audience="Студенты и команды вузов, отвечающие за пропускную систему и сервисы кампуса.",
        terms="В Сеченовском Университете карта совмещает банковскую карту и пропуск. "
              "Для другого вуза нужно уточнить нужные функции, совместимость и условия подключения.",
        matching_guidance="Ищи подтверждённую потребность в пропусках, идентификации или обновлении сервисов кампуса. "
                          "Численность студентов и наличие корпусов сами по себе недостаточны.",
        url="https://welcome.sechenov.ru/education/campus-map/",
        requires_operational_context=True,
        checked_on=date(2026, 11, 14),
    ),
    CatalogEntry(
        id="sber-payroll", kind="sber_product", name="Зарплатный проект Сбера",
        audience="Работодатели и бухгалтерии, которые организуют выплаты сотрудникам.",
        terms="Сбер описывает зачисление зарплаты, работу с электронными реестрами и интеграцию. "
              "Уточнить текущую схему выплат, способ подключения и применимые условия договора.",
        matching_guidance="Ищи подтверждённую задачу по выплатам, обработке реестров или автоматизации расчётов. "
                          "Масштаб учреждения и число сотрудников сами по себе недостаточны.",
        url="https://www.sberbank.ru/ru/legal/bankingservice/cards/salaryproject?tab=rates",
        requires_operational_context=True,
        checked_on=date(2026, 11, 14),
    ),
    CatalogEntry(
        id="sber-health-corporate", kind="sber_product", name="СберЗдоровье · корпоративные медицинские программы",
        audience="Работодатели, которые организуют медицинскую поддержку сотрудников.",
        terms="Провайдер описывает корпоративные медицинские программы. "
              "Состав пакета, географию, категории сотрудников и условия подключения нужно уточнить.",
        matching_guidance="Ищи подтверждённую инициативу по здоровью или соцпакету сотрудников. "
                          "Медицинское направление обучения и наличие студентов сами по себе недостаточны.",
        url="https://docdoc.ru/llm-info",
        requires_operational_context=True,
        checked_on=date(2026, 11, 14),
    ),
    CatalogEntry(
        id="edutoria", kind="sber_product", name="Академия Edutoria",
        audience="Вузы, заинтересованные в совместных онлайн-программах магистратуры или ДПО.",
        terms="Официальный кейс описывает совместные программы. Актуальный набор и формат подключения требуют подтверждения.",
        url="https://developers.sber.ru/portal/cases/edutoria",
        available_for_matching=False,
        checked_on=date(2026, 11, 14),
    ),
)}
MATCH_CATALOG = {key: entry for key, entry in CATALOG.items() if entry.available_for_matching}

MATCH_FIELDS = {
    FactField.PROFILE, FactField.TYPE, FactField.LOCATION, FactField.EDUCATION_AREAS,
    FactField.EDUCATION_LEVELS, FactField.STUDENTS, FactField.STRUCTURE,
    FactField.PROJECT_NAME, FactField.PROJECT_PROGRAM, FactField.PROJECT_ROLE,
    FactField.PROJECT_STATUS, FactField.PROJECT_PERIOD, FactField.PROJECT_DESCRIPTION,
    FactField.EVENT_NAME, FactField.EVENT_TYPE, FactField.EVENT_DATE,
    FactField.EVENT_STATUS, FactField.EVENT_DESCRIPTION,
}
OPERATIONAL_FIELDS = {
    FactField.PROFILE, FactField.STRUCTURE, FactField.PROJECT_NAME,
    FactField.PROJECT_DESCRIPTION, FactField.EVENT_NAME, FactField.EVENT_DESCRIPTION,
}
PROMPT = (Path(__file__).parent / "prompts" / "match_partnership.txt").read_text(encoding="utf-8")


class MatchProposal(MatchArgument):
    catalog_id: str = Field(json_schema_extra={"enum": list(MATCH_CATALOG)})


class MatchOutput(StrictModel):
    matches: list[MatchProposal] = Field(max_length=3)


def partnership_facts(run: ResearchRun) -> list[Fact]:
    return [
        fact for fact in effective_facts(run)
        if fact.institution_id == run.institution.id and fact.field in MATCH_FIELDS and fact.evidence
    ]


async def match_partnership(
    provider: JSONProvider, facts: list[Fact], meeting: MeetingContext
) -> list[PartnershipMatch]:
    if not facts:
        return []
    operational_ids = {fact.fact_id for fact in facts if fact.field in OPERATIONAL_FIELDS}
    catalog = {
        key: entry for key, entry in MATCH_CATALOG.items()
        if operational_ids or not entry.requires_operational_context
    }
    schema = MatchOutput.model_json_schema()
    schema["$defs"]["MatchProposal"]["properties"]["catalog_id"]["enum"] = list(catalog)
    raw = await provider.complete(PROMPT, {
        "meeting": meeting.model_dump(mode="json"),
        "catalog": [entry.model_dump(mode="json", exclude={"available_for_matching", "requires_operational_context"})
                    for entry in catalog.values()],
        "facts": [{
            "fact_id": fact.fact_id,
            "field": fact.field.value,
            "subject": getattr(fact.subject, "entity_name", None),
            "value": fact.value,
            "metric_context": fact.metric_context.model_dump(mode="json") if fact.metric_context else None,
            "published_at": max((proof.published_at.isoformat() for proof in fact.evidence if proof.published_at),
                                default=None),
        } for fact in facts],
    }, schema)
    try:
        output = MatchOutput.model_validate_json(raw)
    except ValidationError:
        raise LLMError("invalid_partnership_response") from None

    fact_ids = {fact.fact_id for fact in facts}
    seen = set()
    matches = []
    for proposal in output.matches:
        catalog_id = proposal.catalog_id
        basis = set(proposal.basis_fact_ids)
        if catalog_id not in catalog or catalog_id in seen or not basis <= fact_ids:
            raise LLMError("invalid_partnership_response")
        seen.add(catalog_id)
        entry = catalog[catalog_id]
        if entry.requires_operational_context and not basis & operational_ids:
            raise LLMError("invalid_partnership_response")
        matches.append(PartnershipMatch(
            **proposal.model_dump(exclude={"catalog_id", "basis_fact_ids"}),
            basis_fact_ids=list(dict.fromkeys(proposal.basis_fact_ids)),
            product_name=entry.name, catalog_url=entry.url, kind=entry.kind,
        ))
    return matches
