"""Конфигурация парсера: тайминги, User-Agent, URL и CSS-селекторы.

Все изменяемые настройки собраны здесь, чтобы не править код парсера при
добавлении нового вуза. Значения можно переопределить переменными окружения:

* ``PARSER_DELAY`` — базовая пауза между запросами, секунды;
* ``PARSER_TIMEOUT`` — таймаут запроса, секунды;
* ``PARSER_RETRIES`` — число попыток;
* ``PARSER_OUTPUT_DIR`` — каталог результатов;
* ``PARSER_NO_ROBOTS=1`` — отключить проверку robots.txt (не рекомендуется);
* ``PARSER_WITH_BIOS=0`` — не загружать биографии руководителей.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "output"
DEFAULT_LOG_DIR = DATA_DIR / "logs"
DEFAULT_CACHE_DIR = DATA_DIR / "cache"

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass
class Settings:
    """Параметры одного запуска парсера."""

    request_delay: float = 2.0
    delay_jitter: float = 0.8
    timeout: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 3.0
    respect_robots: bool = True
    user_agents: tuple[str, ...] = USER_AGENTS
    max_items_per_section: int = 30
    max_pages_per_section: int = 1
    max_document_chars: int = 120_000
    max_response_bytes: int = 5_000_000
    max_total_requests: int = 250
    fetch_bios: bool = True
    max_bios: int = 6
    fetch_item_pages: bool = True
    max_item_documents: int = 8
    news_window_months: int = 24
    use_cache: bool = True
    cache_ttl_hours: int = 24
    use_sqlite: bool = True
    db_path: Path = DATA_DIR / "parser.sqlite3"
    enable_playwright: bool = False
    output_dir: Path = DEFAULT_OUTPUT_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    cache_dir: Path = DEFAULT_CACHE_DIR

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            request_delay=max(1.0, _env_float("PARSER_DELAY", 2.0)),
            delay_jitter=max(0.0, _env_float("PARSER_DELAY_JITTER", 0.8)),
            timeout=max(5.0, _env_float("PARSER_TIMEOUT", 30.0)),
            max_retries=max(1, _env_int("PARSER_RETRIES", 3)),
            backoff_factor=max(1.0, _env_float("PARSER_BACKOFF", 3.0)),
            respect_robots=_env_bool("PARSER_ROBOTS", True),
            max_items_per_section=max(1, _env_int("PARSER_MAX_ITEMS", 30)),
            max_pages_per_section=max(1, _env_int("PARSER_MAX_PAGES", 1)),
            max_document_chars=max(10_000, _env_int("PARSER_MAX_DOC_CHARS", 120_000)),
            max_response_bytes=max(
                100_000, _env_int("PARSER_MAX_RESPONSE_BYTES", 5_000_000)
            ),
            max_total_requests=max(1, _env_int("PARSER_MAX_REQUESTS", 250)),
            fetch_bios=_env_bool("PARSER_WITH_BIOS", True),
            max_bios=max(0, _env_int("PARSER_MAX_BIOS", 6)),
            fetch_item_pages=_env_bool("PARSER_ITEM_PAGES", True),
            max_item_documents=max(0, _env_int("PARSER_MAX_ITEM_DOCS", 8)),
            news_window_months=max(0, _env_int("PARSER_NEWS_MONTHS", 24)),
            use_cache=_env_bool("PARSER_CACHE", True),
            cache_ttl_hours=max(1, _env_int("PARSER_CACHE_TTL_HOURS", 24)),
            use_sqlite=_env_bool("PARSER_SQLITE", True),
            db_path=Path(os.getenv("PARSER_DB_PATH", str(DATA_DIR / "parser.sqlite3"))),
            enable_playwright=_env_bool("PARSER_PLAYWRIGHT", False),
            output_dir=Path(os.getenv("PARSER_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))),
            log_dir=Path(os.getenv("PARSER_LOG_DIR", str(DEFAULT_LOG_DIR))),
            cache_dir=Path(os.getenv("PARSER_CACHE_DIR", str(DEFAULT_CACHE_DIR))),
        )


@dataclass(frozen=True)
class SectionSpec:
    """Описание целевого раздела сайта."""

    name: str
    url: str
    item_selectors: tuple[str, ...] = ()
    content_selectors: tuple[str, ...] = ()
    max_items: int = 30
    description: str = ""
    # Оставлять только элементы, в которых есть ключевые слова типа
    # «грант/проект/НИР». Полезно для обзорных страниц, где кроме проектов
    # публикуются обычные новости.
    require_keywords: bool = False


@dataclass(frozen=True)
class UniversityProfile:
    """Профиль вуза с проверенными точками входа.

    ``sections`` — целевые страницы. ``content_selectors`` — селекторы
    основного контента (если сайт нестандартный). ``bio_urls`` — страницы
    биографий, которые нужно разобрать дополнительно.
    """

    slug: str
    name: str
    url: str
    sections: tuple[SectionSpec, ...]
    aliases: tuple[str, ...] = ()
    content_selectors: tuple[str, ...] = ()
    bio_urls: tuple[str, ...] = ()
    notes: str = ""


DEFAULT_CONTENT_SELECTORS: tuple[str, ...] = (
    "main",
    "[role='main']",
    "article",
    "#content",
    ".content",
    ".page-content",
    ".post-content",
    ".entry-content",
    ".article-content",
    ".node__content",
)

DEFAULT_ITEM_SELECTORS: tuple[str, ...] = (
    "div.post",
    "article",
    ".news-item",
    ".event-card",
    ".views-row",
    ".card",
    ".pk-event-card",
    "li",
)


UNIVERSITY_PROFILES: dict[str, UniversityProfile] = {
    "spbu": UniversityProfile(
        slug="spbu",
        name="Санкт-Петербургский государственный университет",
        url="https://spbu.ru/",
        aliases=("спбгу", "saint petersburg state university", "spbu.ru"),
        content_selectors=(
            "article.editor-wrap",
            "main.page-main-content",
            "main",
        ),
        bio_urls=("https://spbu.ru/rector/biografiya",),
        notes=(
            "Структура и руководство берутся из обязательного раздела "
            "/sveden/struct/; биография ректора — отдельная страница."
        ),
        sections=(
            SectionSpec(
                name="about",
                url="https://spbu.ru/sveden/common",
                content_selectors=(
                    "article.editor-wrap:not(.sveden)",
                    "main.page-main-content",
                ),
                description="Основные сведения об организации",
            ),
            SectionSpec(
                name="leadership",
                url="https://spbu.ru/sveden/struct/",
                description="Структура и органы управления, руководители",
            ),
            SectionSpec(
                name="contacts",
                url="https://spbu.ru/contacts",
                description="Контактная информация",
            ),
            SectionSpec(
                name="projects",
                url="https://spbu.ru/nauka/laboratorii-i-centry",
                content_selectors=("main.page-main-content", "main"),
                description="Лаборатории и научные центры (НИР)",
            ),
            SectionSpec(
                name="news",
                url="https://spbu.ru/news-events/novosti",
                description="Новости о проектах и грантах",
            ),
            SectionSpec(
                name="events",
                url="https://spbu.ru/news-events/calendar",
                description="Календарь событий",
            ),
        ),
    ),
    "hse": UniversityProfile(
        slug="hse",
        name="Национальный исследовательский университет «Высшая школа экономики»",
        url="https://www.hse.ru/",
        aliases=("вшэ", "hse", "hse.ru", "высшая школа экономики"),
        content_selectors=("div.content", "main", "article"),
        notes=(
            "Контакты и руководство — в /sveden/struct/ и /sveden/common/. "
            "Новости и мероприятия — раздельные страницы."
        ),
        sections=(
            SectionSpec(
                name="about",
                url="https://www.hse.ru/sveden/common/",
                description="Основные сведения",
            ),
            SectionSpec(
                name="leadership",
                url="https://www.hse.ru/sveden/struct/",
                description="Структура и органы управления",
            ),
            SectionSpec(
                name="contacts",
                url="https://www.hse.ru/sveden/struct/",
                description="Контакты подразделений",
            ),
            SectionSpec(
                name="projects",
                url="https://www.hse.ru/science/",
                require_keywords=True,
                description="Научные проекты и гранты",
            ),
            SectionSpec(
                name="news",
                url="https://www.hse.ru/news/",
                item_selectors=("div.post",),
                description="Новости",
            ),
            SectionSpec(
                name="events",
                url="https://www.hse.ru/announcements/",
                description="Все мероприятия",
            ),
        ),
    ),
    "urfu": UniversityProfile(
        slug="urfu",
        name=(
            "Уральский федеральный университет имени первого Президента "
            "России Б.Н. Ельцина"
        ),
        url="https://urfu.ru/",
        aliases=("урфу", "urfu", "urfu.ru", "уральский федеральный университет"),
        content_selectors=("main", "#content", ".content", "article"),
        notes=(
            "Руководство и подразделения — /sveden/struct/ (таблицы "
            "«Руководитель подразделения»). Контакты — /ru/contacts/."
        ),
        sections=(
            SectionSpec(name="about", url="https://urfu.ru/ru/about/"),
            SectionSpec(
                name="leadership",
                url="https://urfu.ru/sveden/struct/",
            ),
            SectionSpec(name="contacts", url="https://urfu.ru/ru/contacts/"),
            SectionSpec(
                name="research",
                url="https://urfu.ru/ru/science/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
            SectionSpec(
                name="projects",
                url="https://urfu.ru/ru/partners/projects/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
            SectionSpec(
                name="grants",
                url="https://urfu.ru/ru/science/grants/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
            SectionSpec(
                name="partnerships",
                url="https://urfu.ru/ru/partners/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
            SectionSpec(
                name="achievements",
                url="https://urfu.ru/ru/students/olympic/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
            SectionSpec(
                name="news",
                url="https://urfu.ru/ru/news/",
                item_selectors=("div.post",),
            ),
            SectionSpec(
                name="events",
                url="https://urfu.ru/ru/events/",
                item_selectors=DEFAULT_ITEM_SELECTORS,
            ),
        ),
    ),
    "rsuh": UniversityProfile(
        slug="rsuh",
        name="Российский государственный гуманитарный университет",
        url="https://www.rsuh.ru/",
        aliases=("рггу", "rsuh", "rggu", "rsuh.ru", "рггу.рф"),
        content_selectors=(".page-content", "#content", "main"),
        notes=(
            "Таблицы /sveden/struct/ содержат ФИО, должности, адреса и "
            "контакты руководителей подразделений."
        ),
        sections=(
            SectionSpec(
                name="about",
                url="https://www.rsuh.ru/sveden/common/",
                description="Основные сведения",
            ),
            SectionSpec(
                name="leadership",
                url="https://www.rsuh.ru/sveden/struct/",
                description="Структура и органы управления",
            ),
            SectionSpec(
                name="contacts",
                url="https://www.rsuh.ru/contacts/",
                description="Контактная информация",
            ),
            SectionSpec(
                name="projects",
                url="https://www.rsuh.ru/science/nac_project/",
                description="Национальные проекты и инициативы",
            ),
            SectionSpec(
                name="news",
                url="https://www.rsuh.ru/news/",
                description="Новости",
            ),
            SectionSpec(
                name="events",
                url="https://www.rsuh.ru/anons/",
                description="Анонсы мероприятий",
            ),
        ),
    ),
    # МГУ — SvelteKit-приложение: HTML приходит без контента, поэтому
    # профиль оставлен для проверки graceful degradation и будущего Playwright.
    "msu": UniversityProfile(
        slug="msu",
        name="Московский государственный университет имени М. В. Ломоносова",
        url="https://www.msu.ru/",
        aliases=("мгу", "msu", "msu.ru", "московский государственный университет"),
        notes=(
            "Сайт отдаёт SPA-разметку без SSR-контента. Без Playwright "
            "извлекаются только метаданные; парсер фиксирует предупреждение."
        ),
        sections=(
            SectionSpec(name="about", url="https://www.msu.ru/sveden/common/"),
            SectionSpec(name="leadership", url="https://www.msu.ru/sveden/struct/"),
            SectionSpec(name="contacts", url="https://www.msu.ru/sveden/struct/"),
        ),
    ),
}

# Профили, которые прогоняются командой ``--all``. МГУ исключён из
# автоматического прогона, пока не подключён браузерный рендеринг.
DEFAULT_BATCH_PROFILES: tuple[str, ...] = ("spbu", "hse", "rsuh")

# Ключевые слова для автоматического поиска разделов на незнакомых сайтах.
SECTION_LINK_PATTERNS: dict[str, tuple[str, ...]] = {
    "about": (
        "об университете",
        "об академии",
        "об институте",
        "основные сведения",
        "история университета",
        "история вуза",
        "о нас",
    ),
    "leadership": (
        "руководство",
        "ректорат",
        "ректор",
        "проректор",
        "структура и органы управления",
        "органы управления",
        "администрация",
        "университет в лицах",
    ),
    "contacts": (
        "контакты",
        "контактная информация",
        "приемная",
        "приёмная",
        "обратная связь",
    ),
    "projects": (
        "проекты",
        "наука",
        "научные проекты",
        "приоритет 2030",
        "передовые инженерные школы",
        "национальные проекты",
        "исследования",
    ),
    "research": (
        "научные центры",
        "научно-исследовательская деятельность",
        "научная деятельность",
        "лаборатории",
        "научные лаборатории",
        "исследовательские центры",
        "научные направления",
    ),
    "grants": (
        "гранты",
        "конкурсы",
        "финансирование",
        "поддержка исследований",
        "меры поддержки",
        "стипендии",
    ),
    "partnerships": (
        "партнёры",
        "партнеры",
        "партнёрство",
        "партнерство",
        "индустриальные партнёры",
        "сотрудничество",
        "договоры и соглашения",
        "соглашения о сотрудничестве",
    ),
    "achievements": (
        "достижен",
        "наград",
        "победител",
        "олимпиад",
        "конкурсы студентов",
        "результаты конкурсов",
    ),
    "news": (
        "новости",
        "новости науки",
        "пресс-центр",
        "новостная лента",
    ),
    "events": (
        "мероприятия",
        "анонсы",
        "календарь событий",
        "события",
        "конференции",
        "форумы",
    ),
}

# Стандартные для российских вузов страницы «Сведения об образовательной
# организации» (Приказ Минобрнауки № 785). Используются как fallback.
SVEDEN_SECTIONS: dict[str, str] = {
    "about": "/sveden/common/",
    "leadership": "/sveden/struct/",
    "contacts": "/sveden/struct/",
}

# Фиксированные категории документов (раздел 4 требований).
DOCUMENT_CATEGORIES: tuple[str, ...] = (
    "organization",
    "leadership",
    "contacts",
    "research",
    "projects",
    "grants",
    "project_news",
    "partnerships",
    "achievements",
    "other",
)

# Соответствие внутренних разделов парсера категориям документов.
SECTION_TO_CATEGORY: dict[str, str] = {
    "homepage": "organization",
    "about": "organization",
    "leadership": "leadership",
    "bio": "leadership",
    "contacts": "contacts",
    "research": "research",
    "projects": "projects",
    "grants": "grants",
    "news": "project_news",
    "project_news": "project_news",
    "partnerships": "partnerships",
    "achievements": "achievements",
    "events": "other",
}

# Селекторы мусорных блоков, которые удаляются до извлечения текста.
NOISE_SELECTORS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "svg",
    "canvas",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "dialog",
    "nav",
    "header",
    "footer",
    "aside",
    ".cookie",
    ".cookies",
    ".cookie-banner",
    ".breadcrumb",
    ".breadcrumbs",
    ".menu",
    ".navbar",
    ".navigation",
    ".sidebar",
    ".side-bar",
    ".advert",
    ".ads",
    ".banner",
    ".social",
    ".share",
    ".sharing",
    ".pagination",
    ".pager",
    ".search",
    ".search-form",
    ".modal",
    ".popup",
    ".toolbar",
    ".visually-hidden",
    ".sr-only",
    ".skip-link",
    "#cookie",
    "#breadcrumbs",
    "#menu",
    "#nav",
    "#sidebar",
    "#pagination",
    "#search",
)

# Строки-«пустышки», которые не несут полезной информации.
BOILERPLATE_LINE_PATTERNS: tuple[str, ...] = (
    "перейти к содержимому",
    "перейти к основному содержанию",
    "версия для слабовидящих",
    "обычная версия сайта",
    "мы используем файлы cookie",
    "используя этот сайт",
    "принять cookies",
    "все права защищены",
    "© ",
)

# Классификация проектов по ключевым словам (первое совпадение имеет приоритет).
PROJECT_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "федеральный": (
        "приоритет-2030",
        "приоритет 2030",
        "передовая инженерная школа",
        "передовые инженерные школы",
        "федеральн",
        "национальн",
        "минобрнауки",
        "мегагрант",
        "рнф",
        "российский научный фонд",
        "грант",
    ),
    "региональный": (
        "региональн",
        "городск",
        "областн",
        "муниципальн",
    ),
    "НИР": (
        "нир",
        "ниокр",
        "научно-исследовательск",
        "исследовательск",
        "лаборатор",
    ),
    "международный": (
        "международн",
        "зарубежн",
        "европейск",
        "brics",
        "брикс",
        "шос",
    ),
}

# Классификация мероприятий (первое совпадение имеет приоритет).
EVENT_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "конференция": ("конференц", "симпозиум", "конгресс", "форум"),
    "образовательное": (
        "образовательн",
        "лекц",
        "мастер-класс",
        "семинар",
        "курс",
        "школ",
        "олимпиад",
        "вебинар",
    ),
    "студенческое": (
        "студенческ",
        "студентов",
        "кампус",
        "квест",
        "студвесн",
        "посвящение",
    ),
    "карьерное": ("карьер", "стажиров", "ярмарка вакансий", "работодател"),
    "научное": ("научн", "исследован", "круглый стол", "диспут"),
    "спортивное": ("спорт", "турнир", "соревнован", "олимпиад"),
    "культурное": ("концерт", "выставк", "театр", "фестивал", "экскурс"),
}
