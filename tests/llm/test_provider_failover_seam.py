"""Tests for the LLMProvider failover seam (PR-C).

When the global CircuitBreaker is open AND ``LLM_FAILOVER_ENABLED=True``,
``LLMProvider.resolve_model`` must return the configured fallback model
from ``llm_fallback_model_map``. When either condition is False, the
primary model is returned unchanged.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/llm-circuit-breaker/``
→ "Requirement: Provider failover seam in LLMProvider.resolve_model".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from beever_atlas.llm.provider import LLMProvider
from beever_atlas.services.circuit_breaker import (
    CircuitBreaker,
    init_circuit_breaker,
    reset_circuit_breaker_for_tests,
)


def _make_settings(*, failover_enabled: bool, fallback_map: dict[str, str] | None = None):
    """Construct a minimal settings stub for LLMProvider."""
    return SimpleNamespace(
        llm_failover_enabled=failover_enabled,
        llm_fallback_model_map=fallback_map or {"gemini-2.5-pro": "gemini-2.5-flash-lite"},
        llm_fast_model="gemini-2.5-flash",
        llm_quality_model="gemini-2.5-pro",
    )


@pytest.fixture(autouse=True)
def _reset_breaker_singleton():
    reset_circuit_breaker_for_tests()
    yield
    reset_circuit_breaker_for_tests()


@pytest.mark.asyncio
async def test_failover_off_returns_primary_even_when_breaker_open() -> None:
    """Spec scenario: ``Flag OFF, breaker open → primary model returned``.

    Default behavior. The seam is built but not wired until multi-provider
    key management lands in scope; flipping the flag is the explicit opt-in.
    """
    settings = _make_settings(failover_enabled=False)
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    init_circuit_breaker(breaker)
    await breaker.record_failure()
    assert breaker.is_open()

    provider = LLMProvider(settings)
    with patch("beever_atlas.llm.provider.resolve_model_object", side_effect=lambda s: s):
        result = provider.resolve_model("fact_extractor")

    # Primary model returned — not the fallback.
    assert "flash-lite" not in str(result), (
        "with failover disabled, primary model must NOT be re-mapped"
    )


@pytest.mark.asyncio
async def test_failover_on_breaker_open_returns_fallback() -> None:
    """Spec scenario: ``Flag ON, breaker open → fallback model returned``."""
    settings = _make_settings(
        failover_enabled=True,
        fallback_map={"gemini-2.5-flash": "gemini-2.5-flash-lite"},
    )
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    init_circuit_breaker(breaker)
    await breaker.record_failure()
    assert breaker.is_open()

    provider = LLMProvider(settings)
    with patch("beever_atlas.llm.provider.resolve_model_object", side_effect=lambda s: s):
        result = provider.resolve_model("fact_extractor")
    assert "flash-lite" in str(result), "fallback path should re-map to gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_failover_on_breaker_closed_returns_primary() -> None:
    """Spec scenario: ``Flag ON, breaker closed → primary model returned``.

    Flag-on is not the same as 'always fallback' — failover only fires
    when the breaker is actually open. A healthy upstream keeps using
    the primary regardless of the flag.
    """
    settings = _make_settings(failover_enabled=True)
    breaker = CircuitBreaker(threshold=5, cooldown_seconds=60)
    init_circuit_breaker(breaker)
    # Don't trigger any failures.
    assert not breaker.is_open()

    provider = LLMProvider(settings)
    with patch("beever_atlas.llm.provider.resolve_model_object", side_effect=lambda s: s):
        result = provider.resolve_model("fact_extractor")
    assert "flash-lite" not in str(result)


@pytest.mark.asyncio
async def test_failover_swallows_breaker_errors_and_uses_primary() -> None:
    """If the breaker module itself raises, failover must not crash
    resolution — the primary model is returned and a warning logged."""
    settings = _make_settings(failover_enabled=True)
    provider = LLMProvider(settings)

    # Patch get_circuit_breaker to raise.
    with (
        patch(
            "beever_atlas.services.circuit_breaker.get_circuit_breaker",
            side_effect=RuntimeError("breaker module exploded"),
        ),
        patch("beever_atlas.llm.provider.resolve_model_object", side_effect=lambda s: s),
    ):
        # Should not raise — primary is returned.
        result = provider.resolve_model("fact_extractor")
        assert result is not None


@pytest.mark.asyncio
async def test_failover_uses_primary_when_no_fallback_configured() -> None:
    """Model not present in the fallback map → primary returned even
    when both the breaker is open AND the flag is on. We never invent
    fallbacks; missing entries are a deliberate no-op."""
    settings = _make_settings(
        failover_enabled=True,
        fallback_map={"some-other-model": "some-fallback"},
    )
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    init_circuit_breaker(breaker)
    await breaker.record_failure()

    provider = LLMProvider(settings)
    # Configure the agent override to use a model NOT in the fallback map.
    provider._agent_overrides["fact_extractor"] = "gemini-2.5-pro"  # not mapped
    with patch("beever_atlas.llm.provider.resolve_model_object", side_effect=lambda s: s):
        result = provider.resolve_model("fact_extractor")
    assert str(result) == "gemini-2.5-pro"
