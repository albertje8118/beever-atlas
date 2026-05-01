"""ASGI entry point that activates plugins before serving beever-atlas.

Use this instead of pointing uvicorn directly at ``beever_atlas.server.app:app``
to enable the plugins in ``plugins/``.

Development (auto-reload) — recommended::

    uv run uvicorn start_with_plugins:app --reload

Or activate the project venv first, then use plain uvicorn::

    .venv\\Scripts\\activate   # Windows
    source .venv/bin/activate  # Linux / macOS
    uvicorn start_with_plugins:app --reload

Production::

    uv run uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Docker (override the default CMD)::

    docker run -e COPILOT_GITHUB_TOKEN=... <image> \\
        uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Notes
-----
* ``load_plugins()`` runs at module import time, so it executes once per
  worker/reload process — correctly patching the modules before the app is
  imported.
* The ``plugins/`` directory is importable because this file adds the project
  root to ``sys.path``.
* ``beever_atlas`` is installed as an editable package inside the project
  ``.venv``.  Always run via ``uv run`` or with the venv activated.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Guarantee the project root is on sys.path so 'plugins' is importable in all
# execution contexts (direct python, uvicorn, gunicorn worker, etc.).
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Also add src/ so beever_atlas is importable when the package is not installed
# in the active Python environment (e.g. running with a system uvicorn while
# deps live in .venv).  uv run / venv activation is still preferred because
# third-party dependencies (fastapi, weaviate-client, etc.) must also be present.
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Load .env BEFORE plugin activation so env vars (e.g. COPILOT_GITHUB_TOKEN) are
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
