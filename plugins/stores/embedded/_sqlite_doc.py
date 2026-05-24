"""SQLite-backed persistence for MongoDB stores.

Strategy
--------
All MongoDB stores (MongoDBStore, ChatHistoryStore, ShareStore, WikiCache,
WikiVersionStore) create `AsyncIOMotorClient` instances that are already
patched to use AsyncMongoMockClient from mongomock_motor.

This module:
1. Ensures ALL stores share ONE mongomock database instance (singleton patch).
2. On `startup()`: loads a JSON snapshot from SQLite → restores mongomock.
3. On `shutdown()`: dumps all mongomock collections → saves to SQLite.

Serialization handles datetime, bytes, and ObjectId round-trips.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import aiosqlite

from ._sqlite_db import ensure_data_dir, get_db_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton mongomock client
# ---------------------------------------------------------------------------

_SHARED_CLIENT: Any = None
_SHARED_URI: str = ""


def get_shared_mock_client(uri: str = "") -> Any:
    """Return a shared AsyncMongoMockClient singleton (creates on first call)."""
    global _SHARED_CLIENT, _SHARED_URI
    if _SHARED_CLIENT is None:
        try:
            import mongomock_motor

            _SHARED_CLIENT = mongomock_motor.AsyncMongoMockClient()
            _SHARED_URI = uri
            logger.info("_sqlite_doc: created shared AsyncMongoMockClient singleton")
        except ImportError:
            logger.warning("_sqlite_doc: mongomock_motor not installed")
            _SHARED_CLIENT = None
    return _SHARED_CLIENT


def patch_motor_singleton() -> None:
    """Patch AsyncIOMotorClient to always return the shared mock singleton."""
    try:
        import mongomock_motor
        import motor.motor_asyncio as _mta

        _orig_cls = _mta.AsyncIOMotorClient

        class _SingletonClient:
            """Wrapper that always returns the shared mongomock singleton."""

            def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
                uri = args[0] if args else kwargs.get("host", "")
                return get_shared_mock_client(str(uri))

        _mta.AsyncIOMotorClient = _SingletonClient  # type: ignore[attr-defined]
        logger.info("_sqlite_doc: Motor → singleton AsyncMongoMockClient")
    except ImportError:
        logger.warning("_sqlite_doc: mongomock_motor not installed — Motor not patched")


# ---------------------------------------------------------------------------
# JSON serialization / deserialization with BSON type support
# ---------------------------------------------------------------------------

def _encode_value(v: Any) -> Any:
    """Recursively encode MongoDB document values to JSON-safe types."""
    from datetime import datetime

    try:
        from bson import ObjectId
    except ImportError:
        ObjectId = None  # type: ignore[assignment, misc]

    if ObjectId is not None and isinstance(v, ObjectId):
        return {"$oid": str(v)}
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, bytes):
        return {"$b64": base64.b64encode(v).decode()}
    if isinstance(v, dict):
        return {k: _encode_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_encode_value(item) for item in v]
    return v


def _decode_value(v: Any) -> Any:
    """Recursively decode JSON-safe types back to MongoDB document values."""
    from datetime import datetime

    try:
        from bson import ObjectId
    except ImportError:
        ObjectId = None  # type: ignore[assignment, misc]

    if isinstance(v, dict):
        if "$oid" in v and ObjectId is not None:
            return ObjectId(v["$oid"])
        if "$dt" in v:
            return datetime.fromisoformat(v["$dt"])
        if "$b64" in v:
            return base64.b64decode(v["$b64"])
        return {k: _decode_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_decode_value(item) for item in v]
    return v


def _dumps(doc: Any) -> str:
    return json.dumps(_encode_value(doc), ensure_ascii=False)


def _loads(s: str) -> Any:
    return _decode_value(json.loads(s))


# ---------------------------------------------------------------------------
# SQLite snapshot table
# ---------------------------------------------------------------------------

_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS mongo_snapshots (
    db_name TEXT NOT NULL,
    coll_name TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (db_name, coll_name, doc_id)
)
"""


async def _ensure_snapshot_table() -> None:
    async with aiosqlite.connect(get_db_path()) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(_SNAPSHOT_DDL)
        await conn.commit()


# ---------------------------------------------------------------------------
# Snapshot save / load
# ---------------------------------------------------------------------------

async def save_snapshot() -> None:
    """Dump all mongomock collections to SQLite."""
    client = get_shared_mock_client()
    if client is None:
        return

    await _ensure_snapshot_table()
    db_path = get_db_path()

    try:
        db_names: list[str] = await client.list_database_names()
    except Exception as exc:
        logger.warning("_sqlite_doc: cannot list DB names for snapshot: %s", exc)
        return

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        # Clear old snapshot
        await conn.execute("DELETE FROM mongo_snapshots")

        for db_name in db_names:
            if db_name in ("admin", "local", "config"):
                continue
            db = client[db_name]
            try:
                coll_names: list[str] = await db.list_collection_names()
            except Exception:
                continue
            for coll_name in coll_names:
                coll = db[coll_name]
                try:
                    docs = await coll.find({}).to_list(length=None)
                except Exception:
                    continue
                for doc in docs:
                    doc_id = str(doc.get("_id", ""))
                    try:
                        data_str = _dumps(doc)
                        await conn.execute(
                            "INSERT OR REPLACE INTO mongo_snapshots "
                            "(db_name, coll_name, doc_id, data) VALUES (?, ?, ?, ?)",
                            (db_name, coll_name, doc_id, data_str),
                        )
                    except Exception as exc:
                        logger.warning(
                            "_sqlite_doc: failed to serialize doc %s/%s/%s: %s",
                            db_name, coll_name, doc_id, exc,
                        )

        await conn.commit()
    logger.info("_sqlite_doc: snapshot saved")


async def flush_collection(db_name: str, coll_name: str) -> None:
    """Persist a single mongomock collection to SQLite immediately.

    Much cheaper than ``save_snapshot()`` (which rewrites every collection).
    Use this for write-through persistence after targeted inserts so that a
    subsequent crash does not lose the just-written data.
    """
    client = get_shared_mock_client()
    if client is None:
        return

    await _ensure_snapshot_table()
    db_path = get_db_path()

    coll = client[db_name][coll_name]
    try:
        docs = await coll.find({}).to_list(length=None)
    except Exception as exc:
        logger.warning(
            "_sqlite_doc: flush_collection(%s/%s) find failed: %s", db_name, coll_name, exc
        )
        return

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        # Replace only this collection's rows; leave other collections intact.
        await conn.execute(
            "DELETE FROM mongo_snapshots WHERE db_name=? AND coll_name=?",
            (db_name, coll_name),
        )
        for doc in docs:
            doc_id = str(doc.get("_id", ""))
            try:
                data_str = _dumps(doc)
                await conn.execute(
                    "INSERT OR REPLACE INTO mongo_snapshots "
                    "(db_name, coll_name, doc_id, data) VALUES (?, ?, ?, ?)",
                    (db_name, coll_name, doc_id, data_str),
                )
            except Exception as exc:
                logger.warning(
                    "_sqlite_doc: flush_collection: failed to serialize %s/%s/%s: %s",
                    db_name, coll_name, doc_id, exc,
                )
        await conn.commit()

    logger.info(
        "_sqlite_doc: flushed %d docs for %s/%s", len(docs), db_name, coll_name
    )


async def load_snapshot() -> None:
    """Restore mongomock collections from SQLite snapshot."""
    client = get_shared_mock_client()
    if client is None:
        return

    db_path = get_db_path()
    if not os.path.exists(db_path):
        logger.info("_sqlite_doc: no snapshot DB yet, starting empty")
        return

    await _ensure_snapshot_table()

    try:
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT db_name, coll_name, doc_id, data FROM mongo_snapshots"
            ) as cur:
                rows = await cur.fetchall()
    except Exception as exc:
        logger.warning("_sqlite_doc: failed to read snapshot: %s", exc)
        return

    if not rows:
        logger.info("_sqlite_doc: snapshot is empty, starting fresh")
        return

    count = 0
    for row in rows:
        try:
            doc = _loads(row["data"])
        except Exception as exc:
            logger.warning("_sqlite_doc: failed to deserialize doc: %s", exc)
            continue
        db = client[row["db_name"]]
        coll = db[row["coll_name"]]
        try:
            await coll.replace_one({"_id": doc.get("_id")}, doc, upsert=True)
            count += 1
        except Exception as exc:
            logger.warning("_sqlite_doc: failed to restore doc: %s", exc)

    logger.info("_sqlite_doc: loaded %d docs from snapshot", count)
