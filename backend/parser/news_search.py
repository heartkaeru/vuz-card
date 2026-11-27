"""Экспресс-поиск новостей о сотрудничестве вуза со Сбером.

Порядок движков: Яндекс (XML API, если заданы ключи), Яндекс/Google HTML,
DuckDuckGo HTML, Bing. Яндекс и Google часто показывают капчу — тогда
используется доступный fallback. Для HTML-движков можно включить Playwright.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from logging_config import describe_error

from .models import NewsMention
from .stealth import StealthFetcher
from .utils import normalize_whitespace, parse_date

LOGGER = logging.getLogger(__name__)

# Признаки именно сотрудничества. Слова «Сбер»/«Сбербанк» сюда не входят:
# они есть в каждом результате запроса и ничего не доказывают.
COOPERATION_RE = re.compile(
    r"сотруднич|соглаш|меморандум|договор|партн[её]р|лаборатор|кампус|"
    r"грант|стипенд|стажиров|практик|кафедр|базов[а-я]+\s+кафедр|"
    r"совместн|открыл|запуст|школа\s*21|sberuniversity|сберуниверситет|"
    r"академи|хакатон|конкурс|олимпиад|форум|центр\s+компетенц|"
    r"образовательн[а-я]+\s+программ|(?<![а-яё])ии(?![а-яё])|искусственн[а-я]+\s+интеллект",
    re.IGNORECASE,
)
# Общие слова названий вузов: совпадение по ним не доказывает, что новость о нашем вузе.
GENERIC_NAME_TOKENS = {
    "университет", "университета", "университете", "государственный", "государственного",
    "национальный", "национального", "исследовательский", "исследовательского",
    "федеральный", "федерального", "российский", "российского", "институт", "института",
    "академия", "академии", "высшая", "высшей", "школа", "школы", "технический",
    "технического", "имени", "президента", "россии", "первого", "образования",
    "учреждение", "высшего", "бюджетное", "автономное", "образовательное",
}
BANK_BRANCH_RE = re.compile(
    r"отделени|филиал|банкомат|режим работы|адрес|офис|отделения",
    re.IGNORECASE,
)
SBER_RE = re.compile(r"сбер|sber", re.IGNORECASE)
NEWS_MARKER_RE = re.compile(
    r"открыл|подписал|заключил|запустил|созда|представ|договорил|"
    r"объедин|сотруднич|лаборатор|кампус|грант|проект|олимпиад|"
    r"обучени|стажиров|стипенд|исследован|цифров",
    re.IGNORECASE,
)
# Поисковик, ответивший капчей, пропускается до конца этого окна: повторные
# запросы только тратят время и продлевают блокировку.
ENGINE_COOLDOWN_SECONDS = 30 * 60
_ENGINE_BLOCKED_UNTIL: dict[str, float] = {}
_ENGINE_LOCK = threading.Lock()
_CURRENT_ENGINE = threading.local()


def _blocked(response) -> bool:
    """Ответ — капча/блокировка; поисковик уходит на «перерыв»."""

    if not StealthFetcher.looks_blocked(getattr(response, "status_code", 200), response.text or ""):
        return False
    engine = getattr(_CURRENT_ENGINE, "name", "")
    if engine:
        with _ENGINE_LOCK:
            _ENGINE_BLOCKED_UNTIL[engine] = time.monotonic() + ENGINE_COOLDOWN_SECONDS
        LOGGER.info("Поисковик %s ответил капчей: пропускаю его %s мин", engine, ENGINE_COOLDOWN_SECONDS // 60)
    return True


def _engine_available(engine: str) -> bool:
    with _ENGINE_LOCK:
        until = _ENGINE_BLOCKED_UNTIL.get(engine, 0.0)
    return time.monotonic() >= until


def yandex_api_configured() -> bool:
    return bool(
        os.getenv("YANDEX_SEARCH_API_KEY", "").strip() and os.getenv("YANDEX_SEARCH_FOLDER_ID", "").strip()
    ) or bool(os.getenv("YANDEX_XML_USER", "").strip() and os.getenv("YANDEX_XML_KEY", "").strip())


# Порядок поисковиков. Яндекс и Google — первыми; Bing и DuckDuckGo —
# запасные: они реже закрывают автоматические запросы капчей.
WEB_ENGINES: tuple[str, ...] = ("yandex", "google", "bing", "duckduckgo")
NEWS_ENGINES: tuple[str, ...] = ("yandex", "google", "bing_news", "bing", "duckduckgo")

FILE_URL_RE = re.compile(r"\.(?:xlsx?|docx?|pptx?|pdf|zip|rar)(?:\?|$)", re.I)


def _clean_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("/url?") or "google." in href and "/url?" in href:
        values = parse_qs(urlparse(href).query).get("q") or parse_qs(urlparse(href).query).get("url")
        if values:
            return unquote(values[0])
    if "uddg=" in href:
        values = parse_qs(urlparse(href).query).get("uddg")
        if values:
            return unquote(values[0])
    if "r.search.yahoo.com" in href:
        match = re.search(r"/RU=([^/]+)/", href)
        if match:
            return unquote(match.group(1))
    if "bing.com/news/apiclick.aspx" in href:
        values = parse_qs(urlparse(href).query).get("url")
        if values:
            return unquote(values[0])
    if "bing.com/ck/a" in href:
        values = parse_qs(urlparse(href).query).get("u")
        if values:
            raw = values[0]
            if raw.startswith("a1"):
                raw = raw[2:]
            try:
                padded = raw + "=" * (-len(raw) % 4)
                return base64.urlsafe_b64decode(padded).decode(
                    "utf-8", errors="replace"
                )
            except Exception:  # noqa: BLE001
                return href
    return href


def _date_from_text(*values: str) -> str | None:
    for value in values:
        parsed = parse_date(value or "")
        if parsed:
            return parsed
    return None


def _date_from_rfc(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.date().isoformat()


@dataclass
class SearchHit:  # noqa: D101
    title: str
    url: str
    snippet: str
    date: str | None
    source: str
    query: str


class NewsSearch:
    """Поиск публикаций о сотрудничестве вуза и Сбера."""

    def __init__(
        self,
        fetcher: StealthFetcher,
        *,
        engine: str = "auto",
        timeout: float = 30.0,
        max_age_months: int = 24,
    ) -> None:
        self.fetcher = fetcher
        self.engine = engine
        self.timeout = timeout
        self.max_age_months = max(0, max_age_months)
        # Релевантные, но устаревшие публикации (для предупреждения в карточке).
        self.outdated: list[NewsMention] = []

    def search(
        self,
        university_name: str,
        short_name: str = "",
        max_results: int = 5,
    ) -> list[NewsMention]:
        queries = self._queries(university_name, short_name)
        hits: list[SearchHit] = []
        for query in queries:
            for hit in self._search_query(query, university_name, short_name):
                hits.append(hit)
            if len(hits) >= max_results * 3:
                break

        deduped: list[SearchHit] = []
        seen: set[str] = set()
        for hit in hits:
            key = re.sub(r"[^a-zа-яё0-9]+", "", hit.title.lower())[:120]
            url_key = hit.url.split("#")[0].rstrip("/")
            if key in seen or url_key in seen:
                continue
            seen.add(key)
            seen.add(url_key)
            deduped.append(hit)

        # Поисковики часто не показывают дату. Чтобы отсеять устаревшие
        # публикации, читаем дату со страницы самой новости.
        budget = max_results * 2
        for hit in deduped:
            if hit.date or budget <= 0:
                continue
            budget -= 1
            hit.date = self._published_date(hit.url)

        if self.max_age_months:
            # Нужны актуальные сведения: старые публикации отбрасываем.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * self.max_age_months)).date().isoformat()
            outdated = sorted(
                (hit for hit in deduped if hit.date and hit.date < cutoff),
                key=lambda hit: hit.date or "",
                reverse=True,
            )
            self.outdated = [
                NewsMention(title=hit.title, url=hit.url, snippet=hit.snippet, date=hit.date,
                            source=hit.source, query=hit.query)
                for hit in outdated
            ]
            deduped = [hit for hit in deduped if not hit.date or hit.date >= cutoff]

        dated = sorted(
            (hit for hit in deduped if hit.date),
            key=lambda hit: hit.date or "",
            reverse=True,
        )
        undated = [hit for hit in deduped if not hit.date]
        ordered = (dated + undated)[:max_results]
        return [
            NewsMention(
                title=hit.title,
                url=hit.url,
                snippet=hit.snippet,
                date=hit.date,
                source=hit.source,
                query=hit.query,
            )
            for hit in ordered
        ]

    def _published_date(self, url: str) -> str | None:
        """Дата публикации со страницы новости (meta, JSON-LD, <time>)."""

        try:
            response = self.fetcher.get(url, timeout=min(self.timeout, 15.0))
        except Exception:  # noqa: BLE001 - дата не критична
            return None
        status = getattr(response, "status_code", 200) or 0
        if not response.text or not 200 <= status < 400:
            return None
        from .cleaner import HTMLCleaner

        published = HTMLCleaner._published_at(BeautifulSoup(response.text, "lxml"))
        if published is None:
            return None
        if published.date() > datetime.now(timezone.utc).date() + timedelta(days=1):
            return None
        return published.date().isoformat()

    @staticmethod
    def _queries(name: str, short_name: str) -> list[str]:
        name = normalize_whitespace(name)
        short = normalize_whitespace(short_name)
        # С 2020 года банк называет себя «Сбер»: свежие новости чаще с этим словом.
        queries = [f'"{name}" Сбер сотрудничество', f'"{name}" Сбербанк', f'"{name}" Сбер']
        if short and short.lower() != name.lower():
            queries.extend([f'"{short}" Сбер', f'"{short}" Сбербанк'])
        return queries

    def _search_query(
        self, query: str, name: str, short_name: str
    ) -> list[SearchHit]:
        engines = list(NEWS_ENGINES) if self.engine == "auto" else [self.engine]
        collected: list[SearchHit] = []
        for engine in engines:
            hits = self._engine_hits(engine, query)
            relevant = [
                hit for hit in hits if self._relevant(hit, name, short_name)
            ]
            if relevant:
                LOGGER.info(
                    "Поиск %s: %s релевантных результатов", engine, len(relevant)
                )
                collected.extend(relevant)
            if len(collected) >= 10:
                break
        return collected

    def _engine_hits(self, engine: str, query: str) -> list[SearchHit]:
        if not _engine_available(engine):
            return []
        _CURRENT_ENGINE.name = engine
        try:
            if engine == "yandex":
                return self._yandex(query)
            if engine == "google":
                return self._google(query)
            if engine == "bing_news":
                return self._bing_news(query)
            if engine == "yahoo":
                return self._yahoo(query)
            if engine == "bing":
                return self._bing(query)
            return self._duckduckgo(query)
        except Exception as exc:  # noqa: BLE001 - один движок не критичен
            LOGGER.warning("Поисковик %s не ответил: %s", engine, describe_error(exc))
            return []
        finally:
            _CURRENT_ENGINE.name = ""

    def web_search(self, query: str, engines: tuple[str, ...] = WEB_ENGINES) -> list[SearchHit]:
        """Обычный веб-поиск: первый поисковик, который ответил без капчи.

        Порядок: Яндекс → Google → Bing → DuckDuckGo. Яндекс и Google —
        основные поисковики для русскоязычных сайтов, но автоматические
        запросы без официального API они часто закрывают капчей; тогда
        используется следующий поисковик.
        """

        for engine in engines:
            hits = self._engine_hits(engine, query)
            if hits:
                LOGGER.info("Веб-поиск %s: %s результатов (%s)", engine, len(hits), query)
                return hits
        return []

    def _yandex(self, query: str) -> list[SearchHit]:
        """Яндекс: официальный Yandex Search API, без ключей — обычная выдача.

        С ключами используется только API: это легальный и стабильный канал
        без капчи. Без ключей пробуем обычную выдачу; при капче Яндекс
        пропускается на полчаса.
        """

        if yandex_api_configured():
            return self._yandex_api(query)
        response = self.fetcher.get(
            f"https://yandex.ru/search/?text={quote(query)}",
            timeout=self.timeout,
        )
        if not response.text or _blocked(response):
            return []
        return self._parse_yandex_html(response.text, query)

    def _yandex_api(self, query: str) -> list[SearchHit]:
        """Yandex Search API: v2 (Yandex Cloud, Api-Key), затем v1 (XML)."""

        import requests

        folder = os.getenv("YANDEX_SEARCH_FOLDER_ID", "").strip()
        api_key = os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
        timeout = min(self.timeout, 30.0)
        if folder and api_key:
            try:
                response = requests.post(
                    "https://searchapi.api.cloud.yandex.net/v2/web/search",
                    headers={"Authorization": f"Api-Key {api_key}"},
                    json={
                        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
                        "folderId": folder,
                        "responseFormat": "FORMAT_XML",
                        "l10n": "LOCALIZATION_RU",
                        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "10", "docsInGroup": "1"},
                    },
                    timeout=timeout,
                )
                if response.status_code == 200:
                    raw = response.json().get("rawData", "")
                    xml = base64.b64decode(raw).decode("utf-8", errors="replace") if raw else ""
                    hits = self._parse_yandex_xml(xml, query)
                    if hits or "<error" not in xml[:500]:
                        return hits
                else:
                    LOGGER.warning("Поисковый API Яндекса вернул HTTP %s", response.status_code)
            except (requests.RequestException, ValueError) as exc:
                LOGGER.warning("Поисковый API Яндекса недоступен: %s", describe_error(exc))
        credentials = (
            {"folderid": folder, "apikey": api_key}
            if folder and api_key
            else {
                "user": os.getenv("YANDEX_XML_USER", "").strip(),
                "key": os.getenv("YANDEX_XML_KEY", "").strip(),
            }
        )
        try:
            response = requests.get(
                "https://yandex.ru/search/xml",
                params={**credentials, "query": query, "l10n": "ru", "sortby": "rlv"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            LOGGER.warning("XML-поиск Яндекса недоступен: %s", describe_error(exc))
            return []
        if response.status_code != 200 or "<error" in response.text[:500]:
            LOGGER.warning("XML-поиск Яндекса вернул ошибку HTTP %s", response.status_code)
            return []
        return self._parse_yandex_xml(response.text, query)

    @staticmethod
    def _parse_yandex_xml(xml: str, query: str) -> list[SearchHit]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []
        hits: list[SearchHit] = []
        for doc in root.iter("doc"):
            title_node = doc.find("title")
            # Слова запроса приходят в <hlword>: findtext() их бы потерял.
            title = normalize_whitespace("".join(title_node.itertext())) if title_node is not None else ""
            url = normalize_whitespace(doc.findtext("url", ""))
            passages = " ".join(
                normalize_whitespace("".join(node.itertext()))
                for node in doc.findall("passages/passage")
            )
            pub_date = normalize_whitespace(doc.findtext("pubDate", ""))
            if not title or not url:
                continue
            hits.append(
                SearchHit(
                    title=title,
                    url=url,
                    snippet=passages,
                    date=_date_from_text(pub_date, passages, title),
                    source="yandex",
                    query=query,
                )
            )
        return hits

    @staticmethod
    def _parse_yandex_html(html: str, query: str) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[SearchHit] = []
        for item in soup.select("li.serp-item, .serp-item"):
            anchor = item.select_one(
                "a.OrganicTitle-Link, h2 a, a[href^='http']"
            )
            if not anchor:
                continue
            title = normalize_whitespace(anchor.get_text(" ", strip=True))
            url = anchor.get("href", "")
            snippet_node = item.select_one(
                ".OrganicTextContentSpan, .TextContainer, .serp-item__text"
            )
            snippet = normalize_whitespace(
                snippet_node.get_text(" ", strip=True) if snippet_node else ""
            )
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=_date_from_text(snippet, title, url),
                        source="yandex",
                        query=query,
                    )
                )
        return hits

    def _google(self, query: str) -> list[SearchHit]:
        # Официальный Google Programmable Search JSON API, если заданы ключи:
        # без него Google часто отвечает капчей на автоматические запросы.
        key = os.getenv("GOOGLE_CSE_KEY", "").strip()
        cx = os.getenv("GOOGLE_CSE_ID", "").strip()
        if key and cx:
            params = urlencode({"key": key, "cx": cx, "q": query, "hl": "ru", "num": 10})
            response = self.fetcher.get(
                f"https://www.googleapis.com/customsearch/v1?{params}", timeout=self.timeout
            )
            return self._parse_google_json(response.text, query) if response.text else []
        response = self.fetcher.get(
            f"https://www.google.com/search?q={quote(query)}&hl=ru&num=10",
            timeout=self.timeout,
        )
        if not response.text or _blocked(response):
            return []
        return self._parse_google_html(response.text, query)

    @staticmethod
    def _parse_google_json(payload: str, query: str) -> list[SearchHit]:
        try:
            items = json.loads(payload).get("items", [])
        except (ValueError, AttributeError):
            return []
        hits = []
        for item in items:
            title = normalize_whitespace(item.get("title", ""))
            url = item.get("link", "")
            snippet = normalize_whitespace(item.get("snippet", ""))
            if title and url:
                hits.append(SearchHit(title=title, url=url, snippet=snippet,
                                      date=_date_from_text(snippet), source="google", query=query))
        return hits

    @staticmethod
    def _parse_google_html(html: str, query: str) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[SearchHit] = []
        for block in soup.select("div.MjjYud, div.g"):
            anchor = block.select_one("a[href^='http'], a[href^='/url?']")
            if not anchor:
                continue
            title = normalize_whitespace(
                (block.find("h3") or anchor).get_text(" ", strip=True)
            )
            snippet_node = block.select_one(".VwiC3b, .IsZvec, .s3v9rd")
            snippet = normalize_whitespace(
                snippet_node.get_text(" ", strip=True) if snippet_node else ""
            )
            url = _clean_url(anchor.get("href", ""))
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=_date_from_text(snippet, title, url),
                        source="google",
                        query=query,
                    )
                )
        return hits

    def _duckduckgo(self, query: str) -> list[SearchHit]:
        response = self.fetcher.get(
            f"https://html.duckduckgo.com/html/?q={quote(query)}",
            timeout=self.timeout,
        )
        if not response.text or _blocked(response):
            return []
        soup = BeautifulSoup(response.text, "lxml")
        hits: list[SearchHit] = []
        for item in soup.select("div.result"):
            anchor = item.select_one("a.result__a")
            if not anchor:
                continue
            title = normalize_whitespace(anchor.get_text(" ", strip=True))
            snippet_node = item.select_one(".result__snippet")
            snippet = normalize_whitespace(
                snippet_node.get_text(" ", strip=True) if snippet_node else ""
            )
            url = _clean_url(anchor.get("href", ""))
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=_date_from_text(snippet, title, url),
                        source="duckduckgo",
                        query=query,
                    )
                )
        return hits

    def _bing_news(self, query: str) -> list[SearchHit]:
        response = self.fetcher.get(
            f"https://www.bing.com/news/search?q={quote(query)}&format=rss",
            timeout=self.timeout,
        )
        if not response.text:
            return []
        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError:
            return []
        hits: list[SearchHit] = []
        for item in root.iter("item"):
            title = normalize_whitespace(item.findtext("title", ""))
            url = _clean_url(normalize_whitespace(item.findtext("link", "")))
            snippet = normalize_whitespace(
                re.sub(r"<[^>]+>", " ", item.findtext("description", ""))
            )
            date = _date_from_rfc(item.findtext("pubDate", "")) or _date_from_text(
                snippet, title
            )
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=date,
                        source="bing_news",
                        query=query,
                    )
                )
        return hits

    def _yahoo(self, query: str) -> list[SearchHit]:
        response = self.fetcher.get(
            f"https://search.yahoo.com/search?p={quote(query)}",
            timeout=self.timeout,
        )
        if not response.text:
            return []
        soup = BeautifulSoup(response.text, "lxml")
        hits: list[SearchHit] = []
        for item in soup.select("div.algo"):
            heading = item.find("h3")
            anchor = heading.find("a") if heading else item.select_one("a[href]")
            if not anchor:
                continue
            title = normalize_whitespace(
                (heading or anchor).get_text(" ", strip=True)
            )
            snippet_node = item.select_one(".compText, .aAbs, p")
            snippet = normalize_whitespace(
                snippet_node.get_text(" ", strip=True) if snippet_node else ""
            )
            url = _clean_url(anchor.get("href", ""))
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=_date_from_text(snippet, title, url),
                        source="yahoo",
                        query=query,
                    )
                )
        return hits

    def _bing(self, query: str) -> list[SearchHit]:
        response = self.fetcher.get(
            f"https://www.bing.com/search?q={quote(query)}&setlang=ru",
            timeout=self.timeout,
        )
        if not response.text or _blocked(response):
            return []
        soup = BeautifulSoup(response.text, "lxml")
        hits: list[SearchHit] = []
        for item in soup.select("li.b_algo"):
            anchor = item.select_one("h2 a")
            if not anchor:
                continue
            title = normalize_whitespace(anchor.get_text(" ", strip=True))
            snippet_node = item.select_one("p")
            snippet = normalize_whitespace(
                snippet_node.get_text(" ", strip=True) if snippet_node else ""
            )
            url = _clean_url(anchor.get("href", ""))
            if title and url:
                hits.append(
                    SearchHit(
                        title=title,
                        url=url,
                        snippet=snippet,
                        date=_date_from_text(snippet, title, url),
                        source="bing",
                        query=query,
                    )
                )
        return hits

    @staticmethod
    def _mentions_university(haystack: str, name: str, short_name: str) -> bool:
        """Публикация действительно о нашем вузе, а не о любом «университете»."""

        folded = haystack.replace("ё", "е")
        short = normalize_whitespace(short_name).lower().replace("ё", "е")
        if short and re.search(rf"(?<![а-яa-z0-9]){re.escape(short)}(?![а-яa-z0-9])", folded):
            return True
        full = normalize_whitespace(name).lower().replace("ё", "е")
        if full and full in folded:
            return True
        distinctive = [
            token
            for token in re.findall(r"[а-яa-z\-]{3,}", full)
            if token not in GENERIC_NAME_TOKENS
        ]
        if not distinctive:
            return False
        # Основы слов (без окончаний): «экономики» ~ «экономик».
        stems = {token[: max(4, len(token) - 2)] for token in distinctive}
        hits = sum(1 for stem in stems if stem in folded)
        return hits >= min(2, len(stems))

    @staticmethod
    def _relevant(hit: SearchHit, name: str, short_name: str) -> bool:
        haystack = f"{hit.title} {hit.snippet} {hit.url}".lower()
        if FILE_URL_RE.search(hit.url):
            return False
        if not SBER_RE.search(haystack):
            return False

        if not NewsSearch._mentions_university(haystack, name, short_name):
            return False

        if not COOPERATION_RE.search(haystack):
            return False
        if not hit.date and not NEWS_MARKER_RE.search(haystack):
            return False
        if BANK_BRANCH_RE.search(haystack) and not NEWS_MARKER_RE.search(haystack):
            return False
        return True
