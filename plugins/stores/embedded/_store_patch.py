"""Store patches for the stores.embedded plugin.

Applies patches based on environment variables:

MONGODB_BACKEND=mock
    Replaces motor.motor_asyncio.AsyncIOMotorClient with a singleton
    AsyncMongoMockClient from mongomock_motor.  Data is persisted via a
    JSON snapshot in the shared SQLite file and restored on startup.

WEAVIATE_BACKEND=null
    After StoreClients.from_settings() runs, injects SQLiteVectorStore
    (persistent, SQLite-backed) or NullVectorStore (no-op fallback).

GRAPH_BACKEND=sqlite  (or _SQLITE_GRAPH_OVERRIDE env var)
    After StoreClients.from_settings() runs, injects SQLiteGraphStore.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def apply_store_client_patch() -> None:
    """Monkey-patch StoreClients.from_settings to inject embedded stores."""
    sqlite_graph = (
        os.getenv("_SQLITE_GRAPH_OVERRIDE") == "1"
        or os.getenv("GRAPH_BACKEND", "").lower() == "sqlite"
    )
    null_weaviate = os.getenv("WEAVIATE_BACKEND", "").lower() == "null"
    mock_mongo = os.getenv("MONGODB_BACKEND", "").lower() == "mock"

    if not (sqlite_graph or null_weaviate or mock_mongo):
        return

    from beever_atlas.stores import StoreClients  # beever_atlas already importable here

    _original = StoreClients.from_settings.__func__  # type: ignore[attr-defined]

    def _patched(cls, settings):  # type: ignore[no-untyped-def]
        clients = _original(cls, settings)

        if sqlite_graph:
            from plugins.stores.embedded._sqlite_graph import SQLiteGraphStore
            from beever_atlas.stores.entity_registry import EntityRegistry

            graph_store = SQLiteGraphStore()
            clients.graph = graph_store
            clients.entity_registry = EntityRegistry(graph_store)
            logger.info("embedded_stores: graph -> SQLiteGraphStore")

        if null_weaviate:
            # Try SQLiteVectorStore first; fall back to NullVectorStore
            try:
                from plugins.stores.embedded._sqlite_vector import (
                    SQLiteVectorStore,
                    SQLiteQAHistoryStore,
                )

                clients.weaviate = SQLiteVectorStore()
                clients.qa_history = SQLiteQAHistoryStore()
                logger.info(
                    "embedded_stores: weaviate -> SQLiteVectorStore, "
                    "qa_history -> SQLiteQAHistoryStore"
                )
            except (ImportError, Exception) as exc:
                from plugins.stores.embedded._null_vector import (
                    NullQAHistoryStore,
                    NullVectorStore,
                )

                clients.weaviate = NullVectorStore()
                clients.qa_history = NullQAHistoryStore()
                logger.warning(
                    "embedded_stores: SQLiteVectorStore unavailable (%s), "
                    "falling back to NullVectorStore",
                    exc,
                )

        if mock_mongo:
            from plugins.stores.embedded._null_vector import NullFileStore

            clients.file_store = NullFileStore()
            logger.info("embedded_stores: file_store -> NullFileStore (in-memory)")

        return clients

    StoreClients.from_settings = classmethod(_patched)  # type: ignore[assignment]
    logger.info("embedded_stores: StoreClients.from_settings patched")
