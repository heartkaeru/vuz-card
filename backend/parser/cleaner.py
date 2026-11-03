"""Очистка HTML и подготовка документов для нейросети.

Модуль решает три задачи:

1. Удаляет скрипты, стили, навигацию, cookie-баннеры и прочий «мусор».
2. Выделяет основной контент и сохраняет его структуру (заголовки, абзацы,
   списки, строки таблиц).
3. Формирует ``SourceDocument`` с метаданными и фрагментами, смещения
   которых относятся к очищенному тексту, а не к исходному HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

try:  # опциональный резервный экстрактор основного текста
    import trafilatura  # type: ignore
except ImportError:  # pragma: no cover - зависит от окружения
    trafilatura = None

from .config import (
    BOILERPLATE_LINE_PATTERNS,
    DEFAULT_CONTENT_SELECTORS,
    DOCUMENT_CATEGORIES,
    NOISE_SELECTORS,
    SECTION_TO_CATEGORY,
)
from .models import (
    ContactHint,
    ContactOwner,
    DocumentFragment,
    SourceDocument,
    TextBlock,
    utc_now,
)
from .utils import (
    clean_email,
    clean_phone,
    extract_emails,
    extract_phones,
    looks_like_person_name,
    normalize_whitespace,
    parse_date,
    pick_title,
    sha256_text,
    slugify,
    truncate_text,
    contains_position,
)

LOGGER = logging.getLogger(__name__)

BLOCK_TAGS = {
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "p": "paragraph",
    "li": "list_item",
    "dt": "term",
    "dd": "definition",
    "blockquote": "quote",
    "figcaption": "caption",
    "pre": "preformatted",
    "summary": "summary",
}

CONTAINER_TAGS = {
    "div",
    "section",
    "article",
    "main",
    "ul",
    "ol",
    "dl",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "figure",
    "details",
    "body",
    "address",
}

DEPARTMENT_MARKERS = (
    "приёмная",
    "приемная",
    "ректорат",
    "управление",
    "департамент",
    "отдел",
    "центр",
    "кафедра",
    "институт",
    "факультет",
    "дирекция",
    "лаборатория",
    "служба",
    "сектор",
    "комитет",
    "совет",
    "колледж",
    "лицей",
)

PUBLISHED_META_NAMES = (
    "article:published_time",
    "og:published_time",
    "date",
    "pubdate",
    "publish-date",
    "dc.date",
)


@dataclass
class Block:
    """Структурный блок очищенного текста."""

    kind: str
    text: str


@dataclass
class CleanResult:
    """Результат очистки одной HTML-страницы."""

    title: str
    text: str
    blocks: list[Block] = field(default_factory=list)
    main_html: str = ""
    truncated: bool = False
    description: str = ""
    canonical_url: str = ""
    published_at: datetime | None = None
    language: str = "ru"


def _make_soup(html: str) -> BeautifulSoup:
    """Создаёт BeautifulSoup с lxml, если он установлен."""

    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - нужен рабочий fallback
        return BeautifulSoup(html, "html.parser")


class HTMLCleaner:
    """Извлекает основной текст и структурные блоки из HTML."""

    def __init__(
        self,
        content_selectors: Iterable[str] = (),
        noise_selectors: Iterable[str] = NOISE_SELECTORS,
        max_chars: int = 120_000,
    ) -> None:
        self.content_selectors = tuple(content_selectors) or DEFAULT_CONTENT_SELECTORS
        self.noise_selectors = tuple(noise_selectors)
        self.max_chars = max_chars

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def clean(
        self,
        html: str,
        url: str = "",
        title: str = "",
    ) -> CleanResult:
        """Очищает HTML и возвращает текст, блоки и метаданные."""

        if not html or not html.strip():
            return CleanResult(title=title, text="")

        soup = _make_soup(html)
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()

        html_title = ""
        if soup.title and soup.title.string:
            html_title = soup.title.string
        description = self._meta_content(soup, ("description", "og:description"))
        canonical = self._canonical_url(soup, url)
        published_at = self._published_at(soup)
        language = self._detect_language(soup)

        self._remove_noise(soup)
        main = self._select_main(soup)
        if main is None:
            return CleanResult(
                title=pick_title(html_title, title),
                text="",
                description=description,
                canonical_url=canonical,
                published_at=published_at,
                language=language,
            )

        blocks = self._extract_blocks(main)
        blocks, truncated = self._limit_blocks(blocks, self.max_chars)
        text = "\n\n".join(block.text for block in blocks)

        # Если собственный экстрактор дал слишком мало текста, пробуем
        # Trafilatura (если библиотека установлена).
        if trafilatura is not None and len(text) < 400:
            try:
                extracted = trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    url=url or None,
                )
            except Exception:  # noqa: BLE001 - внешняя библиотека
                extracted = ""
            if extracted and len(extracted) > len(text):
                blocks = [
                    Block("text", normalize_whitespace(part))
                    for part in re.split(r"\n{2,}", extracted)
                    if normalize_whitespace(part)
                ]
                blocks, truncated = self._limit_blocks(blocks, self.max_chars)
                text = "\n\n".join(block.text for block in blocks)

        # Заголовок: <title> обычно информативнее, но у новостей h1 точнее.
        first_heading = next(
            (block.text for block in blocks if block.kind == "heading"), ""
        )
        cleaned_title = pick_title(html_title, title or first_heading)
        if not cleaned_title:
            cleaned_title = first_heading

        return CleanResult(
            title=normalize_whitespace(cleaned_title),
            text=text,
            blocks=blocks,
            main_html=str(main),
            truncated=truncated,
            description=description,
            canonical_url=canonical,
            published_at=published_at,
            language=language,
        )

    def to_document(
        self,
        html: str,
        url: str,
        section: str = "",
        title: str = "",
        content_type: str = "text/html",
    ) -> SourceDocument:
        """Очищает HTML и упаковывает результат в ``SourceDocument``."""

        result = self.clean(html, url=url, title=title)
        blocks, truncated = self._limit_blocks(result.blocks, self.max_chars)
        text, fragments = self._build_fragments(blocks, section=section)

        if truncated:
            LOGGER.info(
                "Документ %s обрезан до %s символов", url, self.max_chars
            )

        source_id = self._source_id(url, section)
        category = SECTION_TO_CATEGORY.get(section, "other")
        if category not in DOCUMENT_CATEGORIES:
            category = "other"
        metadata = {"section": section, "category": category}
        if result.description:
            metadata["description"] = result.description
        if truncated:
            metadata["truncated"] = "true"

        return SourceDocument(
            source_id=source_id,
            url=result.canonical_url or url,
            title=result.title or title,
            section=section,
            category=category,
            content_type=content_type,
            language=result.language or "ru",
            text=text,
            retrieved_at=utc_now(),
            published_at=result.published_at,
            text_hash=sha256_text(text),
            fragments=fragments,
            metadata=metadata,
            blocks=[TextBlock(kind=block.kind, text=block.text) for block in blocks],
            main_html=result.main_html,
        )

    # ------------------------------------------------------------------
    # Удаление мусора и выбор основного контента
    # ------------------------------------------------------------------
    def _remove_noise(self, soup: BeautifulSoup) -> None:
        for selector in self.noise_selectors:
            try:
                for node in soup.select(selector):
                    node.decompose()
            except Exception:  # noqa: BLE001 - некорректный селектор не критичен
                LOGGER.debug("Некорректный селектор шума: %s", selector)

        # Скрытые элементы и элементы с «мусорными» классами/идентификаторами.
        noise_words = {
            "cookie",
            "breadcrumb",
            "advert",
            "advertisement",
            "banner",
            "share",
            "sharing",
            "social",
            "pagination",
            "navbar",
        }
        for node in list(soup.find_all(True)):
            if not isinstance(node, Tag):
                continue
            attrs_dict = getattr(node, "attrs", None) or {}
            class_tokens = {
                str(token).lower() for token in (attrs_dict.get("class") or [])
            }
            node_id = str(attrs_dict.get("id") or "").lower()
            # Сравниваем точные классы, а не подстроки: иначе, например,
            # класс «sidebar__content» с основным контентом будет удалён.
            if class_tokens & noise_words or node_id in noise_words:
                if node.name not in {"main", "article", "body"}:
                    node.decompose()

        for node in soup.select("[hidden], [aria-hidden='true']"):
            if node.name not in {"main", "article", "body"}:
                node.decompose()

    def _select_main(self, soup: BeautifulSoup) -> Tag | None:
        # Селекторы заданы в порядке приоритета. Берём первый селектор,
        # который даёт содержательный блок: это защищает от ситуации, когда
        # <main> включает меню и «перевешивает» article по объёму текста.
        fallback: list[tuple[float, Tag]] = []
        for selector in self.content_selectors:
            try:
                nodes = soup.select(selector)
            except Exception:  # noqa: BLE001
                continue
            selector_candidates: list[tuple[float, Tag]] = []
            for node in nodes:
                text = node.get_text(" ", strip=True)
                if len(text) < 80:
                    continue
                selector_candidates.append((self._content_score(node, text), node))
            if not selector_candidates:
                continue
            best_score, best = max(
                selector_candidates,
                key=lambda item: (
                    item[0],
                    len(item[1].get_text(" ", strip=True)),
                ),
            )
            best_text_len = len(best.get_text(" ", strip=True))
            if best_text_len >= 300:
                return best
            fallback.extend(selector_candidates)

        body = soup.body
        if body is not None:
            body_text = body.get_text(" ", strip=True)
            if len(body_text) >= 80:
                fallback.append((self._content_score(body, body_text), body))

        if not fallback:
            return body if body is not None else soup

        best_score, best = max(
            fallback,
            key=lambda item: (
                item[0],
                len(item[1].get_text(" ", strip=True)),
            ),
        )
        if best.name == "body":
            for score, node in fallback:
                if node.name in {"main", "article"} and score >= best_score * 0.6:
                    return node
        return best

    @staticmethod
    def _content_score(node: Tag, text: str) -> float:
        link_text = " ".join(
            link.get_text(" ", strip=True) for link in node.find_all("a")
        )
        link_density = len(link_text) / max(1, len(text))
        # Штраф за высокую плотность ссылок (меню, списки ссылок).
        return len(text) * (1.0 - min(0.9, link_density) * 0.8)

    # ------------------------------------------------------------------
    # Структурные блоки
    # ------------------------------------------------------------------
    def _extract_blocks(self, root: Tag) -> list[Block]:
        blocks: list[Block] = []
        self._walk(root, blocks)

        cleaned: list[Block] = []
        seen: set[str] = set()
        for block in blocks:
            text = normalize_whitespace(block.text)
            if not text:
                continue
            if len(text) < 2 and block.kind != "heading":
                continue
            if re.fullmatch(r"[\W_]+", text):
                continue
            lowered = text.lower()
            if any(lowered.startswith(pattern) for pattern in BOILERPLATE_LINE_PATTERNS):
                continue
            if lowered in {"наверх", "на главную", "читать далее", "подробнее"}:
                continue
            # Полные дубликаты подряд (частый баг вёрстки).
            key = lowered
            if cleaned and cleaned[-1].text.lower() == key:
                continue
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(Block(kind=block.kind, text=text))
        return cleaned

    def _walk(self, node: Tag, blocks: list[Block]) -> None:
        if not isinstance(node, Tag):
            return
        name = node.name
        if name in {"script", "style", "noscript", "template", "svg", "canvas"}:
            return

        if name == "tr":
            cells = []
            for cell in node.find_all(["th", "td"], recursive=False):
                cell_text = self._node_text(cell)
                if cell_text:
                    cells.append(cell_text)
            if cells:
                blocks.append(Block("table_row", " | ".join(cells)))
            return

        if name in BLOCK_TAGS:
            text = self._node_text(node)
            if text:
                blocks.append(Block(BLOCK_TAGS[name], text))
            return

        if name == "table":
            caption = node.find("caption")
            if caption:
                caption_text = self._node_text(caption)
                if caption_text:
                    blocks.append(Block("table_caption", caption_text))
            for child in node.children:
                if isinstance(child, Tag):
                    self._walk(child, blocks)
            return

        if name in CONTAINER_TAGS:
            before = len(blocks)
            for child in node.children:
                if isinstance(child, Tag):
                    self._walk(child, blocks)
            if len(blocks) == before:
                # Текст лежит прямо в div/section без абзацев.
                text = self._node_text(node)
                if len(text) >= 20:
                    blocks.append(Block("text", text))
            return

        # Строчные элементы: пусть их обрабатывает родитель.
        return

    @staticmethod
    def _node_text(node: Tag) -> str:
        # <br> превращаем в пробел, чтобы не склеивать соседние слова.
        for br in node.find_all("br"):
            br.replace_with(" ")
        text = node.get_text(" ", strip=True)
        return normalize_whitespace(text)

    @staticmethod
    def _limit_blocks(
        blocks: list[Block], max_chars: int
    ) -> tuple[list[Block], bool]:
        if max_chars <= 0:
            return blocks, False
        result: list[Block] = []
        total = 0
        for block in blocks:
            addition = len(block.text) + (2 if result else 0)
            if total + addition > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    result.append(
                        Block(block.kind, truncate_text(block.text, remaining))
                    )
                return result, True
            result.append(block)
            total += addition
        return result, False

    # ------------------------------------------------------------------
    # Фрагменты и контакты
    # ------------------------------------------------------------------
    def _build_fragments(
        self, blocks: list[Block], section: str
    ) -> tuple[str, list[DocumentFragment]]:
        fragments: list[DocumentFragment] = []
        parts: list[str] = []
        offset = 0
        heading = ""
        heading_fragment_id: str | None = None

        for index, block in enumerate(blocks, start=1):
            text = block.text
            if not text:
                continue
            if parts:
                offset += 2  # разделитель "\n\n"
            start = offset
            end = start + len(text)
            fragment_id = f"f{index:04d}"

            if block.kind == "heading":
                heading = text
                heading_fragment_id = fragment_id

            owner = self._infer_owner(heading, section)
            # A default label such as "ВУЗ" is not evidence of ownership.
            # Keep the contact in the text for extraction without inventing a hint.
            hints = self._contact_hints(text, owner, heading_fragment_id) if heading else []

            fragments.append(
                DocumentFragment(
                    fragment_id=fragment_id,
                    start=start,
                    end=end,
                    heading=heading,
                    contact_hints=hints,
                )
            )
            parts.append(text)
            offset = end

        return "\n\n".join(parts), fragments

    @staticmethod
    def _infer_owner(heading: str, section: str) -> ContactOwner:
        heading = normalize_whitespace(heading)
        if heading and looks_like_person_name(heading):
            return ContactOwner(kind="person", label=heading)
        lowered = heading.lower()
        if any(marker in lowered for marker in DEPARTMENT_MARKERS):
            return ContactOwner(kind="department", label=heading)
        if heading and contains_position(heading):
            return ContactOwner(kind="office", label=heading)
        if section in {"leadership", "contacts"} and heading:
            return ContactOwner(kind="office", label=heading)
        if heading:
            return ContactOwner(kind="department", label=heading)
        return ContactOwner(kind="organization", label="ВУЗ")

    @staticmethod
    def _contact_hints(
        text: str,
        owner: ContactOwner,
        heading_fragment_id: str | None,
    ) -> list[ContactHint]:
        hints: list[ContactHint] = []
        context = [heading_fragment_id] if heading_fragment_id else []
        for email in extract_emails(text):
            hints.append(
                ContactHint(
                    field="contact.email",
                    value=email,
                    owner=owner,
                    context_fragment_ids=context,
                )
            )
        for phone in extract_phones(text):
            hints.append(
                ContactHint(
                    field="contact.phone",
                    value=phone,
                    owner=owner,
                    context_fragment_ids=context,
                )
            )
        return hints

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------
    @staticmethod
    def _meta_content(soup: BeautifulSoup, names: tuple[str, ...]) -> str:
        for name in names:
            node = soup.find("meta", attrs={"name": name}) or soup.find(
                "meta", attrs={"property": name}
            )
            if node and node.get("content"):
                return normalize_whitespace(node["content"])
        return ""

    @staticmethod
    def _detect_language(soup: BeautifulSoup) -> str:
        html = soup.find("html")
        value = ""
        if html and html.get("lang"):
            value = str(html["lang"])
        if not value:
            value = HTMLCleaner._meta_content(
                soup, ("language", "og:locale", "content-language")
            )
        value = value.strip().lower().replace("_", "-")
        if value.startswith("ru"):
            return "ru"
        if value.startswith("en"):
            return "en"
        return "ru"

    @staticmethod
    def _canonical_url(soup: BeautifulSoup, fallback: str) -> str:
        node = soup.find("link", rel=lambda value: value and "canonical" in value)
        if node and node.get("href"):
            return normalize_whitespace(node["href"])
        return fallback

    @staticmethod
    def _published_at(soup: BeautifulSoup) -> datetime | None:
        for name in PUBLISHED_META_NAMES:
            node = soup.find("meta", attrs={"name": name}) or soup.find(
                "meta", attrs={"property": name}
            )
            if node and node.get("content"):
                parsed = _parse_datetime(node["content"])
                if parsed:
                    return parsed
        # microdata / OpenGraph варианты
        for attrs in (
            {"itemprop": "datePublished"},
            {"property": "article:published_time"},
            {"name": "datePublished"},
        ):
            node = soup.find(attrs=attrs)
            if node:
                value = node.get("content") or node.get("datetime") or node.get_text(
                    " ", strip=True
                )
                parsed = _parse_datetime(value)
                if parsed:
                    return parsed
        # JSON-LD: {"@type": "NewsArticle", "datePublished": "..."}
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            payload = script.string or script.get_text(" ", strip=True)
            match = re.search(
                r'"datePublished"\s*:\s*"([^"]+)"', payload or "", re.IGNORECASE
            )
            if match:
                parsed = _parse_datetime(match.group(1))
                if parsed:
                    return parsed
        time_node = soup.find("time")
        if time_node:
            parsed = _parse_datetime(
                time_node.get("datetime") or time_node.get_text(" ", strip=True)
            )
            if parsed:
                return parsed
        return None

    @staticmethod
    def _source_id(url: str, section: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.strip("/") or "index"
        readable = slugify(f"{section}-{path}")[:70]
        digest = sha256_text(url)[:8]
        return f"{readable}-{digest}"


def _parse_datetime(value: str) -> datetime | None:
    """Разбирает дату публикации из meta/time."""

    value = normalize_whitespace(value or "")
    if not value:
        return None
    # ISO 8601, включая Z и смещения.
    iso_candidate = value.replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(iso_candidate)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result
    except ValueError:
        pass
    iso_date = parse_date(value)
    if iso_date:
        try:
            return datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------
# Совместимость с прежним API и простые функции
# ----------------------------------------------------------------------
def clean_text(text: str) -> str:
    """Нормализует пробелы и переносы строк в одну строку."""

    return re.sub(r"\s+", " ", normalize_whitespace(text or "")).strip()


def clean_html(html: str, max_chars: int = 120_000) -> str:
    """Возвращает только основной очищенный текст страницы."""

    return HTMLCleaner(max_chars=max_chars).clean(html).text


def extract_blocks(html: str, max_chars: int = 120_000) -> list[Block]:
    """Возвращает структурные блоки страницы."""

    return HTMLCleaner(max_chars=max_chars).clean(html).blocks


def extract_text(html: str, max_chars: int = 120_000) -> str:
    """Псевдоним ``clean_html`` для читаемости."""

    return clean_html(html, max_chars=max_chars)


def normalize_contacts(text: str) -> tuple[list[str], list[str]]:
    """Извлекает email и телефоны из произвольного текста."""

    return extract_emails(text), extract_phones(text)
