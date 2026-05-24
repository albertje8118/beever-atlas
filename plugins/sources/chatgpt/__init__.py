"""ChatGPT history source plugin for beever-atlas.

Adds:
  - ChatGPT conversation history extractor (Playwright auth + httpx fetch)
  - CLI importer that feeds ChatGPT history into the beever-atlas RAG pipeline
  - Scheduled periodic ingestion via APScheduler (every N hours)

Activation
----------
Run the app through ``start_with_plugins.py`` instead of pointing uvicorn
directly at ``beever_atlas.server.app:app``::

    uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Authentication
--------------
Call ``POST /api/plugins/chatgpt/launch-browser`` to open a headed Playwright
browser window at chatgpt.com.  The user logs in normally (Google SSO, email,
MFA) — the session is saved to ``chatgpt_session.json``.  Subsequent syncs
use httpx with the saved cookies; no browser launch is required.

Environment variables
---------------------
``CHATGPT_SYNC_INTERVAL_HOURS``   How often (in hours) to run the scheduled
                                   ChatGPT sync. Default: 6.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def activate() -> None:
    """Apply all monkey-patches to extend beever-atlas with ChatGPT source support."""
    from plugins.sources.chatgpt._scheduler_hook import apply_chatgpt_scheduler_hook

    apply_chatgpt_scheduler_hook()
    logger.info("sources.chatgpt plugin activated — scheduled ChatGPT sync enabled")
