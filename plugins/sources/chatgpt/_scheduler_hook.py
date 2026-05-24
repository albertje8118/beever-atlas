"""Schedule connection-backed ChatGPT refreshes via APScheduler."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_CHATGPT_JOB_ID = "chatgpt-scheduled-sync"

async def run_chatgpt_sync() -> None:
    """Refresh every configured ChatGPT source and resync its selected conversations."""
    from plugins.sources.chatgpt._service import list_chatgpt_connections, sync_chatgpt_connection

    logger.info("sources.chatgpt: scheduled refresh started")
    connections = await list_chatgpt_connections()
    if not connections:
        logger.debug("sources.chatgpt: no ChatGPT connections configured")
        return

    for conn in connections:
        try:
            await sync_chatgpt_connection(conn.id)
            logger.info("sources.chatgpt: refreshed connection %s", conn.id)
        except (RuntimeError, OSError, ValueError) as exc:
            logger.warning("sources.chatgpt: scheduled refresh failed for %s: %s", conn.id, exc)


# ---------------------------------------------------------------------------
# Hook into SyncScheduler.startup() to register the ChatGPT job
# ---------------------------------------------------------------------------

def apply_chatgpt_scheduler_hook() -> None:
    """Patch SyncScheduler.startup() to register the periodic ChatGPT refresh job."""
    from beever_atlas.services.scheduler import SyncScheduler

    _orig_startup = SyncScheduler.startup

    async def _patched_startup(self) -> None:
        await _orig_startup(self)

        if not getattr(self, "_started", False):
            logger.warning(
                "sources.chatgpt: SyncScheduler did not start; skipping ChatGPT job registration"
            )
            return

        try:
            from apscheduler import ConflictPolicy
            from apscheduler.triggers.interval import IntervalTrigger

            hours = int(os.environ.get("CHATGPT_SYNC_INTERVAL_HOURS", "6"))
            scheduler = getattr(self, "_scheduler", None)
            if scheduler is None:
                logger.warning("sources.chatgpt: scheduler instance missing; skipping ChatGPT job registration")
                return
            await scheduler.add_schedule(
                run_chatgpt_sync,
                IntervalTrigger(hours=hours),
                id=_CHATGPT_JOB_ID,
                conflict_policy=ConflictPolicy.replace,
            )
            logger.info(
                "sources.chatgpt: scheduled sync registered — every %d hour(s) (job_id=%s)",
                hours,
                _CHATGPT_JOB_ID,
            )
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "sources.chatgpt: failed to register scheduled sync: %s", exc, exc_info=True
            )

    SyncScheduler.startup = _patched_startup
