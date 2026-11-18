"""Экспресс-поиск новостей о сотрудничестве вуза со Сбером.

Порядок движков: Яндекс (XML API, если заданы ключи), Яндекс/Google HTML,
DuckDuckGo HTML, Bing. Яндекс и Google часто показывают капчу — тогда
используется доступный fallback. Для HTML-движков можно включить Playwright.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .models import NewsMention
from .stealth import StealthFetcher
from .utils import normalize_whitespace, parse_date

LOGGER = logging.getLogger(__name__)

COOPERATION_RE = re.compile(
    r"сотруднич|соглаш|партн[её]р|партн[её]рств|лаборатор|кампус|"
    r"грант|проект|программ|лектор|стипенд|образован|исследован|"
    r"цифров|сбертех|сбербанк|сбер\b",
    re.IGNORECASE,
)
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
FILE_URL_RE = re.compile(r"\.(?:xlsx?|docx?|pptx?|pdf|zip|rar)(?:\?|$)", re.I)


def _clean_url(href: str) -> str:
    if not href:
        return ""
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
class SearchHit:
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
    ) -> None:
        self.fetcher = fetcher
        self.engine = engine
        self.timeout = timeout

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

    @staticmethod
    def _queries(name: str, short_name: str) -> list[str]:
        name = normalize_whitespace(name)
        short = normalize_whitespace(short_name)
        queries = [f'"{name}" Сбербанк сотрудничество', f'"{name}" Сбербанк']
        if short and short.lower() != name.lower():
            queries.append(f'"{short}" Сбербанк')
        return queries

    def _search_query(
        self, query: str, name: str, short_name: str
    ) -> list[SearchHit]:
        engines = (
            ["yandex", "google", "bing_news"]
            if self.engine == "auto"
            else [self.engine]
        )
        collected: list[SearchHit] = []
        for engine in engines:
            try:
                if engine == "yandex":
                    hits = self._yandex(query)
                elif engine == "google":
                    hits = self._google(query)
                elif engine == "bing_news":
                    hits = self._bing_news(query)
                elif engine == "yahoo":
                    hits = self._yahoo(query)
                elif engine == "bing":
                    hits = self._bing(query)
                else:
                    hits = self._duckduckgo(query)
            except Exception as exc:  # noqa: BLE001 - один движок не критичен
                LOGGER.warning("Поиск %s не удался: %s", engine, exc)
                continue
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

    def _yandex(self, query: str) -> list[SearchHit]:
        user = os.getenv("YANDEX_XML_USER", "").strip()
        key = os.getenv("YANDEX_XML_KEY", "").strip()
        if user and key:
            params = urlencode(
                {"user": user, "key": key, "query": query, "lr": "213"}
            )
            response = self.fetcher.get(
                f"https://yandex.ru/search/xml?{params}", timeout=self.timeout
            )
            if response.text and "<error" not in response.text[:500]:
                return self._parse_yandex_xml(response.text, query)

        response = self.fetcher.get(
            f"https://yandex.ru/search/?text={quote(query)}",
            timeout=self.timeout,
        )
        if not response.text or "робот" in response.text.lower()[:2000]:
            return []
        return self._parse_yandex_html(response.text, query)

    @staticmethod
    def _parse_yandex_xml(xml: str, query: str) -> list[SearchHit]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []
        hits: list[SearchHit] = []
        for doc in root.iter("doc"):
            title = normalize_whitespace(doc.findtext("title", ""))
            url = normalize_whitespace(doc.findtext("url", ""))
            passages = " ".join(
                normalize_whitespace(node.text or "")
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
        response = self.fetcher.get(
            f"https://www.google.com/search?q={quote(query)}&hl=ru&num=10",
            timeout=self.timeout,
        )
        if not response.text:
            return []
        return self._parse_google_html(response.text, query)

    @staticmethod
    def _parse_google_html(html: str, query: str) -> list[SearchHit]:
        soup = BeautifulSoup(html, "lxml")
        hits: list[SearchHit] = []
        for block in soup.select("div.MjjYud, div.g"):
            anchor = block.select_one("a[href^='http']")
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
        if not response.text:
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
        if not response.text:
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
    def _relevant(hit: SearchHit, name: str, short_name: str) -> bool:
        haystack = f"{hit.title} {hit.snippet} {hit.url}".lower()
        if FILE_URL_RE.search(hit.url):
            return False
        if not SBER_RE.search(haystack):
            return False

        names = [name, short_name]
        name_tokens = {
            token.lower()
            for value in names
            for token in re.findall(r"[А-ЯЁA-Zа-яёa-z\-]{3,}", value or "")
        }
        if name_tokens and not any(token in haystack for token in name_tokens):
            # Разрешаем короткие аббревиатуры вроде «МГУ», «ВШЭ», «УрФУ».
            if not any(
                token in haystack
                for token in (short_name.lower(), name.lower())
                if token
            ):
                return False

        if not COOPERATION_RE.search(haystack):
            return False
        if not hit.date and not NEWS_MARKER_RE.search(haystack):
            return False
        if BANK_BRANCH_RE.search(haystack) and not NEWS_MARKER_RE.search(haystack):
            return False
        return True
