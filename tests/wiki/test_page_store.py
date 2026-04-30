"""Unit tests for the per-page wiki document store (PR-E).

Tests use a lightweight in-memory fake collection — no live Mongo
container required. Mirrors the pattern used by
``tests/stores/test_channel_messages_store.py``.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-page-store/``
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beever_atlas.models.persistence import (
    WikiPage,
    WikiPageSection,
    WikiTension,
)
from beever_atlas.wiki.page_store import WikiPageStore


# ─────────────────────────────────────────────────────────────────────────────
# Fake Mongo collection — supports only the operators WikiPageStore uses
# ─────────────────────────────────────────────────────────────────────────────


class _FakeUpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _FakeDeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted_count = deleted


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_dir: int = 1

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def __aiter__(self):
        if self._sort_key:
            self._docs.sort(
                key=lambda d: d.get(self._sort_key) or "",
                reverse=(self._sort_dir == -1),
            )
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeWikiPagesCollection:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def create_index(self, *args, **kwargs) -> None:
        pass

    @staticmethod
    def _key(query: dict[str, Any]) -> tuple[str, str, str]:
        return (query["channel_id"], query["target_lang"], query["page_id"])

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, int] | None = None
    ) -> dict[str, Any] | None:
        if (
            "channel_id" in query
            and "target_lang" in query
            and "page_id" in query
            and not isinstance(query["page_id"], dict)
        ):
            doc = self.docs.get(self._key(query))
            return dict(doc) if doc else None
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        rows = [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]
        return _FakeCursor(rows)

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _FakeUpdateResult:
        if (
            "channel_id" in query
            and "target_lang" in query
            and "page_id" in query
            and not isinstance(query["page_id"], dict)
        ):
            key = self._key(query)
            existing = self.docs.get(key)
            if existing is None and not upsert:
                return _FakeUpdateResult(0)
            if existing is None:
                self.docs[key] = {}
                existing = self.docs[key]
            for k, v in update.get("$set", {}).items():
                existing[k] = v
            for k, v in update.get("$push", {}).items():
                if isinstance(v, dict) and "$each" in v:
                    existing.setdefault(k, []).extend(v["$each"])
                else:
                    existing.setdefault(k, []).append(v)
            return _FakeUpdateResult(1)
        return _FakeUpdateResult(0)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> _FakeUpdateResult:
        modified = 0
        for doc in self.docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                modified += 1
        return _FakeUpdateResult(modified)

    async def delete_one(self, query: dict[str, Any]) -> _FakeDeleteResult:
        if "channel_id" in query and "target_lang" in query and "page_id" in query:
            key = self._key(query)
            if key in self.docs:
                del self.docs[key]
                return _FakeDeleteResult(1)
        return _FakeDeleteResult(0)


def _make_store() -> tuple[WikiPageStore, _FakeWikiPagesCollection]:
    fake = _FakeWikiPagesCollection()
    store = WikiPageStore.__new__(WikiPageStore)
    store._db = None  # type: ignore[attr-defined]
    store._collection = fake  # type: ignore[attr-defined]
    return store, fake


def _make_page(
    *,
    channel_id: str = "C1",
    page_id: str = "topic:auth",
    target_lang: str = "en",
    title: str = "Auth",
) -> WikiPage:
    return WikiPage(
        channel_id=channel_id,
        target_lang=target_lang,
        page_id=page_id,
        title=title,
        slug=page_id.replace(":", "-"),
        sections=[
            WikiPageSection(id="overview", title="Overview", content_md="# Auth"),
            WikiPageSection(id="decisions", title="Decisions", content_md=""),
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# get_page / save_page
# ─────────────────────────────────────────────────────────────────────────────


async def test_get_page_returns_none_when_missing() -> None:
    store, _ = _make_store()
    result = await store.get_page("C1", "missing")
    assert result is None


async def test_save_page_then_get_round_trip() -> None:
    store, _ = _make_store()
    page = _make_page()
    await store.save_page(page)
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.title == "Auth"
    assert len(fetched.sections) == 2
    assert fetched.version == 1  # bumped from 0 → 1 on first save


async def test_save_page_bumps_version_on_each_save() -> None:
    store, _ = _make_store()
    page = _make_page()
    await store.save_page(page)
    await store.save_page(page)
    await store.save_page(page)
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.version == 3


async def test_save_page_isolates_channels() -> None:
    """Two channels with the same page_id are independent rows."""
    store, _ = _make_store()
    a = _make_page(channel_id="A", page_id="overview")
    b = _make_page(channel_id="B", page_id="overview")
    await store.save_page(a)
    await store.save_page(b)
    fetched_a = await store.get_page("A", "overview")
    fetched_b = await store.get_page("B", "overview")
    assert fetched_a is not None and fetched_a.channel_id == "A"
    assert fetched_b is not None and fetched_b.channel_id == "B"


async def test_save_page_isolates_target_langs() -> None:
    """English and Chinese pages don't collide on the same page_id."""
    store, _ = _make_store()
    en = _make_page(target_lang="en", title="Auth")
    zh = _make_page(target_lang="zh-HK", title="認證")
    await store.save_page(en)
    await store.save_page(zh)
    fetched_en = await store.get_page("C1", "topic:auth", target_lang="en")
    fetched_zh = await store.get_page("C1", "topic:auth", target_lang="zh-HK")
    assert fetched_en is not None and fetched_en.title == "Auth"
    assert fetched_zh is not None and fetched_zh.title == "認證"


# ─────────────────────────────────────────────────────────────────────────────
# list_pages
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_pages_returns_all_for_channel() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.save_page(_make_page(page_id="b"))
    await store.save_page(_make_page(page_id="c"))
    pages = await store.list_pages("C1")
    assert {p.page_id for p in pages} == {"a", "b", "c"}


async def test_list_pages_does_not_leak_other_channels() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page(channel_id="A", page_id="hidden"))
    await store.save_page(_make_page(channel_id="B", page_id="visible"))
    pages = await store.list_pages("B")
    assert {p.page_id for p in pages} == {"visible"}


# ─────────────────────────────────────────────────────────────────────────────
# mark_dirty / clear_dirty (manual mode for PR-F)
# ─────────────────────────────────────────────────────────────────────────────


async def test_mark_dirty_sets_is_dirty_on_named_pages() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.save_page(_make_page(page_id="b"))
    await store.save_page(_make_page(page_id="c"))
    modified = await store.mark_dirty("C1", ["a", "c"])
    assert modified == 2
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is True
    assert fake.docs[("C1", "en", "b")]["is_dirty"] is False
    assert fake.docs[("C1", "en", "c")]["is_dirty"] is True


async def test_clear_dirty_sets_is_dirty_false() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.mark_dirty("C1", ["a"])
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is True
    cleared = await store.clear_dirty("C1", ["a"])
    assert cleared == 1
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is False


async def test_mark_dirty_with_empty_list_is_noop() -> None:
    store, _ = _make_store()
    modified = await store.mark_dirty("C1", [])
    assert modified == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tensions (used by PR-G's contradiction surfacing)
# ─────────────────────────────────────────────────────────────────────────────


async def test_append_tensions_adds_to_page() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page())
    new_tensions = [
        WikiTension(
            fact_id="f1",
            contradicts_fact_id="f2",
            summary="conflicting decisions",
        )
    ]
    ok = await store.append_tensions("C1", "topic:auth", new_tensions)
    assert ok is True
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert len(fetched.tensions) == 1
    assert fetched.tensions[0].fact_id == "f1"


async def test_append_tensions_with_empty_list_is_noop() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page())
    ok = await store.append_tensions("C1", "topic:auth", [])
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# delete_page
# ─────────────────────────────────────────────────────────────────────────────


async def test_delete_page_removes_the_row() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page())
    deleted = await store.delete_page("C1", "topic:auth")
    assert deleted is True
    assert len(fake.docs) == 0


async def test_delete_missing_page_returns_false() -> None:
    store, _ = _make_store()
    deleted = await store.delete_page("C1", "missing")
    assert deleted is False
