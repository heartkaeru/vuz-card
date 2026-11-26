"""Парсеры агрегаторов вузов: vuzopedia.ru и postupi.online.

Оба сайта отдают HTML, но могут включать Cloudflare/403. Загрузка идёт через
``StealthFetcher`` (curl_cffi + опциональный Playwright).
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup

from .models import AggregatorPassport, Specialty
from .specialties import dedupe_specialties, parse_specialties
from .stealth import StealthFetcher
from .utils import canonical_url, normalize_whitespace

LOGGER = logging.getLogger(__name__)

VUZOPEDIA_BASE = "https://vuzopedia.ru"
POSTUPI_BASE = "https://postupi.online"

# Минимальное совпадение названия, при котором паспорт агрегатора принимается.
NAME_MATCH_THRESHOLD = 45.0
NAME_SUFFIX_MARKERS = ("имени", "им.", "назван", "в честь")


def name_match_score(query: str, title: str) -> float:
    """Насколько название агрегатора соответствует запросу.

    «Московский государственный университет» должен совпасть с МГУ
    («... университет имени М.В. Ломоносова»), а не с МГУСиТ
    («... университет спорта и туризма»).
    """

    q = _fold(query)
    t = _fold(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 100.0
    if t.startswith(q):
        remainder = t[len(q):].lstrip(" .,;:-")
        if not remainder or remainder.startswith(NAME_SUFFIX_MARKERS) or remainder.startswith("("):
            # «имени ...» или «(СПбГУ)» уточняет название, а не меняет вуз.
            return 95.0 - min(len(remainder), 40) * 0.05
        # «... университет спорта и туризма» — другой вуз (МГУСиТ, а не МГУ).
        return 35.0 - min(len(remainder), 60) * 0.1
    if q.startswith(t):
        # Агрегатор показывает сокращённое название без «имени ...».
        remainder = q[len(t):].lstrip(" .,;:-")
        if remainder.startswith(NAME_SUFFIX_MARKERS) or remainder.startswith("("):
            return 90.0
    q_tokens = set(_significant_tokens(q))
    t_tokens = set(_significant_tokens(t))
    if q_tokens and q_tokens == t_tokens:
        return 90.0
    if q_tokens and q_tokens <= t_tokens:
        # Все слова запроса есть, но в названии есть и другие значимые слова:
        # «Российский государственный университет» ≠ «… гуманитарный университет».
        return 35.0
    if len(t_tokens) >= 2 and t_tokens < q_tokens and (q_tokens - t_tokens) <= STATUS_WORDS:
        # Короткое название без статуса: «Казанский университет» =
        # «Казанский федеральный университет».
        return 70.0
    return 30.0 * SequenceMatcher(None, t, q).ratio()


_FOLD_RE = re.compile(r"[«»\"'“”„]")
_NAME_STOPWORDS = {"и", "в", "на", "по", "им", "имени", "федеральное", "государственное",
                   "бюджетное", "автономное", "образовательное", "учреждение", "высшего",
                   "образования", "фгбоу", "фгаоу", "во", "гоу", "ано", "чоу"}


def _fold(value: str) -> str:
    value = _FOLD_RE.sub("", normalize_whitespace(value or "")).lower().replace("ё", "е")
    return value.strip(" .,;:-")


def _significant_tokens(value: str) -> list[str]:
    tokens = re.findall(r"[a-zа-я0-9]+", _fold(value))
    result = []
    for token in tokens:
        if token in _NAME_STOPWORDS:
            continue
        if token in NAME_SUFFIX_MARKERS or token == "им":
            break  # всё после «имени» — уточнение, а не другой вуз
        result.append(token)
    return result


def abbreviation(title: str) -> str:
    """Аббревиатура названия: «Уральский федеральный университет» → «УФУ»."""

    letters = []
    for word in re.findall(r"[А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-]*", normalize_whitespace(title)):
        lowered = word.lower()
        if lowered in NAME_SUFFIX_MARKERS or lowered == "им":
            break
        if lowered in _NAME_STOPWORDS or len(word) < 2:
            continue
        for part in word.split("-"):
            if part:
                letters.append(part[0].upper())
    return "".join(letters).replace("Ё", "Е")


def short_name_matches(query: str, title: str) -> bool:
    """Короткое название («МГУ», «СПбГУ», «НИУ ВШЭ») соответствует полному.

    Подразделения и подведомственные организации («Лицей НИУ ВШЭ»,
    «Набережночелнинский институт КФУ») вузом не считаются.
    """

    q = normalize_whitespace(query)
    if not q or not title:
        return False
    folded_title = _fold(title)
    match = re.search(rf"(?<![а-яa-z0-9]){re.escape(_fold(q))}(?![а-яa-z0-9])", folded_title)
    if match:
        return not SUBORDINATE_RE.search(folded_title[: match.start()])
    if SUBORDINATE_RE.match(folded_title):
        return False
    capitals = re.sub(r"[^А-ЯЁA-Z]", "", q).replace("Ё", "Е")
    if len(capitals) < 2:
        return False
    abbr = abbreviation(title)
    return capitals == abbr or (len(capitals) >= 3 and len(abbr) >= 2 and (
        abbr.endswith(capitals) or (len(abbr) >= 3 and capitals.endswith(abbr))
    ))


# Подразделения и подведомственные организации вуза.
SUBORDINATE_RE = re.compile(
    r"(?<![а-яa-z])(?:лице[йя]|гимнази|школ[аы]\s|факультет|кафедр|филиал|колледж|техникум|"
    r"обсерватори|библиотек|музе[йя]|издательств|институт[а-я]*\s+(?!.*университет$))"
)
# Слова статуса, без которых название вуза остаётся тем же.
STATUS_WORDS = {
    "федеральный", "национальный", "исследовательский", "государственный",
    "приволжский", "российский", "межрегиональный", "автономный",
}


def _query_is_specific(query: str) -> bool:
    return len(normalize_whitespace(query).split()) >= 3


def _passport_matches(query: str, title: str, short_name: str = "") -> bool:
    if not normalize_whitespace(query) or not normalize_whitespace(title):
        # Пустой запрос не может подтвердить, что это тот самый вуз.
        return False
    if not _query_is_specific(query):
        return short_name_matches(query, title)
    if name_match_score(query, title) >= NAME_MATCH_THRESHOLD:
        return True
    return bool(short_name) and short_name_matches(short_name, title) and (
        name_match_score(query, title) >= 25.0
    )

CITY_BY_SUBDOMAIN = {
    "msk": "Москва",
    "spb": "Санкт-Петербург",
    "ekaterinburg": "Екатеринбург",
    "novosibirsk": "Новосибирск",
    "kazan": "Казань",
    "nnovgorod": "Нижний Новгород",
    "samara": "Самара",
    "rostov": "Ростов-на-Дону",
    "krasnodar": "Краснодар",
    "ufa": "Уфа",
    "perm": "Пермь",
    "voronezh": "Воронеж",
    "volgograd": "Волгоград",
    "krasnoyarsk": "Красноярск",
    "omsk": "Омск",
    "chelyabinsk": "Челябинск",
    "tyumen": "Тюмень",
    "saratov": "Саратов",
    "irkutsk": "Иркутск",
    "khabarovsk": "Хабаровск",
    "vladivostok": "Владивосток",
    "tomsk": "Томск",
}


def _text(node: BeautifulSoup | None) -> str:
    if node is None:
        return ""
    return normalize_whitespace(node.get_text(" ", strip=True))


def _int(value: str) -> int | None:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


def _float(value: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", value or "")
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


def _yes_no(value: str) -> bool | None:
    lowered = (value or "").lower()
    if "нет" in lowered or "отсутств" in lowered:
        return False
    if "есть" in lowered or "имеется" in lowered or "да" in lowered:
        return True
    return None


def _is_placeholder_image(src: str) -> bool:
    lowered = (src or "").lower()
    return not src or lowered.startswith("data:") or any(
        marker in lowered
        for marker in ("logo", "icon", "sprite", "placeholder", "spacer", "blank")
    )


def _decode_ddg_url(href: str) -> str:
    if "uddg=" in href:
        parsed = urlparse(href)
        values = parse_qs(parsed.query).get("uddg")
        if values:
            return unquote(values[0])
    return href


def _decode_search_url(href: str) -> str:
    """Прямая ссылка из редиректа DuckDuckGo или Bing."""

    from .news_search import _clean_url

    return _clean_url(_decode_ddg_url(href))


def _ok(response) -> bool:
    """Страница получена: код 2xx/3xx и непустой текст (404 и капча — нет)."""

    status = getattr(response, "status_code", 200) or 0
    return bool(response.text) and 200 <= status < 400


def _card_text(soup: BeautifulSoup) -> str:
    """Текст карточки вуза вокруг заголовка, без блоков «похожие вузы»."""

    soup = BeautifulSoup(str(soup), "lxml")  # копия: исходный документ ещё нужен
    for selector in ("aside", "footer", "nav", "header", "[class*=similar]", "[class*=related]",
                     "[class*=recommend]", "[class*=other-vuz]", "[class*=otherVuz]"):
        for node in soup.select(selector):
            node.decompose()
    heading = soup.find("h1")
    if heading is not None:
        node = heading
        for _ in range(6):
            parent = node.parent
            if parent is None or parent.name in {"body", "html", "[document]"}:
                break
            node = parent
            if len(_text(node)) >= 600:
                break
        text = _text(node)
        if len(text) >= 200:
            return text[:6000]
    return _text(soup)[:6000]


def _status_from_text(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"негосударственн|частн(?:ый|ое)\s+(?:вуз|университет|учреждени|институт)|^частный|[,\s]частный[,\s]", lowered):
        return "частный"
    if "государственн" in lowered:
        return "государственный"
    return ""


def _specialties_from_pages(fetcher: StealthFetcher, urls: list[str]) -> list[Specialty]:
    """Специальности со страниц программ агрегатора (код + название)."""

    found: list[Specialty] = []
    for page_url in urls:
        response = fetcher.get(page_url)
        if not _ok(response):
            continue
        found.extend(parse_specialties(response.text, page_url))
    return dedupe_specialties(found)


FACULTY_MARKERS = ("факультет", "институт", "школа", "центр", "кафедра", "колледж", "академия")


class VuzopediaParser:
    """Парсер карточки вуза на Вузопедии."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def search(self, name: str) -> list[dict[str, str]]:
        query = quote(name)
        url = f"{VUZOPEDIA_BASE}/search/results?q={query}&search_type=all"
        response = self.fetcher.get(url)
        if not _ok(response):
            return []
        soup = BeautifulSoup(response.text, "lxml")
        results: list[dict[str, str]] = []
        for card in soup.select("article.ps-result-card"):
            anchor = card.select_one("a.ps-result-title") or card.select_one("h3 a")
            if not anchor:
                continue
            href = anchor.get("href", "")
            path = urlparse(canonical_url(href, VUZOPEDIA_BASE))
            if "vuzopedia.ru" not in path.netloc or not path.path.startswith("/vuz/"):
                continue
            tags = [
                _text(tag)
                for tag in card.select(".ps-catalog-card__tags span")
                if _text(tag)
            ]
            city = next((tag for tag in tags if tag in CITY_BY_SUBDOMAIN.values()), "")
            results.append(
                {
                    "title": _text(anchor),
                    "url": canonical_url(href, VUZOPEDIA_BASE),
                    "city": city or (tags[1] if len(tags) > 1 else ""),
                }
            )
        return results

    def best_match(self, name: str, short_name: str = "", city: str = "") -> dict[str, str] | None:
        if not normalize_whitespace(name):
            return None
        results = self.search(name)
        if not results and short_name:
            results = self.search(short_name)
        if not results:
            return None
        specific = _query_is_specific(name)

        def rank(pair: tuple[int, dict[str, str]]) -> tuple[float, int, int]:
            index, item = pair
            if specific:
                score = name_match_score(name, item["title"])
            else:
                # Аббревиатура неоднозначна («МГУ» — и Мурманский): доверяем
                # порядку выдачи и городу, а не сходству строк.
                score = 50.0 if short_name_matches(name, item["title"]) else 0.0
            if short_name and short_name_matches(short_name, item["title"]):
                score += 5
            city_match = int(bool(city) and city.lower() in (item.get("city") or "").lower())
            return score, city_match, -index

        _, best = max(enumerate(results), key=rank)
        if not _passport_matches(name, best["title"], short_name):
            LOGGER.info(
                "Агрегатор: нет подходящего совпадения для %s (лучшее: %s)",
                name,
                best["title"],
            )
            return None
        return best

    def parse(self, url: str, city: str = "") -> AggregatorPassport:
        response = self.fetcher.get(url)
        if not _ok(response):
            return AggregatorPassport(source="vuzopedia", url=url)
        soup = BeautifulSoup(response.text, "lxml")
        name = _text(soup.find("h1"))
        # Статус и т. п. ищем в карточке вуза, а не во всей странице:
        # в блоках «похожие вузы» встречаются чужие признаки.
        text = _card_text(soup)

        city_value = city
        if not city_value:
            city_link = soup.select_one("a[href^='/region/city/']")
            city_value = _text(city_link)

        status_match = re.search(r"учредител[ья]\s*(гос|негос|частн)", text, re.IGNORECASE)
        if status_match:
            status = (
                "государственный"
                if status_match.group(1).lower().startswith("гос")
                else "частный"
            )
        else:
            status = _status_from_text(text)

        dormitory_match = re.search(r"общежити[ея]\s+(есть|нет)", text, re.IGNORECASE)
        dormitory = _yes_no(dormitory_match.group(0)) if dormitory_match else None
        military_match = re.search(
            r"(?:воен\.?\s*уч\.?\s*центр|военная\s+кафедра)\s+(есть|нет)",
            text,
            re.IGNORECASE,
        )
        military = _yes_no(military_match.group(0)) if military_match else None

        return AggregatorPassport(
            source="vuzopedia",
            url=url,
            name=name,
            city=city_value,
            status=status,
            dormitory=dormitory,
            military_department=military,
            faculties=self.parse_faculties(url),
            specialties=self.parse_specialties(url),
        )

    def parse_specialties(self, url: str) -> list[Specialty]:
        """Программы подготовки: бакалавриат/специалитет и магистратура."""

        return _specialties_from_pages(
            self.fetcher, [url.rstrip("/") + suffix for suffix in ("/programs/bakispec", "/programs/magistr")]
        )

    def parse_faculties(self, url: str) -> list[str]:
        response = self.fetcher.get(url.rstrip("/") + "/podrazdeleniya")
        if not _ok(response):
            return []
        soup = BeautifulSoup(response.text, "lxml")
        skip = (
            "подразделения", "поиск", "сортировка", "фильтр", "связаться",
            "задайте вопрос", "филиал",
        )
        # На Вузопедии факультеты лежат в карточках .itemVuzTitle.
        result = _faculty_titles(soup.select(".itemVuzTitle"), skip, 140)
        return result or _faculty_titles(soup.find_all("h2"), skip, 120)


class PostupiParser:
    """Парсер карточки вуза на Поступи.Онлайн."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def parse(self, url: str, city: str = "") -> AggregatorPassport:
        response = self.fetcher.get(url)
        if not _ok(response):
            return AggregatorPassport(source="postupi", url=url)
        soup = BeautifulSoup(response.text, "lxml")
        name = _text(soup.find("h1"))
        text = _card_text(soup)

        host = urlparse(url).netloc.lower().split(":")[0]
        subdomain = host.split(".")[0]
        city_value = city or CITY_BY_SUBDOMAIN.get(subdomain, "")

        lowered = text.lower()
        status = _status_from_text(text)

        dormitory = None
        if re.search(
            r"без\s+общежити|общежити[ея]\s+(?:нет|не\s+предоставля)|нет\s+общежити",
            lowered,
        ):
            dormitory = False
        elif re.search(r"(?:^|[,\s])общежити[ея](?:[,\s.]|$)", text, re.IGNORECASE):
            dormitory = True

        military = None
        if re.search(
            r"(?:нет|без)\s+(?:военн|воен\.)|(?:военн[а-яё]*\s+(?:кафедр[а-яё]*|учебн[а-яё]*\s+центр[а-яё]*)"
            r"|воен\.?\s*уч\.?\s*центр)\s+(?:нет|отсутств)",
            lowered,
        ):
            military = False
        elif re.search(
            r"воен\.?\s*уч\.?\s*центр|военн[а-яё]*\s+(?:кафедр|учебн[а-яё]*\s+центр)", text, re.IGNORECASE
        ):
            military = True

        specialties = _specialties_from_pages(
            self.fetcher,
            [
                url.rstrip("/") + suffix
                for suffix in ("/specialnosti/bakalavr/", "/specialnosti/magistr/", "/specialnosti/specialist/")
            ],
        )
        return AggregatorPassport(
            source="postupi",
            url=url,
            name=name,
            city=city_value,
            status=status,
            dormitory=dormitory,
            military_department=military,
            faculties=self.parse_faculties(url),
            specialties=specialties,
        )

    def parse_faculties(self, url: str) -> list[str]:
        response = self.fetcher.get(url.rstrip("/") + "/podrazdeleniya/")
        if not _ok(response):
            return []
        soup = BeautifulSoup(response.text, "lxml")
        skip = ("подразделения", "поиск", "сортировка", "фильтр", "связаться")
        return _faculty_titles(soup.find_all("h2"), skip, 120)


def _faculty_titles(nodes, skip: tuple[str, ...], max_length: int) -> list[str]:
    result: list[str] = []
    for node in nodes:
        value = _text(node)
        lowered = value.lower()
        if not value or len(value) > max_length:
            continue
        if any(marker in lowered for marker in skip):
            continue
        if not any(marker in lowered for marker in FACULTY_MARKERS):
            continue
        result.append(value)
    return list(dict.fromkeys(result))[:40]


class AggregatorService:
    """Резолвинг и объединение паспортов из агрегаторов."""

    def __init__(
        self,
        fetcher: StealthFetcher,
        *,
        sources: Iterable[str] = ("vuzopedia", "postupi"),
        direct_url: str = "",
    ) -> None:
        self.fetcher = fetcher
        self.sources = tuple(sources)
        self.direct_url = direct_url

    def _site_search_url(self, name: str, site: str, full_name: str = "") -> str:
        """Ссылка на карточку вуза в агрегаторе через веб-поиск (Яндекс → Google → …)."""

        from .news_search import NewsSearch

        domain = site.split("/")[0]
        for hit in NewsSearch(self.fetcher).web_search(f"site:{site} {name}"):
            href = _decode_search_url(hit.url)
            match = re.match(rf"(https?://(?:[\w-]+\.)?{re.escape(domain)}/vuz/[^/?#]+/)", href)
            if not match:
                continue
            title = normalize_whitespace(hit.title)
            if full_name and title and not (
                _passport_matches(full_name, title.split("|")[0].split(" - ")[0], name)
                or short_name_matches(name, title)
            ):
                continue
            # Подстраница (/programmy/, /otzyvy/) → корень карточки вуза.
            return match.group(1)
        return ""

    def collect(
        self, name: str, city: str = "", short_name: str = ""
    ) -> list[AggregatorPassport]:
        passports: list[AggregatorPassport] = []
        if self.direct_url:
            source = "vuzopedia" if "vuzopedia" in self.direct_url else "postupi"
            parser = (
                VuzopediaParser(self.fetcher)
                if source == "vuzopedia"
                else PostupiParser(self.fetcher)
            )
            passport = parser.parse(self.direct_url, city=city)
            if passport.name:
                passports.append(passport)
            return passports

        if not normalize_whitespace(name) and not normalize_whitespace(short_name):
            # Без названия нельзя проверить, что карточка агрегатора о нужном вузе.
            return passports

        if "vuzopedia" in self.sources:
            parser = VuzopediaParser(self.fetcher)
            match = parser.best_match(name or short_name, short_name=short_name, city=city)
            if match is None:
                # Поиск Вузопедии закрыт проверкой «не бот» — ищем карточку
                # через обычный поисковик.
                url = self._site_search_url(short_name or name, "vuzopedia.ru/vuz", full_name=name)
                match = {"url": url, "city": ""} if url else None
            if match:
                passport = parser.parse(match["url"], city=match.get("city") or city)
                if passport.name and _passport_matches(name or short_name, passport.name, short_name):
                    passports.append(passport)

        if "postupi" in self.sources:
            url = self._site_search_url(short_name or name, "postupi.online/vuz", full_name=name)
            if url:
                passport = PostupiParser(self.fetcher).parse(url, city=city)
                if passport.name and _passport_matches(name or short_name, passport.name, short_name):
                    passports.append(passport)

        return passports


PASSPORT_FIELDS = ("city", "status", "dormitory", "military_department")


def _filled(passport: AggregatorPassport) -> int:
    return sum(getattr(passport, field) not in (None, "") for field in PASSPORT_FIELDS)


def best_passport(passports: Iterable[AggregatorPassport]) -> AggregatorPassport | None:
    """Выбирает паспорт с максимальным числом заполненных полей."""

    items = list(passports)
    if not items:
        return None
    return max(items, key=lambda item: _filled(item) + len(item.faculties) + len(item.specialties))


def merge_passports(passports: Iterable[AggregatorPassport]) -> AggregatorPassport | None:
    """Объединяет паспорта: недостающие поля берутся из второго агрегатора."""

    items = list(passports)
    if not items:
        return None
    base = max(items, key=_filled)
    data = base.model_dump()
    for passport in items:
        for field in PASSPORT_FIELDS:
            if data.get(field) in (None, "") and getattr(passport, field) not in (None, ""):
                data[field] = getattr(passport, field)
    data["faculties"] = list(dict.fromkeys(
        faculty for passport in items for faculty in passport.faculties
    ))[:40]
    data["specialties"] = dedupe_specialties(
        specialty for passport in items for specialty in passport.specialties
    )
    data["source"] = "+".join(
        dict.fromkeys(passport.source for passport in items if passport.source)
    )
    return AggregatorPassport(**data)
