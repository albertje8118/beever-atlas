"""SQLiteGraphStore — embedded SQLite graph database backend for the GraphStore protocol.

SQLite-backed persistent store: Entity, Event, Media nodes; ENTITY_REL,
HAS_EVENT, REFS_MEDIA edges stored in junction tables.

All async methods use aiosqlite to avoid blocking the event loop.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

import aiosqlite

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph

from ._sqlite_db import ensure_data_dir, get_db_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = [
    """CREATE TABLE IF NOT EXISTS entities (
        name TEXT PRIMARY KEY,
        etype TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT '',
        channel_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        aliases_json TEXT NOT NULL DEFAULT '[]',
        name_vector_json TEXT NOT NULL DEFAULT '[]',
        properties_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS entities_channel ON entities(channel_id)",
    "CREATE INDEX IF NOT EXISTS entities_status ON entities(status)",
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        entity_name TEXT NOT NULL,
        weaviate_fact_id TEXT NOT NULL DEFAULT '',
        message_ts TEXT NOT NULL DEFAULT '',
        channel_id TEXT NOT NULL DEFAULT '',
        media_urls_json TEXT NOT NULL DEFAULT '[]',
        link_urls_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS events_entity ON events(entity_name)",
    "CREATE INDEX IF NOT EXISTS events_channel ON events(channel_id)",
    """CREATE TABLE IF NOT EXISTS media (
        url TEXT PRIMARY KEY,
        media_type TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        channel_id TEXT NOT NULL DEFAULT '',
        message_ts TEXT NOT NULL DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS media_channel ON media(channel_id)",
    """CREATE TABLE IF NOT EXISTS entity_rels (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        rel_type TEXT NOT NULL DEFAULT '',
        context TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 1.0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(source) REFERENCES entities(name) ON DELETE CASCADE,
        FOREIGN KEY(target) REFERENCES entities(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS entity_rels_source ON entity_rels(source)",
    "CREATE INDEX IF NOT EXISTS entity_rels_target ON entity_rels(target)",
    """CREATE TABLE IF NOT EXISTS entity_events (
        entity_name TEXT NOT NULL,
        event_id TEXT NOT NULL,
        PRIMARY KEY(entity_name, event_id),
        FOREIGN KEY(entity_name) REFERENCES entities(name) ON DELETE CASCADE,
        FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS entity_media (
        entity_name TEXT NOT NULL,
        media_url TEXT NOT NULL,
        PRIMARY KEY(entity_name, media_url),
        FOREIGN KEY(entity_name) REFERENCES entities(name) ON DELETE CASCADE,
        FOREIGN KEY(media_url) REFERENCES media(url) ON DELETE CASCADE
    )""",
]


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _row_to_entity(row: aiosqlite.Row) -> GraphEntity:
    name, etype, scope, channel_id, status, al_j, nv_j, pr_j = (
        row["name"], row["etype"], row["scope"], row["channel_id"],
        row["status"], row["aliases_json"], row["name_vector_json"], row["properties_json"],
    )
    return GraphEntity(
        id=name,
        name=name,
        type=etype,
        scope=scope or None,
        channel_id=channel_id,
        status=status,
        aliases=json.loads(al_j) if al_j else [],
        name_vector=json.loads(nv_j) if nv_j and nv_j != "[]" else None,
        properties=json.loads(pr_j) if pr_j else {},
    )


def _row_to_rel(row: aiosqlite.Row) -> GraphRelationship:
    return GraphRelationship(
        id=row["id"],
        type=row["rel_type"],
        source=row["source"],
        target=row["target"],
        context=row["context"] or "",
        confidence=float(row["confidence"]) if row["confidence"] is not None else 1.0,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SQLiteGraphStore:
    """Embedded SQLite-backed GraphStore. All operations use aiosqlite."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or get_db_path()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a new aiosqlite connection with WAL + FK support."""
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            yield conn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        ensure_data_dir()
        await self.ensure_schema()
        logger.info("SQLiteGraphStore: started (path=%s)", self._db_path)

    async def shutdown(self) -> None:
        pass  # Per-operation connections — nothing to close

    async def ensure_schema(self) -> None:
        async with self._conn() as conn:
            for stmt in _DDL:
                await conn.execute(stmt)
            await conn.commit()

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO entities
                   (name, etype, scope, channel_id, status, aliases_json,
                    name_vector_json, properties_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     etype=excluded.etype, scope=excluded.scope,
                     channel_id=excluded.channel_id, status=excluded.status,
                     aliases_json=excluded.aliases_json,
                     name_vector_json=excluded.name_vector_json,
                     properties_json=excluded.properties_json""",
                (
                    entity.name,
                    entity.type or "",
                    entity.scope or "",
                    entity.channel_id or "",
                    entity.status or "active",
                    json.dumps(entity.aliases or []),
                    json.dumps(entity.name_vector or []),
                    json.dumps(entity.properties or {}),
                ),
            )
            await conn.commit()
        return entity.name

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        if not entities:
            return []
        async with self._conn() as conn:
            await conn.executemany(
                """INSERT INTO entities
                   (name, etype, scope, channel_id, status, aliases_json,
                    name_vector_json, properties_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     etype=excluded.etype, scope=excluded.scope,
                     channel_id=excluded.channel_id, status=excluded.status,
                     aliases_json=excluded.aliases_json,
                     name_vector_json=excluded.name_vector_json,
                     properties_json=excluded.properties_json""",
                [
                    (
                        e.name,
                        e.type or "",
                        e.scope or "",
                        e.channel_id or "",
                        e.status or "active",
                        json.dumps(e.aliases or []),
                        json.dumps(e.name_vector or []),
                        json.dumps(e.properties or {}),
                    )
                    for e in entities
                ],
            )
            await conn.commit()
        return [e.name for e in entities]

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT name, etype, scope, channel_id, status, "
                "aliases_json, name_vector_json, properties_json "
                "FROM entities WHERE name = ?",
                (entity_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_entity(row) if row else None

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        return await self.get_entity(name)

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if entity_type:
            clauses.append("LOWER(etype) = LOWER(?)")
            params.append(entity_type)
        if not include_pending:
            clauses.append("status <> 'pending'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with self._conn() as conn:
            async with conn.execute(
                f"SELECT name, etype, scope, channel_id, status, "
                f"aliases_json, name_vector_json, properties_json "
                f"FROM entities {where} LIMIT ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_entity(r) for r in rows]

    async def count_entities(self, channel_id: str | None = None) -> int:
        async with self._conn() as conn:
            if channel_id is None:
                async with conn.execute(
                    "SELECT COUNT(*) as cnt FROM entities WHERE status = 'active'"
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with conn.execute(
                    "SELECT COUNT(*) as cnt FROM entities WHERE channel_id = ? AND status = 'active'",
                    (channel_id,),
                ) as cur:
                    row = await cur.fetchone()
        return row["cnt"] if row else 0

    async def promote_pending_entity(self, entity_name: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE entities SET status='active' WHERE name = ?", (entity_name,)
            )
            await conn.commit()

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM entities WHERE status='pending' "
                "AND created_at < datetime('now', ?)",
                (f"-{grace_period_days} days",),
            ) as cur:
                row = await cur.fetchone()
            count = row[0] if row else 0
            if count:
                await conn.execute(
                    "DELETE FROM entities WHERE status='pending' "
                    "AND created_at < datetime('now', ?)",
                    (f"-{grace_period_days} days",),
                )
                await conn.commit()
        return count

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def _ensure_entity_stub(self, conn: aiosqlite.Connection, name: str) -> None:
        """Insert a minimal entity stub if one doesn't exist."""
        await conn.execute(
            "INSERT OR IGNORE INTO entities (name, etype, scope, channel_id, status, "
            "aliases_json, name_vector_json, properties_json) VALUES (?, '', '', '', 'active', '[]', '[]', '{}')",
            (name,),
        )

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        async with self._conn() as conn:
            await self._ensure_entity_stub(conn, rel.source)
            await self._ensure_entity_stub(conn, rel.target)
            await conn.execute(
                """INSERT INTO entity_rels (id, source, target, rel_type, context, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     source=excluded.source, target=excluded.target,
                     rel_type=excluded.rel_type, context=excluded.context,
                     confidence=excluded.confidence""",
                (rel.id, rel.source, rel.target, rel.type, rel.context or "", rel.confidence),
            )
            await conn.commit()
        return rel.id

    async def batch_upsert_relationships(self, rels: list[GraphRelationship]) -> list[str]:
        if not rels:
            return []
        async with self._conn() as conn:
            for rel in rels:
                await self._ensure_entity_stub(conn, rel.source)
                await self._ensure_entity_stub(conn, rel.target)
            await conn.executemany(
                """INSERT INTO entity_rels (id, source, target, rel_type, context, confidence)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     source=excluded.source, target=excluded.target,
                     rel_type=excluded.rel_type, context=excluded.context,
                     confidence=excluded.confidence""",
                [
                    (r.id, r.source, r.target, r.type, r.context or "", r.confidence)
                    for r in rels
                ],
            )
            await conn.commit()
        return [r.id for r in rels]

    async def list_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[GraphRelationship]:
        async with self._conn() as conn:
            if channel_id:
                async with conn.execute(
                    """SELECT er.id, er.source, er.target, er.rel_type, er.context, er.confidence
                       FROM entity_rels er
                       JOIN entities src ON er.source = src.name
                       JOIN entities tgt ON er.target = tgt.name
                       WHERE src.channel_id = ? OR tgt.channel_id = ?
                       LIMIT ?""",
                    (channel_id, channel_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT id, source, target, rel_type, context, confidence "
                    "FROM entity_rels LIMIT ?",
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        return [_row_to_rel(r) for r in rows]

    async def count_relationships(self, channel_id: str | None = None) -> int:
        async with self._conn() as conn:
            if channel_id:
                async with conn.execute(
                    """SELECT COUNT(*) FROM entity_rels er
                       JOIN entities src ON er.source = src.name
                       JOIN entities tgt ON er.target = tgt.name
                       WHERE src.channel_id = ? OR tgt.channel_id = ?""",
                    (channel_id, channel_id),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with conn.execute("SELECT COUNT(*) FROM entity_rels") as cur:
                    row = await cur.fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Episodic + Media
    # ------------------------------------------------------------------

    async def create_episodic_link(
        self,
        entity_name: str,
        weaviate_fact_id: str,
        message_ts: str,
        channel_id: str = "",
        media_urls: list[str] | None = None,
        link_urls: list[str] | None = None,
    ) -> None:
        event_id = f"{entity_name}:{weaviate_fact_id}"
        async with self._conn() as conn:
            # Ensure entity exists
            async with conn.execute(
                "SELECT name FROM entities WHERE name = ?", (entity_name,)
            ) as cur:
                if await cur.fetchone() is None:
                    return  # Skip if entity doesn't exist
            await conn.execute(
                """INSERT INTO events
                   (id, entity_name, weaviate_fact_id, message_ts, channel_id,
                    media_urls_json, link_urls_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     weaviate_fact_id=excluded.weaviate_fact_id,
                     message_ts=excluded.message_ts, channel_id=excluded.channel_id,
                     media_urls_json=excluded.media_urls_json,
                     link_urls_json=excluded.link_urls_json""",
                (
                    event_id, entity_name, weaviate_fact_id, message_ts, channel_id,
                    json.dumps(media_urls or []), json.dumps(link_urls or []),
                ),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO entity_events (entity_name, event_id) VALUES (?, ?)",
                (entity_name, event_id),
            )
            await conn.commit()

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO media (url, media_type, title, channel_id, message_ts)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     media_type=excluded.media_type, title=excluded.title,
                     channel_id=excluded.channel_id, message_ts=excluded.message_ts""",
                (url, media_type, title, channel_id, message_ts),
            )
            await conn.commit()

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO entity_media (entity_name, media_url) VALUES (?, ?)",
                (entity_name, media_url),
            )
            await conn.commit()

    async def list_media(
        self, channel_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            if channel_id:
                async with conn.execute(
                    "SELECT url, media_type, title, channel_id, message_ts "
                    "FROM media WHERE channel_id = ? LIMIT ?",
                    (channel_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT url, media_type, title, channel_id, message_ts FROM media LIMIT ?",
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        return [
            {"url": r["url"], "media_type": r["media_type"], "title": r["title"],
             "channel_id": r["channel_id"], "message_ts": r["message_ts"]}
            for r in rows
        ]

    async def list_media_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            if channel_id:
                async with conn.execute(
                    """SELECT em.entity_name, em.media_url FROM entity_media em
                       JOIN entities e ON em.entity_name = e.name
                       JOIN media m ON em.media_url = m.url
                       WHERE e.channel_id = ? OR m.channel_id = ?
                       LIMIT ?""",
                    (channel_id, channel_id, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with conn.execute(
                    "SELECT entity_name, media_url FROM entity_media LIMIT ?", (limit,)
                ) as cur:
                    rows = await cur.fetchall()
        return [{"entity_name": r["entity_name"], "media_url": r["media_url"]} for r in rows]

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def get_neighbors(self, entity_id: str, hops: int = 1, limit: int = 50) -> Subgraph:
        async with self._conn() as conn:
            async with conn.execute(
                """SELECT DISTINCT e.name, e.etype, e.scope, e.channel_id, e.status,
                          e.aliases_json, e.name_vector_json, e.properties_json
                   FROM entities e
                   JOIN entity_rels er ON (er.source = ? AND er.target = e.name)
                                       OR (er.target = ? AND er.source = e.name)
                   LIMIT ?""",
                (entity_id, entity_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        nodes = [_row_to_entity(r) for r in rows]
        return Subgraph(nodes=nodes, edges=[])

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        async with self._conn() as conn:
            async with conn.execute(
                """SELECT name, etype, scope, channel_id, status,
                          aliases_json, name_vector_json, properties_json
                   FROM entities WHERE channel_id = ? AND etype = 'Decision' LIMIT ?""",
                (channel_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_entity(r) for r in rows]

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM entities WHERE channel_id = ?", (channel_id,)
            ) as cur:
                ent_count = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM events WHERE channel_id = ?", (channel_id,)
            ) as cur:
                ev_count = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM media WHERE channel_id = ?", (channel_id,)
            ) as cur:
                med_count = (await cur.fetchone())[0]

            # entity_rels cascade-delete when entities are deleted
            await conn.execute(
                "DELETE FROM entities WHERE channel_id = ?", (channel_id,)
            )
            await conn.execute(
                "DELETE FROM events WHERE channel_id = ?", (channel_id,)
            )
            await conn.execute(
                "DELETE FROM media WHERE channel_id = ?", (channel_id,)
            )
            await conn.commit()
        return {
            "entities_deleted": ent_count,
            "events_deleted": ev_count,
            "media_deleted": med_count,
        }

    # ------------------------------------------------------------------
    # Entity-registry support
    # ------------------------------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT name FROM entities WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                return row["name"]
            # Scan aliases JSON (slow but acceptable for embedded dev)
            async with conn.execute(
                "SELECT name, aliases_json FROM entities"
            ) as cur:
                rows = await cur.fetchall()
        for r in rows:
            try:
                aliases: list[str] = json.loads(r["aliases_json"]) if r["aliases_json"] else []
                if name in aliases:
                    return r["name"]
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT name, etype, aliases_json FROM entities"
            ) as cur:
                rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            try:
                aliases = json.loads(r["aliases_json"]) if r["aliases_json"] else []
            except (json.JSONDecodeError, TypeError):
                aliases = []
            result.append({"name": r["name"], "type": r["etype"], "aliases": aliases})
        return result

    async def register_alias(self, canonical: str, alias: str, entity_type: str) -> None:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT aliases_json FROM entities WHERE name = ?", (canonical,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return
            try:
                aliases: list[str] = json.loads(row["aliases_json"]) if row["aliases_json"] else []
            except (json.JSONDecodeError, TypeError):
                aliases = []
            if alias not in aliases:
                aliases.append(alias)
            await conn.execute(
                "UPDATE entities SET aliases_json = ? WHERE name = ?",
                (json.dumps(aliases), canonical),
            )
            await conn.commit()

    async def fuzzy_match_entities(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        try:
            import jellyfish
        except ImportError:
            return []
        async with self._conn() as conn:
            async with conn.execute("SELECT name FROM entities") as cur:
                rows = await cur.fetchall()
        results: list[tuple[str, float]] = []
        for r in rows:
            score = jellyfish.jaro_winkler_similarity(name, r["name"])
            if score >= threshold:
                results.append((r["name"], score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT name, name_vector_json FROM entities "
                "WHERE name_vector_json IS NOT NULL AND name_vector_json <> '[]' "
                "AND name_vector_json <> ''"
            ) as cur:
                rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            try:
                vec = json.loads(r["name_vector_json"])
                if vec:
                    result.append({"name": r["name"], "vec": vec})
            except (json.JSONDecodeError, TypeError):
                pass
        return result

    async def get_entities_missing_name_vectors(self) -> list[str]:
        async with self._conn() as conn:
            async with conn.execute(
                "SELECT name FROM entities "
                "WHERE name_vector_json IS NULL OR name_vector_json = '[]' "
                "OR name_vector_json = ''"
            ) as cur:
                rows = await cur.fetchall()
        return [r["name"] for r in rows]

    async def store_name_vector(self, entity_name: str, vector: list[float]) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE entities SET name_vector_json = ? WHERE name = ?",
                (json.dumps(vector), entity_name),
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict[str, Any]]) -> int:
        count = 0
        for link in links:
            await self.create_episodic_link(
                entity_name=link["entity_name"],
                weaviate_fact_id=link.get("weaviate_fact_id", ""),
                message_ts=link.get("message_ts", ""),
                channel_id=link.get("channel_id", ""),
                media_urls=link.get("media_urls"),
                link_urls=link.get("link_urls"),
            )
            count += 1
        return count

    async def batch_upsert_media(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        async with self._conn() as conn:
            await conn.executemany(
                """INSERT INTO media (url, media_type, title, channel_id, message_ts)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     media_type=excluded.media_type, title=excluded.title,
                     channel_id=excluded.channel_id, message_ts=excluded.message_ts""",
                [
                    (
                        item["url"], item.get("media_type", ""), item.get("title", ""),
                        item.get("channel_id", ""), item.get("message_ts", ""),
                    )
                    for item in items
                ],
            )
            await conn.commit()
        return len(items)

    async def batch_link_entities_to_media(self, links: list[dict[str, Any]]) -> int:
        if not links:
            return 0
        async with self._conn() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO entity_media (entity_name, media_url) VALUES (?, ?)",
                [(link["entity_name"], link["media_url"]) for link in links],
            )
            await conn.commit()
        return len(links)

    async def batch_promote_pending(self, names: list[str]) -> int:
        if not names:
            return 0
        async with self._conn() as conn:
            await conn.executemany(
                "UPDATE entities SET status='active' WHERE name = ?",
                [(name,) for name in names],
            )
            await conn.commit()
        return len(names)

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        if not names:
            return set()
        placeholders = ",".join("?" * len(names))
        async with self._conn() as conn:
            async with conn.execute(
                f"SELECT name FROM entities WHERE name IN ({placeholders})", list(names)
            ) as cur:
                rows = await cur.fetchall()
        return {r["name"] for r in rows}

    # ------------------------------------------------------------------
    # WikiDataGatherer compatibility shims
    # These map the Neo4j-specific WikiDataGatherer API to generic list_entities
    # so wiki generation works with the embedded SQLite graph backend.
    # ------------------------------------------------------------------

    async def list_person_entities_with_edges(self, channel_id: str) -> list[GraphEntity]:
        """Return person entities for the channel (wiki compatibility shim)."""
        return await self.list_entities(channel_id=channel_id, entity_type="person", limit=100)

    async def get_decisions_with_chains(self, channel_id: str) -> list[GraphEntity]:
        """Return decision entities for the channel (wiki compatibility shim)."""
        return await self.get_decisions(channel_id=channel_id, limit=50)

    async def list_technology_entities(self, channel_id: str) -> list[GraphEntity]:
        """Return technology entities for the channel (wiki compatibility shim)."""
        return await self.list_entities(channel_id=channel_id, entity_type="technology", limit=100)

    async def list_project_entities(self, channel_id: str) -> list[GraphEntity]:
        """Return project entities for the channel (wiki compatibility shim)."""
        return await self.list_entities(channel_id=channel_id, entity_type="project", limit=100)

