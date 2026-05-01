"""Import ChatGPT conversation history into beever-atlas.

Reads ``chatgpt_history.json`` (produced by the fetch script) and either
shows a dry-run preview or ingests conversations into the full beever-atlas
RAG pipeline (Weaviate + Neo4j + MongoDB).

Usage::

    # Preview what would be imported (no writes, no API keys needed):
    uv run python -m plugins.chatgpt_copilot.chatgpt.importer

    # Import all conversations (requires GOOGLE_API_KEY + JINA_API_KEY + stores running):
    uv run python -m plugins.chatgpt_copilot.chatgpt.importer --ingest

    # Import a single conversation by ID or partial title match:
    uv run python -m plugins.chatgpt_copilot.chatgpt.importer --ingest --conversation "LiPo Battery"

    # Limit to the N most recent conversations:
    uv run python -m plugins.chatgpt_copilot.chatgpt.importer --ingest --limit 5

    # Use a different JSON file:
    uv run python -m plugins.chatgpt_copilot.chatgpt.importer --file path/to/history.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Project root is 3 levels above: plugins/chatgpt_copilot/chatgpt/importer.py
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Load .env before any project imports
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_HISTORY_FILE = _PROJECT_ROOT / "chatgpt_history.json"

CHATGPT_PLATFORM = "chatgpt"
CHATGPT_AUTHOR_USER = "human"
CHATGPT_AUTHOR_AI = "chatgpt"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(ts_str: str | None, fallback: datetime) -> datetime:
    if not ts_str:
        return fallback
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return fallback


def _conv_to_messages(conv: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a single ChatGPT conversation dict into pipeline-ready message dicts."""
    conv_id = conv.get("id", str(uuid.uuid4()))
    title = conv.get("title") or "Untitled ChatGPT conversation"
    created_dt = _parse_timestamp(conv.get("created"), datetime.now(tz=timezone.utc))
    updated_dt = _parse_timestamp(conv.get("updated"), created_dt)

    messages = []
    for i, msg in enumerate(conv.get("messages", [])):
        role = msg.get("role", "unknown")
        text = (msg.get("text") or "").strip()
        if not text or role == "system":
            continue

        author = CHATGPT_AUTHOR_USER if role == "user" else CHATGPT_AUTHOR_AI
        ts_epoch = created_dt.timestamp() + i
        ts_str = f"{int(ts_epoch)}.{i:03d}"

        messages.append({
            "content": text,
            "text": text,
            "author": author,
            "author_name": "User" if role == "user" else "ChatGPT",
            "platform": CHATGPT_PLATFORM,
            "channel_id": conv_id,
            "channel_name": title,
            "message_id": ts_str,
            "ts": ts_str,
            "timestamp": datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat(),
            "thread_id": None,
            "thread_ts": None,
            "attachments": [],
            "reactions": [],
            "reply_count": 0,
            "raw_metadata": {
                "chatgpt_conversation_id": conv_id,
                "chatgpt_conversation_title": title,
                "chatgpt_updated": updated_dt.isoformat(),
            },
        })

    return messages


# ---------------------------------------------------------------------------
# Dry-run display
# ---------------------------------------------------------------------------


def _show_dry_run(conversations: list[dict[str, Any]], limit: int | None) -> None:
    subset = conversations[:limit] if limit else conversations
    total_msgs = sum(len(c.get("messages", [])) for c in subset)

    print(f"\n{'─' * 70}")
    print(f"  ChatGPT History Import — DRY RUN")
    print(f"{'─' * 70}")
    print(f"  Source file    : {DEFAULT_HISTORY_FILE}")
    print(f"  Conversations  : {len(subset)} / {len(conversations)} total")
    print(f"  Messages total : {total_msgs}")
    print(f"  Mode           : preview only (pass --ingest to write)")
    print()

    for conv in subset:
        msgs = conv.get("messages", [])
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        ai_msgs = [m for m in msgs if m.get("role") == "assistant"]
        archived = " [archived]" if conv.get("archived") else ""
        print(f"  [{conv['id'][:8]}] {conv.get('title', 'Untitled')[:60]}{archived}")
        print(f"           {len(msgs)} messages ({len(user_msgs)} user / {len(ai_msgs)} AI)")
        if msgs:
            preview = (msgs[0].get("text") or "")[:90]
            print(f"           First: {preview!r}")
        print()

    print(f"{'─' * 70}")
    print(f"  To import for real, run with:  --ingest")
    print(f"  Requirements:  GOOGLE_API_KEY, JINA_API_KEY, docker compose up")
    print()


# ---------------------------------------------------------------------------
# Actual ingestion
# ---------------------------------------------------------------------------


async def _ingest(
    conversations: list[dict[str, Any]],
    limit: int | None,
    conversation_filter: str | None,
) -> None:
    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm import init_llm_provider
    from beever_atlas.services.batch_processor import BatchProcessor
    from beever_atlas.stores import StoreClients, init_stores

    settings = get_settings()

    subset = conversations
    if conversation_filter:
        needle = conversation_filter.lower()
        subset = [
            c for c in subset
            if needle in c.get("id", "").lower() or needle in c.get("title", "").lower()
        ]
        if not subset:
            print(f"No conversations matching {conversation_filter!r}. Aborting.")
            return
    if limit:
        subset = subset[:limit]

    print(f"\n{'─' * 70}")
    print(f"  ChatGPT History Import — INGESTING")
    print(f"{'─' * 70}")
    print(f"  Conversations to import : {len(subset)}")

    stores = StoreClients.from_settings(settings)
    init_stores(stores)
    await stores.startup()
    init_llm_provider(settings)

    processor = BatchProcessor()
    sync_job_id = f"chatgpt-import-{uuid.uuid4().hex[:8]}"
    total_facts = 0
    total_entities = 0

    for i, conv in enumerate(subset, 1):
        conv_id = conv.get("id", str(uuid.uuid4()))
        title = conv.get("title") or "Untitled"
        pipeline_msgs = _conv_to_messages(conv)

        if not pipeline_msgs:
            print(f"  [{i}/{len(subset)}] Skipping '{title[:50]}' — no messages")
            continue

        print(f"  [{i}/{len(subset)}] Ingesting '{title[:55]}' ({len(pipeline_msgs)} msgs)…")
        result = await processor.process_messages(
            messages=pipeline_msgs,
            channel_id=conv_id,
            channel_name=title,
            sync_job_id=f"{sync_job_id}-{i}",
        )
        total_facts += result.total_facts
        total_entities += result.total_entities
        print(
            f"           ✓ facts={result.total_facts}  entities={result.total_entities}"
            f"  errors={len(result.errors)}"
        )

    await stores.shutdown()
    print()
    print(f"{'─' * 70}")
    print(f"  Done. Total facts: {total_facts}, Total entities: {total_entities}")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--file",
        default=str(DEFAULT_HISTORY_FILE),
        help="Path to chatgpt_history.json (default: project root)",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Actually ingest into beever-atlas (requires running stores + API keys)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the N most recent conversations",
    )
    parser.add_argument(
        "--conversation",
        default=None,
        metavar="FILTER",
        help="Filter: partial conversation ID or title (case-insensitive)",
    )
    args = parser.parse_args()

    history_path = Path(args.file)
    if not history_path.exists():
        raise SystemExit(
            f"History file not found: {history_path}\n"
            "Run the fetch script first: python -m plugins.chatgpt_copilot.chatgpt.fetch"
        )

    conversations: list[dict[str, Any]] = json.loads(history_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(conversations)} conversations from {history_path.name}")

    if args.conversation:
        needle = args.conversation.lower()
        conversations = [
            c for c in conversations
            if needle in c.get("id", "").lower() or needle in c.get("title", "").lower()
        ]
        print(f"Filtered to {len(conversations)} conversations matching {args.conversation!r}")

    if not args.ingest:
        _show_dry_run(conversations, args.limit)
    else:
        asyncio.run(_ingest(conversations, args.limit, conversation_filter=None))


if __name__ == "__main__":
    main()
