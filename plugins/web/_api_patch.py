"""Patches for the plugin web layer."""

from __future__ import annotations

import logging

from fastapi import Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ChatGPTConnectRequest(BaseModel):
    display_name: str = "ChatGPT History"
    auth_mode: str = "browser"


class ChatGPTUpdateChannelsRequest(BaseModel):
    selected_channels: list[str]


# ---------------------------------------------------------------------------
# Patch: extend /api/settings/models/available with Copilot models
# ---------------------------------------------------------------------------

def patch_models_available() -> None:
    """Replace the /available route to include copilot/github model lists."""
    import beever_atlas.api.models as models_module
    from fastapi.routing import APIRoute

    # Remove the existing /available route (injected before app.py imports it).
    # Routes in an APIRouter store the FULL path (prefix + relative), so we
    # match the suffix instead of an exact "/available" string.
    models_module.router.routes = [
        r for r in models_module.router.routes
        if not (isinstance(r, APIRoute) and r.path.endswith("/available"))
    ]

    # Add a replacement endpoint that includes copilot models
    @models_module.router.get("/available")
    async def get_available_models_extended():
        from beever_atlas.llm.ollama import check_ollama_health
        from plugins.llms.copilot._llm_patch import (
            KNOWN_COPILOT_MODELS,
            KNOWN_GITHUB_MODELS,
            get_copilot_token,
        )
        from beever_atlas.llm.model_resolver import KNOWN_GEMINI_MODELS, KNOWN_OLLAMA_MODELS

        health = await check_ollama_health()

        ollama_models: list[str] = []
        if health["connected"]:
            ollama_models = [f"ollama_chat/{m}" for m in health["models"]]
        elif KNOWN_OLLAMA_MODELS:
            ollama_models = [f"ollama_chat/{m}" for m in KNOWN_OLLAMA_MODELS]

        token = get_copilot_token()
        copilot_connected = bool(token)

        return {
            "gemini": KNOWN_GEMINI_MODELS,
            "ollama": ollama_models,
            "ollama_connected": health["connected"],
            "copilot": [f"copilot/{m}" for m in KNOWN_COPILOT_MODELS],
            "github": [f"github/{m}" for m in KNOWN_GITHUB_MODELS],
            "copilot_connected": copilot_connected,
        }

    logger.info("web plugin: /api/settings/models/available patched to include Copilot models")


def patch_channel_messages_route() -> None:
    """Replace the channel-messages route so multiple file-backed sources coexist."""
    import beever_atlas.api.channels as channels_module
    from datetime import datetime

    from fastapi import Depends, HTTPException, Query
    from fastapi.routing import APIRoute

    channels_module.router.routes = [
        route
        for route in channels_module.router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path.endswith("/api/channels/{channel_id}/messages")
        )
    ]

    @channels_module.router.get(
        "/api/channels/{channel_id}/messages",
        response_model=channels_module.MessagesListResponse,
    )
    async def get_channel_messages_patched(
        channel_id: str,
        limit: int = Query(default=50, ge=1, le=500),
        since: str | None = Query(default=None, description="ISO 8601 datetime filter"),
        before: str | None = Query(
            default=None, description="Message ID cursor - fetch messages before this ID"
        ),
        order: str = Query(
            default="desc", description="Sort order: desc (newest first) or asc (oldest first)"
        ),
        connection_id: str | None = Query(default=None),
        principal: channels_module.Principal = Depends(channels_module.require_user),
    ) -> channels_module.MessagesListResponse:
        await channels_module.assert_channel_access(principal, channel_id)
        stores = channels_module.get_stores()
        fetch_source_messages = getattr(channels_module, "_fetch_source_messages")
        detect_platform_from_channel_id = getattr(
            channels_module,
            "_detect_platform_from_channel_id",
        )
        resolve_adapter_for_channel = getattr(channels_module, "_resolve_adapter_for_channel")

        connections = await stores.platform.list_connections()
        source_conn = None
        if connection_id is not None:
            source_conn = next(
                (
                    connection
                    for connection in connections
                    if connection.id == connection_id
                    and connection.platform == "file"
                    and connection.status == "connected"
                    and channel_id in connection.selected_channels
                ),
                None,
            )
        if source_conn is None:
            source_conn = next(
                (
                    connection
                    for connection in connections
                    if connection.platform == "file"
                    and connection.status == "connected"
                    and channel_id in connection.selected_channels
                ),
                None,
            )
        if source_conn is not None:
            return await fetch_source_messages(
                channel_id,
                limit=limit,
                since=since,
                order=order,
            )

        if detect_platform_from_channel_id(channel_id) is None and not connection_id:
            synced_ids = await stores.mongodb.list_synced_channel_ids()
            if channel_id in synced_ids:
                sync_state = await stores.mongodb.get_channel_sync_state(channel_id)
                total = sync_state.total_synced_messages if sync_state else None
                return channels_module.MessagesListResponse(messages=[], total_count=total)

        adapter = await resolve_adapter_for_channel(channel_id, connection_id)

        since_dt = None
        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

        try:
            messages = await adapter.fetch_history(
                channel_id,
                since=since_dt,
                limit=limit,
                before=before,
                order=order,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found") from exc
        except channels_module.BridgeError as exc:
            raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

        response_messages = [
            channels_module.MessageResponse(
                content=message.content,
                author=message.author,
                author_name=message.author_name,
                author_image=message.author_image,
                platform=message.platform,
                channel_id=message.channel_id,
                channel_name=message.channel_name,
                message_id=message.message_id,
                timestamp=message.timestamp.isoformat(),
                thread_id=message.thread_id,
                attachments=message.attachments,
                reactions=message.reactions,
                reply_count=message.reply_count,
                is_bot=message.raw_metadata.get("is_bot", False),
                links=message.raw_metadata.get("links", []),
            )
            for message in messages
        ]
        total_count = None
        try:
            sync_state = await stores.mongodb.get_channel_sync_state(channel_id)
            if sync_state is not None and sync_state.total_synced_messages:
                total_count = sync_state.total_synced_messages
        except RuntimeError:
            pass
        if total_count is None and hasattr(adapter, "fetch_message_count"):
            total_count = await adapter.fetch_message_count(channel_id)
        return channels_module.MessagesListResponse(
            messages=response_messages,
            total_count=total_count,
        )

    logger.info("web plugin: /api/channels/{channel_id}/messages patched for multi-source file channels")


# ---------------------------------------------------------------------------
# Register /api/plugins/chatgpt/* routes
# ---------------------------------------------------------------------------

def register_chatgpt_routes(app) -> None:
    """Add plugin-owned ChatGPT routes to the FastAPI app."""
    from fastapi import APIRouter, Depends, HTTPException, Request

    from beever_atlas.api.connections import ChannelItem, ConnectionResponse, _to_response
    from beever_atlas.infra.auth import require_user
    from plugins.sources.chatgpt._service import (
        connect_chatgpt_source,
        fetch_and_cache_history,
        get_chatgpt_connection,
        get_chatgpt_status,
        get_fetch_progress,
        list_available_conversations,
        list_chatgpt_connections,
        sync_chatgpt_connection,
        update_chatgpt_connection_channels,
    )
    from plugins.sources.chatgpt._session import probe_browser_session, save_token

    router = APIRouter(prefix="/api/plugins/chatgpt", tags=["plugins-chatgpt"])

    @router.get("/status")
    async def chatgpt_status():
        """Return aggregate ChatGPT plugin status."""
        import os

        payload = await get_chatgpt_status()
        payload["sync_interval_hours"] = int(os.environ.get("CHATGPT_SYNC_INTERVAL_HOURS", "6"))
        return payload

    @router.get("/connections", response_model=list[ConnectionResponse])
    async def list_chatgpt_plugin_connections(principal=Depends(require_user)):
        owner_id = getattr(principal, "id", None) or str(principal)
        connections = await list_chatgpt_connections(owner_id)
        return [_to_response(conn) for conn in connections]

    @router.post("/connect", response_model=ConnectionResponse)
    async def connect_chatgpt(body: ChatGPTConnectRequest, principal=Depends(require_user)):
        owner_id = getattr(principal, "id", None) or str(principal)
        if body.auth_mode not in {"browser", "file_only"}:
            raise HTTPException(status_code=422, detail="auth_mode must be 'browser' or 'file_only'")
        try:
            conn = await connect_chatgpt_source(
                display_name=body.display_name.strip() or "ChatGPT History",
                auth_mode=body.auth_mode,
                owner_principal_id=owner_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _to_response(conn)

    @router.get("/connections/{connection_id}/channels", response_model=list[ChannelItem])
    async def list_chatgpt_channels(connection_id: str, principal=Depends(require_user)):
        owner_id = getattr(principal, "id", None) or str(principal)
        conn = await get_chatgpt_connection(connection_id)
        if getattr(conn, "owner_principal_id", None) != owner_id:
            raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")
        raw_channels = await list_available_conversations(connection_id)
        return [
            ChannelItem(
                channel_id=channel["channel_id"],
                name=channel["name"],
                is_member=channel.get("is_member", True),
                member_count=channel.get("member_count"),
                topic=channel.get("topic"),
            )
            for channel in raw_channels
        ]

    @router.put("/connections/{connection_id}/channels", response_model=ConnectionResponse)
    async def update_chatgpt_channels(
        connection_id: str,
        body: ChatGPTUpdateChannelsRequest,
        principal=Depends(require_user),
    ):
        owner_id = getattr(principal, "id", None) or str(principal)
        conn = await get_chatgpt_connection(connection_id)
        if getattr(conn, "owner_principal_id", None) != owner_id:
            raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")
        try:
            updated = await update_chatgpt_connection_channels(connection_id, body.selected_channels)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _to_response(updated)

    @router.post("/connections/{connection_id}/sync")
    async def sync_chatgpt_source(connection_id: str, principal=Depends(require_user)):
        owner_id = getattr(principal, "id", None) or str(principal)
        conn = await get_chatgpt_connection(connection_id)
        if getattr(conn, "owner_principal_id", None) != owner_id:
            raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")
        await sync_chatgpt_connection(connection_id)
        return {"status": "started", "message": "ChatGPT refresh queued"}

    @router.post("/connections/{connection_id}/fetch-history")
    async def fetch_chatgpt_history(connection_id: str, principal=Depends(require_user)):
        """Re-fetch the full conversation list from the browser and update local cache.

        Does NOT trigger ingestion — only refreshes chatgpt_history.json so the
        conversation picker shows up-to-date conversations.
        """
        owner_id = getattr(principal, "id", None) or str(principal)
        conn = await get_chatgpt_connection(connection_id)
        if getattr(conn, "owner_principal_id", None) != owner_id:
            raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")
        try:
            summary = await fetch_and_cache_history()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", **summary}

    @router.get("/auth-status")
    async def chatgpt_auth_status(principal=Depends(require_user)):
        """Probe the current ChatGPT session by reading browser cookies.

        Possible values for ``authenticated``:
        - ``true``  — valid session found in the user's installed browser
        - ``false`` — not logged in or session expired (open chatgpt.com)
        """
        return await probe_browser_session()

    @router.get("/fetch-progress")
    async def chatgpt_fetch_progress(principal=Depends(require_user)):
        """Return the current fetch-history progress (live count while a fetch is running)."""
        return get_fetch_progress()

    app.include_router(router, dependencies=[Depends(require_user)])



    @app.post("/api/plugins/chatgpt/import-token", tags=["plugins-chatgpt"])
    async def import_chatgpt_token(request: Request):  # noqa: F811
        """Receive an access token from the frontend overlay (JSON, no auth required)."""
        from fastapi.responses import JSONResponse
        try:
            body = await request.json()
            token = (body.get("token") or "").strip()
            if not token:
                return JSONResponse({"detail": "No token received. Please copy the full page content from chatgpt.com/api/auth/session and try again."}, status_code=400)
            if len(token.split(".")) < 3:
                return JSONResponse({"detail": "This doesn't look like a ChatGPT session token. Make sure you copied the full JSON from chatgpt.com/api/auth/session."}, status_code=400)
            save_token(token)
            return JSONResponse({"status": "ok"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"detail": str(exc)}, status_code=500)

    logger.info("web plugin: /api/plugins/chatgpt routes registered")
