"""Patch SyncScheduler to use SQLite (via SQLAlchemy) instead of MongoDB for APScheduler job store.

APScheduler 4.x's MongoDBDataStore requires a running MongoDB, which is unavailable in the
embedded-stores setup.  This patch swaps it for SQLAlchemyDataStore backed by a dedicated
SQLite file so the scheduler starts cleanly without any external database server.

Environment variables
---------------------
BEEVER_SCHEDULER_DB_PATH  (default: .data/beever_scheduler.db)
    Path to the SQLite file used by APScheduler. Kept separate from the main
    beever_atlas.db to avoid table-name conflicts with the app's own tables.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEDULER_DB_ENV = "BEEVER_SCHEDULER_DB_PATH"
_SCHEDULER_DB_DEFAULT = ".data/beever_scheduler.db"


def get_scheduler_db_path() -> str:
    raw = os.getenv(_SCHEDULER_DB_ENV, _SCHEDULER_DB_DEFAULT)
    return str(Path(raw).resolve())


def apply_scheduler_patch() -> None:
    """Monkey-patch SyncScheduler.__init__ to use SQLAlchemyDataStore with SQLite."""
    from beever_atlas.services.scheduler import SyncScheduler
    from apscheduler.datastores.sqlalchemy import SQLAlchemyDataStore
    from apscheduler import AsyncScheduler

    def _patched_init(self, mongodb_uri: str) -> None:
        db_path = get_scheduler_db_path()
        self._mongodb_uri = mongodb_uri
        self._data_store = SQLAlchemyDataStore(f"sqlite+aiosqlite:///{db_path}")
        self._scheduler = AsyncScheduler(data_store=self._data_store)
        self._global_semaphore = None
        self._started = False
        logger.info("embedded_stores: SyncScheduler patched → SQLite datastore at %s", db_path)

    SyncScheduler.__init__ = _patched_init
