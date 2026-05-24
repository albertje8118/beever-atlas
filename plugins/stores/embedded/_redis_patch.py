"""Patch health checks for embedded services to report 'up' (no external servers needed).

When running in fully-embedded mode (WEAVIATE_BACKEND=null, MONGODB_BACKEND=mock,
REDIS_URL=empty), we replace the corresponding health checks with no-op stubs that
always succeed, so the health endpoint reports "healthy" instead of "unhealthy".
"""

from __future__ import annotations

import os


def apply_redis_patch() -> None:
    """Replace all embedded-service health checks with no-op stubs."""
    import beever_atlas.infra.health as _health_mod

    _original_register = _health_mod.register_health_checks

    def _patched_register_health_checks() -> None:
        _original_register()

        registry = _health_mod.health_registry

        async def _noop() -> None:
            """No-op — service replaced by embedded equivalent."""

        if not os.getenv("REDIS_URL"):
            registry.register("redis", _noop, timeout=1.0, critical=False)

        if os.getenv("WEAVIATE_BACKEND", "").lower() == "null":
            registry.register("weaviate", _noop, timeout=1.0, critical=False)

        if os.getenv("MONGODB_BACKEND", "").lower() == "mock":
            registry.register("mongodb", _noop, timeout=1.0, critical=False)

        if os.getenv("GRAPH_BACKEND", "").lower() in ("sqlite", "none") or os.getenv(
            "_SQLITE_GRAPH_OVERRIDE"
        ):
            registry.register("neo4j", _noop, timeout=1.0, critical=False)

    _health_mod.register_health_checks = _patched_register_health_checks

