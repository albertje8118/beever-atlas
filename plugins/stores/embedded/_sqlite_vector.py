"""SQLite-backed embedded store implementations.

This module avoids importing ``beever_atlas`` at module import time because the
plugin system loads before the app package is guaranteed to be importable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite
import numpy as np

from plugins.stores.embedded._sqlite_db import get_db_path

try:
    from plugins.stores.embedded._sqlite_db import ensure_data_dir
except ImportError:  # pragma: no cover - compatibility with minimal helper module

    def ensure_data_dir() -> None:
        path = get_db_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


try:
    from plugins.stores.embedded._sqlite_db import get_connection as _shared_get_connection
except ImportError:  # pragma: no cover - compatibility with the current helper file
    _shared_get_connection = None

if TYPE_CHECKING:
    from beever_atlas.models import AtomicFact, MemoryFilters, PaginatedFacts
    from beever_atlas.models.domain import ChannelSummary, EntityKnowledgeCard, TopicCluster

logger = logging.getLogger(__name__)

_FACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    memory_text TEXT NOT NULL DEFAULT '',
    quality_score REAL DEFAULT 0.0,
    cluster_id TEXT DEFAULT '',
    platform TEXT DEFAULT '',
    author_id TEXT DEFAULT '',
    author_name TEXT DEFAULT '',
    message_ts TEXT DEFAULT '',
    thread_ts TEXT DEFAULT '',
    source_message_id TEXT DEFAULT '',
    importance TEXT DEFAULT '',
    fact_type TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    topic_tags TEXT DEFAULT '[]',
    entity_tags TEXT DEFAULT '[]',
    action_tags TEXT DEFAULT '[]',
    graph_entity_ids TEXT DEFAULT '[]',
    source_media_urls TEXT DEFAULT '[]',
    source_media_names TEXT DEFAULT '[]',
    source_link_urls TEXT DEFAULT '[]',
    source_link_titles TEXT DEFAULT '[]',
    source_link_descriptions TEXT DEFAULT '[]',
    source_media_url TEXT DEFAULT '',
    source_media_type TEXT DEFAULT '',
    valid_at TEXT,
    invalid_at TEXT,
    superseded_by TEXT,
    supersedes TEXT,
    potential_contradiction INTEGER DEFAULT 0,
    thread_context_summary TEXT DEFAULT '',
    text_vector BLOB,
    extra_props TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS facts_channel_tier ON facts(channel_id, tier);
CREATE INDEX IF NOT EXISTS facts_cluster_id ON facts(cluster_id);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(memory_text, content='facts', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS facts_fts_insert AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, memory_text) VALUES (new.rowid, new.memory_text);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_delete AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, memory_text) VALUES ('delete', old.rowid, old.memory_text);
END;
CREATE TRIGGER IF NOT EXISTS facts_fts_update AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, memory_text) VALUES ('delete', old.rowid, old.memory_text);
    INSERT INTO facts_fts(rowid, memory_text) VALUES (new.rowid, new.memory_text);
END;
"""

_QA_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_history (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    citations_json TEXT DEFAULT '{}',
    channel_id TEXT NOT NULL,
    user_id TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    is_deleted INTEGER DEFAULT 0,
    answer_kind TEXT DEFAULT 'answered',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS qa_history_channel_deleted ON qa_history(channel_id, is_deleted);
CREATE VIRTUAL TABLE IF NOT EXISTS qa_history_fts USING fts5(
    question,
    answer,
    content='qa_history',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS qa_history_fts_insert AFTER INSERT ON qa_history BEGIN
    INSERT INTO qa_history_fts(rowid, question, answer)
    VALUES (new.rowid, new.question, new.answer);
END;
CREATE TRIGGER IF NOT EXISTS qa_history_fts_delete AFTER DELETE ON qa_history BEGIN
    INSERT INTO qa_history_fts(qa_history_fts, rowid, question, answer)
    VALUES ('delete', old.rowid, old.question, old.answer);
END;
CREATE TRIGGER IF NOT EXISTS qa_history_fts_update AFTER UPDATE ON qa_history BEGIN
    INSERT INTO qa_history_fts(qa_history_fts, rowid, question, answer)
    VALUES ('delete', old.rowid, old.question, old.answer);
    INSERT INTO qa_history_fts(rowid, question, answer)
    VALUES (new.rowid, new.question, new.answer);
END;
"""

_FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT DEFAULT 'application/octet-stream',
    size_bytes INTEGER,
    owner_user_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_REFUSAL_MARKERS = [
    "no record",
    "no information",
    "I don't have",
    "not identified",
    "couldn't find",
    "no evidence",
]
_REFUSAL_LENGTH_THRESHOLD = 400
_UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_UNCLUSTERED_CLUSTER_IDS = {"", "__none__"}
_ATOMIC_FACT_FIELDS = {
    "id",
    "memory_text",
    "quality_score",
    "tier",
    "cluster_id",
    "channel_id",
    "platform",
    "author_id",
    "author_name",
    "message_ts",
    "thread_ts",
    "source_message_id",
    "topic_tags",
    "entity_tags",
    "action_tags",
    "importance",
    "graph_entity_ids",
    "source_media_url",
    "source_media_type",
    "source_media_urls",
    "source_media_names",
    "source_link_urls",
    "source_link_titles",
    "source_link_descriptions",
    "valid_at",
    "invalid_at",
    "superseded_by",
    "supersedes",
    "potential_contradiction",
    "text_vector",
    "fact_type",
    "thread_context_summary",
}
_TOPIC_CLUSTER_EXTRA_FIELDS = [
    "title",
    "current_state",
    "open_questions",
    "impact_note",
    "member_ids",
    "member_count",
    "key_entities",
    "key_relationships",
    "key_decisions",
    "key_topics",
    "key_facts",
    "decisions",
    "people",
    "technologies",
    "projects",
    "faq_candidates",
    "authors",
    "date_range_start",
    "date_range_end",
    "high_importance_count",
    "media_refs",
    "media_names",
    "link_refs",
    "related_cluster_ids",
    "staleness_score",
    "status",
    "fact_type_counts",
    "worst_staleness",
]
_CHANNEL_SUMMARY_EXTRA_FIELDS = [
    "channel_name",
    "description",
    "themes",
    "momentum",
    "team_dynamics",
    "cluster_count",
    "fact_count",
    "updated_at",
    "key_decisions",
    "key_entities",
    "key_topics",
    "date_range_start",
    "date_range_end",
    "media_count",
    "author_count",
    "worst_staleness",
    "top_decisions",
    "top_people",
    "tech_stack",
    "active_projects",
    "glossary_terms",
    "recent_activity_summary",
    "topic_graph_edges",
]
_ENTITY_CARD_EXTRA_FIELDS = [
    "entity_id",
    "entity_name",
    "entity_type",
    "channel_ids",
    "cluster_ids",
    "fact_count",
    "fact_type_breakdown",
    "key_facts",
    "related_entities",
    "last_mentioned_at",
    "staleness_score",
    "updated_at",
]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)



def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")



def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback



def _serialize_datetime(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()



def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None



def _classify_answer(answer: str) -> str:
    try:
        if len(answer) < _REFUSAL_LENGTH_THRESHOLD and any(
            marker.lower() in answer.lower() for marker in _REFUSAL_MARKERS
        ):
            return "refused"
        return "answered"
    except Exception:
        return "answered"



def _deterministic_id(prefix: str, value: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{prefix}:{value}"))



def _atomic_fact_cls():
    from beever_atlas.models import AtomicFact

    return AtomicFact



def _memory_filters_cls():
    from beever_atlas.models import MemoryFilters

    return MemoryFilters



def _paginated_facts_cls():
    from beever_atlas.models import PaginatedFacts

    return PaginatedFacts



def _topic_cluster_cls():
    from beever_atlas.models.domain import TopicCluster

    return TopicCluster



def _channel_summary_cls():
    from beever_atlas.models.domain import ChannelSummary

    return ChannelSummary



def _entity_card_cls():
    from beever_atlas.models.domain import EntityKnowledgeCard

    return EntityKnowledgeCard


class _SQLiteStoreMixin:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or get_db_path()

    async def _connect(self) -> aiosqlite.Connection:
        ensure_data_dir()
        if _shared_get_connection is not None:
            try:
                conn = await _shared_get_connection(self._db_path)
            except TypeError:
                conn = await _shared_get_connection()
        else:
            conn = await aiosqlite.connect(self._db_path)
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA temp_store=MEMORY;")
        conn.row_factory = aiosqlite.Row
        return conn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._connect()
        try:
            yield conn
        finally:
            await conn.close()

    async def _execute_schema(self, schema_sql: str) -> None:
        async with self._connection() as conn:
            await conn.executescript(schema_sql)
            await conn.commit()

    @staticmethod
    def _build_fact_filter_clauses(
        *,
        channel_id: str | None = None,
        tier: str | None = None,
        filters: "MemoryFilters | None" = None,
        include_superseded: bool = True,
        require_vector: bool = False,
        cluster_id: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(tier)
        if cluster_id is not None:
            clauses.append("cluster_id = ?")
            params.append(cluster_id)
        if not include_superseded:
            clauses.append("invalid_at IS NULL")
        if require_vector:
            clauses.append("text_vector IS NOT NULL")
        if filters is not None:
            if getattr(filters, "topic", None):
                clauses.append(
                    "EXISTS (SELECT 1 FROM json_each(facts.topic_tags) WHERE json_each.value = ?)"
                )
                params.append(filters.topic)
            if getattr(filters, "entity", None):
                clauses.append(
                    "EXISTS (SELECT 1 FROM json_each(facts.entity_tags) WHERE json_each.value = ?)"
                )
                params.append(filters.entity)
            if getattr(filters, "importance", None):
                clauses.append("importance = ?")
                params.append(filters.importance)
            if getattr(filters, "since", None):
                clauses.append("datetime(valid_at) >= datetime(?)")
                params.append(filters.since)
            if getattr(filters, "until", None):
                clauses.append("datetime(valid_at) <= datetime(?)")
                params.append(filters.until)
        return clauses, params


class SQLiteVectorStore(_SQLiteStoreMixin):
    """SQLite replacement for ``WeaviateStore`` using a single shared facts table."""

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path=db_path)

    async def startup(self) -> None:
        await self._ensure_schema()

    async def shutdown(self) -> None:
        return None

    async def ensure_schema(self) -> None:
        await self._ensure_schema()

    async def _ensure_schema(self) -> None:
        await self._execute_schema(_FACTS_SCHEMA)

    @staticmethod
    def _vec_to_blob(vec: list[float] | None) -> bytes | None:
        if not vec:
            return None
        arr = np.asarray(vec, dtype=np.float32)
        if arr.size == 0:
            return None
        norm = float(np.linalg.norm(arr))
        if norm > 0.0:
            arr = arr / norm
        return arr.astype(np.float32).tobytes()

    @staticmethod
    def _normalize_cluster_id(value: Any) -> str:
        if value in (None, *tuple(_UNCLUSTERED_CLUSTER_IDS)):
            return ""
        return str(value)

    @staticmethod
    def _decode_cluster_id(value: Any) -> str | None:
        if value in (None, *tuple(_UNCLUSTERED_CLUSTER_IDS)):
            return None
        return str(value)

    @staticmethod
    def _blob_to_vec(blob: bytes | None) -> list[float] | None:
        if not blob:
            return None
        arr = np.frombuffer(blob, dtype=np.float32)
        if arr.size == 0:
            return None
        return arr.astype(np.float32).tolist()

    @staticmethod
    def _cosine_sim(a: bytes, b: bytes) -> float:
        try:
            left = np.frombuffer(a, dtype=np.float32)
            right = np.frombuffer(b, dtype=np.float32)
        except ValueError:
            return 0.0
        if left.size == 0 or right.size == 0 or left.size != right.size:
            return 0.0
        score = float(np.dot(left, right))
        return max(-1.0, min(1.0, score))

    @staticmethod
    def _fact_to_params(fact: "AtomicFact") -> dict[str, Any]:
        raw = fact.model_dump(mode="python")
        extra_props = {k: v for k, v in raw.items() if k not in _ATOMIC_FACT_FIELDS}
        now = datetime.now(tz=UTC).isoformat()
        return {
            "id": fact.id,
            "tier": fact.tier,
            "channel_id": fact.channel_id,
            "memory_text": fact.memory_text,
            "quality_score": float(fact.quality_score),
            "cluster_id": SQLiteVectorStore._normalize_cluster_id(fact.cluster_id),
            "platform": fact.platform,
            "author_id": fact.author_id,
            "author_name": fact.author_name,
            "message_ts": fact.message_ts,
            "thread_ts": fact.thread_ts or "",
            "source_message_id": fact.source_message_id,
            "importance": fact.importance,
            "fact_type": fact.fact_type,
            "status": str(extra_props.get("status") or "active"),
            "topic_tags": _json_dumps(fact.topic_tags),
            "entity_tags": _json_dumps(fact.entity_tags),
            "action_tags": _json_dumps(fact.action_tags),
            "graph_entity_ids": _json_dumps(fact.graph_entity_ids),
            "source_media_urls": _json_dumps(fact.source_media_urls),
            "source_media_names": _json_dumps(fact.source_media_names),
            "source_link_urls": _json_dumps(fact.source_link_urls),
            "source_link_titles": _json_dumps(fact.source_link_titles),
            "source_link_descriptions": _json_dumps(fact.source_link_descriptions),
            "source_media_url": fact.source_media_url,
            "source_media_type": fact.source_media_type,
            "valid_at": _serialize_datetime(fact.valid_at),
            "invalid_at": _serialize_datetime(fact.invalid_at),
            "superseded_by": fact.superseded_by,
            "supersedes": fact.supersedes,
            "potential_contradiction": 1 if fact.potential_contradiction else 0,
            "thread_context_summary": fact.thread_context_summary,
            "text_vector": SQLiteVectorStore._vec_to_blob(fact.text_vector),
            "extra_props": _json_dumps(extra_props),
            "created_at": _serialize_datetime(extra_props.get("created_at")) or now,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_fact(row: aiosqlite.Row) -> "AtomicFact":
        AtomicFact = _atomic_fact_cls()
        extra_props = _json_loads(row["extra_props"], {})
        payload = {
            "id": row["id"],
            "memory_text": row["memory_text"],
            "quality_score": float(row["quality_score"] or 0.0),
            "tier": row["tier"] or "atomic",
            "cluster_id": SQLiteVectorStore._decode_cluster_id(row["cluster_id"]),
            "channel_id": row["channel_id"] or "",
            "platform": row["platform"] or "slack",
            "author_id": row["author_id"] or "",
            "author_name": row["author_name"] or "",
            "message_ts": row["message_ts"] or "",
            "thread_ts": row["thread_ts"] or None,
            "source_message_id": row["source_message_id"] or "",
            "topic_tags": _json_loads(row["topic_tags"], []),
            "entity_tags": _json_loads(row["entity_tags"], []),
            "action_tags": _json_loads(row["action_tags"], []),
            "importance": row["importance"] or "medium",
            "graph_entity_ids": _json_loads(row["graph_entity_ids"], []),
            "source_media_url": row["source_media_url"] or "",
            "source_media_type": row["source_media_type"] or "",
            "source_media_urls": _json_loads(row["source_media_urls"], []),
            "source_media_names": _json_loads(row["source_media_names"], []),
            "source_link_urls": _json_loads(row["source_link_urls"], []),
            "source_link_titles": _json_loads(row["source_link_titles"], []),
            "source_link_descriptions": _json_loads(row["source_link_descriptions"], []),
            "valid_at": _parse_datetime(row["valid_at"]),
            "invalid_at": _parse_datetime(row["invalid_at"]),
            "superseded_by": row["superseded_by"] or None,
            "supersedes": row["supersedes"] or None,
            "potential_contradiction": bool(row["potential_contradiction"]),
            "text_vector": SQLiteVectorStore._blob_to_vec(row["text_vector"]),
            "fact_type": row["fact_type"] or "",
            "thread_context_summary": row["thread_context_summary"] or "",
        }
        payload.update(extra_props)
        return AtomicFact(**payload)

    @staticmethod
    def _cluster_to_params(cluster: "TopicCluster") -> dict[str, Any]:
        raw = cluster.model_dump(mode="python")
        extra_props = {field: raw.get(field) for field in _TOPIC_CLUSTER_EXTRA_FIELDS}
        extra_props["created_at"] = _serialize_datetime(raw.get("created_at"))
        extra_props["updated_at"] = _serialize_datetime(raw.get("updated_at"))
        now = datetime.now(tz=UTC).isoformat()
        return {
            "id": cluster.id,
            "tier": "topic",
            "channel_id": cluster.channel_id,
            "memory_text": cluster.summary,
            "quality_score": 0.0,
            "cluster_id": "",
            "platform": "",
            "author_id": "",
            "author_name": "",
            "message_ts": "",
            "thread_ts": "",
            "source_message_id": "",
            "importance": "",
            "fact_type": "",
            "status": cluster.status,
            "topic_tags": _json_dumps(cluster.topic_tags),
            "entity_tags": _json_dumps([]),
            "action_tags": _json_dumps([]),
            "graph_entity_ids": _json_dumps([]),
            "source_media_urls": _json_dumps([]),
            "source_media_names": _json_dumps([]),
            "source_link_urls": _json_dumps([]),
            "source_link_titles": _json_dumps([]),
            "source_link_descriptions": _json_dumps([]),
            "source_media_url": "",
            "source_media_type": "",
            "valid_at": None,
            "invalid_at": None,
            "superseded_by": None,
            "supersedes": None,
            "potential_contradiction": 0,
            "thread_context_summary": "",
            "text_vector": SQLiteVectorStore._vec_to_blob(cluster.centroid_vector),
            "extra_props": _json_dumps(extra_props),
            "created_at": extra_props.get("created_at") or now,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_cluster(row: aiosqlite.Row) -> "TopicCluster":
        TopicCluster = _topic_cluster_cls()
        extra_props = _json_loads(row["extra_props"], {})
        return TopicCluster(
            id=row["id"],
            tier=row["tier"] or "topic",
            channel_id=row["channel_id"] or "",
            title=extra_props.get("title", ""),
            summary=row["memory_text"] or "",
            current_state=extra_props.get("current_state", ""),
            open_questions=extra_props.get("open_questions", ""),
            impact_note=extra_props.get("impact_note", ""),
            topic_tags=_json_loads(row["topic_tags"], []),
            member_ids=extra_props.get("member_ids", []) or [],
            member_count=int(extra_props.get("member_count", 0) or 0),
            centroid_vector=SQLiteVectorStore._blob_to_vec(row["text_vector"]),
            created_at=_parse_datetime(extra_props.get("created_at")) or datetime.now(tz=UTC),
            updated_at=_parse_datetime(extra_props.get("updated_at")) or datetime.now(tz=UTC),
            key_entities=extra_props.get("key_entities", []) or [],
            key_relationships=extra_props.get("key_relationships", []) or [],
            date_range_start=extra_props.get("date_range_start", ""),
            date_range_end=extra_props.get("date_range_end", ""),
            authors=extra_props.get("authors", []) or [],
            media_refs=extra_props.get("media_refs", []) or [],
            media_names=extra_props.get("media_names", []) or [],
            link_refs=extra_props.get("link_refs", []) or [],
            high_importance_count=int(extra_props.get("high_importance_count", 0) or 0),
            related_cluster_ids=extra_props.get("related_cluster_ids", []) or [],
            staleness_score=float(extra_props.get("staleness_score", 0.0) or 0.0),
            status=extra_props.get("status", row["status"] or "active"),
            fact_type_counts=extra_props.get("fact_type_counts", {}) or {},
            key_facts=extra_props.get("key_facts", []) or [],
            decisions=extra_props.get("decisions", []) or [],
            people=extra_props.get("people", []) or [],
            technologies=extra_props.get("technologies", []) or [],
            projects=extra_props.get("projects", []) or [],
            faq_candidates=extra_props.get("faq_candidates", []) or [],
        )

    @staticmethod
    def _summary_to_params(summary: "ChannelSummary") -> dict[str, Any]:
        raw = summary.model_dump(mode="python")
        extra_props = {field: raw.get(field) for field in _CHANNEL_SUMMARY_EXTRA_FIELDS}
        now = datetime.now(tz=UTC).isoformat()
        summary_id = summary.id or _deterministic_id("summary", summary.channel_id)
        return {
            "id": summary_id,
            "tier": "channel_summary",
            "channel_id": summary.channel_id,
            "memory_text": summary.text,
            "quality_score": 0.0,
            "cluster_id": "",
            "platform": "",
            "author_id": "",
            "author_name": "",
            "message_ts": "",
            "thread_ts": "",
            "source_message_id": "",
            "importance": "",
            "fact_type": "",
            "status": "active",
            "topic_tags": _json_dumps([]),
            "entity_tags": _json_dumps([]),
            "action_tags": _json_dumps([]),
            "graph_entity_ids": _json_dumps([]),
            "source_media_urls": _json_dumps([]),
            "source_media_names": _json_dumps([]),
            "source_link_urls": _json_dumps([]),
            "source_link_titles": _json_dumps([]),
            "source_link_descriptions": _json_dumps([]),
            "source_media_url": "",
            "source_media_type": "",
            "valid_at": None,
            "invalid_at": None,
            "superseded_by": None,
            "supersedes": None,
            "potential_contradiction": 0,
            "thread_context_summary": "",
            "text_vector": None,
            "extra_props": _json_dumps(extra_props),
            "created_at": _serialize_datetime(raw.get("updated_at")) or now,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_summary(row: aiosqlite.Row) -> "ChannelSummary":
        ChannelSummary = _channel_summary_cls()
        extra_props = _json_loads(row["extra_props"], {})
        return ChannelSummary(
            id=row["id"],
            tier=row["tier"] or "channel_summary",
            channel_id=row["channel_id"] or "",
            channel_name=extra_props.get("channel_name", ""),
            text=row["memory_text"] or "",
            description=extra_props.get("description", ""),
            themes=extra_props.get("themes", ""),
            momentum=extra_props.get("momentum", ""),
            team_dynamics=extra_props.get("team_dynamics", ""),
            cluster_count=int(extra_props.get("cluster_count", 0) or 0),
            fact_count=int(extra_props.get("fact_count", 0) or 0),
            updated_at=_parse_datetime(extra_props.get("updated_at")) or datetime.now(tz=UTC),
            key_decisions=extra_props.get("key_decisions", []) or [],
            key_entities=extra_props.get("key_entities", []) or [],
            key_topics=extra_props.get("key_topics", []) or [],
            date_range_start=extra_props.get("date_range_start", ""),
            date_range_end=extra_props.get("date_range_end", ""),
            media_count=int(extra_props.get("media_count", 0) or 0),
            author_count=int(extra_props.get("author_count", 0) or 0),
            worst_staleness=float(extra_props.get("worst_staleness", 0.0) or 0.0),
            top_decisions=extra_props.get("top_decisions", []) or [],
            top_people=extra_props.get("top_people", []) or [],
            tech_stack=extra_props.get("tech_stack", []) or [],
            active_projects=extra_props.get("active_projects", []) or [],
            glossary_terms=extra_props.get("glossary_terms", []) or [],
            recent_activity_summary=extra_props.get("recent_activity_summary", {}) or {},
            topic_graph_edges=extra_props.get("topic_graph_edges", []) or [],
        )

    @staticmethod
    def _entity_card_to_params(card: "EntityKnowledgeCard") -> dict[str, Any]:
        raw = card.model_dump(mode="python")
        extra_props = {field: raw.get(field) for field in _ENTITY_CARD_EXTRA_FIELDS}
        now = datetime.now(tz=UTC).isoformat()
        entity_key = card.entity_id or card.entity_name or card.id
        card_id = card.id or _deterministic_id("entity_card", entity_key)
        return {
            "id": card_id,
            "tier": "entity_card",
            "channel_id": "",
            "memory_text": card.summary,
            "quality_score": 0.0,
            "cluster_id": "",
            "platform": "",
            "author_id": "",
            "author_name": "",
            "message_ts": "",
            "thread_ts": "",
            "source_message_id": "",
            "importance": "",
            "fact_type": "",
            "status": "active",
            "topic_tags": _json_dumps([]),
            "entity_tags": _json_dumps([]),
            "action_tags": _json_dumps([]),
            "graph_entity_ids": _json_dumps([]),
            "source_media_urls": _json_dumps([]),
            "source_media_names": _json_dumps([]),
            "source_link_urls": _json_dumps([]),
            "source_link_titles": _json_dumps([]),
            "source_link_descriptions": _json_dumps([]),
            "source_media_url": "",
            "source_media_type": "",
            "valid_at": None,
            "invalid_at": None,
            "superseded_by": None,
            "supersedes": None,
            "potential_contradiction": 0,
            "thread_context_summary": "",
            "text_vector": None,
            "extra_props": _json_dumps(extra_props),
            "created_at": _serialize_datetime(raw.get("updated_at")) or now,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_entity_card(row: aiosqlite.Row) -> "EntityKnowledgeCard":
        EntityKnowledgeCard = _entity_card_cls()
        extra_props = _json_loads(row["extra_props"], {})
        return EntityKnowledgeCard(
            id=row["id"],
            tier=row["tier"] or "entity_card",
            entity_id=extra_props.get("entity_id", ""),
            entity_name=extra_props.get("entity_name", ""),
            entity_type=extra_props.get("entity_type", ""),
            channel_ids=extra_props.get("channel_ids", []) or [],
            cluster_ids=extra_props.get("cluster_ids", []) or [],
            fact_count=int(extra_props.get("fact_count", 0) or 0),
            fact_type_breakdown=extra_props.get("fact_type_breakdown", {}) or {},
            key_facts=extra_props.get("key_facts", []) or [],
            related_entities=extra_props.get("related_entities", []) or [],
            last_mentioned_at=extra_props.get("last_mentioned_at", ""),
            staleness_score=float(extra_props.get("staleness_score", 0.0) or 0.0),
            summary=row["memory_text"] or "",
            updated_at=_parse_datetime(extra_props.get("updated_at")) or datetime.now(tz=UTC),
        )

    async def _upsert_fact_row(self, params: dict[str, Any]) -> str:
        columns = list(params.keys())
        placeholders = ", ".join(f":{column}" for column in columns)
        update_columns = [column for column in columns if column not in {"id", "created_at"}]
        updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        sql = (
            f"INSERT INTO facts ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        async with self._connection() as conn:
            await conn.execute(sql, params)
            await conn.commit()
        return str(params["id"])

    async def upsert_fact(self, fact: "AtomicFact") -> str:
        return await self._upsert_fact_row(self._fact_to_params(fact))

    async def batch_upsert_facts(self, facts: list["AtomicFact"]) -> list[str]:
        if not facts:
            return []
        params_list = [self._fact_to_params(fact) for fact in facts]
        columns = list(params_list[0].keys())
        placeholders = ", ".join(f":{column}" for column in columns)
        update_columns = [column for column in columns if column not in {"id", "created_at"}]
        updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        sql = (
            f"INSERT INTO facts ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        async with self._connection() as conn:
            await conn.executemany(sql, params_list)
            await conn.commit()
        return [fact.id for fact in facts]

    async def update_fact_cluster(self, fact_id: str, cluster_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE facts SET cluster_id = ?, updated_at = datetime('now') WHERE id = ?",
                (self._normalize_cluster_id(cluster_id), fact_id),
            )
            await conn.commit()

    async def get_fact(self, fact_id: str) -> "AtomicFact | None":
        async with self._connection() as conn:
            cursor = await conn.execute("SELECT * FROM facts WHERE id = ? LIMIT 1", (fact_id,))
            row = await cursor.fetchone()
        return self._row_to_fact(row) if row is not None else None

    async def list_facts(
        self,
        channel_id: str,
        filters: "MemoryFilters",
        page: int = 1,
        limit: int = 20,
    ) -> "PaginatedFacts":
        PaginatedFacts = _paginated_facts_cls()
        page = max(1, page)
        limit = max(1, limit)
        clauses, params = self._build_fact_filter_clauses(
            channel_id=channel_id,
            tier="atomic",
            filters=filters,
            include_superseded=True,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * limit
        async with self._connection() as conn:
            count_cursor = await conn.execute(f"SELECT COUNT(*) FROM facts {where_sql}", params)
            total = int((await count_cursor.fetchone())[0])
            data_cursor = await conn.execute(
                f"SELECT * FROM facts {where_sql} ORDER BY COALESCE(valid_at, created_at) DESC, id ASC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            rows = await data_cursor.fetchall()
        pages = max(1, math.ceil(total / limit)) if total else 1
        return PaginatedFacts(
            memories=[self._row_to_fact(row) for row in rows],
            total=total,
            page=page,
            pages=pages,
        )

    async def count_facts(self, channel_id: str | None = None) -> int:
        clauses, params = self._build_fact_filter_clauses(channel_id=channel_id, tier="atomic")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._connection() as conn:
            cursor = await conn.execute(f"SELECT COUNT(*) FROM facts {where_sql}", params)
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def delete_by_channel(self, channel_id: str) -> int:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM facts WHERE channel_id = ?", (channel_id,))
            cursor = await conn.execute("SELECT changes()")
            row = await cursor.fetchone()
            await conn.commit()
        return int(row[0]) if row else 0

    async def delete_all(self) -> int:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM facts")
            cursor = await conn.execute("SELECT changes()")
            row = await cursor.fetchone()
            await conn.commit()
        return int(row[0]) if row else 0

    async def semantic_search(
        self,
        query_vector: list[float],
        channel_id: str | None = None,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        query_blob = self._vec_to_blob(query_vector)
        if query_blob is None:
            return []
        typed_filters = filters if filters is not None else None
        clauses, params = self._build_fact_filter_clauses(
            channel_id=channel_id,
            tier="atomic",
            filters=typed_filters,
            include_superseded=include_superseded,
            require_vector=True,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._connection() as conn:
            cursor = await conn.execute(f"SELECT * FROM facts {where_sql}", params)
            rows = await cursor.fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            blob = row["text_vector"]
            if blob is None:
                continue
            score = self._cosine_sim(query_blob, blob)
            if score < threshold:
                continue
            results.append(
                {
                    "fact": self._row_to_fact(row),
                    "similarity_score": round(score, 4),
                }
            )
        results.sort(key=lambda item: item["similarity_score"], reverse=True)
        return results[:limit]

    async def _bm25_rows(
        self,
        query: str,
        channel_id: str,
        *,
        tier: str = "atomic",
        limit: int = 10,
        include_superseded: bool = True,
    ) -> list[tuple[aiosqlite.Row, float]]:
        clauses = ["f.channel_id = ?", "f.tier = ?"]
        params: list[Any] = [query, channel_id, tier]
        if not include_superseded:
            clauses.append("f.invalid_at IS NULL")
        where_extra = f" AND {' AND '.join(clauses)}"
        sql = (
            "SELECT f.*, bm25(facts_fts) AS bm25_rank "
            "FROM facts f JOIN facts_fts ON f.rowid = facts_fts.rowid "
            f"WHERE facts_fts MATCH ?{where_extra} "
            "ORDER BY bm25_rank LIMIT ?"
        )
        params.append(limit)
        async with self._connection() as conn:
            try:
                cursor = await conn.execute(sql, params)
                rows = await cursor.fetchall()
            except aiosqlite.OperationalError:
                escaped = query.replace('"', '""')
                cursor = await conn.execute(sql, [f'"{escaped}"', *params[1:]])
                rows = await cursor.fetchall()
        return [(row, float(row["bm25_rank"] or 0.0)) for row in rows]

    async def bm25_search(
        self,
        query: str,
        channel_id: str,
        tier: str = "atomic",
        limit: int = 10,
    ) -> list["AtomicFact"]:
        rows = await self._bm25_rows(query, channel_id, tier=tier, limit=limit)
        return [self._row_to_fact(row) for row, _ in rows]

    @staticmethod
    def _normalize_bm25_scores(rows: list[tuple[aiosqlite.Row, float]]) -> dict[str, float]:
        if not rows:
            return {}
        raw_scores = [score for _, score in rows]
        best = min(raw_scores)
        worst = max(raw_scores)
        if math.isclose(best, worst):
            return {row["id"]: 1.0 for row, _ in rows}
        normalized: dict[str, float] = {}
        for row, raw in rows:
            normalized[row["id"]] = 1.0 - ((raw - best) / (worst - best))
        return normalized

    async def true_hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        channel_id: str,
        tier: str = "atomic",
        limit: int = 20,
        alpha: float | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        resolved_alpha = alpha if alpha is not None else 0.6
        try:
            from beever_atlas.infra.config import get_settings

            resolved_alpha = alpha if alpha is not None else get_settings().weaviate_hybrid_alpha
        except Exception:
            pass
        vector_results, bm25_rows = await asyncio.gather(
            self.semantic_search(
                query_vector=query_vector,
                channel_id=channel_id,
                filters=None,
                limit=limit * 3,
                threshold=0.0,
                include_superseded=include_superseded,
            ),
            self._bm25_rows(
                query_text,
                channel_id,
                tier=tier,
                limit=limit * 3,
                include_superseded=include_superseded,
            ),
        )
        bm25_scores = self._normalize_bm25_scores(bm25_rows)
        merged: dict[str, dict[str, Any]] = {}
        for item in vector_results:
            fact = item["fact"]
            merged[fact.id] = {
                "fact": fact,
                "vector_score": float(item["similarity_score"]),
                "bm25_score": 0.0,
            }
        for row, _ in bm25_rows:
            fact = self._row_to_fact(row)
            entry = merged.setdefault(
                fact.id,
                {"fact": fact, "vector_score": 0.0, "bm25_score": 0.0},
            )
            entry["bm25_score"] = bm25_scores.get(fact.id, 0.0)
            entry["fact"] = fact
        results: list[dict[str, Any]] = []
        for entry in merged.values():
            score = (resolved_alpha * entry["vector_score"]) + (
                (1.0 - resolved_alpha) * entry["bm25_score"]
            )
            results.append(
                {
                    "fact": entry["fact"],
                    "similarity_score": round(score, 4),
                    "vector_score": round(entry["vector_score"], 4),
                    "bm25_score": round(entry["bm25_score"], 4),
                }
            )
        results.sort(key=lambda item: item["similarity_score"], reverse=True)
        return results[:limit]

    async def pseudo_hybrid_search(
        self,
        query_vector: list[float],
        channel_id: str,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        MemoryFilters = _memory_filters_cls()
        vector_results, field_results = await asyncio.gather(
            self.semantic_search(
                query_vector=query_vector,
                channel_id=channel_id,
                filters=filters,
                limit=limit,
                threshold=threshold,
                include_superseded=include_superseded,
            ),
            self.list_facts(
                channel_id=channel_id,
                filters=filters or MemoryFilters(),
                page=1,
                limit=limit,
            ),
        )
        seen_ids: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in vector_results:
            fact = item["fact"]
            seen_ids.add(fact.id)
            merged.append(item)
        for fact in field_results.memories:
            if not include_superseded and fact.invalid_at is not None:
                continue
            if fact.id in seen_ids:
                for item in merged:
                    if item["fact"].id == fact.id:
                        item["similarity_score"] = min(1.0, item["similarity_score"] + 0.1)
                        break
                continue
            seen_ids.add(fact.id)
            merged.append({"fact": fact, "similarity_score": 0.5})
        merged.sort(key=lambda item: item["similarity_score"], reverse=True)
        return merged[:limit]

    async def supersede_fact(self, old_fact_id: str, new_fact_id: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE facts SET invalid_at = ?, superseded_by = ?, updated_at = datetime('now') WHERE id = ?",
                (now, new_fact_id, old_fact_id),
            )
            await conn.execute(
                "UPDATE facts SET supersedes = ?, updated_at = datetime('now') WHERE id = ?",
                (old_fact_id, new_fact_id),
            )
            await conn.commit()

    async def flag_potential_contradiction(self, fact_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE facts SET potential_contradiction = 1, updated_at = datetime('now') WHERE id = ?",
                (fact_id,),
            )
            await conn.commit()

    async def fetch_by_ids(self, fact_ids: list[str]) -> list["AtomicFact"]:
        if not fact_ids:
            return []
        placeholders = ", ".join("?" for _ in fact_ids)
        async with self._connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders})",
                fact_ids,
            )
            rows = await cursor.fetchall()
        by_id = {row["id"]: self._row_to_fact(row) for row in rows}
        return [by_id[fact_id] for fact_id in fact_ids if fact_id in by_id]

    async def get_unclustered_facts(
        self,
        channel_id: str,
        limit: int | None = None,
    ) -> list["AtomicFact"]:
        rows: list[Any] = []
        async for fact in self.iter_unclustered_facts(channel_id, page_size=limit or 200):
            rows.append(fact)
            if limit is not None and len(rows) >= limit:
                break
        return rows

    async def iter_unclustered_facts(
        self,
        channel_id: str,
        page_size: int = 200,
    ) -> AsyncIterator["AtomicFact"]:
        offset = 0
        while True:
            async with self._connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM facts WHERE channel_id = ? AND tier = 'atomic' AND "
                    "(cluster_id = '' OR cluster_id = '__none__' OR cluster_id IS NULL) "
                    "ORDER BY id LIMIT ? OFFSET ?",
                    (channel_id, page_size, offset),
                )
                rows = await cursor.fetchall()
            if not rows:
                return
            for row in rows:
                yield self._row_to_fact(row)
            if len(rows) < page_size:
                return
            offset += page_size

    async def iter_all_fact_ids(
        self,
        channel_id: str,
        page_size: int = 500,
    ) -> AsyncIterator[tuple[str, str]]:
        offset = 0
        while True:
            async with self._connection() as conn:
                cursor = await conn.execute(
                    "SELECT id, cluster_id FROM facts WHERE channel_id = ? AND tier = 'atomic' "
                    "ORDER BY id LIMIT ? OFFSET ?",
                    (channel_id, page_size, offset),
                )
                rows = await cursor.fetchall()
            if not rows:
                return
            for row in rows:
                cluster_id = self._decode_cluster_id(row["cluster_id"])
                yield str(row["id"]), cluster_id or "__none__"
            if len(rows) < page_size:
                return
            offset += page_size

    async def upsert_cluster(self, cluster: "TopicCluster") -> str:
        return await self._upsert_fact_row(self._cluster_to_params(cluster))

    async def list_clusters(self, channel_id: str) -> list["TopicCluster"]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND tier = 'topic' ORDER BY updated_at DESC, id ASC",
                (channel_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_cluster(row) for row in rows]

    async def get_cluster(self, cluster_id: str) -> "TopicCluster | None":
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE id = ? AND tier = 'topic' LIMIT 1",
                (cluster_id,),
            )
            row = await cursor.fetchone()
        return self._row_to_cluster(row) if row is not None else None

    async def get_cluster_members(self, cluster_id: str, limit: int = 100) -> list["AtomicFact"]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE cluster_id = ? AND tier = 'atomic' ORDER BY id LIMIT ?",
                (cluster_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    async def fetch_all_cluster_members(
        self,
        channel_id: str,
        cluster_id: str,
        limit: int = 500,
    ) -> list["AtomicFact"]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND cluster_id = ? AND tier = 'atomic' "
                "ORDER BY id LIMIT ?",
                (channel_id, cluster_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    async def delete_cluster(self, cluster_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM facts WHERE id = ? AND tier = 'topic'", (cluster_id,))
            await conn.commit()

    async def reset_cluster_assignments(self, channel_id: str) -> int:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE facts SET cluster_id = '__none__', updated_at = datetime('now') "
                "WHERE channel_id = ? AND tier = 'atomic'",
                (channel_id,),
            )
            cursor = await conn.execute("SELECT changes()")
            row = await cursor.fetchone()
            await conn.commit()
        return int(row[0]) if row else 0

    async def get_channel_summary(self, channel_id: str) -> "ChannelSummary | None":
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND tier = 'channel_summary' LIMIT 1",
                (channel_id,),
            )
            row = await cursor.fetchone()
        return self._row_to_summary(row) if row is not None else None

    async def upsert_channel_summary(self, summary: "ChannelSummary") -> str:
        return await self._upsert_fact_row(self._summary_to_params(summary))

    async def get_entity_card(self, entity_id: str) -> "EntityKnowledgeCard | None":
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE tier = 'entity_card' AND (id = ? OR "
                "json_extract(extra_props, '$.entity_id') = ? OR json_extract(extra_props, '$.entity_name') = ?) "
                "LIMIT 1",
                (entity_id, entity_id, entity_id),
            )
            row = await cursor.fetchone()
        return self._row_to_entity_card(row) if row is not None else None

    async def upsert_entity_card(self, card: "EntityKnowledgeCard") -> str:
        return await self._upsert_fact_row(self._entity_card_to_params(card))

    async def delete_entity_card(self, entity_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "DELETE FROM facts WHERE tier = 'entity_card' AND (id = ? OR "
                "json_extract(extra_props, '$.entity_id') = ? OR json_extract(extra_props, '$.entity_name') = ?)",
                (entity_id, entity_id, entity_id),
            )
            await conn.commit()

    async def list_entity_cards(
        self,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list["EntityKnowledgeCard"]:
        params: list[Any] = []
        sql = "SELECT * FROM facts WHERE tier = 'entity_card'"
        if channel_id:
            sql += (
                " AND EXISTS (SELECT 1 FROM json_each(facts.extra_props, '$.channel_ids') "
                "WHERE json_each.value = ?)"
            )
            params.append(channel_id)
        sql += " ORDER BY updated_at DESC, id ASC LIMIT ?"
        params.append(limit)
        async with self._connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_entity_card(row) for row in rows]

    async def batch_upsert_entity_cards(self, cards: list["EntityKnowledgeCard"]) -> None:
        if not cards:
            return
        params_list = [self._entity_card_to_params(card) for card in cards]
        columns = list(params_list[0].keys())
        placeholders = ", ".join(f":{column}" for column in columns)
        update_columns = [column for column in columns if column not in {"id", "created_at"}]
        updates = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
        sql = (
            f"INSERT INTO facts ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        async with self._connection() as conn:
            await conn.executemany(sql, params_list)
            await conn.commit()

    async def count_unclustered_facts(self, channel_id: str) -> int:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM facts WHERE channel_id = ? AND tier = 'atomic' AND "
                "(cluster_id = '' OR cluster_id = '__none__' OR cluster_id IS NULL)",
                (channel_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_clusters(self, channel_id: str) -> int:
        return await self.count_by_tier(channel_id, "topic")

    async def count_by_tier(self, channel_id: str, tier: str) -> int:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM facts WHERE channel_id = ? AND tier = ?",
                (channel_id, tier),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_facts_for_cluster_reset(
        self,
        channel_id: str,
        tier: str = "atomic",
    ) -> list["AtomicFact"]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND tier = ? AND text_vector IS NOT NULL ORDER BY id",
                (channel_id, tier),
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    async def batch_update_cluster_ids(self, updates: list[tuple[str, str]]) -> None:
        if not updates:
            return
        async with self._connection() as conn:
            await conn.executemany(
                "UPDATE facts SET cluster_id = ?, updated_at = datetime('now') WHERE id = ?",
                [
                    (self._normalize_cluster_id(cluster_id), fact_id)
                    for fact_id, cluster_id in updates
                ],
            )
            await conn.commit()

    async def batch_update_fact_clusters(self, updates: list[tuple[str, str]]) -> None:
        await self.batch_update_cluster_ids(updates)

    async def fetch_media_facts(self, channel_id: str, limit: int = 500) -> list["AtomicFact"]:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND tier = 'atomic' AND ("
                "json_array_length(source_media_urls) > 0 OR json_array_length(source_link_urls) > 0) "
                "ORDER BY id LIMIT ?",
                (channel_id, limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    async def fetch_recent_facts(
        self,
        channel_id: str,
        days: int = 7,
        limit: int = 500,
    ) -> list["AtomicFact"]:
        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM facts WHERE channel_id = ? AND tier = 'atomic' AND datetime(valid_at) >= datetime(?) "
                "ORDER BY datetime(valid_at) DESC LIMIT ?",
                (channel_id, cutoff.isoformat(), limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]


class SQLiteQAHistoryStore(_SQLiteStoreMixin):
    """SQLite replacement for ``QAHistoryStore``."""

    FIND_QA_SCAN_CAP: int = 1000

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path=db_path)

    async def startup(self) -> None:
        await self.ensure_schema()

    async def shutdown(self) -> None:
        return None

    async def ensure_schema(self) -> None:
        await self._execute_schema(_QA_HISTORY_SCHEMA)

    async def write_qa_entry(
        self,
        question: str,
        answer: str,
        citations: list[dict] | dict,
        channel_id: str,
        user_id: str,
        session_id: str,
    ) -> str:
        from beever_atlas.agents.citations.persistence import upgrade_envelope

        entry_id = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        envelope = upgrade_envelope(citations)
        async with self._connection() as conn:
            await conn.execute(
                "INSERT INTO qa_history (id, question, answer, citations_json, channel_id, user_id, "
                "session_id, timestamp, is_deleted, answer_kind, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    entry_id,
                    question,
                    answer,
                    _json_dumps(envelope),
                    channel_id,
                    user_id,
                    session_id,
                    now,
                    _classify_answer(answer),
                    now,
                    now,
                ),
            )
            await conn.commit()
        return entry_id

    async def _fts_search(self, channel_id: str, query: str, limit: int) -> list[aiosqlite.Row]:
        sql = (
            "SELECT q.*, bm25(qa_history_fts) AS bm25_rank "
            "FROM qa_history q JOIN qa_history_fts ON q.rowid = qa_history_fts.rowid "
            "WHERE qa_history_fts MATCH ? AND q.channel_id = ? AND q.is_deleted = 0 "
            "ORDER BY bm25_rank LIMIT ?"
        )
        async with self._connection() as conn:
            try:
                cursor = await conn.execute(sql, (query, channel_id, limit))
                rows = await cursor.fetchall()
            except aiosqlite.OperationalError:
                escaped = query.replace('"', '""')
                cursor = await conn.execute(sql, (f'"{escaped}"', channel_id, limit))
                rows = await cursor.fetchall()
        return rows

    @staticmethod
    def _row_to_qa_entry(row: aiosqlite.Row) -> dict[str, Any]:
        from beever_atlas.agents.citations.persistence import as_legacy_items

        raw = _json_loads(row["citations_json"], [])
        return {
            "question": row["question"] or "",
            "answer": row["answer"] or "",
            "citations": as_legacy_items(raw),
            "timestamp": row["timestamp"] or "",
            "session_id": row["session_id"] or "",
            "id": row["id"],
            "answer_kind": row["answer_kind"] or "answered",
        }

    async def true_hybrid_search(
        self,
        channel_id: str,
        query: str,
        query_vector: list[float],
        limit: int = 5,
        alpha: float | None = None,
    ) -> list[dict]:
        rows = await self._fts_search(channel_id, query, limit)
        return [self._row_to_qa_entry(row) for row in rows]

    async def search_qa_history(
        self,
        channel_id: str,
        query: str,
        limit: int = 5,
        query_vector: list[float] | None = None,
    ) -> list[dict]:
        rows = await self._fts_search(channel_id, query, limit)
        return [self._row_to_qa_entry(row) for row in rows]

    async def soft_delete(self, entry_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE qa_history SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
                (entry_id,),
            )
            await conn.commit()

    async def find_qa_entries_citing_source(self, source_id: str, limit: int = 20) -> dict:
        from beever_atlas.agents.citations.persistence import upgrade_envelope

        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM qa_history WHERE is_deleted = 0 ORDER BY timestamp DESC LIMIT ?",
                (self.FIND_QA_SCAN_CAP,),
            )
            rows = await cursor.fetchall()
        entries: list[dict[str, Any]] = []
        scanned = 0
        for row in rows:
            scanned += 1
            if len(entries) >= limit:
                continue
            env = upgrade_envelope(_json_loads(row["citations_json"], []))
            if any(
                isinstance(source, dict) and source.get("id") == source_id
                for source in (env.get("sources") or [])
            ):
                entries.append(
                    {
                        "id": row["id"],
                        "question": row["question"] or "",
                        "answer": row["answer"] or "",
                        "timestamp": row["timestamp"] or "",
                        "session_id": row["session_id"] or "",
                        "channel_id": row["channel_id"] or "",
                    }
                )
        return {
            "entries": entries,
            "truncated": scanned >= self.FIND_QA_SCAN_CAP,
            "scanned": scanned,
        }


class _SQLiteGridOut:
    """Minimal GridOut-compatible object for SQLite-backed file reads."""

    def __init__(self, content: bytes, filename: str, metadata: dict[str, Any]) -> None:
        self._content = content
        self.filename = filename
        self.metadata = metadata

    async def read(self) -> bytes:
        return self._content


class SQLiteFileStore(_SQLiteStoreMixin):
    """SQLite BLOB-backed replacement for the Mongo GridFS file store."""

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path=db_path)

    async def startup(self) -> None:
        await self.ensure_schema()

    async def ensure_schema(self) -> None:
        await self._execute_schema(_FILES_SCHEMA)

    def close(self) -> None:
        return None

    async def save(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str,
        owner_user_id: str = "",
    ) -> str:
        file_id = uuid.uuid4().hex
        async with self._connection() as conn:
            await conn.execute(
                "INSERT INTO files (file_id, content, filename, mime_type, size_bytes, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_id, content, filename, mime_type, len(content), owner_user_id),
            )
            await conn.commit()
        return file_id

    async def open(self, file_id: str) -> _SQLiteGridOut | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT file_id, content, filename, mime_type, owner_user_id, size_bytes FROM files "
                "WHERE file_id = ? LIMIT 1",
                (file_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _SQLiteGridOut(
            content=bytes(row["content"]),
            filename=row["filename"] or "file",
            metadata={
                "file_id": row["file_id"],
                "owner_user_id": row["owner_user_id"] or "",
                "mime_type": row["mime_type"] or "application/octet-stream",
                "size_bytes": int(row["size_bytes"] or 0),
            },
        )

    async def delete(self, file_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
            await conn.commit()

