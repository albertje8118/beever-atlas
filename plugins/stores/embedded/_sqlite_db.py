"""Shared SQLite connection helpers for embedded stores.

All embedded store modules import get_db_path() to locate the shared DB file.
The DB is opened fresh per operation (aiosqlite handles connection pooling
internally), with WAL mode and foreign keys enabled on every new connection.
"""
from __future__ import annotations

import os

SQLITE_DB_PATH_DEFAULT = ".data/beever_atlas.db"


def get_db_path() -> str:
    return os.environ.get("BEEVER_SQLITE_DB_PATH", SQLITE_DB_PATH_DEFAULT)


def ensure_data_dir() -> None:
    """Create the directory that will hold the SQLite file if it doesn't exist."""
    path = get_db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
