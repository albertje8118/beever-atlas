"""Monkey-patch: replace EmbedderAgent's Jina backend with GitHub Copilot embeddings.

Uses the same ``POST https://api.githubcopilot.com/embeddings`` endpoint that
backs the LLM plugin — no extra auth required.  The default model is
``text-embedding-3-small`` (1536-dim), configurable via env vars.

Environment variables
---------------------
``COPILOT_EMBED_MODEL``       Model ID (default: ``text-embedding-3-small``)
``COPILOT_EMBED_DIMENSIONS``  Output dimensions (default: ``1536``)
``COPILOT_EMBED_RPM``         Rate-limit cap, requests/min (default: ``60``)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100
_MAX_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# ---------------------------------------------------------------------------
# Rate limiter (lazy singleton, same pattern as JINA_LIMITER)
# ---------------------------------------------------------------------------

_copilot_embed_limiter = None


def _get_embed_limiter():
    global _copilot_embed_limiter
    if _copilot_embed_limiter is None:
        from aiolimiter import AsyncLimiter

        rpm = int(os.environ.get("COPILOT_EMBED_RPM", "60"))
        _copilot_embed_limiter = AsyncLimiter(max_rate=rpm, time_period=60)
    return _copilot_embed_limiter


class _LazyEmbedLimiter:
    async def __aenter__(self):
        return await _get_embed_limiter().__aenter__()

    async def __aexit__(self, *args):
        return await _get_embed_limiter().__aexit__(*args)


_COPILOT_EMBED_LIMITER = _LazyEmbedLimiter()

# ---------------------------------------------------------------------------
# Replacement for EmbedderAgent._jina_embed_batch
# ---------------------------------------------------------------------------


async def _copilot_embed_batch(
    self: Any,
    texts: list[str],
    *,
    sync_job_id: str,
    channel_id: str,
    batch_num: str | int,
) -> list[list[float]]:
    """Send texts to the GitHub Copilot embeddings API and return vectors.

    Drop-in replacement for ``EmbedderAgent._jina_embed_batch``.
    Same signature; same retry / back-off logic.
    """
    from plugins.llms.copilot._llm_patch import COPILOT_API_BASE, get_copilot_token

    token = get_copilot_token()
    if not token:
        raise RuntimeError(
            "COPILOT_EMBED: no GitHub token found — set COPILOT_GITHUB_TOKEN, "
            "GH_TOKEN, or log in with `gh auth login`."
        )

    model = os.environ.get("COPILOT_EMBED_MODEL", "text-embedding-3-small")
    dimensions = int(os.environ.get("COPILOT_EMBED_DIMENSIONS", "1536"))

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Copilot-Integration-Id": "vscode-chat",
    }
    url = f"{COPILOT_API_BASE}/embeddings"
    all_vectors: list[list[float]] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for chunk_start in range(0, len(texts), _BATCH_SIZE):
            chunk = texts[chunk_start : chunk_start + _BATCH_SIZE]
            chunk_index = (chunk_start // _BATCH_SIZE) + 1
            total_chunks = ((len(texts) - 1) // _BATCH_SIZE) + 1
            logger.info(
                "CopilotEmbedder: chunk start job_id=%s channel=%s batch=%s chunk=%d/%d size=%d model=%s",
                sync_job_id,
                channel_id,
                batch_num,
                chunk_index,
                total_chunks,
                len(chunk),
                model,
            )
            payload: dict[str, Any] = {
                "model": model,
                "input": chunk,
                "dimensions": dimensions,
            }

            attempt = 0
            while True:
                try:
                    async with _COPILOT_EMBED_LIMITER:
                        response = await client.post(url, headers=headers, json=payload)
                except (
                    httpx.ConnectError,
                    httpx.ReadTimeout,
                    httpx.RemoteProtocolError,
                ) as transient_err:
                    attempt += 1
                    if attempt > _MAX_RETRIES:
                        raise
                    wait = (2**attempt) * (1 + random.uniform(-0.2, 0.2))
                    logger.warning(
                        "CopilotEmbedder: transient %s job_id=%s channel=%s batch=%s chunk=%d/%d retry_in=%.1fs attempt=%d/%d",
                        type(transient_err).__name__,
                        sync_job_id,
                        channel_id,
                        batch_num,
                        chunk_index,
                        total_chunks,
                        wait,
                        attempt,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code in _RETRYABLE_STATUS:
                    attempt += 1
                    if attempt > _MAX_RETRIES:
                        response.raise_for_status()
                    wait = (2**attempt) * (1 + random.uniform(-0.2, 0.2))
                    logger.warning(
                        "CopilotEmbedder: retryable status=%d job_id=%s channel=%s batch=%s chunk=%d/%d retry_in=%.1fs attempt=%d/%d",
                        response.status_code,
                        sync_job_id,
                        channel_id,
                        batch_num,
                        chunk_index,
                        total_chunks,
                        wait,
                        attempt,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()
                for item in data["data"]:
                    all_vectors.append(item["embedding"])
                logger.info(
                    "CopilotEmbedder: chunk done job_id=%s channel=%s batch=%s chunk=%d/%d embedded=%d",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    chunk_index,
                    total_chunks,
                    len(data["data"]),
                )
                break

    return all_vectors


# ---------------------------------------------------------------------------
# Standalone helper (for use outside the agent context, e.g. tests)
# ---------------------------------------------------------------------------


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using the Copilot API. Standalone (no `self` required)."""
    return await _copilot_embed_batch(
        None,  # self — unused in the body
        texts,
        sync_job_id="standalone",
        channel_id="standalone",
        batch_num=0,
    )


# ---------------------------------------------------------------------------
# Patch entry point
# ---------------------------------------------------------------------------


def apply_embed_patches() -> None:
    """Replace ``EmbedderAgent._jina_embed_batch`` with Copilot implementation."""
    from beever_atlas.agents.ingestion.embedder import EmbedderAgent

    EmbedderAgent._jina_embed_batch = _copilot_embed_batch  # type: ignore[method-assign]
    logger.info(
        "CopilotEmbedder: patched EmbedderAgent._jina_embed_batch → GitHub Copilot API (%s, %s-dim)",
        os.environ.get("COPILOT_EMBED_MODEL", "text-embedding-3-small"),
        os.environ.get("COPILOT_EMBED_DIMENSIONS", "1536"),
    )
