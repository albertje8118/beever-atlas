"""Store patches for the embedded_stores plugin.

Applies two optional patches based on environment variables:

MONGODB_BACKEND=mock
    Replaces motor.motor_asyncio.AsyncIOMotorClient with
    mongomock_motor.AsyncMongoMockClient. This must happen before any
    beever_atlas module instantiates a Motor client, so it is applied
    at plugin activate() time.

WEAVIATE_BACKEND=null
    After StoreClients.from_settings() runs, replaces the WeaviateStore
    and QAHistoryStore with NullVectorStore / NullQAHistoryStore.

GRAPH_BACKEND=kuzu  (signalled via _KUZU_OVERRIDE env var)
    After StoreClients.from_settings() runs (which created a NullGraphStore
    because we pre-set GRAPH_BACKEND=none), replaces it with KuzuGraphStore.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def apply_motor_patch() -> None:
    """Patch Motor → mongomock_motor (call before any beever_atlas import)."""
    if os.getenv("MONGODB_BACKEND", "").lower() != "mock":
        return
    try:
        import mongomock_motor
        import motor.motor_asyncio as _mta

        _mta.AsyncIOMotorClient = mongomock_motor.AsyncMongoMockClient  # type: ignore[attr-defined]
        logger.info("embedded_stores: Motor → mongomock_motor.AsyncMongoMockClient")
    except ImportError:
        logger.warning(
            "embedded_stores: mongomock_motor not installed; MONGODB_BACKEND=mock ignored"
        )


def apply_store_client_patch() -> None:
    """Monkey-patch StoreClients.from_settings to inject embedded stores."""
    kuzu_override = os.getenv("_KUZU_OVERRIDE") == "1"
    null_weaviate = os.getenv("WEAVIATE_BACKEND", "").lower() == "null"
    mock_mongo = os.getenv("MONGODB_BACKEND", "").lower() == "mock"

    if not (kuzu_override or null_weaviate or mock_mongo):
        return

    from beever_atlas.stores import StoreClients  # beever_atlas already imported at this point

    _original = StoreClients.from_settings.__func__  # type: ignore[attr-defined]

    def _patched(cls, settings):  # type: ignore[no-untyped-def]
        clients = _original(cls, settings)

        if kuzu_override:
            from plugins.embedded_stores._kuzu_graph import KuzuGraphStore
            from beever_atlas.stores.entity_registry import EntityRegistry

            db_path = os.getenv("KUZU_DB_PATH", ":memory:")
            kuzu_store = KuzuGraphStore(db_path)
            clients.graph = kuzu_store
            clients.entity_registry = EntityRegistry(kuzu_store)
            logger.info("embedded_stores: graph → KuzuGraphStore (path=%s)", db_path)

        if null_weaviate:
            from plugins.embedded_stores._null_vector import NullVectorStore, NullQAHistoryStore

            clients.weaviate = NullVectorStore()
            clients.qa_history = NullQAHistoryStore()
            logger.info("embedded_stores: weaviate → NullVectorStore, qa_history → NullQAHistoryStore")

        if mock_mongo:
            from plugins.embedded_stores._null_vector import NullFileStore

            clients.file_store = NullFileStore()
            logger.info("embedded_stores: file_store → NullFileStore (in-memory)")

        return clients

    StoreClients.from_settings = classmethod(_patched)  # type: ignore[assignment]
    logger.info("embedded_stores: StoreClients.from_settings patched")
