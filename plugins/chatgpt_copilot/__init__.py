"""ChatGPT history + GitHub Models plugin for beever-atlas.

Adds:
  - GitHub Models / Copilot API as an LLM provider (``github/<model>`` strings)
  - ChatGPT conversation history extractor (CDP-based, Edge browser)
  - CLI importer that feeds ChatGPT history into the beever-atlas RAG pipeline

Activation
----------
Run the app through ``start_with_plugins.py`` instead of pointing uvicorn
directly at ``beever_atlas.server.app:app``::

    uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Environment variables
---------------------
``GITHUB_TOKEN``   Personal access token for GitHub Models API.
                   No special scopes needed for public models.
                   Required when using ``github/`` model strings.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def activate() -> None:
    """Apply all monkey-patches to extend beever-atlas with this plugin's features."""
    from plugins.chatgpt_copilot._llm_patch import apply_llm_patches

    apply_llm_patches()
    logger.info("chatgpt_copilot plugin activated — GitHub Models LLM support enabled")
