"""Monkey-patches that extend beever-atlas's LLM layer with GitHub Copilot API support.

Why monkey-patching?
--------------------
beever-atlas's ``model_resolver`` and LLM ``provider`` modules are designed
for Gemini + Ollama only.  Rather than touching those upstream source files
(which would cause merge conflicts on every rebase), this module patches the
live module objects at import time so the rest of the codebase is unaware.

What gets patched
-----------------
``beever_atlas.llm.model_resolver``
  * Adds ``KNOWN_COPILOT_MODELS`` / ``KNOWN_GITHUB_MODELS`` constants
  * Adds ``is_copilot_model()`` / ``is_github_model()`` helpers
  * Extends ``resolve_model_object()`` to handle ``copilot/`` and ``github/``
    prefixed strings
  * Extends ``validate_model_string()`` to accept both prefixes

``beever_atlas.llm.provider``
  * Replaces ``_validate_model_resolution()`` so the startup validator handles
    Ollama, GitHub Models, and GitHub Copilot API models.

Model prefixes
--------------
``copilot/<model>``  — Uses the GitHub Copilot API (api.githubcopilot.com).
                       Authenticated via ``COPILOT_GITHUB_TOKEN`` env var, or
                       ``GH_TOKEN``, or automatically from ``gh auth token``.
                       No separate PAT needed — your existing ``gh`` CLI login
                       is sufficient.  Lists all models via /models endpoint.

``github/<model>``   — Uses the GitHub Models API (models.inference.ai.azure.com)
                       via litellm's built-in routing.  Requires a ``GITHUB_TOKEN``
                       PAT in the environment.

Re-applying after an upstream update
-------------------------------------
1. Run the tests to surface any signature changes.
2. If ``_validate_model_resolution`` changed upstream, update the ``else``
   branch below (the "pure-Gemini path") to mirror the new upstream logic;
   the copilot/github/ollama intercept branches stay the same.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# GitHub Copilot API endpoint (OpenAI-compatible)
COPILOT_API_BASE = "https://api.githubcopilot.com"
GITHUB_MODELS_API_BASE = "https://models.github.ai/inference"

# Models available via the GitHub Copilot API (``copilot/<model>``).
# Fetched live via get_copilot_models(); this list is a fallback for validation.
# Source: GET https://api.githubcopilot.com/models (requires Copilot subscription)
KNOWN_COPILOT_MODELS: list[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5-mini",
    "gpt-5.4-mini",
    "claude-sonnet-4",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "claude-opus-4.5",
    "claude-opus-4.7",
    "claude-haiku-4.5",
    "gemini-2.5-pro",
]

# Public GitHub Models available via ``github/<name>`` litellm routing.
# See https://github.com/marketplace/models for the full catalogue.
KNOWN_GITHUB_MODELS: list[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "o1",
    "o1-mini",
    "o3-mini",
    "Meta-Llama-3.1-405B-Instruct",
    "Meta-Llama-3.1-70B-Instruct",
    "Mistral-large-2407",
    "Phi-3.5-MoE-instruct",
    "claude-3-5-sonnet",
]


def get_copilot_token() -> str:
    """Resolve the GitHub token for the Copilot API.

    Priority order (mirrors the official Copilot SDK):
    1. ``COPILOT_GITHUB_TOKEN`` env var
    2. ``GH_TOKEN`` env var
    3. ``GITHUB_TOKEN`` env var
    4. ``gh auth token`` CLI output (requires ``gh`` to be installed and logged in)

    Returns an empty string if no token can be found.
    """
    for env_var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(env_var, "").strip()
        if token:
            return token
    # Fall back to the GitHub CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
        token = result.stdout.strip()
        if token:
            logger.debug("chatgpt_copilot: obtained Copilot token from gh CLI")
            return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def require_copilot_token() -> str:
    """Return a usable Copilot token or raise a clear startup/runtime error."""
    token = get_copilot_token()
    if token:
        return token
    raise RuntimeError(
        "GitHub Copilot token not available. Set COPILOT_GITHUB_TOKEN or run the app "
        "through plugins/dev.ps1 after `gh auth login`."
    )


def get_copilot_models() -> list[str]:
    """Fetch available model IDs from the GitHub Copilot API.

    Returns the fallback ``KNOWN_COPILOT_MODELS`` list if the API call fails.
    """
    import urllib.request
    import json

    token = get_copilot_token()
    if not token:
        logger.warning("chatgpt_copilot: no token found — cannot fetch Copilot models")
        return KNOWN_COPILOT_MODELS

    req = urllib.request.Request(
        f"{COPILOT_API_BASE}/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return [m["id"] for m in data.get("data", [])]
    except Exception as exc:
        logger.warning("chatgpt_copilot: failed to fetch Copilot models: %s", exc)
        return KNOWN_COPILOT_MODELS


def is_copilot_model(model_string: str) -> bool:
    """Return True when *model_string* uses the ``copilot/`` prefix."""
    return model_string.startswith("copilot/")


def is_github_model(model_string: str) -> bool:
    """Return True when *model_string* uses the ``github/`` prefix (GitHub Models API)."""
    return model_string.startswith("github/")


def apply_llm_patches() -> None:
    """Apply all LLM-related monkey-patches to beever-atlas modules."""
    _patch_litellm_client_strip_response_format()
    _patch_model_resolver()
    _patch_provider()
    _redirect_gemini_to_copilot()
    _patch_embedder_for_copilot()
    logger.debug(
        "chatgpt_copilot: GitHub Copilot + GitHub Models LLM patches applied"
    )


# ── LiteLLMClient global patch: strip response_format for Copilot ────────────

def _patch_litellm_client_strip_response_format() -> None:
    """Patch LiteLLMClient.acompletion to fix Copilot API compatibility.

    Two issues fixed:
    1. ``response_format`` — Copilot API returns HTTP 403 when
       ``response_format={"type": "json_object"}`` is sent.  Only ``json_object``
       is blocked; ``json_schema`` (which ADK uses for openai/* models with
       output_schema) is supported and preserved.
    2. Concurrency limit — Copilot API returns HTTP 403 when too many requests
       are in-flight simultaneously.  A semaphore (max 3 concurrent) prevents
       this and retry-with-backoff handles transient 403/429 errors.
    """
    from google.adk.models.lite_llm import LiteLLMClient
    import asyncio
    import time
    import threading

    _copilot_semaphore: list = []  # lazy-init holder
    _copilot_sync_semaphore: list = []

    def _get_sem() -> asyncio.Semaphore:
        if not _copilot_semaphore:
            _copilot_semaphore.append(asyncio.Semaphore(3))
        return _copilot_semaphore[0]

    def _get_sync_sem() -> threading.BoundedSemaphore:
        if not _copilot_sync_semaphore:
            _copilot_sync_semaphore.append(threading.BoundedSemaphore(3))
        return _copilot_sync_semaphore[0]

    def _strip_json_object_format(kwargs: dict) -> None:
        """Remove response_format only when it is the forbidden json_object type."""
        rf = kwargs.get("response_format")
        if rf is not None:
            rf_type = rf.get("type") if isinstance(rf, dict) else None
            if rf_type == "json_object":
                logger.debug(
                    "Copilot patch: stripping response_format=json_object "
                    "(model=%s) — Copilot API returns 403 for this format",
                    kwargs.get("model", "?"),
                )
                kwargs.pop("response_format")

    _orig = LiteLLMClient.acompletion
    _orig_sync = LiteLLMClient.completion

    async def _patched(self, model, messages, tools, **kwargs):
        if kwargs.get("api_base") == COPILOT_API_BASE:
            _strip_json_object_format(kwargs)
            async with _get_sem():
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        return await _orig(self, model, messages, tools, **kwargs)
                    except Exception as exc:
                        _s = str(exc)
                        if ("403" in _s or "forbidden" in _s.lower() or "429" in _s) and attempt < max_retries - 1:
                            wait = (2 ** attempt) + __import__("random").uniform(0, 1)
                            logger.warning(
                                "Copilot API error (attempt %d/%d), retrying in %.1fs",
                                attempt + 1, max_retries, wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        raise
        return await _orig(self, model, messages, tools, **kwargs)

    def _patched_sync(self, model, messages, tools, stream=False, **kwargs):
        if kwargs.get("api_base") == COPILOT_API_BASE:
            _strip_json_object_format(kwargs)
            semaphore = _get_sync_sem()
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    acquired = semaphore.acquire(blocking=False)
                    if not acquired:
                        # Keep sync callers under the same Copilot concurrency cap.
                        time.sleep(0.1)
                        continue
                    try:
                        return _orig_sync(
                            self,
                            model,
                            messages,
                            tools,
                            stream=stream,
                            **kwargs,
                        )
                    finally:
                        semaphore.release()
                except Exception as exc:
                    _s = str(exc)
                    if ("403" in _s or "forbidden" in _s.lower() or "429" in _s) and attempt < max_retries - 1:
                        wait = (2 ** attempt) + __import__("random").uniform(0, 1)
                        logger.warning(
                            "Copilot API error (sync attempt %d/%d), retrying in %.1fs",
                            attempt + 1, max_retries, wait,
                        )
                        time.sleep(wait)
                        continue
                    raise
            raise RuntimeError("Copilot sync completion could not acquire concurrency slot")
        return _orig_sync(self, model, messages, tools, stream=stream, **kwargs)

    LiteLLMClient.acompletion = _patched
    LiteLLMClient.completion = _patched_sync
    logger.info("LLM patch: LiteLLMClient.acompletion patched to strip response_format for Copilot")




def _patch_model_resolver() -> None:
    import beever_atlas.llm.model_resolver as _mr

    # Expose constants / helpers on the module so other code can import them.
    _mr.KNOWN_COPILOT_MODELS = KNOWN_COPILOT_MODELS  # type: ignore[attr-defined]
    _mr.KNOWN_GITHUB_MODELS = KNOWN_GITHUB_MODELS  # type: ignore[attr-defined]
    _mr.is_copilot_model = is_copilot_model  # type: ignore[attr-defined]
    _mr.is_github_model = is_github_model  # type: ignore[attr-defined]
    _mr.get_copilot_token = get_copilot_token  # type: ignore[attr-defined]
    _mr.get_copilot_models = get_copilot_models  # type: ignore[attr-defined]

    _orig_resolve = _mr.resolve_model_object

    def _resolve(model_string: str) -> Any:
        if model_string.startswith("copilot/"):
            model_id = model_string[len("copilot/"):]
            token = require_copilot_token()
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(
                model=f"openai/{model_id}",
                api_base=COPILOT_API_BASE,
                api_key=token,
                extra_headers={"Editor-Version": "copilot-cli/1.0.0"},
                drop_params=True,
            )
        if model_string.startswith("github/"):
            model_id = model_string[len("github/"):]
            token = require_copilot_token()
            os.environ.setdefault("GITHUB_TOKEN", token)
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(
                model=f"openai/{model_id}",
                api_base=GITHUB_MODELS_API_BASE,
                api_key=token,
            )
        return _orig_resolve(model_string)

    _mr.resolve_model_object = _resolve

    # Also patch the local reference in provider.py (imported by name, not via module).
    import beever_atlas.llm.provider as _prov
    _prov.resolve_model_object = _resolve

    _orig_validate = _mr.validate_model_string

    def _validate(model_string: str) -> str | None:
        if model_string.startswith(("copilot/", "github/")):
            return None
        return _orig_validate(model_string)

    _mr.validate_model_string = _validate


# ── provider._validate_model_resolution patch ─────────────────────────────────

def _patch_provider() -> None:
    import beever_atlas.llm.provider as _prov
    from beever_atlas.llm.model_resolver import is_ollama_model, resolve_model_object

    _orig_validate = _prov._validate_model_resolution

    _LITELLM_PREFIXES = ("ollama_chat/", "github/", "copilot/")

    def _patched_validate_model_resolution(provider: Any) -> None:
        fast_name: str = provider.fast
        quality_name: str = provider.quality

        # If neither tier uses a LiteLlm model, delegate to upstream.
        if not any(
            model.startswith(_LITELLM_PREFIXES)
            for model in (fast_name, quality_name)
        ):
            _orig_validate(provider)
            return

        from google.adk.models.registry import LLMRegistry

        for tier, model_name in (("fast", fast_name), ("quality", quality_name)):
            if model_name.startswith(_LITELLM_PREFIXES):
                try:
                    resolve_model_object(model_name)
                except Exception as exc:
                    raise RuntimeError(
                        "Invalid LLM config: tier=%s model=%s cannot be resolved. "
                        "Check litellm>=1.75.5 is installed and the model name is valid."
                        % (tier, model_name)
                    ) from exc
                logger.info(
                    "LLMProvider: validated tier=%s model=%s (plugin:LiteLlm)", tier, model_name
                )
            else:
                try:
                    LLMRegistry.resolve(model_name)
                except Exception as exc:
                    raise RuntimeError(
                        "Invalid LLM config: tier=%s model=%s cannot be resolved by ADK. "
                        "Ensure the model name is a valid Gemini model string."
                        % (tier, model_name)
                    ) from exc
                logger.info("LLMProvider: validated tier=%s model=%s", tier, model_name)

    _prov._validate_model_resolution = _patched_validate_model_resolution


# ── Gemini → Copilot redirect (when GOOGLE_API_KEY is absent) ─────────────────

def _redirect_gemini_to_copilot() -> None:
    """Redirect Gemini model names to Copilot equivalents when no GOOGLE_API_KEY is set.

    When ``GOOGLE_API_KEY`` is not set (fully embedded / Copilot-only mode),
    the default ``DEFAULT_AGENT_MODELS`` map (all ``gemini-*``) causes every ADK
    agent call to fail.  Mutating the dict in-place redirects them to the
    Copilot fast model instead, so the existing priority chain in
    ``LLMProvider.resolve_model()`` naturally picks up the right model.
    """
    import os
    if os.getenv("GOOGLE_API_KEY"):
        return  # Gemini is available — no redirect needed

    import beever_atlas.llm.model_resolver as _mr
    from beever_atlas.infra.config import get_settings

    settings = get_settings()
    fast_model = settings.llm_fast_model  # e.g. "copilot/gpt-5-mini"

    # Mutate the shared dict so all importers (provider.py, tests, etc.) see it.
    for agent_name, model in list(_mr.DEFAULT_AGENT_MODELS.items()):
        if model.startswith("gemini-"):
            _mr.DEFAULT_AGENT_MODELS[agent_name] = fast_model

    logger.info(
        "LLM patch: GOOGLE_API_KEY absent — redirected all gemini-* agent models to '%s'",
        fast_model,
    )


# ── Embedder patch: Copilot embeddings when JINA_API_KEY is absent ────────────

def _patch_embedder_for_copilot() -> None:
    """Replace EmbedderAgent._jina_embed_batch with a Copilot-backed version.

    When ``JINA_API_KEY`` is not set, the Jina embedder sends an empty Bearer
    token causing ``LocalProtocolError``.  This patch transparently reroutes
    embedding calls to GitHub Copilot's OpenAI-compatible embeddings endpoint
    (``/embeddings``), which accepts the same ``gh auth token``.
    """
    import os
    if os.getenv("JINA_API_KEY"):
        return  # Jina key available — no redirect needed

    from beever_atlas.agents.ingestion.embedder import EmbedderAgent

    _copilot_embed_url = f"{COPILOT_API_BASE}/embeddings"
    _copilot_embed_model = "text-embedding-3-small"

    async def _copilot_jina_embed_batch(
        self,
        texts: list,
        *,
        sync_job_id: str = "unknown",
        channel_id: str = "unknown",
        batch_num=1,
    ) -> list:
        import httpx
        token = get_copilot_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Editor-Version": "copilot-cli/1.0.0",
        }
        all_vectors: list = []
        _batch = 100
        async with httpx.AsyncClient(timeout=60.0) as client:
            for start in range(0, len(texts), _batch):
                chunk = texts[start: start + _batch]
                logger.info(
                    "EmbedderAgent(copilot): embedding %d texts job_id=%s",
                    len(chunk), sync_job_id,
                )
                resp = await client.post(
                    _copilot_embed_url,
                    headers=headers,
                    json={"model": _copilot_embed_model, "input": chunk},
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data["data"]:
                    all_vectors.append(item["embedding"])
        return all_vectors

    EmbedderAgent._jina_embed_batch = _copilot_jina_embed_batch
    logger.info(
        "LLM patch: JINA_API_KEY absent — EmbedderAgent redirected to Copilot embeddings (%s)",
        _copilot_embed_url,
    )


