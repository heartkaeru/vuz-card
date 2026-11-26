"""Async SQLAlchemy repository for the Supabase university API."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

try:
    from parser.public_http import website_key
except ImportError:
    from backend.parser.public_http import website_key


class RepositoryNotFound(LookupError):
    pass


class RepositoryConflict(ValueError):
    pass


class RepositoryValidationError(ValueError):
    pass


def _async_engine(dsn: str | None = None) -> AsyncEngine:
    dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")).strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for the Supabase API")
    url = make_url(dsn)
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg", query=query)
    elif url.drivername != "postgresql+asyncpg":
        raise RuntimeError("DATABASE_URL must use PostgreSQL")
    else:
        url = url.set(query=query)
    connect_args = {"prepared_statement_cache_size": 0}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = "require" if sslmode in {"require", "allow", "prefer"} else True
    elif url.host not in {None, "localhost", "127.0.0.1", "::1"}:
        connect_args["ssl"] = "require"
    return create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return _async_engine()


async def _set_actor(conn, actor_type: str, actor_label: str, actor_id: str | None = None):
    if actor_type not in {"human", "ai", "migration", "system"}:
        raise ValueError("Unknown audit actor type")
    await conn.execute(text("SELECT set_config('vuz.actor_type', :value, true)"), {"value": actor_type})
    await conn.execute(text("SELECT set_config('vuz.actor_label', :value, true)"),
                       {"value": (actor_label or actor_type)[:200]})
    await conn.execute(text("SELECT set_config('vuz.actor_id', :value, true)"),
                       {"value": actor_id or ""})


def _source_kind(actor_type: str, source_url: str | None) -> str:
    return "web" if source_url and urlsplit(source_url).scheme in {"http", "https"} else actor_type


def _uuid(value):
    return value if isinstance(value, UUID) else UUID(str(value))


async def _source_id(conn, actor_type: str, actor_label: str, payload: dict, source_url: str | None = None) -> UUID:
    kind = _source_kind(actor_type, source_url)
    if kind not in {"manual", "ai", "web"}:
        kind = "manual"
    source_path = source_url or f"{actor_type}://api/{actor_label[:120]}"
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row = await conn.execute(text("""
        INSERT INTO public.vuz_sources(id,source_path,sha256,source_kind,is_demo)
        VALUES (:id,:path,:sha,:kind,false)
        ON CONFLICT(source_path,sha256) DO UPDATE SET source_path=EXCLUDED.source_path
        RETURNING id
    """), {"id": uuid4(), "path": source_path, "sha": digest, "kind": kind})
    return row.scalar_one()


class SupabaseUniversityRepository:
    def __init__(self, engine: AsyncEngine | None = None, dsn: str | None = None):
        self.engine = engine or (get_engine() if dsn is None else _async_engine(dsn))

    async def _university_row(self, conn, university_id: str):
        result = await conn.execute(text("""
            SELECT u.id::text AS id, u.name, COALESCE(u.short_name,u.name) AS short_name,
                   COALESCE(le.inn,u.reported_inn,'') AS inn, COALESCE(u.website,'') AS website,
                   COALESCE(u.region,'') AS region, COALESCE(u.city,'') AS city,
                   u.profile,u.institution_type,u.photo_url,
                   (SELECT o.full_name FROM public.vuz_officials o
                    WHERE o.university_id=u.id AND o.position ILIKE '%ректор%'
                      AND o.position NOT ILIKE '%проректор%' AND o.appointment_status='current'
                    ORDER BY o.verification_status='verified' DESC, o.full_name LIMIT 1) AS rector_fio,
                   u.version, COALESCE(u.updated_at,u.created_at) AS updated_at,u.created_at,
                   (SELECT m.value FROM public.vuz_university_metrics m
                    WHERE m.university_id=u.id AND m.metric_code='payroll_fund'
                    ORDER BY m.verification_status='verified' DESC, m.source_as_of DESC NULLS LAST LIMIT 1) AS fot_volume,
                   (SELECT m.value::integer FROM public.vuz_university_metrics m
                    WHERE m.university_id=u.id AND m.metric_code='active_contracts_reported'
                    ORDER BY m.verification_status='verified' DESC, m.source_as_of DESC NULLS LAST LIMIT 1) AS contracts_count,
                   COALESCE((SELECT json_agg(p.name ORDER BY p.name) FROM public.vuz_projects p
                    WHERE p.university_id=u.id AND p.partnership_status IN ('reported','planned','active')), '[]'::json) AS active_projects
            FROM public.vuz_universities u
            LEFT JOIN public.vuz_legal_entities le ON le.id=u.legal_entity_id
            WHERE u.id=CAST(:id AS uuid)
        """), {"id": _uuid(university_id)})
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def list_universities(self, search: str | None = None, region: str | None = None):
        async with self.engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT u.id::text AS id, u.name, COALESCE(u.short_name,u.name) AS short_name,
                       COALESCE(le.inn,u.reported_inn,'') AS inn, COALESCE(u.website,'') AS website,
                       COALESCE(u.region,'') AS region, COALESCE(u.city,'') AS city,
                       u.profile,u.institution_type,u.photo_url,
                       (SELECT o.full_name FROM public.vuz_officials o WHERE o.university_id=u.id
                        AND o.position ILIKE '%ректор%' AND o.position NOT ILIKE '%проректор%'
                        AND o.appointment_status='current' ORDER BY o.verification_status='verified' DESC LIMIT 1) AS rector_fio,
                       u.version, COALESCE(u.updated_at,u.created_at) AS updated_at,u.created_at
                FROM public.vuz_universities u LEFT JOIN public.vuz_legal_entities le ON le.id=u.legal_entity_id
                WHERE (:search IS NULL OR u.name ILIKE :pattern OR COALESCE(u.short_name,'') ILIKE :pattern)
                  AND (:region IS NULL OR COALESCE(u.region,'') ILIKE :region)
                ORDER BY u.name
            """), {"search": search, "pattern": f"%{search}%" if search else None,
                    "region": region})
            return [self._format_university(row) for row in result.mappings()]

    async def list_cards(self):
        rows = await self.list_universities()
        return [{key: row[key] for key in ("id", "name", "city", "website", "version", "created_at")}
                for row in rows]

    async def get_card(self, card_id: str):
        try:
            UUID(card_id)
        except (ValueError, TypeError, AttributeError):
            return None
        async with self.engine.connect() as conn:
            row = await self._university_row(conn, card_id)
            if row is None:
                return None
            return self._format_university({key: row[key] for key in
                ("id", "name", "city", "website", "version", "created_at")})

    async def create_or_resolve_card(self, name: str, city: str, website: str):
        name = " ".join(name.split())
        city = " ".join(city.split())
        website = website.strip()
        cards = await self.list_cards()
        if website:
            matches = [card for card in cards if website_key(card["website"]) == website_key(website)]
            if len(matches) > 1:
                raise RepositoryConflict("chat_collection_card_ambiguous")
            if matches:
                card = matches[0]
                if city and card["city"] and city.casefold() != card["city"].casefold():
                    raise RepositoryConflict("chat_collection_card_ambiguous")
                return card
        async with self.engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT id::text AS id,name,COALESCE(city,'') AS city,COALESCE(website,'') AS website,
                       version,created_at
                FROM public.vuz_universities WHERE name_key=public.vuz_name_key(:name)
            """), {"name": name})
            rows = [dict(row) for row in result.mappings()]
        if len(rows) > 1:
            raise RepositoryConflict("chat_collection_card_ambiguous")
        if rows:
            card = self._format_university(rows[0])
            if city and card["city"] and city.casefold() != card["city"].casefold():
                raise RepositoryConflict("chat_collection_card_ambiguous")
            if website and card["website"] and website_key(website) != website_key(card["website"]):
                raise RepositoryConflict("chat_collection_card_ambiguous")
            return card
        item = await self.create_university({
            "name": name, "short_name": name, "region": None, "city": city or None,
            "website": website or None, "inn": None, "profile": None,
            "institution_type": "unknown", "verification_status": "ai_draft",
        }, actor_type="ai", actor_label="vuz-card-runtime")
        return self._format_university({key: item[key] for key in
            ("id", "name", "city", "website", "version", "created_at")})

    @staticmethod
    def _format_university(row):
        item = dict(row)
        if item.get("updated_at"):
            item["updated_at"] = item["updated_at"].isoformat()
        if item.get("created_at"):
            item["created_at"] = item["created_at"].isoformat()
        if item.get("fot_volume") is not None:
            item["fot_volume"] = str(item["fot_volume"])
        if item.get("contracts_count") is not None:
            item["contracts_count"] = int(item["contracts_count"])
        return item

    async def get_university(self, university_id: str, include_related: bool = True):
        async with self.engine.connect() as conn:
            item = await self._university_row(conn, university_id)
            if item is None:
                return None
            item = self._format_university(item)
            if include_related:
                item["officials"] = await self._list_officials(conn, university_id)
                item["metrics"] = await self._list_metrics(conn, university_id)
            return item

    async def create_university(self, values: dict, *, actor_type="human", actor_label="api"):
        university_id = uuid4()
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            try:
                await conn.execute(text("""
                    INSERT INTO public.vuz_universities
                      (id,name,short_name,region,city,website,reported_inn,profile,institution_type,
                       verification_status,is_demo)
                    VALUES (CAST(:id AS uuid),:name,:short_name,:region,:city,:website,:inn,:profile,
                            :institution_type,:verification_status,false)
                """), {"id": university_id, **values})
            except Exception as exc:
                if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23505":
                    raise RepositoryConflict("university_already_exists") from None
                raise
            return await self._university_row(conn, str(university_id))

    async def update_university(self, university_id: str, values: dict, version: int,
                                *, actor_type="human", actor_label="api"):
        columns = {"name", "short_name", "region", "city", "website", "reported_inn", "profile",
                   "institution_type", "verification_status", "photo_url"}
        updates = {key: value for key, value in values.items() if key in columns}
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            await conn.execute(text("SELECT set_config('vuz.expected_version',:version,true)"),
                               {"version": str(version)})
            current = await self._university_row(conn, university_id)
            if current is None:
                raise RepositoryNotFound("university_not_found")
            if int(current["version"]) != version:
                raise RepositoryConflict("stale_university_version")
            if updates:
                assignments = ",".join(f"{key}=:{key}" for key in updates)
                try:
                    await conn.execute(text(f"UPDATE public.vuz_universities SET {assignments} WHERE id=CAST(:id AS uuid)"),
                                       {**updates, "id": _uuid(university_id)})
                except Exception as exc:
                    if getattr(getattr(exc, "orig", None), "sqlstate", None) == "40001":
                        raise RepositoryConflict("stale_university_version") from None
                    raise
            return await self._university_row(conn, university_id)

    async def delete_university(self, university_id: str, *, actor_type="human", actor_label="api"):
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            result = await conn.execute(text("SELECT public.vuz_purge_university(CAST(:id AS uuid))"),
                                        {"id": _uuid(university_id)})
            if not result.scalar_one():
                raise RepositoryNotFound("university_not_found")

    async def _list_officials(self, conn, university_id: str):
        result = await conn.execute(text("""
            SELECT o.id::text AS id,o.full_name AS fio,o.position,o.department,o.responsibilities,
                   o.photo_url,o.source_url,o.verification_status AS status,o.version,o.appointment_status,
                   max(c.value_raw) FILTER (WHERE c.channel_type='email') AS email,
                   max(c.value_raw) FILTER (WHERE c.channel_type='phone') AS phone
            FROM public.vuz_officials o LEFT JOIN public.vuz_official_contacts c ON c.official_id=o.id
            WHERE o.university_id=CAST(:id AS uuid)
            GROUP BY o.id ORDER BY o.position,o.full_name
        """), {"id": _uuid(university_id)})
        return [dict(row) for row in result.mappings()]

    async def list_officials(self, university_id: str):
        async with self.engine.connect() as conn:
            if not await self._university_row(conn, university_id):
                raise RepositoryNotFound("university_not_found")
            return await self._list_officials(conn, university_id)

    async def create_official(self, university_id: str, values: dict, *, actor_type="human", actor_label="api"):
        official_id = uuid4()
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            if not await self._university_row(conn, university_id):
                raise RepositoryNotFound("university_not_found")
            source_id = await _source_id(conn, actor_type, actor_label, values, values.get("source_url"))
            db_values = {key: value for key, value in values.items()
                         if key in {"fio", "position", "department", "responsibilities", "photo_url",
                                    "source_url", "appointment_status", "status"}}
            await conn.execute(text("""
                INSERT INTO public.vuz_officials
                  (id,university_id,full_name,position,department,responsibilities,photo_url,source_url,
                   appointment_status,verification_status,is_demo,source_id,external_key)
                VALUES (CAST(:id AS uuid),CAST(:university_id AS uuid),:fio,:position,:department,
                   :responsibilities,:photo_url,:source_url,:appointment_status,:status,false,
                   CAST(:source_id AS uuid),:external_key)
            """), {"id": official_id, "university_id": _uuid(university_id), "source_id": source_id,
                    "external_key": str(uuid4()), **db_values})
            await self._replace_contacts(conn, official_id, values.get("email"), values.get("phone"), source_id)
            return next(item for item in await self._list_officials(conn, university_id) if item["id"] == str(official_id))

    async def update_official(self, university_id: str, official_id: str, values: dict,
                              *, actor_type="human", actor_label="api"):
        columns = {"fio": "full_name", "position": "position", "department": "department",
                   "responsibilities": "responsibilities", "photo_url": "photo_url", "source_url": "source_url",
                   "status": "verification_status", "appointment_status": "appointment_status"}
        updates = {columns[key]: value for key, value in values.items() if key in columns}
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            if not await self._university_row(conn, university_id):
                raise RepositoryNotFound("university_not_found")
            existing = (await conn.execute(text("SELECT id::text FROM public.vuz_officials WHERE id=CAST(:oid AS uuid) AND university_id=CAST(:uid AS uuid)"),
                                           {"oid": _uuid(official_id), "uid": _uuid(university_id)})).scalar_one_or_none()
            if not existing:
                raise RepositoryNotFound("official_not_found")
            source_id = await _source_id(conn, actor_type, actor_label, values, values.get("source_url"))
            updates["source_id"] = source_id
            assignments = ",".join(f"{key}=:{key}" for key in updates)
            await conn.execute(text(f"UPDATE public.vuz_officials SET {assignments} WHERE id=CAST(:id AS uuid)"),
                               {**updates, "id": _uuid(official_id)})
            if "email" in values or "phone" in values:
                current = await conn.execute(text("""
                    SELECT channel_type,max(value_raw) AS value_raw FROM public.vuz_official_contacts
                    WHERE official_id=CAST(:id AS uuid) GROUP BY channel_type
                """), {"id": _uuid(official_id)})
                contacts = {row["channel_type"]: row["value_raw"] for row in current.mappings()}
                email = values.get("email", contacts.get("email"))
                phone = values.get("phone", contacts.get("phone"))
                await self._replace_contacts(conn, official_id, email, phone, source_id)
            return next(item for item in await self._list_officials(conn, university_id) if item["id"] == official_id)

    async def _replace_contacts(self, conn, official_id: str, email: str | None, phone: str | None,
                                source_id: str, verification_status: str = "needs_review"):
        for channel, value in (("email", email), ("phone", phone)):
            current = await conn.execute(text("""
                SELECT id::text,value_raw,verification_status FROM public.vuz_official_contacts
                WHERE official_id=CAST(:id AS uuid) AND channel_type=:channel
            """), {"id": _uuid(official_id), "channel": channel})
            existing = current.mappings().one_or_none()
            if not value:
                if existing:
                    await conn.execute(text("DELETE FROM public.vuz_official_contacts WHERE id=CAST(:id AS uuid)"),
                                       {"id": _uuid(existing["id"])})
                continue
            raw = value.strip()
            normalized = raw.lower() if channel == "email" else re.sub(r"[^0-9+]", "", raw)
            if existing:
                if existing["value_raw"] != raw or existing["verification_status"] != verification_status:
                    await conn.execute(text("""
                        UPDATE public.vuz_official_contacts
                        SET value_raw=:raw,value_normalized=:normalized,source_id=CAST(:source_id AS uuid),
                            verification_status=:verification_status
                        WHERE id=CAST(:id AS uuid)
                    """), {"id": _uuid(existing["id"]), "raw": raw, "normalized": normalized,
                            "source_id": source_id, "verification_status": verification_status})
            else:
                await conn.execute(text("""
                    INSERT INTO public.vuz_official_contacts
                      (id,official_id,channel_type,value_raw,value_normalized,verification_status,source_id)
                    VALUES (CAST(:id AS uuid),CAST(:official_id AS uuid),:channel,:raw,:normalized,
                            :verification_status,CAST(:source_id AS uuid))
                """), {"id": uuid4(), "official_id": _uuid(official_id), "channel": channel,
                        "raw": raw, "normalized": normalized, "source_id": source_id,
                        "verification_status": verification_status})

    async def delete_official(self, university_id: str, official_id: str, *, actor_type="human", actor_label="api"):
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            result = await conn.execute(text("""
                DELETE FROM public.vuz_official_contacts WHERE official_id=CAST(:oid AS uuid)
                  AND official_id IN (SELECT id FROM public.vuz_officials WHERE id=CAST(:oid AS uuid) AND university_id=CAST(:uid AS uuid))
            """), {"oid": _uuid(official_id), "uid": _uuid(university_id)})
            official = await conn.execute(text("DELETE FROM public.vuz_officials WHERE id=CAST(:oid AS uuid) AND university_id=CAST(:uid AS uuid)"),
                                          {"oid": _uuid(official_id), "uid": _uuid(university_id)})
            if official.rowcount == 0:
                raise RepositoryNotFound("official_not_found")

    async def _list_metrics(self, conn, university_id: str):
        result = await conn.execute(text("""
            SELECT id::text AS id,university_id::text AS university_id,metric_code,value,unit,
                   period_start,period_end,period_kind,aggregation_kind,scope_code,methodology,scenario,
                   source_as_of,raw_value,verification_status,version
            FROM public.vuz_university_metrics WHERE university_id=CAST(:id AS uuid)
            ORDER BY metric_code,period_start DESC NULLS LAST,source_as_of DESC NULLS LAST
        """), {"id": _uuid(university_id)})
        return [dict(row) for row in result.mappings()]

    async def list_metrics(self, university_id: str):
        async with self.engine.connect() as conn:
            if not await self._university_row(conn, university_id):
                raise RepositoryNotFound("university_not_found")
            return await self._list_metrics(conn, university_id)

    async def create_metric(self, university_id: str, values: dict, *, actor_type="human", actor_label="api"):
        metric_id = uuid4()
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            if not await self._university_row(conn, university_id):
                raise RepositoryNotFound("university_not_found")
            source_id = await _source_id(conn, actor_type, actor_label, values, values.get("source_url"))
            db_values = {key: value for key, value in values.items() if key != "source_url"}
            await conn.execute(text("""
                INSERT INTO public.vuz_university_metrics
                  (id,university_id,metric_code,value,unit,period_start,period_end,period_kind,aggregation_kind,
                   scope_code,methodology,scenario,source_as_of,raw_value,verification_status,is_demo,
                   source_id,external_key)
                VALUES (CAST(:id AS uuid),CAST(:university_id AS uuid),:metric_code,:value,:unit,:period_start,
                   :period_end,:period_kind,:aggregation_kind,:scope_code,:methodology,:scenario,:source_as_of,
                   :raw_value,:verification_status,false,CAST(:source_id AS uuid),:external_key)
            """), {"id": metric_id, "university_id": _uuid(university_id), "source_id": source_id,
                    "external_key": str(uuid4()), **db_values})
            return next(item for item in await self._list_metrics(conn, university_id) if item["id"] == str(metric_id))

    async def update_metric(self, university_id: str, metric_id: str, values: dict,
                            *, actor_type="human", actor_label="api"):
        columns = {"metric_code", "value", "unit", "period_start", "period_end", "period_kind",
                   "aggregation_kind", "scope_code", "methodology", "scenario", "source_as_of",
                   "raw_value", "verification_status"}
        updates = {key: value for key, value in values.items() if key in columns}
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            existing = (await conn.execute(text("SELECT id::text FROM public.vuz_university_metrics WHERE id=CAST(:mid AS uuid) AND university_id=CAST(:uid AS uuid)"),
                                           {"mid": _uuid(metric_id), "uid": _uuid(university_id)})).scalar_one_or_none()
            if not existing:
                raise RepositoryNotFound("metric_not_found")
            if updates:
                source_id = await _source_id(conn, actor_type, actor_label, values, values.get("source_url"))
                updates["source_id"] = source_id
                assignments = ",".join(f"{key}=:{key}" for key in updates)
                await conn.execute(text(f"UPDATE public.vuz_university_metrics SET {assignments} WHERE id=CAST(:id AS uuid)"),
                                   {**updates, "id": _uuid(metric_id)})
            return await self._list_metrics(conn, university_id)

    async def delete_metric(self, university_id: str, metric_id: str, *, actor_type="human", actor_label="api"):
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            result = await conn.execute(text("DELETE FROM public.vuz_university_metrics WHERE id=CAST(:mid AS uuid) AND university_id=CAST(:uid AS uuid)"),
                                        {"mid": _uuid(metric_id), "uid": _uuid(university_id)})
            if result.rowcount == 0:
                raise RepositoryNotFound("metric_not_found")

    async def apply_ai_review(self, university_id: str, facts: list[dict], *, actor_label: str,
                              actor_type: str = "ai"):
        """Write approved facts to the relational card; database triggers audit each field."""
        try:
            university_id = str(UUID(university_id))
        except (ValueError, TypeError, AttributeError):
            return 0
        async with self.engine.begin() as conn:
            await _set_actor(conn, actor_type, actor_label)
            university = (await conn.execute(text(
                "SELECT id::text,is_demo FROM public.vuz_universities WHERE id=CAST(:id AS uuid)"),
                {"id": _uuid(university_id)})).mappings().one_or_none()
            if not university:
                return 0

            applied = 0
            people: dict[str, dict] = {}
            normalized_fields = {
                "institution.profile", "institution.type", "institution.students",
                "appointment.position", "appointment.department", "appointment.responsibilities",
                "appointment.status", "contact.email", "contact.phone",
            }
            for fact in facts:
                field = str(fact.get("field", ""))
                value = str(fact.get("value", "")).strip()
                evidence = fact.get("evidence") or []
                source_url = evidence[0].get("url") if evidence else None
                subject = fact.get("subject") or {}
                if field not in normalized_fields and field and value:
                    fact_source = await _source_id(
                        conn, actor_type, actor_label,
                        {"fact_id": fact.get("fact_id"), "field": field, "value": value}, source_url,
                    )
                    await conn.execute(text("""
                        INSERT INTO public.vuz_legacy_fields
                          (id,university_id,source_id,source_key,field_name,value,reason)
                        VALUES (CAST(:id AS uuid),CAST(:uid AS uuid),CAST(:source_id AS uuid),
                                :source_key,:field,CAST(:value AS jsonb),:reason)
                        ON CONFLICT(source_id,source_key,field_name) DO UPDATE
                          SET value=EXCLUDED.value,reason=EXCLUDED.reason
                    """), {"id": uuid4(), "uid": _uuid(university_id), "source_id": fact_source,
                            "source_key": str(fact.get("fact_id") or uuid4()), "field": field,
                            "value": json.dumps(value, ensure_ascii=False),
                            "reason": f"Approved fact; actor={actor_label[:160]}"})
                    applied += 1
                if field == "institution.profile" and value:
                    await conn.execute(text("UPDATE public.vuz_universities SET profile=:value WHERE id=CAST(:id AS uuid)"),
                                       {"id": _uuid(university_id), "value": value})
                    applied += 1
                elif field == "institution.type":
                    institution_type = value.casefold().strip()
                    if institution_type in {"university", "spo", "mixed", "unknown"}:
                        await conn.execute(text("UPDATE public.vuz_universities SET institution_type=:value WHERE id=CAST(:id AS uuid)"),
                                           {"id": _uuid(university_id), "value": institution_type})
                        applied += 1
                elif field == "institution.students" and value:
                    applied += await self._apply_ai_student_metric(conn, university_id, value, fact,
                                                                    bool(university["is_demo"]), actor_label)
                name = subject.get("person_name")
                if not name:
                    continue
                person = people.setdefault(name.casefold(), {"fio": name, "source_url": source_url})
                if source_url and not person.get("source_url"):
                    person["source_url"] = source_url
                if field == "person.name":
                    person["fio"] = value
                elif field == "appointment.position":
                    person["position"] = value
                elif field == "appointment.department":
                    person["department"] = value
                elif field == "appointment.responsibilities":
                    person["responsibilities"] = value
                elif field == "appointment.status" and value in {"current", "former", "unknown"}:
                    person["appointment_status"] = value
                elif field == "contact.email":
                    person["email"] = value
                elif field == "contact.phone":
                    person["phone"] = value

            for person in people.values():
                existing = (await conn.execute(text("""
                    SELECT id::text AS id,position,appointment_status FROM public.vuz_officials
                    WHERE university_id=CAST(:uid AS uuid) AND lower(full_name)=lower(:fio)
                    ORDER BY appointment_status='current' DESC,verification_status='verified' DESC LIMIT 1
                """), {"uid": _uuid(university_id), "fio": person["fio"]})).mappings().one_or_none()
                if not existing and not person.get("position"):
                    continue
                source_id = await _source_id(conn, actor_type, actor_label, person, person.get("source_url"))
                verification_status = "ai_draft" if university["is_demo"] else "verified"
                if existing:
                    fields = {key: person[key] for key in ("fio", "position", "department", "responsibilities") if key in person}
                    setters = {"fio": "full_name", "position": "position", "department": "department",
                               "responsibilities": "responsibilities"}
                    updates = {setters[key]: value for key, value in fields.items()}
                    updates.update({"source_id": source_id, "source_url": person.get("source_url"),
                                    "verification_status": verification_status})
                    if "appointment_status" in person:
                        updates["appointment_status"] = person["appointment_status"]
                    assignments = ",".join(f"{key}=:{key}" for key in updates)
                    await conn.execute(text(f"UPDATE public.vuz_officials SET {assignments} WHERE id=CAST(:id AS uuid)"),
                                       {**updates, "id": _uuid(existing["id"])})
                    official_id = existing["id"]
                else:
                    official_id = uuid4()
                    await conn.execute(text("""
                        INSERT INTO public.vuz_officials
                          (id,university_id,full_name,position,department,responsibilities,source_url,
                           appointment_status,verification_status,is_demo,source_id,external_key)
                        VALUES (CAST(:id AS uuid),CAST(:uid AS uuid),:fio,:position,:department,:responsibilities,
                           :source_url,:appointment_status,:verification_status,:is_demo,CAST(:source_id AS uuid),:external_key)
                    """), {"id": official_id, "uid": _uuid(university_id), "fio": person["fio"],
                            "position": person.get("position") or "Руководитель",
                            "department": person.get("department"),
                            "responsibilities": person.get("responsibilities"),
                            "appointment_status": person.get("appointment_status", "current"),
                            "source_url": person.get("source_url"), "verification_status": verification_status,
                            "is_demo": bool(university["is_demo"]), "source_id": source_id,
                            "external_key": f"ai:{person['fio'].casefold()}:{str(uuid4())}"})
                if "email" in person or "phone" in person:
                    contacts = (await conn.execute(text("""
                        SELECT channel_type,max(value_raw) AS value_raw FROM public.vuz_official_contacts
                        WHERE official_id=CAST(:id AS uuid) GROUP BY channel_type
                    """), {"id": _uuid(official_id)})).mappings().all()
                    current = {item["channel_type"]: item["value_raw"] for item in contacts}
                    await self._replace_contacts(conn, official_id, person.get("email", current.get("email")),
                                                 person.get("phone", current.get("phone")), source_id,
                                                 verification_status=verification_status)
                applied += 1
            return applied

    async def _apply_ai_student_metric(self, conn, university_id: str, raw_value: str,
                                       fact: dict, is_demo: bool, actor_label: str) -> int:
        compact = raw_value.replace("\u00a0", " ").replace(" ", "")
        match = re.search(r"\d+(?:[,.]\d+)?", compact)
        if not match:
            return 0
        number = Decimal(match.group(0).replace(",", "."))
        if "млн" in raw_value.casefold():
            number *= Decimal(1_000_000)
        elif "тыс" in raw_value.casefold():
            number *= Decimal(1_000)
        context = fact.get("metric_context") or {}
        period_text = str(context.get("period", ""))
        year_match = re.search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", period_text)
        period_start = date(int(year_match.group(1)), 1, 1) if year_match else None
        period_end = date(int(year_match.group(1)), 12, 31) if year_match else None
        scope_text = str(context.get("scope", "unknown")).strip()
        scope_code = re.sub(r"[^\w]+", "_", scope_text.casefold()).strip("_")[:80] or "unknown"
        unit = str(context.get("unit") or "person")[:80]
        evidence = fact.get("evidence") or []
        source_url = evidence[0].get("url") if evidence else None
        retrieved = evidence[0].get("retrieved_at") if evidence else None
        retrieved_at = datetime.fromisoformat(retrieved.replace("Z", "+00:00")) if isinstance(retrieved, str) else retrieved
        verification_status = "verified" if period_start and scope_code != "unknown" and not is_demo else "ai_draft"
        source_id = await _source_id(conn, "ai", actor_label, {"raw_value": raw_value, "fact": fact.get("fact_id")}, source_url)
        existing = (await conn.execute(text("""
            SELECT id::text AS id FROM public.vuz_university_metrics
            WHERE university_id=CAST(:uid AS uuid) AND metric_code='student_count'
              AND period_start IS NOT DISTINCT FROM :period_start
              AND scope_code=:scope AND scenario='reported'
            ORDER BY verification_status='verified' DESC LIMIT 1
        """), {"uid": _uuid(university_id), "period_start": period_start, "scope": scope_code})).mappings().one_or_none()
        values = {"university_id": _uuid(university_id), "value": number, "unit": unit,
                  "period_start": period_start, "period_end": period_end,
                  "period_kind": "year" if period_start else "unknown",
                  "aggregation_kind": "snapshot" if period_start else "unknown",
                  "scope_code": scope_code, "methodology": "AI extraction from cited source",
                  "scenario": "reported", "source_as_of": retrieved_at, "raw_value": raw_value,
                  "verification_status": verification_status, "source_id": source_id}
        if existing:
            updates = ",".join(f"{key}=:{key}" for key in values if key not in {"university_id"})
            await conn.execute(text(f"UPDATE public.vuz_university_metrics SET {updates} WHERE id=CAST(:id AS uuid)"),
                               {**values, "id": _uuid(existing["id"])})
        else:
            await conn.execute(text("""
                INSERT INTO public.vuz_university_metrics
                  (id,university_id,metric_code,value,unit,period_start,period_end,period_kind,aggregation_kind,
                   scope_code,methodology,scenario,source_as_of,raw_value,verification_status,is_demo,
                   source_id,external_key)
                VALUES (CAST(:id AS uuid),CAST(:university_id AS uuid),'student_count',:value,:unit,:period_start,
                   :period_end,:period_kind,:aggregation_kind,:scope_code,:methodology,:scenario,:source_as_of,
                   :raw_value,:verification_status,:is_demo,CAST(:source_id AS uuid),:external_key)
            """), {"id": uuid4(), "is_demo": is_demo,
                    "external_key": f"ai:student_count:{scope_code}:{period_text or 'unknown'}:{str(uuid4())}", **values})
        return 1
