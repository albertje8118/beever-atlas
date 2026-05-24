"""Web plugin entrypoint for backend route patches and plugin-owned UI overlays."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def activate() -> None:
    """Apply backend patches and register plugin routes."""
    from plugins.web._api_patch import patch_channel_messages_route, patch_models_available

    # Patch the models/available endpoint BEFORE app.py imports the router
    patch_models_available()
    patch_channel_messages_route()

    # Register chatgpt source routes AFTER the FastAPI app is available.
    # We defer import so app.py finishes building before we call include_router.
    _register_routes_deferred()


def _register_routes_deferred() -> None:
    """Import the FastAPI app and attach plugin routes.

    By the time this runs (plugin activation, before uvicorn starts serving),
    app.py has NOT yet been imported.  We import it here — which triggers full
    app construction — so all upstream routers are in place.  The chatgpt
    router is then appended last.  This is safe because no stores or async I/O
    are touched at import time; everything requiring running stores lives inside
    the ``lifespan`` context that uvicorn manages separately.
    """
    try:
        from beever_atlas.server.app import app
        from plugins.web._api_patch import register_chatgpt_routes

        register_chatgpt_routes(app)
    except (ImportError, RuntimeError, AttributeError):
        logger.exception("web plugin: failed to register chatgpt routes (non-fatal)")
