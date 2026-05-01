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
    _patch_model_resolver()
    _patch_provider()
    logger.debug(
        "chatgpt_copilot: GitHub Copilot + GitHub Models LLM patches applied"
    )


# ── model_resolver patches ────────────────────────────────────────────────────

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
            token = get_copilot_token()
            from google.adk.models.lite_llm import LiteLlm
            # Use litellm's openai/ provider with the Copilot API base URL.
            # The gho_ / ghu_ / github_pat_ token works directly — no exchange needed.
            return LiteLlm(
                model=f"openai/{model_id}",
                api_base=COPILOT_API_BASE,
                api_key=token or "no-token",
            )
        if model_string.startswith("github/"):
            # litellm reads GITHUB_TOKEN from environment automatically.
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(model=model_string)
        return _orig_resolve(model_string)

    _mr.resolve_model_object = _resolve

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
