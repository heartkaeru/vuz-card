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

from .models import AggregatorPassport
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

    q = normalize_whitespace(query).lower().strip(" .,;:-")
    t = normalize_whitespace(title).lower().strip(" .,;:-")
    if not q or not t:
        return 0.0
    if q == t:
        return 100.0
    if t.startswith(q):
        remainder = t[len(q):].lstrip(" .,;:-")
        if remainder.startswith(NAME_SUFFIX_MARKERS):
            # «имени ...» уточняет название, а не меняет вуз.
            return 95.0 - min(len(remainder), 40) * 0.05
        return 60.0 - min(len(remainder), 60) * 0.1
    q_tokens = q.split()
    t_tokens = set(t.split())
    if q_tokens and all(token in t_tokens for token in q_tokens):
        return 45.0
    return 30.0 * SequenceMatcher(None, t, q).ratio()


def _query_is_specific(query: str) -> bool:
    return len(normalize_whitespace(query).split()) >= 3


def _passport_matches(query: str, title: str) -> bool:
    if not _query_is_specific(query):
        return True
    return name_match_score(query, title) >= NAME_MATCH_THRESHOLD

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


class VuzopediaParser:
    """Парсер карточки вуза на Вузопедии."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def search(self, name: str) -> list[dict[str, str]]:
        query = quote(name)
        url = f"{VUZOPEDIA_BASE}/search/results?q={query}&search_type=all"
        response = self.fetcher.get(url)
        if not response.text:
            return []
        soup = BeautifulSoup(response.text, "lxml")
        results: list[dict[str, str]] = []
        for card in soup.select("article.ps-result-card"):
            anchor = card.select_one("a.ps-result-title") or card.select_one(
                "h3 a"
            )
            if not anchor:
                continue
            href = anchor.get("href", "")
            if not href.startswith("/vuz/"):
                continue
            tags = [
                _text(tag)
                for tag in card.select(".ps-catalog-card__tags span")
                if _text(tag)
            ]
            results.append(
                {
                    "title": _text(anchor),
                    "url": canonical_url(href, VUZOPEDIA_BASE),
                    "city": tags[1] if len(tags) > 1 else "",
                }
            )
        return results

    def best_match(self, name: str) -> dict[str, str] | None:
        results = self.search(name)
        if not results:
            return None
        best = max(results, key=lambda item: name_match_score(name, item["title"]))
        if not _passport_matches(name, best["title"]):
            LOGGER.info(
                "Агрегатор: нет подходящего совпадения для %s (лучшее: %s)",
                name,
                best["title"],
            )
            return None
        return best

    def parse(self, url: str, city: str = "") -> AggregatorPassport:
        response = self.fetcher.get(url)
        if not response.text:
            source = "vuzopedia" if "Vuzopedia" in type(self).__name__ else "postupi"
            return AggregatorPassport(source=source, url=url)
        soup = BeautifulSoup(response.text, "lxml")
        name = _text(soup.find("h1"))
        text = _text(soup)

        city_value = city
        if not city_value:
            city_link = soup.select_one("a[href^='/region/city/']")
            city_value = _text(city_link)

        status = ""
        status_match = re.search(
            r"учредител[ья]\s*(гос|негос|частн)", text, re.IGNORECASE
        )
        if status_match:
            status = (
                "государственный"
                if status_match.group(1).lower().startswith("гос")
                else "частный"
            )
        elif "негосударственн" in text.lower() or "частный" in text.lower():
            status = "частный"
        elif "государственный" in text.lower():
            status = "государственный"

        dormitory = _yes_no(
            (re.search(r"общежитие\s+(есть|нет)", text, re.IGNORECASE) or [""])[0]
        )
        military_match = re.search(
            r"(?:воен\.?\s*уч\.?\s*центр|военная\s+кафедра)\s+(есть|нет)",
            text,
            re.IGNORECASE,
        )
        military = _yes_no(military_match.group(0)) if military_match else None

        budget_places = paid_places = None
        avg_ege = None
        for card in soup.select(".vxStatCard"):
            label = _text(card.select_one(".vxStatCard__label")).lower()
            value = _text(card.select_one(".vxStatCard__value"))
            if "бюджетных мест" in label:
                budget_places = _int(value)
            elif "платных мест" in label:
                paid_places = _int(value)
            elif "средний балл" in label:
                avg_ege = _float(value)

        if avg_ege is None:
            match = re.search(
                r"средний\s+балл[^0-9]{0,80}(\d{2}[.,]\d+)",
                text,
                re.IGNORECASE,
            )
            if match:
                avg_ege = _float(match.group(1))

        faculties = self.parse_faculties(url)
        return AggregatorPassport(
            source="vuzopedia",
            url=url,
            name=name,
            city=city_value,
            status=status,
            avg_ege=avg_ege,
            budget_places=budget_places,
            paid_places=paid_places,
            dormitory=dormitory,
            military_department=military,
            faculties=faculties,
        )

    def parse_faculties(self, url: str) -> list[str]:
        page_url = url.rstrip("/") + "/podrazdeleniya"
        response = self.fetcher.get(page_url)
        if not response.text:
            return []
        soup = BeautifulSoup(response.text, "lxml")
        result: list[str] = []
        skip = (
            "подразделения",
            "поиск",
            "сортировка",
            "фильтр",
            "связаться",
            "задайте вопрос",
            "филиал",
        )
        # На Вузопедии факультеты лежат в карточках .itemVuzTitle.
        for node in soup.select(".itemVuzTitle"):
            value = _text(node)
            lowered = value.lower()
            if not value or len(value) > 140:
                continue
            if any(marker in lowered for marker in skip):
                continue
            if not any(
                marker in lowered
                for marker in ("факультет", "институт", "школа", "центр", "кафедра")
            ):
                continue
            result.append(value)
        if result:
            return list(dict.fromkeys(result))[:40]

        for heading in soup.find_all("h2"):
            value = _text(heading)
            lowered = value.lower()
            if not value or len(value) > 120:
                continue
            if any(marker in lowered for marker in skip):
                continue
            if not any(
                marker in lowered
                for marker in ("факультет", "институт", "школа", "центр", "кафедра")
            ):
                continue
            result.append(value)
        return list(dict.fromkeys(result))[:40]


class PostupiParser:
    """Парсер карточки вуза на Поступи.Онлайн."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def parse(self, url: str, city: str = "") -> AggregatorPassport:
        response = self.fetcher.get(url)
        if not response.text:
            source = "vuzopedia" if "Vuzopedia" in type(self).__name__ else "postupi"
            return AggregatorPassport(source=source, url=url)
        soup = BeautifulSoup(response.text, "lxml")
        name = _text(soup.find("h1"))
        text = _text(soup)

        host = urlparse(url).netloc.lower().split(":")[0]
        subdomain = host.split(".")[0]
        city_value = city or CITY_BY_SUBDOMAIN.get(subdomain, "")

        lowered = text.lower()
        if "негосударственн" in lowered or "частный" in lowered:
            status = "частный"
        elif "государственный" in lowered:
            status = "государственный"
        else:
            status = ""

        dormitory = None
        if re.search(r"(?:^|[,\s])общежитие(?:[,\s]|$)", text, re.IGNORECASE):
            dormitory = True
        elif "без общежития" in lowered or "общежитие нет" in lowered:
            dormitory = False

        military = None
        if re.search(
            r"воен\.?\s*уч\.?\s*центр|военная\s+кафедра", text, re.IGNORECASE
        ):
            military = True

        budget_match = re.search(
            r"(\d[\d\s]*)\s*бюджет\.?\s*мест", text, re.IGNORECASE
        )
        paid_match = re.search(
            r"(\d[\d\s]*)\s*платных\s*мест", text, re.IGNORECASE
        )
        budget_places = _int(budget_match.group(1)) if budget_match else None
        paid_places = _int(paid_match.group(1)) if paid_match else None

        avg_ege = self._average_ege(soup)
        faculties = self.parse_faculties(url)

        return AggregatorPassport(
            source="postupi",
            url=url,
            name=name,
            city=city_value,
            status=status,
            avg_ege=avg_ege,
            budget_places=budget_places,
            paid_places=paid_places,
            dormitory=dormitory,
            military_department=military,
            faculties=faculties,
        )

    @staticmethod
    def _average_ege(soup: BeautifulSoup) -> float | None:
        for slide in soup.select(".swiper-score__slide"):
            spans = [_text(span) for span in slide.find_all("span")]
            if len(spans) < 3 or not spans[0].isdigit():
                continue
            values = [
                _float(value)
                for value in spans[1:]
                if re.fullmatch(r"\d+(?:[.,]\d+)?", value)
            ]
            values = [value for value in values if value is not None]
            if values:
                return values[0]
        return None

    def parse_faculties(self, url: str) -> list[str]:
        page_url = url.rstrip("/") + "/podrazdeleniya/"
        response = self.fetcher.get(page_url)
        if not response.text:
            return []
        soup = BeautifulSoup(response.text, "lxml")
        result: list[str] = []
        skip = ("подразделения", "поиск", "сортировка", "фильтр", "связаться")
        for heading in soup.find_all("h2"):
            value = _text(heading)
            lowered = value.lower()
            if not value or len(value) > 120:
                continue
            if any(marker in lowered for marker in skip):
                continue
            if not any(
                marker in lowered
                for marker in ("факультет", "институт", "школа", "центр", "кафедра")
            ):
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

    def _ddg_site_url(self, name: str, site: str) -> str:
        query = quote(f"site:{site} {name}")
        response = self.fetcher.get(
            f"https://html.duckduckgo.com/html/?q={query}"
        )
        if not response.text:
            return ""
        soup = BeautifulSoup(response.text, "lxml")
        for anchor in soup.select("a.result__a"):
            href = _decode_ddg_url(anchor.get("href", ""))
            if site.split("/")[0] in href and "/vuz/" in href:
                return href
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

        if "vuzopedia" in self.sources:
            parser = VuzopediaParser(self.fetcher)
            match = parser.best_match(name)
            if match:
                passport = parser.parse(match["url"], city=match.get("city", city))
                if passport.name and _passport_matches(name, passport.name):
                    passports.append(passport)

        if "postupi" in self.sources:
            url = self._ddg_site_url(short_name or name, "postupi.online/vuz")
            if url:
                passport = PostupiParser(self.fetcher).parse(url, city=city)
                if passport.name and _passport_matches(name, passport.name):
                    passports.append(passport)

        return passports


def best_passport(passports: Iterable[AggregatorPassport]) -> AggregatorPassport | None:
    """Выбирает паспорт с максимальным числом заполненных полей."""

    def score(passport: AggregatorPassport) -> int:
        values = (
            passport.city,
            passport.status,
            passport.avg_ege,
            passport.budget_places,
            passport.paid_places,
            passport.dormitory,
            passport.military_department,
        )
        return sum(value is not None and value != "" for value in values) + len(
            passport.faculties
        )

    items = list(passports)
    return max(items, key=score) if items else None


def merge_passports(passports: Iterable[AggregatorPassport]) -> AggregatorPassport | None:
    """Объединяет паспорта: недостающие поля берутся из второго агрегатора."""

    items = list(passports)
    if not items:
        return None
    base = max(
        items,
        key=lambda passport: sum(
            value is not None and value != ""
            for value in (
                passport.city,
                passport.status,
                passport.avg_ege,
                passport.budget_places,
                passport.paid_places,
                passport.dormitory,
                passport.military_department,
            )
        ),
    )
    data = base.model_dump()
    for passport in items:
        for field in (
            "city",
            "status",
            "avg_ege",
            "budget_places",
            "paid_places",
            "dormitory",
            "military_department",
        ):
            if data.get(field) in (None, "") and getattr(passport, field) not in (
                None,
                "",
            ):
                data[field] = getattr(passport, field)
    faculties: list[str] = []
    for passport in items:
        faculties.extend(passport.faculties)
    data["faculties"] = list(dict.fromkeys(faculties))[:40]
    data["source"] = "+".join(
        dict.fromkeys(passport.source for passport in items if passport.source)
    )
    return AggregatorPassport(**data)
