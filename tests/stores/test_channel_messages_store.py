"""Tests for the ``channel_messages`` Message Store accessors on MongoDBStore.

Covers the contract introduced by PR-A of the OSS pipeline + wiki redesign:

  * idempotent upsert by ``(source_id, channel_id, message_id)`` with
    ``$setOnInsert`` for extraction state (so a re-sync does not reset
    rows the worker has already moved past ``pending``)
  * read accessor for the dual-read fallback in ``get_channel_messages``
  * status-aggregation accessor for the future extraction-status endpoint
  * ``find_channel_message_by_message_id`` for the phantom raw_messages fix
  * state-machine validation in ``update_channel_message_status``

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/``.

No live Mongo — uses lightweight fakes that exercise the call shape +
in-memory ``$setOnInsert``/``$set`` semantics so the test stays fast.

Convention: no `@pytest.mark.asyncio` decorators; pyproject sets
`asyncio_mode = "auto"`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beever_atlas.models.persistence import ChannelMessage
from beever_atlas.stores.mongodb_store import MongoDBStore


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight fake collection — mimics motor's surface for the methods we use
# ─────────────────────────────────────────────────────────────────────────────


class _FakeBulkResult:
    def __init__(self, inserted: int, modified: int, matched: int, upserted: int) -> None:
        self.inserted_count = inserted
        self.modified_count = modified
        self.matched_count = matched
        self.upserted_ids = {i: object() for i in range(upserted)}


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_dir: int = 1
        self._limit: int | None = None

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = n
        return self

    def __aiter__(self):
        if self._sort_key is not None:
            self._docs.sort(
                key=lambda d: d.get(self._sort_key) or "",
                reverse=(self._sort_dir == -1),
            )
        if self._limit is not None:
            self._docs = self._docs[: self._limit]
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeAggregateCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _FakeChannelMessages:
    """In-memory stand-in for the ``channel_messages`` motor collection.

    Implements just enough of the surface to exercise ``upsert_channel_messages``,
    ``get_channel_messages``, ``count_channel_messages_by_status``,
    ``find_channel_message_by_message_id``, and ``update_channel_message_status``.
    """

    def __init__(self) -> None:
        # Key = (source_id, channel_id, message_id) → doc dict
        self._docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(filter_: dict[str, Any]) -> tuple[str, str, str]:
        return (filter_["source_id"], filter_["channel_id"], filter_["message_id"])

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> _FakeBulkResult:
        inserted = 0
        modified = 0
        matched = 0
        upserted = 0
        for op in ops:
            filter_ = op._filter  # private attr, but UpdateOne exposes it
            update = op._doc
            key = self._key(filter_)
            existing = self._docs.get(key)
            set_part = update.get("$set", {})
            on_insert = update.get("$setOnInsert", {})
            if existing is None:
                self._docs[key] = {**on_insert, **set_part}
                inserted += 1
                upserted += 1
            else:
                matched += 1
                # Only $set fields update; $setOnInsert is ignored on existing rows.
                changed = False
                for k, v in set_part.items():
                    if existing.get(k) != v:
                        existing[k] = v
                        changed = True
                if changed:
                    modified += 1
        return _FakeBulkResult(inserted, modified, matched, upserted)

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        rows = []
        for doc in self._docs.values():
            if self._matches(doc, query):
                rows.append({k: v for k, v in doc.items()})
        return _FakeCursor(rows)

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, int] | None = None
    ) -> dict[str, Any] | None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                return {k: v for k, v in doc.items()}
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                return

    def aggregate(self, pipeline: list[dict[str, Any]]) -> _FakeAggregateCursor:
        rows: list[dict[str, Any]] = []
        match: dict[str, Any] = {}
        group_field: str | None = None
        for stage in pipeline:
            if "$match" in stage:
                match = stage["$match"]
            elif "$group" in stage:
                # group expression like {"_id": "$extraction_status", "n": {"$sum": 1}}
                expr = stage["$group"]["_id"]
                if isinstance(expr, str) and expr.startswith("$"):
                    group_field = expr[1:]
        if group_field is None:
            return _FakeAggregateCursor([])
        counts: dict[Any, int] = {}
        for doc in self._docs.values():
            if not self._matches(doc, match):
                continue
            value = doc.get(group_field)
            counts[value] = counts.get(value, 0) + 1
        for value, n in counts.items():
            rows.append({"_id": value, "n": n})
        return _FakeAggregateCursor(rows)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                if "$lt" in v and not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc.get(k) >= v["$gte"]):
                    return False
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True


def _store_with_fake() -> tuple[MongoDBStore, _FakeChannelMessages]:
    fake = _FakeChannelMessages()
    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_messages = fake  # type: ignore[attr-defined]
    return store, fake


def _msg(
    source_id: str = "slack",
    channel_id: str = "C123",
    message_id: str = "m1",
    content: str = "hello",
    extraction_status: str = "pending",
) -> ChannelMessage:
    return ChannelMessage(
        source_id=source_id,
        channel_id=channel_id,
        message_id=message_id,
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        author="alice",
        content=content,
        extraction_status=extraction_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Idempotent upsert
# ─────────────────────────────────────────────────────────────────────────────


async def test_upsert_same_triple_twice_yields_one_document() -> None:
    """Spec scenario: ``Same message inserted twice``."""
    store, fake = _store_with_fake()
    msg = _msg(content="first")
    r1 = await store.upsert_channel_messages([msg])
    r2 = await store.upsert_channel_messages([_msg(content="first")])

    assert r1["inserted"] == 1
    assert r2["inserted"] == 0
    assert r2["matched"] == 1
    assert len(fake._docs) == 1


async def test_upsert_two_sources_same_message_id_yields_two_documents() -> None:
    """Spec scenario: ``Two messages with identical message_id but different
    source_id``."""
    store, fake = _store_with_fake()
    a = _msg(source_id="slack", message_id="m1")
    b = _msg(source_id="discord", message_id="m1")
    await store.upsert_channel_messages([a, b])

    assert len(fake._docs) == 2
    assert ("slack", "C123", "m1") in fake._docs
    assert ("discord", "C123", "m1") in fake._docs


async def test_upsert_resync_preserves_done_extraction_status() -> None:
    """Spec scenario: ``Re-sync of an already-extracted message``.

    ``$setOnInsert`` for ``extraction_status`` MUST preserve a row already in
    ``done`` so a re-sync does NOT re-queue it for extraction.
    """
    store, fake = _store_with_fake()
    # First sync — message lands as pending, then worker promotes it to done.
    await store.upsert_channel_messages([_msg(content="v1")])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"

    # Re-sync with the same message (e.g. user clicks Sync again).
    await store.upsert_channel_messages([_msg(content="v1-edited")])

    doc = fake._docs[("slack", "C123", "m1")]
    # Mutable content updated.
    assert doc["content"] == "v1-edited"
    # Extraction status preserved — this is the $setOnInsert contract.
    assert doc["extraction_status"] == "done"


# ─────────────────────────────────────────────────────────────────────────────
# Read accessor
# ─────────────────────────────────────────────────────────────────────────────


async def test_get_channel_messages_returns_all_for_channel() -> None:
    store, _ = _store_with_fake()
    msgs = [_msg(message_id=f"m{i}") for i in range(5)]
    await store.upsert_channel_messages(msgs)
    rows = await store.get_channel_messages(channel_id="C123", limit=10)
    assert len(rows) == 5
    assert {r["message_id"] for r in rows} == {f"m{i}" for i in range(5)}


async def test_get_channel_messages_respects_limit() -> None:
    store, _ = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id=f"m{i}") for i in range(10)])
    rows = await store.get_channel_messages(channel_id="C123", limit=3)
    assert len(rows) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Status aggregation
# ─────────────────────────────────────────────────────────────────────────────


async def test_count_channel_messages_by_status_zero_fills_missing() -> None:
    """Spec scenario: ``Channel has mixed extraction states``.

    The aggregation MUST always return all four keys, zero-filled for any
    status that has no rows.
    """
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id=f"m{i}") for i in range(5)])
    # Promote two rows to done, one to extracting, one to failed; leave one pending.
    fake._docs[("slack", "C123", "m0")]["extraction_status"] = "done"
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"
    fake._docs[("slack", "C123", "m2")]["extraction_status"] = "extracting"
    fake._docs[("slack", "C123", "m3")]["extraction_status"] = "failed"
    # m4 stays pending.

    counts = await store.count_channel_messages_by_status("C123")
    assert counts == {"pending": 1, "extracting": 1, "done": 2, "failed": 1}


async def test_count_channel_messages_by_status_empty_channel_zero_fills() -> None:
    store, _ = _store_with_fake()
    counts = await store.count_channel_messages_by_status("C_EMPTY")
    assert counts == {"pending": 0, "extracting": 0, "done": 0, "failed": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Phantom raw_messages fix (preprocessor + coreference resolver lookups)
# ─────────────────────────────────────────────────────────────────────────────


async def test_find_channel_message_by_message_id_returns_doc() -> None:
    """Used by preprocessor.py:252 / coreference_resolver.py:49 to replace
    the prior phantom ``raw_messages`` reads."""
    store, _ = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id="m_parent", content="parent text")])
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_parent")
    assert doc is not None
    assert doc["content"] == "parent text"


async def test_find_channel_message_by_message_id_returns_none_when_missing() -> None:
    store, _ = _store_with_fake()
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_missing")
    assert doc is None


# ─────────────────────────────────────────────────────────────────────────────
# State-machine validation
# ─────────────────────────────────────────────────────────────────────────────


async def test_update_status_accepts_legal_transition() -> None:
    """``pending → extracting`` is the worker's atomic-claim transition (PR-B)."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="extracting",
    )
    assert ok is True
    assert fake._docs[("slack", "C123", "m1")]["extraction_status"] == "extracting"


async def test_update_status_rejects_illegal_transition() -> None:
    """``done → pending`` without going through ``failed → pending`` retry path
    is illegal — the worker MUST not silently re-queue a finished message."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"

    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="pending",
    )
    assert ok is False
    assert fake._docs[("slack", "C123", "m1")]["extraction_status"] == "done"


async def test_update_status_failed_increments_attempt_count() -> None:
    """When a message transitions to ``failed``, ``attempt_count`` increments
    so the worker can apply exponential backoff (PR-C)."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "extracting"
    fake._docs[("slack", "C123", "m1")]["attempt_count"] = 1

    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="failed",
        last_error="503 UNAVAILABLE",
    )
    assert ok is True
    doc = fake._docs[("slack", "C123", "m1")]
    assert doc["extraction_status"] == "failed"
    assert doc["attempt_count"] == 2
    assert doc["last_error"] == "503 UNAVAILABLE"


async def test_update_status_returns_false_when_message_missing() -> None:
    store, _ = _store_with_fake()
    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m_missing",
        new_status="extracting",
    )
    assert ok is False
