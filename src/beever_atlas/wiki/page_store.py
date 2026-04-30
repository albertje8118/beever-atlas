"""Per-page wiki document store (PR-E).

Replaces the flat ``pages`` subdoc on the legacy ``wiki_cache`` row
with one MongoDB document per ``(channel_id, target_lang, page_id)``.
Per-page documents are the structural prerequisite for everything in
PR-F (incremental maintainer) and PR-G (lint + tensions) — neither
can work over the monolithic schema.

The legacy ``WikiCache`` is retained during the dual-write window;
the ``PER_PAGE_WIKI`` flag dispatches reads. Writes go to the new
collection unconditionally so a soak rollback (flag → OFF) reads the
legacy doc but doesn't lose any page edits made under the new path.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-page-store/``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from beever_atlas.models.persistence import WikiPage, WikiPageSection, WikiTension

logger = logging.getLogger(__name__)


class WikiPageStore:
    """Per-page accessor over the ``wiki_pages`` collection.

    Compound unique index on ``(channel_id, target_lang, page_id)``
    gives idempotent upsert. The ``version`` field is bumped on every
    write so PR-F can archive prior versions (PR-E doesn't archive
    yet — that's task 6.4 / 6.5 deferred to maintainer landing).
    """

    def __init__(self, db: AsyncIOMotorDatabase | None = None) -> None:
        # ``db`` may be injected by the application's MongoDBStore so
        # this object shares the existing connection pool. When None,
        # callers must call ``bind_db`` before any read/write.
        self._db = db
        self._collection: Any = None
        if db is not None:
            self._collection = db["wiki_pages"]

    @classmethod
    def from_client(
        cls, client: AsyncIOMotorClient, db_name: str = "beever_atlas"
    ) -> "WikiPageStore":
        return cls(db=client[db_name])

    def bind_db(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db
        self._collection = db["wiki_pages"]

    async def ensure_indexes(self) -> None:
        if self._collection is None:
            return
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("page_id", 1)],
            unique=True,
            name="wiki_pages_compound_unique",
        )
        # Secondary index for ``list_pages`` performance — newest first.
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("updated_at", -1)],
            name="wiki_pages_channel_updated",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_page(
        self, channel_id: str, page_id: str, target_lang: str = "en"
    ) -> WikiPage | None:
        """Read one page by its compound key.

        Returns None on miss — callers (the wiki UI tool callers) do
        NOT need to know the difference between \"page never existed\"
        and \"page was deleted\". Both are no-content responses.
        """
        if self._collection is None:
            return None
        doc = await self._collection.find_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            }
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return WikiPage.model_validate(doc)

    async def list_pages(self, channel_id: str, target_lang: str = "en") -> list[WikiPage]:
        """List all pages for a channel + language, newest-edited first."""
        if self._collection is None:
            return []
        cursor = self._collection.find({"channel_id": channel_id, "target_lang": target_lang}).sort(
            "updated_at", -1
        )
        pages: list[WikiPage] = []
        async for doc in cursor:
            doc.pop("_id", None)
            pages.append(WikiPage.model_validate(doc))
        return pages

    async def save_page(self, page: WikiPage) -> None:
        """Idempotent upsert — bumps ``version`` and ``updated_at``.

        ``version`` increments by exactly one per save, so a future
        ``WikiVersionStore`` migration can archive each prior version
        without losing the linear edit history.
        """
        if self._collection is None:
            raise RuntimeError("WikiPageStore not bound to a database")
        existing = await self._collection.find_one(
            {
                "channel_id": page.channel_id,
                "target_lang": page.target_lang,
                "page_id": page.page_id,
            },
            {"version": 1},
        )
        next_version = int(existing.get("version", 0)) + 1 if existing else 1
        doc = page.model_dump(mode="json")
        doc["version"] = next_version
        doc["updated_at"] = datetime.now(tz=UTC).isoformat()
        if "created_at" not in doc or doc["created_at"] is None:
            doc["created_at"] = doc["updated_at"]
        await self._collection.update_one(
            {
                "channel_id": page.channel_id,
                "target_lang": page.target_lang,
                "page_id": page.page_id,
            },
            {"$set": doc},
            upsert=True,
        )

    async def mark_dirty(
        self, channel_id: str, page_ids: list[str], target_lang: str = "en"
    ) -> int:
        """Set ``is_dirty=True`` on the named pages.

        Used by the maintainer's ``manual`` mode (PR-F) — the
        Maintain Wiki button reads pages where ``is_dirty=True`` and
        processes them on demand.
        """
        if self._collection is None or not page_ids:
            return 0
        result = await self._collection.update_many(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": {"$in": page_ids},
            },
            {
                "$set": {
                    "is_dirty": True,
                    "updated_at": datetime.now(tz=UTC),
                }
            },
        )
        return int(result.modified_count)

    async def clear_dirty(
        self, channel_id: str, page_ids: list[str], target_lang: str = "en"
    ) -> int:
        """Clear ``is_dirty=False`` after the maintainer processes a page."""
        if self._collection is None or not page_ids:
            return 0
        result = await self._collection.update_many(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": {"$in": page_ids},
            },
            {
                "$set": {
                    "is_dirty": False,
                    "updated_at": datetime.now(tz=UTC),
                }
            },
        )
        return int(result.modified_count)

    async def append_tensions(
        self,
        channel_id: str,
        page_id: str,
        new_tensions: list[WikiTension],
        target_lang: str = "en",
    ) -> bool:
        """Append contradictions to a page's ``tensions`` list.

        Used by PR-G's lint pass + the contradiction detector wire-up.
        Idempotent — duplicate (fact_id, contradicts_fact_id) tuples
        are deduped on read by the renderer.
        """
        if self._collection is None or not new_tensions:
            return False
        result = await self._collection.update_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            },
            {
                "$push": {"tensions": {"$each": [t.model_dump(mode="json") for t in new_tensions]}},
                "$set": {"updated_at": datetime.now(tz=UTC)},
            },
        )
        return bool(result.modified_count)

    async def delete_page(self, channel_id: str, page_id: str, target_lang: str = "en") -> bool:
        if self._collection is None:
            return False
        result = await self._collection.delete_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            }
        )
        return bool(result.deleted_count)


__all__ = ["WikiPageStore", "WikiPage", "WikiPageSection", "WikiTension"]
