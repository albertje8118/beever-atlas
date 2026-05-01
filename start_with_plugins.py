"""ASGI entry point that activates plugins before serving beever-atlas.

Use this instead of pointing uvicorn directly at ``beever_atlas.server.app:app``
to enable the plugins in ``plugins/``.

Development (auto-reload)::

    uvicorn start_with_plugins:app --reload --host 0.0.0.0 --port 8000

Production::

    uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

With uv::

    uv run uvicorn start_with_plugins:app --reload

Docker (override the default CMD)::

    docker run -e GITHUB_TOKEN=... <image> \\
        uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Notes
-----
* ``load_plugins()`` runs at module import time, so it executes once per
  worker/reload process — correctly patching the modules before the app is
  imported.
* The ``plugins/`` directory is importable because uvicorn adds the CWD
  (project root) to ``sys.path`` before importing this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Guarantee the project root is on sys.path so 'plugins' is importable in all
# execution contexts (direct python, uvicorn, gunicorn worker, etc.).
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env BEFORE plugin activation so env vars (e.g. GITHUB_TOKEN) are
# available during patching.  app.py also calls load_dotenv(), which is
# idempotent, so double-loading is harmless.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

# Activate all plugins before the app module is imported.
from plugins.loader import load_plugins  # noqa: E402

load_plugins()

# Import the app *after* patches are applied.  Each uvicorn reload worker
# re-imports this module from scratch, so plugins are always active.
from beever_atlas.server.app import app  # noqa: E402, F401  # re-exported for uvicorn

__all__ = ["app"]
