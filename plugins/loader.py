"""Plugin loader — discovers and activates all registered beever-atlas plugins.

Usage::

    from plugins.loader import load_plugins
    load_plugins()

Plugins are activated in order.  Each plugin module must expose an
``activate()`` function.  Failures in individual plugins are logged but do
not prevent other plugins from loading.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Ordered list of plugin module paths to activate at startup.
# Add or remove entries here to enable/disable plugins.
_PLUGINS: list[str] = [
    "plugins.stores.embedded",   # must load first — patches Motor before any beever_atlas import
    "plugins.llms.copilot",      # GitHub Copilot / GitHub Models LLM provider
    "plugins.sources.chatgpt",   # ChatGPT history ingestion + scheduled sync
    "plugins.web",               # web UI extensions: Copilot model picker + ChatGPT source panel
]


def load_plugins() -> None:
    """Import and activate every plugin listed in ``_PLUGINS``."""
    for plugin_path in _PLUGINS:
        try:
            plugin = importlib.import_module(plugin_path)
            if hasattr(plugin, "activate"):
                plugin.activate()
                logger.info("Loaded plugin: %s", plugin_path)
            else:
                logger.warning("Plugin %s has no activate() — skipped", plugin_path)
        except Exception:
            logger.exception("Failed to load plugin: %s", plugin_path)
