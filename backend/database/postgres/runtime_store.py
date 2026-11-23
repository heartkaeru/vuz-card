"""Runtime access to the Supabase university registry."""

import os
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from app.services.llm.workflow_models import WorkflowError
from database.postgres.db import connect as connect_postgres, set_actor
from database.schema import connect as connect_sqlite
from parser.public_http import website_key


class SupabaseRegistry:
    """Small server-side repository for the existing vuz_universities table."""

    def __init__(self, dsn: str | None = None):
        self.dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
        if not self.dsn:
            raise RuntimeError("DATABASE_URL is required for the Supabase registry")

    def _connect(self):
        try:
            return connect_postgres(self.dsn, application_name="vuz-card-api", row_factory=dict_row)
        except Exception:
            # psycopg errors can include connection details; never return those to the UI.
            raise RuntimeError("Supabase connection failed; check DATABASE_URL and network access") from None

    @staticmethod
    def _card(row):
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "city": row["city"] or "",
            "website": row["website"] or "",
            "version": row["version"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        }

    def list_cards(self):
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, name, city, website, version, created_at
                   FROM public.vuz_universities ORDER BY name"""
            ).fetchall()
        return [self._card(row) for row in rows]

    def get_card(self, card_id: str):
        try:
            parsed_id = str(UUID(card_id))
        except (ValueError, TypeError, AttributeError):
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT id, name, city, website, version, created_at
                   FROM public.vuz_universities WHERE id=%s::uuid""",
                (parsed_id,),
            ).fetchone()
        return self._card(row) if row else None

    def find_by_website(self, website: str):
        key = website_key(website)
        if not key:
            return None
        matches = [card for card in self.list_cards() if website_key(card["website"]) == key]
        if len(matches) > 1:
            raise WorkflowError("chat_collection_card_ambiguous")
        return matches[0] if matches else None

    def create_or_resolve(self, name: str, city: str, website: str):
        name = " ".join(name.split())
        city = " ".join(city.split())
        website = website.strip()
        if website:
            existing = self.find_by_website(website)
            if existing:
                if city and existing["city"] and city.casefold() != existing["city"].casefold():
                    raise WorkflowError("chat_collection_card_ambiguous")
                return existing

        with self._connect() as db:
            with db.transaction():
                set_actor(db, kind="ai", label="vuz-card-runtime")
                existing = db.execute(
                    """SELECT id, name, city, website, version, created_at
                       FROM public.vuz_universities
                       WHERE name_key=public.vuz_name_key(%s)""",
                    (name,),
                ).fetchone()
                if existing:
                    card = self._card(existing)
                    if city and card["city"] and city.casefold() != card["city"].casefold():
                        raise WorkflowError("chat_collection_card_ambiguous")
                    if website and card["website"] and website_key(website) != website_key(card["website"]):
                        raise WorkflowError("chat_collection_card_ambiguous")
                    return card

                row = db.execute(
                    """INSERT INTO public.vuz_universities
                       (id, name, city, website, institution_type, is_demo, verification_status)
                       VALUES (%s::uuid, %s, NULLIF(%s, ''), NULLIF(%s, ''), 'unknown', false, 'ai_draft')
                       RETURNING id, name, city, website, version, created_at""",
                    (str(uuid4()), name, city, website),
                ).fetchone()
                return self._card(row)

    def delete_card(self, card_id: str, actor: str = "") -> bool:
        try:
            parsed_id = str(UUID(card_id))
        except (ValueError, TypeError, AttributeError):
            return False
        try:
            with self._connect() as db:
                with db.transaction():
                    set_actor(db, kind="system", label=f"card-purge:{actor[:120] or 'api'}")
                    deleted = db.execute(
                        "SELECT public.vuz_purge_university(%s::uuid)", (parsed_id,),
                    ).fetchone()[0]
                    return bool(deleted)
        except Exception as exc:
            if (getattr(exc, "sqlstate", None) == "P0001"
                    and getattr(getattr(exc, "diag", None), "message_primary", None) == "university_has_children"):
                raise WorkflowError("university_has_children") from None
            raise RuntimeError("Supabase card deletion failed") from None


class SupabaseDemoRepository:
    """Use Supabase for registry cards and retain SQLite for AI workflow state."""

    def __init__(self, sqlite_path: Path, dsn: str | None = None):
        from database.demo1_store import DemoRepository

        self._local = DemoRepository(sqlite_path)
        self._registry = SupabaseRegistry(dsn)
        self.path = self._local.path

    def __getattr__(self, name):
        return getattr(self._local, name)

    def _remember(self, card):
        with closing(connect_sqlite(self.path)) as db, db:
            db.execute(
                """INSERT INTO llm_card(id,name,city,website,version,created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, city=excluded.city,
                     website=excluded.website, version=excluded.version""",
                (card["id"], card["name"], card["city"], card["website"], card["version"],
                 card["created_at"] or ""),
            )
        return self._local.card(card["id"])

    def _local_match(self, website):
        key = website_key(website)
        if not key:
            return None
        matches = [card for card in self._local.cards() if website_key(card["website"]) == key]
        if len(matches) > 1:
            raise WorkflowError("chat_collection_card_ambiguous")
        return matches[0] if matches else None

    def cards(self):
        local_cards = self._local.cards()
        local_by_id = {card["id"]: card for card in local_cards}
        result = []
        for card in self._registry.list_cards():
            saved = local_by_id.pop(card["id"], None)
            if saved is None:
                key = website_key(card["website"])
                matches = [item for item in local_by_id.values()
                           if key and website_key(item["website"]) == key]
                if len(matches) > 1:
                    raise WorkflowError("chat_collection_card_ambiguous")
                if matches:
                    saved = matches[0]
                    local_by_id.pop(saved["id"], None)
            if saved:
                result.append({**card, "id": saved["id"], "latest_run": saved["latest_run"]})
            else:
                result.append({**card, "latest_run": None})
        # Preserve pre-existing local drafts until they are explicitly matched or migrated.
        result.extend(local_by_id.values())
        return result

    def card(self, card_id):
        try:
            return self._local.card(card_id)
        except WorkflowError as exc:
            if str(exc) != "card_not_found":
                raise
        card = self._registry.get_card(card_id)
        if card is None:
            raise WorkflowError("card_not_found")
        return self._remember(card)

    def create_card(self, name, city, website):
        card = self._registry.create_or_resolve(name, city, website)
        local = self._local_match(card["website"])
        if local:
            return self._local.card(local["id"])
        return self._remember(card)

    def start_card_intake(self, name, city, website, request_key, source_options):
        card = self._registry.create_or_resolve(name, city, website)
        local = self._local_match(card["website"])
        if not local:
            self._remember(card)
        return self._local.start_card_intake(
            card["name"], card["city"], card["website"], request_key, source_options,
        )

    def delete_card(self, card_id, actor=""):
        local_exists = self._local.has_card(card_id)
        if local_exists:
            self._local.ensure_card_deletable(card_id)
        registry_deleted = self._registry.delete_card(card_id, actor=actor)
        local_deleted = self._local.delete_card(card_id, actor=actor) if local_exists else False
        if not registry_deleted and not local_deleted:
            raise WorkflowError("card_not_found")
        return {"deleted": True}
