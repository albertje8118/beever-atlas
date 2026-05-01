"""Null store implementations for embedded_stores plugin.

NullVectorStore — no-op WeaviateStore (WEAVIATE_BACKEND=null)
NullQAHistoryStore — no-op QAHistoryStore (WEAVIATE_BACKEND=null)
NullFileStore — in-memory FileStore (MONGODB_BACKEND=mock, avoids GridFS)

Every method returns an empty/default value so the application starts without
external services. Search and RAG features return empty results but the API responds.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from beever_atlas.models import AtomicFact, MemoryFilters, PaginatedFacts

if TYPE_CHECKING:
    from beever_atlas.models.domain import ChannelSummary, EntityKnowledgeCard, TopicCluster


class NullVectorStore:
    """No-op replacement for WeaviateStore — all operations return empty/default values."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def ensure_schema(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_fact(self, fact: AtomicFact) -> str:
        return fact.id

    async def batch_upsert_facts(self, facts: list[AtomicFact]) -> list[str]:
        return [f.id for f in facts]

    async def update_fact_cluster(self, fact_id: str, cluster_id: str) -> None:
        pass

    async def supersede_fact(self, old_fact_id: str, new_fact_id: str) -> None:
        pass

    async def flag_potential_contradiction(self, fact_id: str) -> None:
        pass

    async def batch_update_fact_clusters(self, updates: list[tuple[str, str]]) -> None:
        pass

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_fact(self, fact_id: str) -> AtomicFact | None:
        return None

    async def list_facts(
        self,
        channel_id: str,
        filters: MemoryFilters,
        page: int = 1,
        limit: int = 20,
    ) -> PaginatedFacts:
        return PaginatedFacts(memories=[], total=0, page=page, pages=1)

    async def count_facts(self, channel_id: str | None = None) -> int:
        return 0

    async def delete_by_channel(self, channel_id: str) -> int:
        return 0

    async def delete_all(self) -> int:
        return 0

    async def fetch_by_ids(self, fact_ids: list[str]) -> list[AtomicFact]:
        return []

    async def fetch_media_facts(self, channel_id: str, limit: int = 500) -> list[AtomicFact]:
        return []

    async def fetch_recent_facts(
        self, channel_id: str, days: int = 7, limit: int = 500
    ) -> list[AtomicFact]:
        return []

    # ------------------------------------------------------------------
    # Search operations
    # ------------------------------------------------------------------

    async def semantic_search(
        self,
        query_vector: list[float],
        channel_id: str | None = None,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        return []

    async def bm25_search(
        self,
        query: str,
        channel_id: str,
        tier: str = "atomic",
        limit: int = 10,
    ) -> list[AtomicFact]:
        return []

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
        return []

    async def pseudo_hybrid_search(
        self,
        query_vector: list[float],
        channel_id: str,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # Unclustered / iteration
    # ------------------------------------------------------------------

    async def get_unclustered_facts(
        self,
        channel_id: str,
        limit: int | None = None,
    ) -> list[AtomicFact]:
        return []

    async def iter_unclustered_facts(
        self,
        channel_id: str,
        page_size: int = 200,
    ) -> AsyncIterator[AtomicFact]:
        # Empty async generator
        if False:
            yield  # type: ignore[misc]

    async def iter_all_fact_ids(
        self,
        channel_id: str,
        page_size: int = 500,
    ) -> AsyncIterator[tuple[str, str]]:
        # Empty async generator
        if False:
            yield  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Cluster / summary operations
    # ------------------------------------------------------------------

    async def upsert_cluster(self, cluster: "TopicCluster") -> str:
        return cluster.id

    async def list_clusters(self, channel_id: str) -> list["TopicCluster"]:
        return []

    async def get_cluster(self, cluster_id: str) -> "TopicCluster | None":
        return None

    async def get_cluster_members(
        self, cluster_id: str, limit: int = 100
    ) -> list[AtomicFact]:
        return []

    async def fetch_all_cluster_members(
        self, channel_id: str, cluster_id: str, limit: int = 500
    ) -> list[AtomicFact]:
        return []

    async def delete_cluster(self, cluster_id: str) -> None:
        pass

    async def upsert_channel_summary(self, summary: "ChannelSummary") -> str:
        return getattr(summary, "id", str(uuid.uuid4()))

    async def get_channel_summary(self, channel_id: str) -> "ChannelSummary | None":
        return None

    async def upsert_entity_card(self, card: "EntityKnowledgeCard") -> str:
        return getattr(card, "id", str(uuid.uuid4()))

    async def get_entity_card(self, entity_name: str) -> "EntityKnowledgeCard | None":
        return None

    async def list_entity_cards(
        self, channel_id: str | None = None, limit: int = 50
    ) -> list["EntityKnowledgeCard"]:
        return []


class NullQAHistoryStore:
    """No-op replacement for QAHistoryStore."""

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def ensure_schema(self) -> None:
        pass

    async def write_qa_entry(
        self,
        question: str,
        answer: str,
        citations: list[dict] | dict,
        channel_id: str,
        user_id: str,
        session_id: str,
    ) -> str:
        return str(uuid.uuid4())

    async def true_hybrid_search(
        self,
        channel_id: str,
        query: str,
        query_vector: list[float],
        limit: int = 5,
        alpha: float | None = None,
    ) -> list[dict]:
        return []

    async def search_qa_history(
        self,
        channel_id: str,
        query: str,
        limit: int = 5,
        query_vector: list[float] | None = None,
    ) -> list[dict]:
        return []

    async def soft_delete(self, entry_id: str) -> None:
        pass

    async def find_qa_entries_citing_source(
        self, source_id: str, limit: int = 20
    ) -> dict:
        return {"entries": [], "truncated": False, "scanned": 0}


# ---------------------------------------------------------------------------
# NullFileStore — in-memory replacement for FileStore (avoids GridFS compat)
# ---------------------------------------------------------------------------


class _MockGridOut:
    """Minimal GridOut-compatible object for NullFileStore.open()."""

    def __init__(self, content: bytes, filename: str, metadata: dict) -> None:
        self._content = content
        self.filename = filename
        self.metadata = metadata

    async def read(self) -> bytes:
        return self._content


class NullFileStore:
    """In-memory replacement for FileStore — stores blobs in a plain dict.

    Used when MONGODB_BACKEND=mock to avoid the AsyncIOMotorGridFSBucket
    type check that rejects mongomock database objects.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, tuple[bytes, str, dict]] = {}  # file_id → (content, filename, meta)

    async def startup(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def save(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str,
        owner_user_id: str,
    ) -> str:
        file_id = uuid.uuid4().hex
        self._blobs[file_id] = (
            content,
            filename,
            {"file_id": file_id, "owner_user_id": owner_user_id, "mime_type": mime_type},
        )
        return file_id

    async def open(self, file_id: str) -> "_MockGridOut | None":
        entry = self._blobs.get(file_id)
        if entry is None:
            return None
        content, filename, metadata = entry
        return _MockGridOut(content, filename, metadata)
