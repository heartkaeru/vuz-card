"""Хранилище документов и кэш обхода.

* :class:`DocumentStore` — SQLite-хранилище очищенных документов и статусов
  обхода (требование раздела 6: прототип на SQLite).
* :class:`CrawlCache` — JSON-кэш скачанных HTML-страниц и хэшей текста.
  Позволяет не перекачивать неизменённые страницы между запусками
  (требование пункта 9 правил обхода).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SourceDocument

LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class DocumentStore:
    """SQLite-хранилище документов и истории обхода."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                source_id TEXT PRIMARY KEY,
                university TEXT NOT NULL DEFAULT '',
                official_domain TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other',
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                collected_at TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text/html'
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                university TEXT NOT NULL DEFAULT '',
                official_domain TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other',
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                collected_at TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text/html'
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_status (
                url TEXT PRIMARY KEY,
                status_code INTEGER,
                error TEXT,
                last_attempt TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def save_document(
        self,
        document: SourceDocument,
        university_name: str,
        official_domain: str,
    ) -> None:
        published = (
            document.published_at.isoformat() if document.published_at else None
        )
        values = (
            document.source_id,
            university_name,
            official_domain,
            document.category,
            document.title,
            document.url,
            published,
            _iso(document.retrieved_at),
            document.language,
            document.text,
            document.text_hash,
            document.content_type,
        )
        self._connection.execute(
            """
            INSERT INTO documents (
                source_id, university, official_domain, category, title, url,
                published_at, collected_at, language, content, content_hash,
                content_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                university=excluded.university,
                official_domain=excluded.official_domain,
                category=excluded.category,
                title=excluded.title,
                url=excluded.url,
                published_at=excluded.published_at,
                collected_at=excluded.collected_at,
                language=excluded.language,
                content=excluded.content,
                content_hash=excluded.content_hash,
                content_type=excluded.content_type
            """,
            values,
        )
        last = self._connection.execute(
            "SELECT content_hash FROM document_history WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (document.source_id,),
        ).fetchone()
        if last is not None and last[0] == document.text_hash:
            # Текст не изменился: история не растёт от повторных запусков.
            self._connection.commit()
            return
        self._connection.execute(
            """
            INSERT INTO document_history (
                source_id, university, official_domain, category, title, url,
                published_at, collected_at, language, content, content_hash,
                content_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self._connection.commit()

    def record_status(
        self, url: str, status_code: int | None, error: str = ""
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO crawl_status (url, status_code, error, last_attempt)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status_code=excluded.status_code,
                error=excluded.error,
                last_attempt=excluded.last_attempt
            """,
            (url, status_code, error, _iso()),
        )
        self._connection.commit()

    def get_status(self, url: str) -> dict[str, object] | None:
        cursor = self._connection.execute(
            "SELECT status_code, error, last_attempt FROM crawl_status WHERE url=?",
            (url,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "status_code": row[0],
            "error": row[1],
            "last_attempt": row[2],
        }

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:  # pragma: no cover - защита от повторного close
            pass


class CrawlCache:
    """Файловый кэш HTML-страниц с TTL и хэшами текста.

    Индекс (даты, статусы, хэши) хранится в JSON, а сам HTML — отдельными
    файлами в каталоге ``<имя индекса>_pages``. Индекс остаётся маленьким:
    его не нужно целиком переписывать вместе со всеми страницами.
    """

    def __init__(self, path: Path | str, ttl_hours: int = 24) -> None:
        self.path = Path(path)
        self.pages_dir = self.path.with_name(self.path.stem + "_pages")
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self._data: dict[str, dict[str, object]] = {}
        self._html: dict[str, str] = {}
        self._dirty = False
        self._load()

    @staticmethod
    def _file_name(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:40] + ".html"

    def _read_html(self, url: str, entry: dict[str, object]) -> str:
        if url in self._html:
            return self._html[url]
        legacy = entry.get("html")
        if isinstance(legacy, str):
            return legacy
        name = entry.get("html_file")
        if not isinstance(name, str):
            return ""
        try:
            return (self.pages_dir / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._data = {
                    str(key): value
                    for key, value in loaded.items()
                    if isinstance(value, dict)
                }
        except (OSError, ValueError):
            self._data = {}

    def _fresh(self, entry: dict[str, object], field: str = "retrieved_at") -> bool:
        stamp = entry.get(field) or entry.get("retrieved_at") or entry.get("last_attempt")
        if not isinstance(stamp, str):
            return False
        try:
            retrieved = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        return _now() - retrieved <= self.ttl

    def get_page(
        self, url: str, allow_stale: bool = False
    ) -> dict[str, object] | None:
        entry = self._data.get(url)
        if not entry:
            return None
        if not allow_stale and not self._fresh(entry):
            return None
        html = self._read_html(url, entry)
        if not html:
            return None
        return {**entry, "html": html}

    def set_page(
        self,
        url: str,
        html: str,
        final_url: str,
        content_type: str,
        status_code: int = 200,
    ) -> None:
        self._data[url] = {
            "html_file": self._file_name(url),
            "final_url": final_url,
            "content_type": content_type,
            "status_code": status_code,
            "retrieved_at": _iso(),
        }
        self._html[url] = html
        self._dirty = True

    def set_text_hash(self, url: str, text_hash: str) -> None:
        entry = self._data.setdefault(url, {})
        entry["text_hash"] = text_hash
        entry["processed_at"] = _iso()
        self._dirty = True

    def set_failure(self, url: str, status_code: int | None, error: str) -> None:
        entry = self._data.setdefault(url, {})
        entry["status_code"] = status_code
        entry["error"] = error
        entry["last_attempt"] = _iso()
        self._dirty = True

    def get_text_hash(self, url: str) -> str:
        value = self._data.get(url, {}).get("text_hash")
        return str(value) if value else ""

    def get_failure(self, url: str) -> dict[str, object] | None:
        entry = self._data.get(url)
        # Свежесть ошибки считается по времени последней попытки, а не по
        # дате старой успешной загрузки той же страницы.
        if not entry or not entry.get("error") or not self._fresh(entry, "last_attempt"):
            return None
        return entry

    def prune(self, max_entries: int = 5000) -> None:
        if len(self._data) <= max_entries:
            return
        ordered = sorted(
            self._data.items(),
            key=lambda item: str(item[1].get("retrieved_at", "")),
            reverse=True,
        )
        for url, entry in ordered[max_entries:]:
            self._html.pop(url, None)
            name = entry.get("html_file")
            if isinstance(name, str):
                try:
                    (self.pages_dir / name).unlink()
                except OSError:
                    pass
        self._data = dict(ordered[:max_entries])
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        try:
            if self._html:
                self.pages_dir.mkdir(parents=True, exist_ok=True)
            for url, html in list(self._html.items()):
                entry = self._data.get(url)
                if entry is None:
                    continue
                name = str(entry.get("html_file") or self._file_name(url))
                page_tmp = self.pages_dir / f"{name}.{os.getpid()}.tmp"
                page_tmp.write_text(html, encoding="utf-8")
                page_tmp.replace(self.pages_dir / name)
                entry["html_file"] = name
                entry.pop("html", None)
            self._html.clear()
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False)
            temporary.replace(self.path)
            self._dirty = False
        except OSError as exc:  # pragma: no cover - диск/права
            LOGGER.warning("Не удалось сохранить кэш %s: %s", self.path, exc)
