"""Monkey-patches that extend beever-atlas's LLM layer with GitHub Models support.

Why monkey-patching?
--------------------
beever-atlas's ``model_resolver`` and LLM ``provider`` modules are designed
for Gemini + Ollama only.  Rather than touching those upstream source files
(which would cause merge conflicts on every rebase), this module patches the
live module objects at import time so the rest of the codebase is unaware.

What gets patched
-----------------
``beever_atlas.llm.model_resolver``
  * Adds ``KNOWN_GITHUB_MODELS`` constant
  * Adds ``is_github_model()`` helper
  * Extends ``resolve_model_object()`` to handle ``github/`` prefixed strings
  * Extends ``validate_model_string()`` to accept ``github/`` prefixed strings

``beever_atlas.llm.provider``
  * Replaces ``_validate_model_resolution()`` so the startup validator handles
    Ollama *and* GitHub models (the upstream version only calls ADK's
    ``LLMRegistry.resolve()``, which only understands Gemini strings).

Re-applying after an upstream update
-------------------------------------
1. Run the tests in the patched state to surface any signature changes.
2. If ``_validate_model_resolution`` changed upstream, update the ``else``
   branch below (the "pure-Gemini path") to mirror the new upstream logic;
   the github/Ollama intercept branches stay the same.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

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


def is_github_model(model_string: str) -> bool:
    """Return True when *model_string* refers to a GitHub Models / Copilot model."""
    return model_string.startswith("github/")


def apply_llm_patches() -> None:
    """Apply all LLM-related monkey-patches to beever-atlas modules."""
    _patch_model_resolver()
    _patch_provider()
    logger.debug("chatgpt_copilot: GitHub Models LLM patches applied to model_resolver + provider")


# ── model_resolver patches ────────────────────────────────────────────────────

def _patch_model_resolver() -> None:
    import beever_atlas.llm.model_resolver as _mr

    # Expose constants / helpers on the module so other code can import them.
    _mr.KNOWN_GITHUB_MODELS = KNOWN_GITHUB_MODELS  # type: ignore[attr-defined]
    _mr.is_github_model = is_github_model  # type: ignore[attr-defined]

    # Extend resolve_model_object ─ prepend a github/ intercept, fall through
    # to the original for everything else.
    _orig_resolve = _mr.resolve_model_object

    def _resolve(model_string: str) -> Any:
        if model_string.startswith("github/"):
            # litellm reads GITHUB_TOKEN from the environment automatically.
            # Set it explicitly from the current env so it's always visible.
            token = os.environ.get("GITHUB_TOKEN", "")
            if token:
                os.environ["GITHUB_TOKEN"] = token
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(model=model_string)
        return _orig_resolve(model_string)

    _mr.resolve_model_object = _resolve

    # Extend validate_model_string ─ accept github/ strings.
    _orig_validate = _mr.validate_model_string

    def _validate(model_string: str) -> str | None:
        if model_string.startswith("github/"):
            return None
        return _orig_validate(model_string)

    _mr.validate_model_string = _validate


# ── provider._validate_model_resolution patch ─────────────────────────────────

def _patch_provider() -> None:
    import beever_atlas.llm.provider as _prov
    from beever_atlas.llm.model_resolver import is_ollama_model, resolve_model_object

    # Keep a reference to the upstream function.  When neither fast nor quality
    # model is a LiteLlm model, we delegate entirely to the original so future
    # upstream changes to validation logic are automatically inherited.
    _orig_validate = _prov._validate_model_resolution

    def _patched_validate_model_resolution(provider: Any) -> None:
        fast_name: str = provider.fast
        quality_name: str = provider.quality

        # If neither tier uses a LiteLlm model, delegate to upstream — this
        # means any upstream changes to the validation logic are inherited
        # automatically for pure-Gemini configurations.
        if not any(
            model.startswith(("ollama_chat/", "github/"))
            for model in (fast_name, quality_name)
        ):
            _orig_validate(provider)
            return

        # At least one tier is LiteLlm-based — validate each tier ourselves
        # so we can handle the mixed Gemini + LiteLlm case correctly.
        from google.adk.models.registry import LLMRegistry

        for tier, model_name in (("fast", fast_name), ("quality", quality_name)):
            if is_ollama_model(model_name) or model_name.startswith("github/"):
                try:
                    resolve_model_object(model_name)
                except Exception as exc:
                    raise RuntimeError(
                        "Invalid LLM config: tier=%s model=%s cannot be resolved. "
                        "Check that litellm>=1.75.5 is installed and the model name is valid."
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
                        "Ensure LiteLLM is installed (litellm>=1.75.5) and model names are valid."
                        % (tier, model_name)
                    ) from exc
                logger.info("LLMProvider: validated tier=%s model=%s", tier, model_name)

    _prov._validate_model_resolution = _patched_validate_model_resolution
