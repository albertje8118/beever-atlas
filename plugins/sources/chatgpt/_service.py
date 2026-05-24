"""ChatGPT plugin helpers built on the existing file-source contract."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from beever_atlas.stores import get_stores

from plugins.sources.chatgpt._session import (
    export_history_from_browser,
    fetch_all_stubs,
    fetch_conversations_by_ids,
    get_fetch_progress as session_get_fetch_progress,
    probe_browser_session,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_HISTORY_FILE = _PROJECT_ROOT / "chatgpt_history.json"
_CHATGPT_CONNECTION_PLATFORM = "file"
_CHATGPT_SOURCE_KIND = "chatgpt"


def _parse_timestamp(ts_str: str | None, fallback: datetime) -> datetime:
    if not ts_str:
        return fallback
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, AttributeError):
        return fallback


def _load_history() -> list[dict[str, Any]]:
    if not _HISTORY_FILE.exists():
        return []
    return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))


def _save_history(conversations: list[dict[str, Any]]) -> None:
    _HISTORY_FILE.write_text(
        json.dumps(conversations, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _conversation_summary(conv: dict[str, Any]) -> dict[str, Any]:
    message_count = len([m for m in conv.get("messages", []) if (m.get("text") or "").strip()])
    updated = conv.get("updated") or conv.get("created") or ""
    flags = []
    if conv.get("pinned"):
        flags.append("Pinned")
    if conv.get("archived"):
        flags.append("Archived")
    if conv.get("project_name"):
        flags.append(f"Project: {conv['project_name']}")
    status = " · ".join(flags) if flags else "Active"
    topic = f"{status} · {message_count} messages"
    if updated:
        topic = f"{topic} · updated {updated}"
    return {
        "channel_id": conv.get("id") or str(uuid.uuid4()),
        "name": conv.get("title") or "Untitled ChatGPT conversation",
        "topic": topic,
        "member_count": message_count,
        "is_member": True,
        "pinned": bool(conv.get("pinned")),
        "project_id": conv.get("project_id"),
        "project_name": conv.get("project_name"),
    }


def _conversation_to_docs(conv: dict[str, Any]) -> list[dict[str, Any]]:
    conv_id = conv.get("id") or str(uuid.uuid4())
    title = conv.get("title") or "Untitled ChatGPT conversation"
    created_dt = _parse_timestamp(conv.get("created"), datetime.now(tz=UTC))
    updated_dt = _parse_timestamp(conv.get("updated"), created_dt)
    docs: list[dict[str, Any]] = []

    for index, msg in enumerate(conv.get("messages", [])):
        role = msg.get("role", "unknown")
        text = (msg.get("text") or "").strip()
        if not text or role == "system":
            continue
        ts_epoch = created_dt.timestamp() + index
        timestamp = datetime.fromtimestamp(ts_epoch, tz=UTC)
        docs.append(
            {
                "channel_id": conv_id,
                "message_id": f"{conv_id}:{index}",
                "content": text,
                "author": "human" if role == "user" else "chatgpt",
                "author_name": "User" if role == "user" else "ChatGPT",
                "author_image": None,
                "platform": "file",
                "channel_name": title,
                "timestamp": timestamp,
                "timestamp_iso": timestamp.isoformat(),
                "thread_id": None,
                "attachments": [],
                "reactions": [],
                "reply_count": 0,
                "source": "chatgpt_history",
                "chatgpt_conversation_id": conv_id,
                "chatgpt_conversation_title": title,
                "chatgpt_updated": updated_dt.isoformat(),
                "plugin_source": _CHATGPT_SOURCE_KIND,
            }
        )
    return docs


async def _ensure_history_available(auth_mode: str) -> tuple[list[dict[str, Any]], str]:
    if auth_mode == "browser":
        try:
            conversations = await _export_history_async()
            if conversations:
                _save_history(conversations)
                return conversations, "browser"
        except (RuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("chatgpt service: browser refresh failed: %s", exc)
        cached = _load_history()
        if cached:
            return cached, "cache"
        raise RuntimeError(
            "No authenticated ChatGPT browser session was found and no cached history file exists."
        )

    cached = _load_history()
    if cached:
        return cached, "file"
    raise RuntimeError("chatgpt_history.json was not found. Connect with browser mode or provide a history file.")


async def _export_history_async() -> list[dict[str, Any]]:
    return await export_history_from_browser()


async def fetch_and_cache_history() -> dict[str, Any]:
    """Re-fetch the full conversation stub list from ChatGPT and save to cache.

    Stores lightweight metadata only (title, id, timestamps — no messages),
    so this is fast even for thousands of conversations.  Full message content
    is fetched on-demand during ingestion via export_history_from_browser().
    """
    stubs = await fetch_all_stubs()
    conversations = [
        {
            "id": s.get("id"),
            "title": s.get("title") or "Untitled ChatGPT conversation",
            "created": s.get("create_time"),
            "updated": s.get("update_time"),
            "archived": s.get("_archived", False),
            "pinned": bool(s.get("is_pinned")),
            "project_id": s.get("_project_id"),
            "project_name": s.get("_project_name"),
            "messages": [],
        }
        for s in stubs
    ]
    _save_history(conversations)
    active = sum(1 for c in conversations if not c.get("archived"))
    archived = sum(1 for c in conversations if c.get("archived"))
    pinned = sum(1 for c in conversations if c.get("pinned"))
    in_projects = sum(1 for c in conversations if c.get("project_id"))
    return {
        "total": len(conversations),
        "active": active,
        "archived": archived,
        "pinned": pinned,
        "in_projects": in_projects,
    }


def get_fetch_progress() -> dict[str, Any]:
    """Return current fetch-history progress (fetched count + running flag)."""
    return session_get_fetch_progress()


def _connection_metadata(auth_mode: str, history_source: str) -> dict[str, str]:
    return {
        "plugin_source": _CHATGPT_SOURCE_KIND,
        "auth_mode": auth_mode,
        "history_source": history_source,
        "last_auth_check_at": datetime.now(tz=UTC).isoformat(),
    }


def _is_chatgpt_credentials(credentials: dict[str, Any]) -> bool:
    return str(credentials.get("plugin_source") or "") == _CHATGPT_SOURCE_KIND


def _is_chatgpt_connection(conn, stores) -> bool:
    if conn.platform != _CHATGPT_CONNECTION_PLATFORM:
        return False
    try:
        credentials = stores.platform.decrypt_connection_credentials(conn)
    except (RuntimeError, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _is_chatgpt_credentials(credentials)


async def list_chatgpt_connections(owner_principal_id: str | None = None) -> list[Any]:
    stores = get_stores()
    connections = await stores.platform.list_connections()
    result = [conn for conn in connections if _is_chatgpt_connection(conn, stores)]
    if owner_principal_id is not None:
        result = [
            conn
            for conn in result
            if getattr(conn, "owner_principal_id", None) == owner_principal_id
        ]
    return result


async def get_chatgpt_connection(connection_id: str):
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None or not _is_chatgpt_connection(conn, stores):
        raise RuntimeError(f"Connection {connection_id} is not a ChatGPT source")
    return conn


async def connect_chatgpt_source(
    *,
    display_name: str,
    auth_mode: str,
    owner_principal_id: str,
):
    stores = get_stores()
    _conversations, history_source = await _ensure_history_available(auth_mode)
    metadata = _connection_metadata(auth_mode, history_source)

    existing = next((conn for conn in await list_chatgpt_connections(owner_principal_id)), None)
    if existing is not None:
        updated = await stores.platform.update_connection(
            existing.id,
            display_name=display_name or existing.display_name,
            status="connected",
            error_message=None,
            credentials=metadata,
        )
        return updated or existing

    return await stores.platform.create_connection(
        platform=_CHATGPT_CONNECTION_PLATFORM,
        display_name=display_name or "ChatGPT History",
        credentials=metadata,
        status="connected",
        source="ui",
        owner_principal_id=owner_principal_id,
    )


async def list_available_conversations(connection_id: str) -> list[dict[str, Any]]:
    stores = get_stores()
    conn = await get_chatgpt_connection(connection_id)
    credentials = stores.platform.decrypt_connection_credentials(conn)
    auth_mode = str(credentials.get("auth_mode") or "browser")
    conversations, _history_source = await _ensure_history_available(auth_mode)
    return sorted(
        [_conversation_summary(conv) for conv in conversations],
        key=lambda item: item.get("topic", ""),
        reverse=True,
    )


async def materialize_selected_conversations(
    connection_id: str,
    selected_channels: list[str],
) -> None:
    """Fetch full message content for the selected conversations and write to MongoDB.

    Only downloads messages for the conversations the user actually picked— not
    the entire history cache.  The stub cache (chatgpt_history.json) is used to
    verify the conversation IDs are valid before fetching.
    """
    stores = get_stores()
    conn = await get_chatgpt_connection(connection_id)

    # Verify all selected IDs exist in the stub cache
    cached = _load_history()
    known_ids = {str(c.get("id") or "") for c in cached} if cached else set()
    missing = [cid for cid in selected_channels if cid not in known_ids]
    if missing and cached:
        # Allow unknown IDs if the cache is empty (first run before a Fetch)
        raise RuntimeError(f"Selected ChatGPT conversation(s) not found in local cache: {', '.join(missing)}. Click \"Fetch from ChatGPT\" first.")

    # Fetch full content only for the selected conversations
    conversations = await fetch_conversations_by_ids(selected_channels)
    by_id = {str(conv.get("id") or ""): conv for conv in conversations}

    inaccessible = [channel_id for channel_id in selected_channels if channel_id not in by_id]
    if inaccessible:
        raise RuntimeError(
            "ChatGPT listed the selected conversation(s) but blocked downloading their "
            f"message content: {', '.join(inaccessible)}. The imported token is enough "
            "to list titles, but full message download currently requires a live browser "
            "session that can access each conversation. Open chatgpt.com in your browser, "
            "reconnect the session, then fetch and save again."
        )

    # Fall back to cached stub metadata for any conversation we could not fetch
    stub_by_id = {str(c.get("id") or ""): c for c in (cached or [])}

    await stores.mongodb.db["imported_messages"].create_index([("channel_id", 1), ("timestamp", -1)])

    for channel_id in selected_channels:
        conv = by_id.get(channel_id) or stub_by_id.get(channel_id, {})
        docs = _conversation_to_docs(conv)
        await stores.mongodb.db["imported_messages"].delete_many({"channel_id": channel_id})
        if docs:
            await stores.mongodb.db["imported_messages"].insert_many(docs)
        await stores.mongodb.log_activity(
            event_type="chatgpt_import_started",
            channel_id=channel_id,
            details={
                "channel_name": conv.get("title") or channel_id,
                "connection_id": connection_id,
                "total_messages": len(docs),
                "source": _CHATGPT_SOURCE_KIND,
            },
        )
        await stores.mongodb.update_channel_sync_state(
            channel_id=channel_id,
            last_sync_ts="",
            set_total=len(docs),
        )

    # Write-through: persist imported_messages to SQLite immediately so a
    # crash between now and process-exit does not lose the just-downloaded data.
    if os.getenv("MONGODB_BACKEND", "").lower() == "mock":
        try:
            from plugins.stores.embedded._sqlite_doc import flush_collection
            await flush_collection("beever_atlas", "imported_messages")
        except Exception as exc:  # noqa: BLE001
            logger.warning("chatgpt/_service: write-through flush failed: %s", exc)


async def update_chatgpt_connection_channels(
    connection_id: str,
    selected_channels: list[str],
):
    stores = get_stores()
    conn = await get_chatgpt_connection(connection_id)
    await materialize_selected_conversations(connection_id, selected_channels)
    updated = await stores.platform.update_connection(
        connection_id,
        selected_channels=selected_channels,
    )
    if updated is None:
        raise RuntimeError(f"Connection {connection_id} was not found")

    new_channels = set(selected_channels) - set(conn.selected_channels)
    if new_channels:
        from beever_atlas.api.sync import get_sync_runner

        runner = get_sync_runner()
        for channel_id in new_channels:
            try:
                await runner.start_sync(channel_id, sync_type="full", connection_id=connection_id)
            except ValueError:
                logger.debug("chatgpt service: sync already running for %s", channel_id)
            except (RuntimeError, LookupError, OSError) as exc:
                logger.warning("chatgpt service: failed to trigger sync for %s: %s", channel_id, exc)
    return updated


async def sync_chatgpt_connection(connection_id: str) -> None:
    conn = await get_chatgpt_connection(connection_id)
    if conn.selected_channels:
        await materialize_selected_conversations(connection_id, list(conn.selected_channels))
        from beever_atlas.api.sync import get_sync_runner

        runner = get_sync_runner()
        for channel_id in conn.selected_channels:
            try:
                await runner.start_sync(channel_id, sync_type="full", connection_id=connection_id)
            except ValueError:
                logger.debug("chatgpt service: sync already running for %s", channel_id)
            except (RuntimeError, LookupError, OSError) as exc:
                logger.warning("chatgpt service: failed to sync %s: %s", channel_id, exc)


async def get_chatgpt_status() -> dict[str, Any]:
    history_exists = _HISTORY_FILE.exists()
    conversations = _load_history() if history_exists else []
    browser_status = await _probe_async()
    connections = await list_chatgpt_connections()
    selected_channel_count = sum(len(conn.selected_channels) for conn in connections)
    return {
        "enabled": True,
        "history_file_exists": history_exists,
        "total_conversations": len(conversations),
        "connected_sources": len(connections),
        "selected_conversations": selected_channel_count,
        "browser_available": bool(browser_status.get("browser_available")),
        "browser_authenticated": bool(browser_status.get("authenticated")),
        "browser_reason": browser_status.get("reason"),
        "auth_source": browser_status.get("source"),
    }


async def _probe_async() -> dict[str, object]:
    return await probe_browser_session()