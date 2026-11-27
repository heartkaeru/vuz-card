"""Propose meeting opportunities from the reviewed card and a fixed catalog."""

import json
import re
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, ValidationError, field_validator

from .changes import effective_facts, normalized
from .client import JSONProvider, LLMError
from .evidence import literal_value_in_text
from .models import Fact, FactField, MeetingContext, StrictModel
from .workflow_models import ResearchRun


CatalogKind = Literal["sber_product", "external_course"]
MAX_MATCHES = 3


class MatchArgument(StrictModel):
    proposal: str = Field(min_length=1, max_length=220)
    rationale: str = Field(min_length=1, max_length=220)
    discussion_question: str = Field(min_length=1, max_length=180)
    basis_fact_ids: list[str] = Field(min_length=1, max_length=10)


class RegionalCase(StrictModel):
    title: str
    location: str
    url: HttpUrl


class PartnershipMatch(MatchArgument):
    product_name: str = Field(min_length=1, max_length=100)
    catalog_url: HttpUrl
    kind: CatalogKind = "sber_product"
    regional_case: RegionalCase | None = None
    event_start: date | None = None

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
    event_start: date | None = None


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
        id="sber-spo-banking-practice", kind="sber_product", name="Сбер · практическая подготовка по банковскому делу",
        audience="Колледжи и подразделения СПО с программой «Банковское дело».",
        terms="Возможный формат для переговоров с территориальной командой Сбера. "
              "Учебная площадка, места практики и формат зависят от отдельной договорённости.",
        url="https://www.sberbank.ru/ru/sberpress/all/article?blockID=1303&lang=ru&newsID=b949a60a-e1ec-4741-a180-bf49b02c479d&regionID=77&type=NEWS",
        checked_on=date(2026, 11, 27),
    ),
    CatalogEntry(
        id="sber-ai-transformation-2026-12", kind="sber_product",
        name="СберУниверситет · ИИ-трансформация университетов",
        audience="Руководители и команды вузов, внедряющие ИИ в образовательные или управленческие процессы.",
        terms="Интенсив 2–3 декабря 2026 года. Возможность участия и регистрации уточнить у организатора.",
        url="https://sberuniversity.ru/learning/program/ai-transformatsiya-universitetov/",
        matching_guidance="Выбирай только при подтверждённой задаче применения ИИ в работе университета "
                          "или развитии компетенций преподавателей и руководителей. Общего ИТ-профиля мало.",
        event_start=date(2026, 12, 2), checked_on=date(2026, 11, 27),
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
REGIONAL_CASES = {
    "novosibirsk-banking-spo": ("sber-spo-banking-practice", RegionalCase(
        title="Совместная программа по финансам для студентов СПО НГУЭУ",
        location="Новосибирск",
        url="https://bi.nsuem.ru/employee/news-and-classifieds/detail.php?ELEMENT_ID=147740",
    )),
    "saratov-medical-school-21": ("school-21", RegionalCase(
        title="Соглашение о цифровой подготовке студентов медицинского университета",
        location="Саратов",
        url="https://21-school.ru/news/vraci-pisut-kod-skola-cifrovyx-texnologii-poiavitsia-v-saratove",
    )),
    "siberia-university-workshops": ("sber-university-academic-center", RegionalCase(
        title="Практикумы по ИИ для руководителей университетов Сибири",
        location="Сибирь",
        url="https://sberuniversity.ru/news/Sber-i-universitety-Sibiri-obedinili-ekspertizu-dlya-poiska-otvetov-na-vyzovy-vysshego-obrazovaniya/",
    )),
}
EVENT_TOPIC = re.compile(r"\b(?:искусственн\w* интеллект\w*|ии|нейросет\w*)\b")
EVENT_NEED = re.compile(r"\b(?:преподавател\w*|руководител\w*|управлен\w*|внедр\w*|повышен\w* квалификац\w*)")
SPO_LEVEL = re.compile(r"\bспо\b|средне\w* профессиональн\w* образован\w*|\bколледж\w*\b")
SPO_BANKING = re.compile(r"\b38\.02\.07\b|\bбанковск\w* дел\w*\b")

MATCH_FIELDS = {
    FactField.PROFILE, FactField.TYPE, FactField.LOCATION, FactField.EDUCATION_AREAS,
    FactField.EDUCATION_LEVELS, FactField.STUDENTS, FactField.STRUCTURE,
    FactField.PROJECT_NAME, FactField.PROJECT_PROGRAM, FactField.PROJECT_ROLE,
    FactField.PROJECT_PARTNER, FactField.PROJECT_STATUS, FactField.PROJECT_PERIOD, FactField.PROJECT_DESCRIPTION,
    FactField.EVENT_NAME, FactField.EVENT_TYPE, FactField.EVENT_DATE,
    FactField.EVENT_STATUS, FactField.EVENT_DESCRIPTION,
}
OPERATIONAL_FIELDS = {
    FactField.PROFILE, FactField.STRUCTURE, FactField.PROJECT_NAME,
    FactField.PROJECT_DESCRIPTION, FactField.EVENT_NAME, FactField.EVENT_DESCRIPTION,
}
OPERATIONAL_TOPICS = {
    "sber-campus-card": (
        re.compile(r"\b(?:кампусн\w* карт\w*|скуд|пропускн\w* (?:систем\w*|режим\w*)|"
                   r"систем\w* пропуск\w*|(?:цифров\w*|электронн\w*) пропуск\w*|"
                   r"контрол\w* доступ\w* в (?:корпус\w*|кампус\w*))\b"),
    ),
    "sber-payroll": (
        re.compile(r"\b(?:зарплат\w*|заработн\w* плат\w*|"
                   r"реестр\w* (?:\w+ ){0,3}(?:выплат\w*|расчет\w*) (?:\w+ ){0,2}сотрудник\w*)\b"),
        re.compile(r"\b(?:реестр\w*|автоматиз\w*|расчет\w*|зачислен\w*|"
                   r"перечислен\w*|зарплатн\w* проект\w*|схем\w* выплат\w*)\b"),
    ),
    "sber-health-corporate": (
        re.compile(r"\b(?:медицинск\w* (?:консультац\w*|поддерж\w*|помощ\w*|страхован\w*|"
                   r"программ\w*|обслуживан\w*|услуг\w*|осмотр\w*)|"
                   r"телемедицин\w*|медосмотр\w*|медстрахова\w*|диспансеризац\w*|дмс)\b"),
        re.compile(r"\b(?:сотрудник\w*|работник\w*|преподавател\w*|персонал\w*|трудов\w* коллектив\w*)\b"),
    ),
}
OPERATIONAL_INITIATIVE = re.compile(
    r"\b(?:модерниз\w*|автоматиз\w*|внедр(?:я|ени|ить)\w*|обнов[ли]\w*|интеграци\w*|"
    r"планиру\w*|запланирова\w*|предусматрива\w*|потребност\w*|необходимост\w*|нужда\w*|требу[ею]тся|"
    r"объявил\w* проект\w*|анонсирова\w*|реализу\w*|разрабатыва\w*|закуп\w*|"
    r"запуска\w*|переход\w* на)\b"
)
INACTIVE_INITIATIVE = re.compile(
    r"\b(?:не (?:планиру\w*|предусматрива\w*|требу\w*|нужн\w*|нужда\w*|будет)|"
    r"(?:отмен(?:ен|ил)|заверш(?:ен|ил))\w*|отказ\w* от|отсутств\w* потребност\w*|"
    r"(?:учебн\w*|курсов\w*) проект\w*)\b"
)
PLANNED_INITIATIVE = re.compile(
    r"\b(?:планиру\w*|запланирова\w*|анонсирова\w*|анонс\w*|объявил\w* проект\w*)\b"
)
CONTEXT_ONLY_FIELDS = {
    FactField.TYPE, FactField.LOCATION, FactField.EDUCATION_LEVELS, FactField.STUDENTS,
}
PROMPT = (Path(__file__).parent / "prompts" / "match_partnership.txt").read_text(encoding="utf-8")


class MatchProposal(MatchArgument):
    catalog_id: str = Field(json_schema_extra={"enum": list(MATCH_CATALOG)})
    regional_case_id: str | None = None


class MatchOutput(StrictModel):
    matches: list[MatchProposal] = Field(max_length=MAX_MATCHES)


def partnership_facts(run: ResearchRun) -> list[Fact]:
    return [
        fact for fact in effective_facts(run)
        if fact.institution_id == run.institution.id and fact.field in MATCH_FIELDS and fact.evidence
    ]


def has_partnership_basis(facts: Iterable[Fact]) -> bool:
    return any(fact.field not in CONTEXT_ONLY_FIELDS for fact in facts)


def operational_initiative(fact: Fact, topics: tuple[re.Pattern[str], ...]) -> str | None:
    for sentence in re.split(r"[;\n]+|(?<=[.!?])\s+", fact.value):
        wording = normalized(sentence)
        if (OPERATIONAL_INITIATIVE.search(wording) and not INACTIVE_INITIATIVE.search(wording)
                and all(topic.search(wording) for topic in topics)
                and any(literal_value_in_text(sentence, proof.quote) for proof in fact.evidence)):
            return " ".join(sentence.split())
    return None


def event_is_upcoming(event_start: date, meeting: MeetingContext, today: date) -> bool:
    try:
        meeting_date = date.fromisoformat(meeting.date) if meeting.date else today
    except ValueError:
        meeting_date = today
    return event_start > max(today, meeting_date)


async def match_partnership(
    provider: JSONProvider, facts: list[Fact], meeting: MeetingContext, *, today: date | None = None,
) -> list[PartnershipMatch]:
    if not has_partnership_basis(facts):
        return []
    operational_basis = {
        key: {fact.fact_id: initiative for fact in facts if fact.field in OPERATIONAL_FIELDS
              and (initiative := operational_initiative(fact, topics))}
        for key, topics in OPERATIONAL_TOPICS.items()
    }
    event_basis = {
        fact.fact_id for fact in facts
        if fact.field not in CONTEXT_ONLY_FIELDS and
        EVENT_TOPIC.search(normalized(fact.value)) and EVENT_NEED.search(normalized(fact.value))
    }
    has_spo_level = any(
        fact.field in {FactField.TYPE, FactField.EDUCATION_LEVELS} and SPO_LEVEL.search(normalized(fact.value))
        for fact in facts
    )
    spo_basis = {
        fact.fact_id for fact in facts if fact.field == FactField.EDUCATION_AREAS
        and SPO_BANKING.search(normalized(fact.value))
        and (has_spo_level or SPO_LEVEL.search(normalized(fact.value)) or "38.02.07" in fact.value)
    }
    required_basis = {
        **{key: set(items) for key, items in operational_basis.items()},
        "sber-ai-transformation-2026-12": event_basis, "sber-spo-banking-practice": spo_basis,
    }
    today = today or datetime.now(timezone.utc).date()
    catalog = {
        key: entry for key, entry in MATCH_CATALOG.items()
        if (key not in required_basis or required_basis[key])
        and (entry.event_start is None or event_is_upcoming(entry.event_start, meeting, today))
    }
    schema = MatchOutput.model_json_schema()
    schema["$defs"]["MatchProposal"]["properties"]["catalog_id"]["enum"] = list(catalog)
    payload = {
        "meeting": meeting.model_dump(mode="json"),
        "catalog": [
            {**entry.model_dump(mode="json", exclude={"available_for_matching", "requires_operational_context"}),
             "regional_cases": [
                 {"id": case_id, **case.model_dump(mode="json")}
                 for case_id, (product_id, case) in REGIONAL_CASES.items() if product_id == key
             ],
             **({"required_basis_fact_ids": sorted(required_basis[key])} if key in required_basis else {})}
            for key, entry in catalog.items()
        ],
        "facts": [{
            "fact_id": fact.fact_id,
            "field": fact.field.value,
            "subject": getattr(fact.subject, "entity_name", None),
            "value": fact.value,
            "metric_context": fact.metric_context.model_dump(mode="json") if fact.metric_context else None,
            "published_at": max((proof.published_at.isoformat() for proof in fact.evidence if proof.published_at),
                                default=None),
        } for fact in facts],
    }
    raw = await provider.complete(PROMPT, payload, schema)
    try:
        return _validate_matches(raw, catalog, facts, required_basis, operational_basis)
    except LLMError as exc:
        if str(exc) != "invalid_partnership_response":
            raise
    retry_prompt = PROMPT + (
        "\nПредыдущий ответ не прошёл проверку. Сверь catalog_id, basis_fact_ids и regional_case_id "
        "с входными данными. Пропусти предложение, если ему не хватает подтверждённых оснований."
    )
    raw = await provider.complete(retry_prompt, payload, schema)
    return _validate_matches(raw, catalog, facts, required_basis, operational_basis)


def _validate_matches(raw, catalog, facts, required_basis, operational_basis) -> list[PartnershipMatch]:
    try:
        output = json.loads(raw)
    except json.JSONDecodeError:
        raise LLMError("invalid_partnership_response") from None
    if (not isinstance(output, dict) or output.keys() != {"matches"}
            or not isinstance(output["matches"], list) or len(output["matches"]) > MAX_MATCHES):
        raise LLMError("invalid_partnership_response")

    fact_ids = {fact.fact_id for fact in facts}
    basis_ids = {fact.fact_id for fact in facts if fact.field not in CONTEXT_ONLY_FIELDS}
    seen = set()
    matches = []
    invalid = False
    for item in output["matches"]:
        try:
            proposal = MatchProposal.model_validate(item)
        except ValidationError:
            invalid = True
            continue
        catalog_id = proposal.catalog_id
        basis = set(proposal.basis_fact_ids)
        if catalog_id not in catalog or not basis <= fact_ids:
            invalid = True
            continue
        if catalog_id in seen:
            continue
        if not basis & basis_ids:
            continue
        entry = catalog[catalog_id]
        if catalog_id in required_basis and not basis & required_basis[catalog_id]:
            invalid = True
            continue
        case_ref = REGIONAL_CASES.get(proposal.regional_case_id)
        if proposal.regional_case_id and (case_ref is None or case_ref[0] != catalog_id):
            invalid = True
            continue
        rationale = proposal.rationale
        if entry.requires_operational_context:
            supported = operational_basis.get(catalog_id, {})
            initiatives = [supported[fact_id] for fact_id in proposal.basis_fact_ids if fact_id in supported]
            if not initiatives:
                invalid = True
                continue
            planned = next((text for text in initiatives if PLANNED_INITIATIVE.search(normalized(text))), None)
            if planned:
                rationale = planned if len(planned) <= 220 else "В источнике указаны планы вуза по этой инициативе."
        matches.append(PartnershipMatch(
            **proposal.model_dump(exclude={"catalog_id", "basis_fact_ids", "rationale", "regional_case_id"}),
            rationale=rationale,
            basis_fact_ids=list(dict.fromkeys(proposal.basis_fact_ids)),
            product_name=entry.name, catalog_url=entry.url, kind=entry.kind,
            regional_case=case_ref[1] if case_ref else None, event_start=entry.event_start,
        ))
        seen.add(catalog_id)
    if invalid and not matches:
        raise LLMError("invalid_partnership_response")
    return matches
