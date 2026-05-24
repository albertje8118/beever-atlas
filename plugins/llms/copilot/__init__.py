"""GitHub Copilot LLM plugin for beever-atlas.

Adds:
  - GitHub Copilot API as an LLM provider (``copilot/<model>`` strings)
  - GitHub Models API as an LLM provider (``github/<model>`` strings)

Activation
----------
Run the app through ``start_with_plugins.py`` instead of pointing uvicorn
directly at ``beever_atlas.server.app:app``::

    uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000

Environment variables
---------------------
``COPILOT_GITHUB_TOKEN``   Token for the GitHub Copilot API.  Falls back to
                            ``GH_TOKEN``, ``GITHUB_TOKEN``, or ``gh auth token``.
``GITHUB_TOKEN``           PAT for the GitHub Models API (``github/`` prefix).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def activate() -> None:
    """Apply all monkey-patches to extend beever-atlas with Copilot LLM support."""
    from plugins.llms.copilot._llm_patch import apply_llm_patches
    from plugins.llms.copilot._embedder import apply_embed_patches

    apply_llm_patches()
    apply_embed_patches()
    logger.info(
        "llms.copilot plugin activated — GitHub Copilot + GitHub Models LLM support enabled; "
        "embeddings via GitHub Copilot API"
    )
