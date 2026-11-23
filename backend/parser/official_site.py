"""Автообнаружение официального сайта и страницы ректора.

Никаких заранее прописанных ссылок: поисковик находит официальный сайт,
затем на сайте ищется страница ректора, из неё извлекаются ФИО, фото,
статус, контакты приёмной и краткая биография.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlencode, urlparse

from bs4 import BeautifulSoup

from .models import Leadership
from .news_search import _clean_url
from .stealth import StealthFetcher
from .utils import (
    canonical_url,
    clean_email,
    extract_emails,
    extract_phones,
    find_person_names,
    find_photo,
    is_same_domain,
    looks_like_person_name,
    normalize_whitespace,
    truncate_text,
)

LOGGER = logging.getLogger(__name__)

EXCLUDED_DOMAINS = (
    "vuzopedia.ru",
    "postupi.online",
    "wikipedia.org",
    "wikidata.org",
    "vk.com",
    "ok.ru",
    "youtube.com",
    "youtu.be",
    "t.me",
    "dzen.ru",
    "mail.ru",
    "yandex.ru",
    "google.com",
    "bing.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "sberbank.ru",
    "sber.ru",
    "hh.ru",
    "rabota.ru",
    "avito.ru",
    "2gis.ru",
    "zoon.ru",
)

RECTOR_LINK_RE = re.compile(
    r"ректор|rector|руководств|management|administration|администрац",
    re.IGNORECASE,
)
RECTOR_TITLE_RE = re.compile(r"ректор|rector", re.IGNORECASE)
# «Форумы ректоров», «совет ректоров» — не страница ректора.
RECTOR_PLURAL_RE = re.compile(r"ректор(?:ов|ам|ами|ах|ы)\b", re.IGNORECASE)
RECTOR_WHOLE_RE = re.compile(r"\bректор\b", re.IGNORECASE)
ACTING_RE = re.compile(r"и\.?\s*о\.?\s*ректор|исполняющ[а-яё]*\s+обязанност", re.I)


class OfficialSiteDiscovery:
    """Находит сайт и страницу ректора без ручных ссылок."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def discover(
        self, university_name: str, short_name: str = ""
    ) -> tuple[Leadership | None, str, str]:
        site_url = self.find_site(university_name, short_name)
        if not site_url:
            LOGGER.info("Официальный сайт не найден: %s", university_name)
            return None, "", ""
        rector_url = self.find_rector_page(site_url)
        if not rector_url:
            LOGGER.info("Страница ректора не найдена: %s", site_url)
            return None, site_url, ""
        leader = self.extract_rector(rector_url)
        return leader, site_url, rector_url

    def find_site(self, university_name: str, short_name: str = "") -> str:
        wiki_site = self._wikipedia_site(university_name)
        if wiki_site:
            return wiki_site

        queries = []
        if short_name:
            queries.append(f'"{short_name}" официальный сайт')
        queries.extend(
            [
                f'"{university_name}" официальный сайт',
                f"{short_name or university_name} официальный сайт",
            ]
        )
        for query in queries:
            for url, title in self._search(query):
                host = urlparse(url).netloc.lower().split(":")[0]
                if not host or any(domain in host for domain in EXCLUDED_DOMAINS):
                    continue
                haystack = f"{title} {host}".lower()
                if short_name and short_name.lower() in haystack:
                    parsed = urlparse(url)
                    return f"{parsed.scheme}://{parsed.netloc}/"
                if not self._matches_name(title, host, university_name):
                    continue
                parsed = urlparse(url)
                return f"{parsed.scheme}://{parsed.netloc}/"
        return ""

    def find_rector_page(self, site_url: str) -> str:
        probed = self._probe_rector_paths(site_url)
        if probed:
            return probed

        response = self.fetcher.get(site_url)
        if response.text:
            soup = BeautifulSoup(response.text, "lxml")
            candidates: list[tuple[int, str]] = []
            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "")
                text = normalize_whitespace(anchor.get_text(" ", strip=True))
                haystack = f"{href} {text}".lower()
                if "проректор" in haystack or "prorector" in haystack:
                    score = 2
                elif RECTOR_PLURAL_RE.search(haystack):
                    score = 1
                elif RECTOR_WHOLE_RE.search(text) or "rector" in href.lower():
                    score = 20
                elif RECTOR_TITLE_RE.search(haystack):
                    score = 12
                elif "руководств" in haystack or "management" in haystack:
                    score = 6
                elif "администрац" in haystack or "administration" in haystack:
                    score = 4
                else:
                    continue
                absolute = canonical_url(href, site_url)
                if not is_same_domain(absolute, site_url):
                    continue
                candidates.append((score, absolute))
            if candidates:
                candidates.sort(key=lambda item: (-item[0], len(item[1])))
                return candidates[0][1]

        host = urlparse(site_url).netloc
        for url, title in self._search(f"site:{host} ректор"):
            if not is_same_domain(url, site_url):
                continue
            if "проректор" in f"{url} {title}".lower():
                continue
            return url
        return ""

    def _probe_rector_paths(self, site_url: str) -> str:
        """Проверяет типовые адреса страницы ректора без ручных ссылок."""

        base = site_url.rstrip("/")
        for path in (
            "/rector/",
            "/rector",
            "/ru/rector/",
            "/about/rector/",
            "/ru/about/rector/",
            "/rektor/",
            "/rukovodstvo/",
        ):
            url = base + path
            response = self.fetcher.get(url, timeout=12)
            if not response.text:
                continue
            soup = BeautifulSoup(response.text, "lxml")
            title = normalize_whitespace(
                (soup.title.get_text(" ", strip=True) if soup.title else "")
            )
            heading = normalize_whitespace(
                (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
            )
            haystack = f"{title} {heading}".lower()
            if "проректор" in haystack or "prorector" in haystack:
                continue
            if "ректор" in haystack or "rector" in haystack:
                return url
        return ""

    def extract_rector(self, page_url: str) -> Leadership | None:
        response = self.fetcher.get(page_url)
        if not response.text:
            return None
        soup = BeautifulSoup(response.text, "lxml")
        text = normalize_whitespace(soup.get_text(" ", strip=True))
        name = self._find_name(soup, text)
        if not name:
            LOGGER.info("Не удалось определить ФИО ректора: %s", page_url)
            return None
        name = self._normalize_name(name)

        profile_soup, profile_text, profile_url = self._follow_profile(
            soup, page_url, name
        )
        original_soup, original_url, original_text = soup, page_url, text
        if profile_soup is not None:
            soup = profile_soup
            text = profile_text
            page_url = profile_url or page_url

        acting = bool(ACTING_RE.search(text[:5000]))
        status = "и.о." if acting else "ректор"
        position = "и.о. ректора" if acting else "ректор"
        photo = (
            find_photo(soup, page_url, name)
            or find_photo(original_soup, original_url, name)
            or find_photo(soup, page_url, "ректор")
            or find_photo(original_soup, original_url, "ректор")
        )

        combined_text = f"{text} {original_text}"
        emails = [clean_email(value) for value in extract_emails(combined_text)]
        emails = [
            value
            for value in emails
            if value and not any(
                marker in value.lower()
                for marker in ("protocol", "press", "priem", "info")
            )
        ]
        phones = extract_phones(text) or extract_phones(original_text)
        bio = self._find_bio(soup, text) or self._find_bio(
            original_soup, original_text
        )

        return Leadership(
            full_name=name,
            position=position,
            status=status,
            bio=bio or None,
            email=emails[0] if emails else None,
            phone=phones[0] if phones else None,
            office_phone=phones[0] if phones else None,
            reception_phone=phones[0] if phones else None,
            reception_email=emails[0] if emails else None,
            photo_url=photo or None,
            source_url=page_url,
            source_quote=truncate_text(bio or text, 300),
            is_acting=acting,
        )

    def _follow_profile(
        self, soup: BeautifulSoup, page_url: str, name: str
    ) -> tuple[BeautifulSoup | None, str, str]:
        """Если на странице есть ссылка на профиль ректора — читает её."""

        tokens = [token for token in name.split() if len(token) >= 4]
        for anchor in soup.select("a[href]"):
            text = normalize_whitespace(anchor.get_text(" ", strip=True))
            if not text:
                continue
            lowered = text.lower()
            if tokens and not any(token.lower() in lowered for token in tokens):
                continue
            url = canonical_url(anchor.get("href", ""), page_url)
            if not is_same_domain(url, page_url) or url == page_url:
                continue
            response = self.fetcher.get(url)
            if not response.text:
                continue
            profile = BeautifulSoup(response.text, "lxml")
            profile_text = normalize_whitespace(profile.get_text(" ", strip=True))
            if tokens and not any(
                token.lower() in profile_text.lower() for token in tokens
            ):
                continue
            return profile, profile_text, url
        return None, "", ""

    @staticmethod
    def _normalize_name(name: str) -> str:
        parts = name.split()
        if len(parts) == 3 and re.search(
            r"(?:вич|вна|чна)$", parts[1].lower()
        ):
            return f"{parts[2]} {parts[0]} {parts[1]}"
        return name

    @staticmethod
    def _find_name(soup: BeautifulSoup, text: str) -> str:
        nav_words = {
            "все",
            "разделы",
            "студентам",
            "учеба",
            "расписание",
            "университет",
            "институт",
            "факультет",
            "школа",
            "центр",
            "россии",
            "высшая",
            "экономики",
            "федеральный",
            "ректор",
            "проректор",
            "образование",
            "наука",
            "новости",
            "контакты",
            "главная",
            "поступление",
            "история",
            "музей",
            "награды",
            "документы",
            "закупки",
            "библиотека",
            "совет",
            "состав",
            "преподавательский",
            "олимпиады",
            "курсы",
            "события",
            "проекты",
            "партнеры",
            "работодателям",
            "прессе",
            "сотрудникам",
            "выпускникам",
            "школьникам",
            "абитуриентам",
            "календарь",
            "услуги",
            "сервисы",
            "вход",
            "меню",
        }
        name_re = re.compile(
            r"(?=\b([А-ЯЁ][а-яё\-]{2,})\s+([А-ЯЁ][а-яё\-]{2,})"
            r"\s+([А-ЯЁ][а-яё\-]{2,})\b)"
        )
        candidates: list[tuple[int, str]] = []
        for match in name_re.finditer(text):
            parts = [match.group(1), match.group(2), match.group(3)]
            if any(part.lower() in nav_words for part in parts):
                continue
            name = " ".join(parts)
            if not looks_like_person_name(name):
                continue
            score = 0
            context = text[max(0, match.start() - 20) : match.start()]
            context_clean = context.lower().strip(" .:—–-/\n\t")
            if context_clean.endswith("ректор"):
                score += 10000
            nearest = min(
                (
                    abs(match.start() - pos)
                    for pos in re.finditer("ректор", text, re.IGNORECASE)
                    for pos in [pos.start()]
                ),
                default=10**6,
            )
            score += max(0, 1000 - nearest)
            if any(
                name.lower() in normalize_whitespace(heading.get_text(" ", strip=True)).lower()
                for heading in soup.find_all(["h1", "h2"])
            ):
                score += 500
            candidates.append((score, name))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        # 2. Явные конструкции «ректор ... ФИО» / «и.о. ректора ... ФИО».
        for pattern in (
            r"(?:и\.?\s*о\.?\s*)?ректор[а-яё]*\s*[:\-–—]?\s*([А-ЯЁ][^.;]{5,120})",
            r"([А-ЯЁ][^.;]{5,120})\s*[—-]\s*(?:и\.?\s*о\.?\s*)?ректор",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            names = [
                name
                for name in find_person_names(match.group(1))
                if not any(
                    word in name.lower()
                    for word in ("стал", "исполняющ", "обязанност")
                )
            ]
            if names:
                return names[0]

        # 3. Заголовки страницы.
        for heading in soup.find_all(["h1", "h2"]):
            value = normalize_whitespace(heading.get_text(" ", strip=True))
            if "проректор" in value.lower():
                continue
            names = find_person_names(value)
            if names:
                return names[0]

        # 4. Любое ФИО на странице, ближайшее к слову «ректор».
        names = [
            name
            for name in find_person_names(text)
            if not any(
                word in name.lower()
                for word in ("стал", "исполняющ", "обязанност")
            )
        ]
        if not names:
            return ""
        rector_pos = text.lower().find("ректор")
        if rector_pos < 0:
            return names[0]
        return min(
            names,
            key=lambda name: abs(text.lower().find(name.lower()) - rector_pos),
        )

    @staticmethod
    def _find_bio(soup: BeautifulSoup, text: str) -> str:
        markers = (
            "родил",
            "окончил",
            "кандидат наук",
            "доктор наук",
            "трудовая деятельность",
            "начал работать",
        )
        nav_markers = (
            "студентам",
            "расписание",
            "поступление",
            "контакты",
            "новости",
            "разделы",
            "олимпиады",
            "календарь",
            "меню",
            "личный кабинет",
            "расширенный поиск",
            "домашняя страница",
            "академическая деятельность",
            "общественная деятельность",
            "в новостях",
            "найти",
        )
        paragraphs: list[str] = []
        for block in soup.find_all(["p", "div", "section", "li"]):
            value = normalize_whitespace(block.get_text(" ", strip=True))
            if not 80 <= len(value) <= 1500:
                continue
            lowered = value.lower()
            if any(marker in lowered for marker in nav_markers):
                continue
            if any(marker in lowered for marker in markers):
                paragraphs.append(value)
        if not paragraphs:
            for line in re.split(r"(?<=[.!?])\s+", text):
                if 80 <= len(line) <= 700 and any(
                    marker in line.lower() for marker in markers
                ):
                    paragraphs.append(line)
        return truncate_text(" ".join(dict.fromkeys(paragraphs[:3])), 900)

    def _wikipedia_site(self, university_name: str) -> str:
        """Официальный сайт из Wikidata (P856) — надёжный fallback к поиску."""

        try:
            search = self._wiki_api(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": university_name,
                    "srlimit": 5,
                    "format": "json",
                }
            )
            pages = search.get("query", {}).get("search", [])
            title = ""
            for page in pages:
                candidate = page.get("title", "")
                lowered = candidate.lower()
                if any(
                    word in lowered
                    for word in ("университет", "институт", "академия", "школа")
                ):
                    title = candidate
                    break
            if not title and pages:
                title = pages[0].get("title", "")
            if not title:
                return ""

            infobox_site = self._wikipedia_infobox_site(title)
            if infobox_site:
                return infobox_site

            props = self._wiki_api(
                {
                    "action": "query",
                    "prop": "pageprops",
                    "titles": title,
                    "format": "json",
                }
            )
            page_map = props.get("query", {}).get("pages", {})
            qid = ""
            for page in page_map.values():
                qid = page.get("pageprops", {}).get("wikibase_item", "")
                if qid:
                    break
            if not qid:
                return ""

            response = self.fetcher.get(
                f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
            )
            payload = json.loads(response.text) if response.text else {}
            entity = payload.get("entities", {}).get(qid, {})
            for claim in entity.get("claims", {}).get("P856", []):
                value = (
                    claim.get("mainsnak", {})
                    .get("datavalue", {})
                    .get("value", "")
                )
                if isinstance(value, str) and value.startswith("http"):
                    parsed = urlparse(value)
                    site = f"https://{parsed.netloc}/"
                    probe = self.fetcher.get(site, timeout=12)
                    if probe.final_url:
                        final = urlparse(probe.final_url)
                        return f"{final.scheme}://{final.netloc}/"
                    if probe.status_code and probe.text:
                        return site
                    return ""
        except (ValueError, KeyError, TypeError) as exc:
            LOGGER.debug("Wikidata не дал сайт для %s: %s", university_name, exc)
        return ""

    def _wikipedia_infobox_site(self, title: str) -> str:
        url = "https://ru.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
        response = self.fetcher.get(url)
        if not response.text:
            return ""
        soup = BeautifulSoup(response.text, "lxml")
        for row in soup.select("table.infobox tr"):
            text = normalize_whitespace(row.get_text(" ", strip=True)).lower()
            if "сайт" not in text:
                continue
            anchor = row.select_one("a[href^='http']")
            if not anchor:
                continue
            value = anchor.get("href", "")
            if value.startswith("http"):
                parsed = urlparse(value)
                site = f"https://{parsed.netloc}/"
                probe = self.fetcher.get(site, timeout=12)
                if probe.final_url:
                    final = urlparse(probe.final_url)
                    return f"{final.scheme}://{final.netloc}/"
                if probe.status_code and probe.text:
                    return site
        return ""

    def _wiki_api(self, params: dict[str, object]) -> dict:
        url = "https://ru.wikipedia.org/w/api.php?" + urlencode(params)
        response = self.fetcher.get(url)
        if not response.text:
            return {}
        return json.loads(response.text)

    def _search(self, query: str) -> list[tuple[str, str]]:
        from urllib.parse import quote

        response = self.fetcher.get(
            f"https://www.bing.com/search?q={quote(query)}&setlang=ru"
        )
        if not response.text:
            return []
        soup = BeautifulSoup(response.text, "lxml")
        results: list[tuple[str, str]] = []
        for item in soup.select("li.b_algo"):
            anchor = item.select_one("h2 a")
            if not anchor:
                continue
            url = _clean_url(anchor.get("href", ""))
            title = normalize_whitespace(anchor.get_text(" ", strip=True))
            if url.startswith("http"):
                results.append((url, title))
        return results

    @staticmethod
    def _matches_name(title: str, host: str, name: str) -> bool:
        haystack = f"{title} {host}".lower()
        tokens = [
            token.lower()
            for token in re.findall(r"[А-ЯЁA-Zа-яёa-z\-]{3,}", name)
        ]
        if tokens:
            return any(token in haystack for token in tokens)
        return normalize_whitespace(name).lower() in haystack

def resolve_official_site(
    university_name: str,
    short_name: str = "",
    *,
    timeout: float = 25.0,
    enable_playwright: bool = False,
) -> str:
    """??????? ????????? ??????: ????? ??????????? ???? ?? ???????? ????."""

    fetcher = StealthFetcher(
        timeout=timeout, enable_playwright=enable_playwright
    )
    try:
        return OfficialSiteDiscovery(fetcher).find_site(
            university_name, short_name
        )
    finally:
        fetcher.close()
