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
    registrable_domain,
    slugify,
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
    "ucheba.ru",
    "tabiturient.ru",
    "studika.ru",
    "vuzoteka.ru",
    "propostuplenie.ru",
    "abiturient.ru",
    "unipage.net",
    "univerlist.com",
    "edunetwork.ru",
    "minobrnauki.gov.ru",
    "gosuslugi.ru",
    "obrnadzor.gov.ru",
    "rbc.ru",
    "tass.ru",
    "ria.ru",
    "rg.ru",
    "kommersant.ru",
    "vedomosti.ru",
    "interfax.ru",
    "lenta.ru",
    "habr.com",
    "github.com",
    "linkedin.com",
    "telegram.org",
    "rutube.ru",
    "list-org.com",
    "rusprofile.ru",
    "checko.ru",
    "zachestnyibiznes.ru",
)

# Общие слова названий: совпадение только по ним не подтверждает вуз.
GENERIC_NAME_WORDS = {
    "университет", "университета", "государственный", "государственного",
    "национальный", "исследовательский", "федеральный", "российский",
    "институт", "академия", "высшая", "школа", "технический", "имени",
    "президента", "россии", "первого", "федеральное", "государственное",
    "бюджетное", "автономное", "образовательное", "учреждение", "высшего",
    "образования", "официальный", "сайт",
}

# «ректор» как отдельное слово: не «проректор» и не «директор».
RECTOR_WORD_RE = re.compile(
    r"(?<![а-яёa-z])(?:ректор[а-яё]{0,2}|rector|rektor)(?![а-яёa-z])", re.IGNORECASE
)
RECTOR_HREF_RE = re.compile(r"(?<![a-z])(?:rector|rektor)(?![a-z])", re.IGNORECASE)
GENERIC_RECTOR_MAILBOXES = ("rector", "rectorat", "rektor", "priemnaya", "priemn", "reception", "secretar", "office", "kanc")


def host_is_excluded(host: str) -> bool:
    host = (host or "").lower().strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in EXCLUDED_DOMAINS)


def distinctive_tokens(name: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[а-яёa-z\-]{3,}", (name or "").lower().replace("ё", "е"))
        if token not in GENERIC_NAME_WORDS
    ]

RECTOR_LINK_RE = re.compile(
    r"ректор|rector|руководств|management|administration|администрац",
    re.IGNORECASE,
)
RECTOR_TITLE_RE = re.compile(r"(?<!ди)ректор|rector", re.IGNORECASE)
# «Форумы ректоров», «совет ректоров» — не страница ректора.
RECTOR_PLURAL_RE = re.compile(r"ректор(?:ов|ам|ами|ах|ы)\b", re.IGNORECASE)
RECTOR_WHOLE_RE = re.compile(r"\bректор\b", re.IGNORECASE)
ACTING_RE = re.compile(
    r"(?<![а-яё])(?:и\.?\s*о\.?\s*ректор|врио\s+ректор|временно\s+исполняющ|исполняющ[а-яё]*\s+обязанност[а-яё]*\s+ректор)",
    re.I,
)
# Слова интерфейса, которые не могут быть частью ФИО.
NAME_STOPWORDS = {
    "поиск",
    "русский",
    "английский",
    "english",
    "версия",
    "меню",
    "главная",
    "новости",
    "контакты",
    "ректор",
    "проректор",
    "директор",
}

_MORPH_TRIED = False
_MORPH_ANALYZER = None


def _morph_analyzer():
    """Ленивая инициализация pymorphy3 (необязательная зависимость)."""

    global _MORPH_TRIED, _MORPH_ANALYZER
    if not _MORPH_TRIED:
        _MORPH_TRIED = True
        try:
            import pymorphy3

            _MORPH_ANALYZER = pymorphy3.MorphAnalyzer()
        except Exception:  # noqa: BLE001 - морфология не критична
            _MORPH_ANALYZER = None
    return _MORPH_ANALYZER


def _is_patronymic(word: str) -> bool:
    morph = _morph_analyzer()
    if morph is None:
        return bool(re.search(r"(?:вич|вна|чна)$", word.lower()))
    return any("Patr" in parsed.tag.grammemes for parsed in morph.parse(word))


def _nominative(word: str) -> str:
    """Возвращает ФИО-токен в именительном падеже, если это безопасно."""

    morph = _morph_analyzer()
    if morph is None or len(word) < 4:
        return word
    parses = [
        parsed
        for parsed in morph.parse(word)
        if {"Surn", "Name", "Patr"} & parsed.tag.grammemes
    ]
    if not parses:
        return word
    if any("nomn" in parsed.tag.grammemes for parsed in parses):
        return word
    for parsed in parses:
        if "anim" not in parsed.tag.grammemes:
            continue
        inflected = parsed.inflect({"nomn", "sing"})
        if inflected is not None and inflected.word:
            return "-".join(part.capitalize() for part in inflected.word.split("-"))
    return word


def _looks_like_first_name(word: str) -> bool:
    """Личное имя (Илья, Анна) по словарю pymorphy3; без словаря — нет."""

    morph = _morph_analyzer()
    if morph is None:
        return False
    return any("Name" in parsed.tag.grammemes for parsed in morph.parse(word))


class OfficialSiteDiscovery:
    """Находит сайт и страницу ректора без ручных ссылок."""

    def __init__(self, fetcher: StealthFetcher) -> None:
        self.fetcher = fetcher

    def discover(
        self, university_name: str, short_name: str = "", site_url: str = ""
    ) -> tuple[Leadership | None, str, str]:
        """Ищет ректора. Если официальный сайт уже известен, он не ищется заново."""

        site_url = site_url or self.find_site(university_name, short_name)
        if not site_url:
            LOGGER.info("Официальный сайт не найден: %s", university_name)
            return None, "", ""
        rector_url = self.find_rector_page(site_url)
        if not rector_url:
            LOGGER.info("Страница ректора не найдена: %s", site_url)
            return None, site_url, ""
        leader = self.extract_rector(rector_url)
        return leader, site_url, rector_url

    # ------------------------------------------------------------------
    # Официальный сайт
    # ------------------------------------------------------------------
    def find_site(self, university_name: str, short_name: str = "") -> str:
        site = self._find_site(university_name, short_name)
        return self._prefer_root(site, university_name, short_name) if site else ""

    def _prefer_root(self, site: str, university_name: str, short_name: str) -> str:
        """Поддомен (school.hse.ru) → основной сайт вуза (hse.ru), если он о том же вузе."""

        parsed = urlparse(site)
        host = parsed.netloc.lower().split(":")[0]
        bare = host[4:] if host.startswith("www.") else host
        root = registrable_domain(bare)
        if not root or root == bare:
            return site
        for candidate in (f"https://www.{root}/", f"https://{root}/"):
            verified = self._verify_site(candidate, university_name, short_name)
            if verified:
                return verified
        return site

    def _find_site(self, university_name: str, short_name: str = "") -> str:
        if not normalize_whitespace(university_name) and not normalize_whitespace(short_name):
            return ""
        wiki_site = self._wikipedia_site(university_name or short_name, short_name)
        if wiki_site:
            return wiki_site

        queries = []
        if short_name:
            queries.append(f'"{short_name}" официальный сайт')
        if university_name:
            queries.append(f'"{university_name}" официальный сайт')
        queries.append(f"{short_name or university_name} официальный сайт")
        checked: set[str] = set()
        for query in queries:
            for url, title in self._search(query):
                parsed = urlparse(url)
                host = parsed.netloc.lower().split(":")[0]
                if not host or host_is_excluded(host):
                    continue
                root = f"{parsed.scheme}://{parsed.netloc}/"
                if root in checked:
                    continue
                if not self._matches_name(title, host, university_name, short_name):
                    continue
                checked.add(root)
                # Заголовок выдачи может врать: подтверждаем по главной странице.
                verified = self._verify_site(root, university_name, short_name)
                if verified:
                    return verified
        return ""

    def _verify_site(self, site_url: str, university_name: str, short_name: str = "") -> str:
        """Главная страница доступна и действительно о нашем вузе."""

        response = self.fetcher.get(site_url, timeout=15)
        if not _response_ok(response):
            return ""
        final = urlparse(response.final_url or site_url)
        if final.netloc and host_is_excluded(final.netloc.split(":")[0]):
            return ""
        soup = BeautifulSoup(response.text, "lxml")
        head = " ".join(
            normalize_whitespace(node.get_text(" ", strip=True))
            for node in soup.find_all(["title", "h1"])[:5]
        )
        text = f"{head} {normalize_whitespace(soup.get_text(' ', strip=True))[:8000]}"
        if not self._text_mentions(text, university_name, short_name):
            return ""
        scheme = final.scheme or "https"
        return _origin(scheme, final.netloc or urlparse(site_url).netloc)

    @staticmethod
    def _text_mentions(text: str, university_name: str, short_name: str = "") -> bool:
        folded = (text or "").lower().replace("ё", "е")
        short = normalize_whitespace(short_name).lower().replace("ё", "е")
        if short and re.search(rf"(?<![а-яa-z0-9]){re.escape(short)}(?![а-яa-z0-9])", folded):
            return True
        full = normalize_whitespace(university_name).lower().replace("ё", "е")
        if full and full in folded:
            return True
        tokens = distinctive_tokens(university_name)
        if not tokens:
            return False
        stems = {token[: max(4, len(token) - 2)] for token in tokens}
        return sum(1 for stem in stems if stem in folded) >= min(2, len(stems))

    # ------------------------------------------------------------------
    # Страница ректора
    # ------------------------------------------------------------------
    def find_rector_page(self, site_url: str) -> str:
        probed = self._probe_rector_paths(site_url)
        if probed:
            return probed

        homepage = self._fetch_soup(site_url)
        found = self._scan_page_for_rector(homepage, site_url, 12)
        if found:
            return found

        # Ссылку «Ректор» часто держат на официальной странице структуры.
        for page_url in self._structure_pages(homepage, site_url):
            if page_url == site_url:
                continue
            structure = self._fetch_soup(page_url)
            found = self._scan_page_for_rector(structure, site_url, 0)
            if found:
                return found

        found = self._scan_page_for_rector(homepage, site_url, 0)
        if found:
            return found

        host = urlparse(site_url).netloc
        for url, title in self._search(f"site:{host} ректор"):
            if not is_same_domain(url, site_url):
                continue
            haystack = f"{url} {title}"
            if "проректор" in haystack.lower() or not (
                RECTOR_WORD_RE.search(haystack) or RECTOR_HREF_RE.search(url)
            ):
                continue
            if self._is_news_url(url):
                continue
            return url
        return ""

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        response = self.fetcher.get(url)
        if not _response_ok(response):
            return None
        return BeautifulSoup(response.text, "lxml")

    def _structure_pages(
        self, soup: BeautifulSoup | None, site_url: str
    ) -> list[str]:
        pages: list[str] = [site_url.rstrip("/") + "/sveden/struct/"]
        if soup is not None:
            for anchor in soup.select("a[href]"):
                href = anchor.get("href", "")
                text = normalize_whitespace(
                    anchor.get_text(" ", strip=True)
                ).lower()
                lowered = href.lower()
                if not (
                    "структура и органы управления" in text
                    or "sveden/struct" in lowered
                    or re.search(r"/(?:official/)?struct(?:ure)?/?$", lowered)
                ):
                    continue
                absolute = canonical_url(href, site_url)
                if is_same_domain(absolute, site_url) and absolute not in pages:
                    pages.append(absolute)
        return pages

    @staticmethod
    def _is_news_url(url: str) -> bool:
        lowered = url.lower()
        return bool(
            re.search(r"/(news|novosti|press|events|announce|anons)(/|$)", lowered)
            or re.search(r"/20\d{2}/\d{1,2}/", lowered)
            or re.search(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", lowered)
        )

    def _scan_page_for_rector(
        self, soup: BeautifulSoup | None, site_url: str, min_score: int = 0
    ) -> str:
        if soup is None:
            return ""
        candidates: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            text = normalize_whitespace(anchor.get_text(" ", strip=True))
            haystack = f"{href} {text}".lower()
            if "проректор" in haystack or "prorector" in haystack:
                continue
            if RECTOR_PLURAL_RE.search(haystack):
                continue
            if RECTOR_WHOLE_RE.search(text) or RECTOR_HREF_RE.search(href):
                score = 20
            elif RECTOR_WORD_RE.search(haystack):
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
            if self._is_news_url(absolute):
                # Новость «директор института...» — не страница ректора.
                continue
            candidates.append((score, absolute))
        candidates = [item for item in candidates if item[0] >= min_score]
        if not candidates:
            return ""
        candidates.sort(key=lambda item: (-item[0], len(item[1])))
        return candidates[0][1]

    def _probe_rector_paths(self, site_url: str) -> str:
        """Проверяет типовые адреса страницы ректора без ручных ссылок."""

        base = site_url.rstrip("/")
        for path in (
            "/rector/",
            "/ru/rector/",
            "/about/rector/",
            "/ru/about/rector/",
            "/rektor/",
            "/university/rector/",
            "/rukovodstvo/",
        ):
            url = base + path
            response = self.fetcher.get(url, timeout=12)
            # 404/«мягкий 404» с редиректом на главную — не страница ректора.
            if not _response_ok(response):
                continue
            final_url = response.final_url or url
            if not is_same_domain(final_url, site_url):
                continue
            if urlparse(final_url).path.strip("/") == "":
                continue
            soup = BeautifulSoup(response.text, "lxml")
            title = normalize_whitespace(
                (soup.title.get_text(" ", strip=True) if soup.title else "")
            )
            heading = normalize_whitespace(
                (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
            )
            haystack = f"{title} {heading}"
            if "проректор" in haystack.lower() or "prorector" in haystack.lower():
                continue
            if RECTOR_WORD_RE.search(haystack) or (
                path == "/rukovodstvo/" and "руководств" in haystack.lower()
            ):
                return final_url
        return ""

    # ------------------------------------------------------------------
    # Данные ректора
    # ------------------------------------------------------------------
    @staticmethod
    def _content_soup(soup: BeautifulSoup) -> BeautifulSoup:
        """Копия страницы без шапки, меню и подвала (там общие контакты)."""

        copy = BeautifulSoup(str(soup), "lxml")
        for node in copy.select(
            "script, style, noscript, header, footer, nav, aside, form, "
            ".menu, .navbar, .breadcrumbs, .breadcrumb, #header, #footer, #menu"
        ):
            if node.name == "header" and node.find_parent(["main", "article"]) is not None:
                continue
            node.decompose()
        return copy

    def extract_rector(self, page_url: str) -> Leadership | None:
        response = self.fetcher.get(page_url)
        if not _response_ok(response):
            return None
        soup = BeautifulSoup(response.text, "lxml")
        content = self._content_soup(soup)
        text = normalize_whitespace(content.get_text(" ", strip=True))
        name = self._find_name(content, text)
        if not name:
            LOGGER.info("Не удалось определить ФИО ректора: %s", page_url)
            return None
        name = self._normalize_name(name)
        if any(part.lower().strip(".,") in NAME_STOPWORDS for part in name.split()):
            LOGGER.info("ФИО ректора похоже на заголовок страницы: %s", name)
            return None
        if not self._page_looks_like_rector(page_url, content, text, name):
            LOGGER.info(
                "Страница %s не похожа на страницу ректора (%s)", page_url, name
            )
            return None

        profile_soup, _, profile_url = self._follow_profile(content, page_url, name)
        original_soup, original_url, original_text = soup, page_url, text
        if profile_soup is not None:
            soup = profile_soup
            content = self._content_soup(profile_soup)
            text = normalize_whitespace(content.get_text(" ", strip=True))
            page_url = profile_url or page_url

        acting = self._is_acting(text, name) or self._is_acting(original_text, name)
        status = "и.о." if acting else "ректор"
        position = "и.о. ректора" if acting else "ректор"
        photo = (
            find_photo(soup, page_url, name)
            or find_photo(original_soup, original_url, name)
        )

        combined_text = f"{text} {original_text}"
        emails = [clean_email(value) for value in extract_emails(combined_text)]
        emails = [value for value in dict.fromkeys(emails) if value]
        personal_emails = [value for value in emails if self._email_matches(value, name)]
        reception_emails = [
            value
            for value in emails
            if any(marker in value.split("@")[0].lower() for marker in GENERIC_RECTOR_MAILBOXES)
        ]
        reception_phone = self._labelled_phone(text) or self._labelled_phone(original_text)
        phones = extract_phones(text) or extract_phones(original_text)
        bio = self._find_bio(content, text) or self._find_bio(
            self._content_soup(original_soup), original_text
        )

        return Leadership(
            full_name=name,
            position=position,
            status=status,
            bio=bio or None,
            email=(personal_emails or reception_emails or [None])[0],
            phone=reception_phone or (phones[0] if phones else None),
            office_phone=phones[0] if phones else None,
            reception_phone=reception_phone or None,
            reception_email=(reception_emails[0] if reception_emails else None),
            photo_url=photo or None,
            source_url=page_url,
            source_quote=truncate_text(bio or text, 300),
            is_acting=acting,
        )

    @staticmethod
    def _is_acting(text: str, name: str) -> bool:
        """«И.о. ректора» рядом с ФИО, а не где-то в меню или новостях."""

        surname = (name.split() or [""])[0].lower()
        lowered = (text or "").lower()
        for match in ACTING_RE.finditer(lowered):
            window = lowered[max(0, match.start() - 200): match.end() + 200]
            if not surname or surname[:-1] in window:
                return True
        return False

    @staticmethod
    def _email_matches(email: str, name: str) -> bool:
        local = re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())
        for token in re.findall(r"[А-ЯЁа-яё\-]{4,}", name or ""):
            translit = re.sub(r"[^a-z0-9]", "", slugify(token))
            if len(translit) >= 4 and translit[:6] in local:
                return True
        return False

    @staticmethod
    def _labelled_phone(text: str) -> str:
        """Телефон с подписью приёмной/секретаря."""

        # «Приёмная комиссия» — это приём абитуриентов, не приёмная ректора.
        for match in re.finditer(
            r"при[её]мн[а-яё]*+(?!\s+комисс)|секретар[а-яё]*|канцеляр[а-яё]*", text or "", re.IGNORECASE
        ):
            phones = extract_phones(text[match.end(): match.end() + 120])
            if phones:
                return phones[0]
        return ""

    def _follow_profile(
        self, soup: BeautifulSoup, page_url: str, name: str
    ) -> tuple[BeautifulSoup | None, str, str]:
        """Если на странице есть ссылка на профиль ректора — читает её."""

        tokens = [token.lower() for token in name.split() if len(token) >= 4]
        if not tokens:
            return None, "", ""
        attempts = 0
        for anchor in soup.select("a[href]"):
            if attempts >= 3:
                break
            text = normalize_whitespace(anchor.get_text(" ", strip=True)).lower()
            # Ссылка должна называть человека (минимум два слова ФИО), а не
            # просто упоминать фамилию в заголовке новости.
            if sum(1 for token in tokens if token[:-1] in text) < min(2, len(tokens)):
                continue
            url = canonical_url(anchor.get("href", ""), page_url)
            if not is_same_domain(url, page_url) or url == page_url or self._is_news_url(url):
                continue
            attempts += 1
            response = self.fetcher.get(url)
            if not _response_ok(response):
                continue
            profile = BeautifulSoup(response.text, "lxml")
            profile_text = normalize_whitespace(profile.get_text(" ", strip=True))
            if not any(token in profile_text.lower() for token in tokens):
                continue
            return profile, profile_text, url
        return None, "", ""

    @staticmethod
    def _page_looks_like_rector(
        page_url: str, soup: BeautifulSoup, text: str, name: str
    ) -> bool:
        """Отсекает случайные страницы, где ФИО просто встретилось в тексте."""

        path = urlparse(page_url).path.lower()
        if RECTOR_HREF_RE.search(path) or re.search(r"rukovoditel|rukovodstvo", path):
            return True
        tokens = [token for token in name.split() if len(token) >= 4]
        if not tokens:
            return False
        headings = " ".join(
            normalize_whitespace(heading.get_text(" ", strip=True)).lower()
            for heading in soup.find_all(["h1", "h2"])
        )
        if not any(token.lower() in headings for token in tokens):
            return False
        for match in RECTOR_WORD_RE.finditer(text):
            window = text[max(0, match.start() - 300) : match.start() + 300].lower()
            if any(token.lower() in window for token in tokens):
                return True
        return False

    @staticmethod
    def _normalize_name(name: str) -> str:
        parts = name.split()
        if len(parts) == 3 and _is_patronymic(parts[1]):
            parts = [parts[2], parts[0], parts[1]]
        return " ".join(
            _nominative(part) if len(part) > 2 else part for part in parts
        )

    @staticmethod
    def _find_name(soup: BeautifulSoup, text: str) -> str:
        nav_words = {
            "все", "разделы", "студентам", "учеба", "расписание", "университет",
            "институт", "факультет", "школа", "центр", "россии", "высшая",
            "экономики", "федеральный", "ректор", "проректор", "образование",
            "наука", "новости", "контакты", "главная", "поступление", "история",
            "музей", "награды", "документы", "закупки", "библиотека", "совет",
            "состав", "преподавательский", "олимпиады", "курсы", "события",
            "проекты", "партнеры", "работодателям", "прессе", "сотрудникам",
            "выпускникам", "школьникам", "абитуриентам", "календарь", "услуги",
            "сервисы", "вход", "меню", "поиск", "русский", "английский",
            "english", "версия", "слабовидящих",
        }
        name_re = re.compile(
            r"(?=\b([А-ЯЁ][а-яё\-]{2,})\s+([А-ЯЁ][а-яё\-]{2,})"
            r"\s+([А-ЯЁ][а-яё\-]{2,})\b)"
        )
        rector_positions = [match.start() for match in RECTOR_WORD_RE.finditer(text)]
        other_positions = [
            match.start() for match in re.finditer(r"проректор|директор|декан", text, re.IGNORECASE)
        ]
        headings = [
            normalize_whitespace(heading.get_text(" ", strip=True)).lower()
            for heading in soup.find_all(["h1", "h2"])
        ]
        candidates: list[tuple[int, str]] = []
        for match in name_re.finditer(text):
            parts = [match.group(1), match.group(2), match.group(3)]
            if any(part.lower() in nav_words for part in parts):
                continue
            name = " ".join(parts)
            if not looks_like_person_name(name):
                continue
            score = 0
            context = text[max(0, match.start() - 60) : match.start()]
            context_clean = context.lower().strip(" .:—–-/\n\t,")
            if re.search(r"(?<![а-яё])(?:и\.?\s*о\.?\s*)?ректор[а-яё]{0,2}$", context_clean):
                score += 10000
            elif re.search(r"(?:проректор|директор|декан)[а-яё]{0,2}(?:\s+по\s+[а-яё\s,]+)?$", context_clean):
                # ФИО сразу после «Проректор по …» — не ректор.
                score -= 10000
            nearest = min((abs(match.start() - pos) for pos in rector_positions), default=10**6)
            nearest_other = min((abs(match.start() - pos) for pos in other_positions), default=10**6)
            score += max(0, 1000 - nearest)
            if nearest_other < nearest:
                score -= 500
            if any(name.lower() in heading for heading in headings):
                score += 500
            candidates.append((score, name))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            if candidates[0][0] > -5000:
                return candidates[0][1]

        # 2. Явные конструкции «ректор ... ФИО» / «и.о. ректора ... ФИО».
        for pattern in (
            r"(?<![а-яё])(?:и\.?\s*о\.?\s*)?ректор[а-яё]{0,2}\s*[:\-–—]?\s*([А-ЯЁ][^.;]{5,120})",
            r"([А-ЯЁ][^.;]{5,120})\s*[—-]\s*(?:и\.?\s*о\.?\s*)?ректор(?![а-яё])",
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

        # 2б. «Ректор Илья Обабков» — имя и фамилия без отчества сразу после
        # слова «ректор» (на страницах-обращениях ректора так часто подписывают).
        for match in re.finditer(
            r"(?<![А-ЯЁа-яё])(?i:(?:и\.?\s*о\.?\s*)?ректор[а-яё]{0,2})\s*[:\-–—]?\s*"
            r"([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]{2,})?)(?![А-ЯЁа-яё])",
            text,
        ):
            first, second = match.group(1), match.group(2)
            if {first.lower(), second.lower()} & (nav_words | NAME_STOPWORDS):
                continue
            if _looks_like_first_name(first) or _looks_like_first_name(second):
                return f"{first} {second}"

        # 3. Заголовки страницы.
        for heading in soup.find_all(["h1", "h2"]):
            value = normalize_whitespace(heading.get_text(" ", strip=True))
            if re.search(r"проректор|директор|декан", value, re.IGNORECASE):
                continue
            names = find_person_names(value)
            if names:
                return names[0]

        # 4. Любое ФИО на странице, ближайшее к слову «ректор».
        if not rector_positions:
            return ""
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
        rector_pos = rector_positions[0]
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
        # Сначала абзацы, затем «листовые» контейнеры: крупные div включают
        # соседние блоки и чужие биографии.
        blocks = soup.find_all(["p", "li"]) + [
            node for node in soup.find_all(["div", "section"]) if not node.find(["div", "section", "p"])
        ]
        for block in blocks:
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
        return truncate_text(" ".join(list(dict.fromkeys(paragraphs))[:3]), 900)

    # ------------------------------------------------------------------
    # Википедия / Wikidata
    # ------------------------------------------------------------------
    def _wikipedia_site(self, university_name: str, short_name: str = "") -> str:
        """Официальный сайт из статьи Википедии/Wikidata (P856).

        Статья принимается, только если её название соответствует вузу:
        первая статья выдачи со словом «университет» может быть о другом вузе.
        """

        from .aggregators import _passport_matches, short_name_matches

        try:
            search = self._wiki_api(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": university_name,
                    "srlimit": 8,
                    "format": "json",
                }
            )
            pages = search.get("query", {}).get("search", [])
            titles = []
            for page in pages:
                title = page.get("title", "")
                bare = re.sub(r"\s*\(.*?\)\s*$", "", title)
                if title and (
                    _passport_matches(university_name, bare, short_name)
                    or (short_name and short_name_matches(short_name, bare))
                ):
                    titles.append(title)
            for title in titles[:2]:
                site = self._wikipedia_infobox_site(title)
                if site:
                    return site
                site = self._wikidata_site(title)
                if site:
                    return site
        except (ValueError, KeyError, TypeError) as exc:
            LOGGER.debug("Wikidata не дал сайт для %s: %s", university_name, exc)
        return ""

    def _wikidata_site(self, title: str) -> str:
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
        claims = [
            claim
            for claim in entity.get("claims", {}).get("P856", [])
            if claim.get("rank") != "deprecated"
        ]
        # Предпочтительное значение (rank=preferred) — актуальный сайт.
        claims.sort(key=lambda claim: claim.get("rank") != "preferred")
        for claim in claims:
            value = (
                claim.get("mainsnak", {})
                .get("datavalue", {})
                .get("value", "")
            )
            if isinstance(value, str) and value.startswith("http"):
                site = self._probe_site(value)
                if site:
                    return site
        return ""

    def _probe_site(self, value: str) -> str:
        """Сайт из справочника доступен; возвращает адрес после редиректов."""

        parsed = urlparse(value)
        if not parsed.netloc or host_is_excluded(parsed.netloc.split(":")[0]):
            return ""
        for scheme in ("https", "http"):
            site = f"{scheme}://{parsed.netloc}/"
            probe = self.fetcher.get(site, timeout=12)
            if not _response_ok(probe):
                continue
            final = urlparse(probe.final_url or site)
            return _origin(final.scheme or scheme, final.netloc or parsed.netloc)
        return ""

    def _wikipedia_infobox_site(self, title: str) -> str:
        url = "https://ru.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
        response = self.fetcher.get(url)
        if not response.text:
            return ""
        soup = BeautifulSoup(response.text, "lxml")
        for row in soup.select("table.infobox tr"):
            header = row.find("th")
            label = normalize_whitespace((header or row).get_text(" ", strip=True)).lower()
            if not re.match(r"(?:официальный\s+)?сайт", label):
                continue
            for anchor in row.select("a[href^='http']"):
                value = anchor.get("href", "")
                if "wikipedia.org" in value or "wikidata.org" in value:
                    continue
                site = self._probe_site(value)
                if site:
                    return site
        return ""

    def _wiki_api(self, params: dict[str, object]) -> dict:
        url = "https://ru.wikipedia.org/w/api.php?" + urlencode(params)
        response = self.fetcher.get(url)
        if not response.text:
            return {}
        return json.loads(response.text)

    def _search(self, query: str) -> list[tuple[str, str]]:
        """Веб-поиск: Яндекс → Google → Bing → DuckDuckGo (первый без капчи)."""

        from .news_search import NewsSearch

        hits = NewsSearch(self.fetcher).web_search(query)
        return [(_clean_url(hit.url), hit.title) for hit in hits if _clean_url(hit.url).startswith("http")]

    @staticmethod
    def _matches_name(title: str, host: str, name: str, short_name: str = "") -> bool:
        """Результат выдачи похож на сайт нашего вуза, а не любого вуза."""

        haystack = f"{title} {host}".lower().replace("ё", "е")
        short = normalize_whitespace(short_name).lower().replace("ё", "е")
        if short:
            if re.search(rf"(?<![а-яa-z0-9]){re.escape(short)}(?![а-яa-z0-9])", haystack):
                return True
            translit = re.sub(r"[^a-z0-9]", "", slugify(short))
            if len(translit) >= 3 and translit in host.replace("-", ""):
                return True
        tokens = distinctive_tokens(name)
        if not tokens:
            return bool(name) and normalize_whitespace(name).lower() in haystack
        stems = {token[: max(4, len(token) - 2)] for token in tokens}
        return sum(1 for stem in stems if stem in haystack) >= min(2, len(stems))


def _origin(scheme: str, netloc: str) -> str:
    """Корень сайта без порта по умолчанию: https://tpu.ru:443 → https://tpu.ru/."""

    netloc = netloc.lower()
    if (scheme == "https" and netloc.endswith(":443")) or (scheme == "http" and netloc.endswith(":80")):
        netloc = netloc.rsplit(":", 1)[0]
    return f"{scheme}://{netloc}/"


def _response_ok(response: object) -> bool:
    """Ответ с кодом 2xx/3xx и непустым текстом (404 и капча не подходят)."""

    status = getattr(response, "status_code", 200) or 0
    return bool(getattr(response, "text", "")) and 200 <= status < 400


def resolve_official_site(
    university_name: str,
    short_name: str = "",
    *,
    timeout: float = 25.0,
    enable_playwright: bool = False,
) -> str:
    """Находит официальный сайт вуза по названию (без ручной ссылки)."""

    fetcher = StealthFetcher(
        timeout=timeout, enable_playwright=enable_playwright
    )
    try:
        return OfficialSiteDiscovery(fetcher).find_site(
            university_name, short_name
        )
    finally:
        fetcher.close()
