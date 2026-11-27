"""Специальности (направления подготовки) вуза и направления сотрудничества со Сбером.

Источник специальностей — обязательный раздел официального сайта
«Сведения об образовательной организации → Образование» (``/sveden/education/``),
где программы размечены микроразметкой ``eduCode``/``eduName``/``eduLevel``.
Если сайт недоступен, используются страницы программ агрегаторов: там
специальности тоже указаны с кодом вида ``09.03.01``.

Направления сотрудничества со Сбером выводятся правилами из специальностей,
научных направлений и найденных новостей. Это подсказка для подготовки к
встрече, а не факт: каждое направление сопровождается основаниями.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Iterable

from bs4 import BeautifulSoup

from .models import CooperationDirection, NewsMention, Specialty
from .utils import normalize_whitespace

# Код направления подготовки: «09.03.01». Второе число — уровень.
CODE_RE = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})\.(\d{2})(?![\d.])")
LEVEL_BY_CODE = {
    "02": "СПО",
    "03": "бакалавриат",
    "04": "магистратура",
    "05": "специалитет",
    "06": "аспирантура",
    "07": "ординатура",
    "08": "ординатура",
    "09": "ассистентура-стажировка",
}

# Укрупнённые группы специальностей и направлений (УГСН).
UGSN = {
    "01": "Математика и механика",
    "02": "Компьютерные и информационные науки",
    "03": "Физика и астрономия",
    "04": "Химия",
    "05": "Науки о Земле",
    "06": "Биологические науки",
    "07": "Архитектура",
    "08": "Техника и технологии строительства",
    "09": "Информатика и вычислительная техника",
    "10": "Информационная безопасность",
    "11": "Электроника, радиотехника и системы связи",
    "12": "Фотоника, приборостроение, оптические и биотехнические системы и технологии",
    "13": "Электро- и теплоэнергетика",
    "14": "Ядерная энергетика и технологии",
    "15": "Машиностроение",
    "16": "Физико-технические науки и технологии",
    "17": "Оружие и системы вооружения",
    "18": "Химические технологии",
    "19": "Промышленная экология и биотехнологии",
    "20": "Техносферная безопасность и природообустройство",
    "21": "Прикладная геология, горное дело, нефтегазовое дело и геодезия",
    "22": "Технологии материалов",
    "23": "Техника и технологии наземного транспорта",
    "24": "Авиационная и ракетно-космическая техника",
    "25": "Аэронавигация и эксплуатация авиационной и ракетно-космической техники",
    "26": "Техника и технологии кораблестроения и водного транспорта",
    "27": "Управление в технических системах",
    "28": "Нанотехнологии и наноматериалы",
    "29": "Технологии лёгкой промышленности",
    "30": "Фундаментальная медицина",
    "31": "Клиническая медицина",
    "32": "Науки о здоровье и профилактическая медицина",
    "33": "Фармация",
    "34": "Сестринское дело",
    "35": "Сельское, лесное и рыбное хозяйство",
    "36": "Ветеринария и зоотехния",
    "37": "Психологические науки",
    "38": "Экономика и управление",
    "39": "Социология и социальная работа",
    "40": "Юриспруденция",
    "41": "Политические науки и регионоведение",
    "42": "Средства массовой информации и информационно-библиотечное дело",
    "43": "Сервис и туризм",
    "44": "Образование и педагогические науки",
    "45": "Языкознание и литературоведение",
    "46": "История и археология",
    "47": "Философия, этика и религиоведение",
    "48": "Теология",
    "49": "Физическая культура и спорт",
    "50": "Искусствознание",
    "51": "Культуроведение и социокультурные проекты",
    "52": "Сценические искусства и литературное творчество",
    "53": "Музыкальное искусство",
    "54": "Изобразительное и прикладные виды искусств",
    "55": "Экранные искусства",
    "56": "Военное управление",
    "57": "Обеспечение безопасности",
    "58": "Востоковедение и африканистика",
}

# Направления, интересные Сберу, и признаки, по которым они видны у вуза.
SBER_DIRECTIONS: tuple[tuple[str, frozenset[str], tuple[str, ...]], ...] = (
    (
        "Искусственный интеллект, данные и разработка ПО",
        frozenset({"01", "02", "09", "27"}),
        ("искусственн", "машинн", "данных", "data", "программн", "информатик", "цифров", "нейросет"),
    ),
    (
        "Кибербезопасность",
        frozenset({"10"}),
        ("кибер", "информационн безопасн", "защит информац"),
    ),
    (
        "Финансы, экономика и банковское дело",
        frozenset({"38"}),
        ("финанс", "банк", "экономик", "бизнес-информатик", "менеджмент"),
    ),
    (
        "Право и комплаенс",
        frozenset({"40"}),
        ("юриспруденц", "правов", "комплаенс"),
    ),
    (
        "Медицина и здравоохранение (цифровая медицина)",
        frozenset({"30", "31", "32", "33", "34"}),
        ("медицин", "здравоохранен", "фармац"),
    ),
    (
        "Образование и EdTech (СберУниверситет, Школа 21)",
        frozenset({"44"}),
        ("педагог", "образовательн технолог", "edtech"),
    ),
    (
        "Связь, электроника и ИТ-инфраструктура",
        frozenset({"11", "12", "13"}),
        ("телеком", "связ", "электроник", "радиотехник"),
    ),
    (
        "Маркетинг, медиа и клиентский сервис",
        frozenset({"42", "43"}),
        ("маркетинг", "реклам", "медиа", "сервис"),
    ),
    (
        "Психология и HR",
        frozenset({"37", "39"}),
        ("психолог", "персонал", "hr"),
    ),
)

# Нормализованные названия (без регистра и «ё»), чтобы сравнивать без опечаток.
def _fold(value: str) -> str:
    return normalize_whitespace(value or "").lower().replace("ё", "е")


def _clean_name(value: str) -> str:
    value = normalize_whitespace(CODE_RE.sub(" ", value or ""))
    value = re.sub(r"^[\s\-–—:,.|]+|[\s\-–—:,.|]+$", "", value)
    return value[:200]


def make_specialty(code: str, name: str, source_url: str, level: str = "") -> Specialty | None:
    match = CODE_RE.fullmatch(code.strip())
    name = _clean_name(name)
    if not match or len(name) < 3 or not re.search(r"[А-ЯЁа-яё]", name):
        return None
    group = match.group(1)
    return Specialty(
        code=code.strip(),
        name=name,
        level=level or LEVEL_BY_CODE.get(match.group(2), ""),
        group_code=group,
        group_name=UGSN.get(group, ""),
        source_url=source_url,
    )


def parse_specialties(html: str | BeautifulSoup, source_url: str) -> list[Specialty]:
    """Специальности со страницы: микроразметка, таблицы, затем текст с кодами."""

    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html or "", "lxml")
    found: list[Specialty] = []

    # 1. Микроразметка «Сведений об образовательной организации».
    for node in soup.select('[itemprop~="eduCode"]'):
        code_match = CODE_RE.search(node.get_text(" ", strip=True))
        if not code_match:
            continue
        row = node.find_parent(["tr", "li", "div"]) or node.parent
        name_node = row.select_one('[itemprop~="eduName"]') if row else None
        level_node = row.select_one('[itemprop~="eduLevel"]') if row else None
        name = name_node.get_text(" ", strip=True) if name_node else ""
        level = normalize_whitespace(level_node.get_text(" ", strip=True)).lower() if level_node else ""
        specialty = make_specialty(code_match.group(0), name, source_url, _level_label(level))
        if specialty:
            found.append(specialty)

    # 2. Строки таблиц и пункты списков «09.03.01 Информатика и …».
    if not found:
        for row in soup.find_all(["tr", "li", "a", "p", "h3", "h4", "div"]):
            if row.find(["tr", "li", "p", "div"]) is not None and row.name in {"div", "li"}:
                continue
            text = normalize_whitespace(row.get_text(" ", strip=True))
            if len(text) > 400:
                continue
            match = CODE_RE.search(text)
            if not match:
                continue
            if row.name == "tr":
                cells = [normalize_whitespace(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
                name = next(
                    (cell for cell in cells if not CODE_RE.fullmatch(cell) and re.search(r"[А-ЯЁа-яё]{3}", cell)),
                    "",
                )
            else:
                name = text[match.end():] or text[: match.start()]
            specialty = make_specialty(match.group(0), name, source_url)
            if specialty:
                found.append(specialty)
    return dedupe_specialties(found)


def _level_label(value: str) -> str:
    for marker, label in (
        ("бакалавр", "бакалавриат"),
        ("магистр", "магистратура"),
        ("специалит", "специалитет"),
        ("аспирант", "аспирантура"),
        ("ординатур", "ординатура"),
        ("среднее", "СПО"),
    ):
        if marker in value:
            return label
    return ""


def dedupe_specialties(items: Iterable[Specialty]) -> list[Specialty]:
    result: "OrderedDict[tuple[str, str], Specialty]" = OrderedDict()
    for item in items:
        key = (item.code, _fold(item.name))
        if key not in result:
            result[key] = item
    return sorted(result.values(), key=lambda item: (item.code, item.name))


def cooperation_directions(
    specialties: Iterable[Specialty],
    research_names: Iterable[str] = (),
    news: Iterable[NewsMention] = (),
) -> list[CooperationDirection]:
    """Направления сотрудничества со Сбером с основаниями для каждого."""

    specialties = list(specialties)
    research = [normalize_whitespace(name) for name in research_names if name]
    news = list(news)
    directions: list[CooperationDirection] = []
    for area, codes, keywords in SBER_DIRECTIONS:
        programs = [item for item in specialties if item.group_code in codes]
        labs = [name for name in research if any(keyword in _fold(name) for keyword in keywords)]
        mentions = [
            item
            for item in news
            if any(keyword in _fold(f"{item.title} {item.snippet}") for keyword in keywords)
        ]
        if not programs and not labs and not mentions:
            continue
        evidence: list[str] = []
        if programs:
            examples = ", ".join(f"{item.code} {item.name}" for item in programs[:3])
            evidence.append(f"Программ подготовки: {len(programs)} (например: {examples})")
        if labs:
            evidence.append("Научные подразделения: " + "; ".join(labs[:3]))
        for item in mentions[:3]:
            evidence.append(f"Новость{f' от {item.date}' if item.date else ''}: {item.title} ({item.url})")
        directions.append(
            CooperationDirection(
                area=area,
                programs_count=len(programs),
                evidence=evidence,
                has_existing_cooperation=bool(mentions),
                source_urls=list(dict.fromkeys(
                    [item.source_url for item in programs[:3] if item.source_url] + [item.url for item in mentions[:3]]
                )),
            )
        )
    # Сначала направления, где сотрудничество уже есть, затем по числу программ.
    directions.sort(key=lambda item: (not item.has_existing_cooperation, -item.programs_count))
    return directions
