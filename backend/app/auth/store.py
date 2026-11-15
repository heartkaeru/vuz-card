import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Optional


BACKEND_DIR = Path(__file__).resolve().parents[2]


def _database_path() -> Path:
    configured = os.environ.get("VUZ_AUTH_DB")
    path = Path(configured) if configured else BACKEND_DIR / "data" / "auth.sqlite3"
    if not path.is_absolute():
        path = BACKEND_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(_database_path(), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    return db


def initialize_auth_store() -> None:
    with closing(_connect()) as db:
        with db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS auth_user (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user' CHECK(role = 'user'),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )


def create_user(email: str, password_hash: str) -> Optional[dict]:
    user_id = str(uuid.uuid4())
    try:
        with closing(_connect()) as db:
            with db:
                db.execute(
                    "INSERT INTO auth_user (id, email, password_hash) VALUES (?, ?, ?)",
                    (user_id, email, password_hash),
                )
    except sqlite3.IntegrityError:
        return None
    return {"id": user_id, "email": email, "role": "user"}


def get_user_by_email(email: str) -> Optional[dict]:
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT id, email, password_hash, role FROM auth_user WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
    return dict(row) if row else None
