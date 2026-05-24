"""stores.embedded plugin — swap external databases for embedded/SQLite alternatives.

Environment variables
---------------------
GRAPH_BACKEND=sqlite
    Use SQLiteGraphStore instead of Neo4j or NebulaGraph.

WEAVIATE_BACKEND=null
    Use SQLiteVectorStore (persistent) or NullVectorStore (no-op fallback).

MONGODB_BACKEND=mock
    Replace the real MongoDB Motor client with a singleton mongomock_motor
    instance.  Data is persisted as a JSON snapshot in the shared SQLite file
    and restored on startup.

BEEVER_SQLITE_DB_PATH  (default: .data/beever_atlas.db)
    Path to the shared SQLite database file used by all embedded stores.

Typical local dev .env
-----------------------
GRAPH_BACKEND=sqlite
WEAVIATE_BACKEND=null
MONGODB_BACKEND=mock
# BEEVER_SQLITE_DB_PATH=.data/beever_atlas.db  # default
"""

from __future__ import annotations

import atexit
import logging
import os

logger = logging.getLogger(__name__)


def activate() -> None:
    """Called by plugins/loader.py at startup, before the app is imported."""

    # --- Ensure SQLite data directory exists ---
    from plugins.stores.embedded._sqlite_db import ensure_data_dir

    ensure_data_dir()

    # --- Step 1: MongoDB singleton patch (must run before any Motor client is created) ---
    if os.getenv("MONGODB_BACKEND", "").lower() == "mock":
        from plugins.stores.embedded._sqlite_doc import patch_motor_singleton, load_snapshot

        patch_motor_singleton()

        # Load the persisted snapshot into the mongomock singleton.
        # Run in a dedicated thread so it gets its own event loop, avoiding
        # conflicts with any loop that uvicorn or asyncio has already configured.
        import asyncio
        import threading

        _load_error: list[Exception] = []

        def _run_load() -> None:
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            try:
                _loop.run_until_complete(load_snapshot())
            except Exception as exc:
                _load_error.append(exc)
            finally:
                _loop.close()
                asyncio.set_event_loop(None)

        t = threading.Thread(target=_run_load, daemon=True)
        t.start()
        t.join(timeout=30)
        if _load_error:
            logger.warning(
                "embedded_stores: MongoDB snapshot load error: %s; "
                "starting with empty in-memory state",
                _load_error[0],
            )

        # Register atexit handler to save snapshot on process exit
        from plugins.stores.embedded._sqlite_doc import save_snapshot

        def _atexit_save() -> None:
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            try:
                _loop.run_until_complete(save_snapshot())
                logger.info("embedded_stores: MongoDB snapshot saved on exit")
            except Exception as exc:
                logger.warning("embedded_stores: snapshot save failed on exit: %s", exc)
            finally:
                _loop.close()
                asyncio.set_event_loop(None)

        atexit.register(_atexit_save)
        logger.info("embedded_stores: MongoDB snapshot registered for atexit save")

    # --- Step 2: Signal SQLiteGraphStore override ---
    if os.getenv("GRAPH_BACKEND", "").lower() == "sqlite":
        os.environ["GRAPH_BACKEND"] = "none"
        os.environ["_SQLITE_GRAPH_OVERRIDE"] = "1"
        logger.info("embedded_stores: GRAPH_BACKEND=sqlite -> SQLiteGraphStore")

    # --- Step 3: Patch StoreClients.from_settings to inject our embedded stores ---
    from plugins.stores.embedded._store_patch import apply_store_client_patch

    apply_store_client_patch()

    # --- Step 4: Patch SyncScheduler to use SQLite instead of MongoDB ---
    from plugins.stores.embedded._scheduler_patch import apply_scheduler_patch

    apply_scheduler_patch()

    # --- Step 5: Patch Redis health check to report 'disabled' (no Redis in embedded mode) ---
    from plugins.stores.embedded._redis_patch import apply_redis_patch

    apply_redis_patch()

    logger.info("embedded_stores plugin activated")
