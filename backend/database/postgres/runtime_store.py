"""Supabase registry adapter with SQLite-backed AI workflow state."""

import os
from contextlib import closing
from pathlib import Path

from app.services.llm.workflow_models import WorkflowError
from database.postgres.api_repository import (
    RepositoryConflict,
    RepositoryNotFound,
    SupabaseUniversityRepository,
)
from database.schema import connect as connect_sqlite
from parser.public_http import website_key


class SupabaseRegistry:
    """Async SQLAlchemy access to the Supabase university registry."""

    def __init__(self, dsn: str | None = None):
        self.dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
        if not self.dsn:
            raise RuntimeError("DATABASE_URL is required for the Supabase registry")
        self._repository = SupabaseUniversityRepository(dsn=self.dsn)

    async def list_cards(self):
        return await self._repository.list_cards()

    async def get_card(self, card_id: str):
        return await self._repository.get_card(card_id)

    async def create_or_resolve(self, name: str, city: str, website: str):
        try:
            return await self._repository.create_or_resolve_card(name, city, website)
        except RepositoryConflict as exc:
            if str(exc) == "chat_collection_card_ambiguous":
                raise WorkflowError(str(exc)) from None
            raise

    async def delete_card(self, card_id: str, actor: str = "") -> bool:
        try:
            await self._repository.delete_university(
                card_id, actor_type="system", actor_label=f"card-purge:{actor[:120] or 'api'}"
            )
            return True
        except RepositoryNotFound:
            return False
        except Exception as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate == "P0001":
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

    async def cards(self):
        local_cards = self._local.cards()
        local_by_id = {card["id"]: card for card in local_cards}
        result = []
        for card in await self._registry.list_cards():
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
        result.extend(local_by_id.values())
        return result

    async def resolve_registry_id(self, workflow_card_id: str):
        """Map a legacy SQLite workflow ID to its authoritative Supabase UUID."""
        card = await self._registry.get_card(workflow_card_id)
        if card is not None:
            return card["id"]
        try:
            local_card = self._local.card(workflow_card_id)
        except WorkflowError as exc:
            if str(exc) == "card_not_found":
                return workflow_card_id
            raise
        key = website_key(local_card["website"])
        if not key:
            return workflow_card_id
        matches = [item for item in await self._registry.list_cards()
                   if website_key(item["website"]) == key]
        if len(matches) > 1:
            raise WorkflowError("chat_collection_card_ambiguous")
        return matches[0]["id"] if matches else workflow_card_id

    async def card(self, card_id):
        card = await self._registry.get_card(card_id)
        if card is not None:
            return self._remember(card)
        try:
            return self._local.card(card_id)
        except WorkflowError as exc:
            if str(exc) != "card_not_found":
                raise
        raise WorkflowError("card_not_found")

    async def create_card(self, name, city, website):
        card = await self._registry.create_or_resolve(name, city, website)
        local = self._local_match(card["website"])
        if local:
            return self._local.card(local["id"])
        return self._remember(card)

    async def start_card_intake(self, name, city, website, request_key, source_options):
        card = await self._registry.create_or_resolve(name, city, website)
        local = self._local_match(card["website"])
        if not local:
            self._remember(card)
        return self._local.start_card_intake(
            card["name"], card["city"], card["website"], request_key, source_options,
        )

    async def delete_card(self, card_id, actor=""):
        local_exists = self._local.has_card(card_id)
        if local_exists:
            self._local.ensure_card_deletable(card_id)
        registry_deleted = await self._registry.delete_card(card_id, actor=actor)
        local_deleted = self._local.delete_card(card_id, actor=actor) if local_exists else False
        if not registry_deleted and not local_deleted:
            raise WorkflowError("card_not_found")
        return {"deleted": True}
