"""End-to-end simulation test — ChatGPT data through all 4 storage engines + Wiki.

Tests the full beever-atlas pipeline using real ChatGPT conversation history,
GitHub Copilot as the LLM, and all four embedded storage engines:
  1. Vector store   (SQLiteVectorStore via Copilot embeddings)
  2. BM25 / FTS5    (built into SQLiteVectorStore)
  3. Graph store    (SQLiteGraphStore)
  4. DocDB          (mongomock_motor + SQLite-backed persistence)

Pipeline stages exercised:
  Preprocessor → FactExtractor → EntityExtractor → Embedder
  → CrossBatchValidator → Persister → ConsolidationService → WikiBuilder

Usage::

    uv run python scripts/e2e_test.py
    uv run python scripts/e2e_test.py --limit 1
    uv run python scripts/e2e_test.py --conversation "LiPo"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

# ── Bootstrap ─────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

# Silence noisy library logs
for _name in (
    "google", "google.adk", "google.genai", "httpx", "httpcore",
    "aiohttp", "grpc", "urllib3", "litellm", "LiteLLM",
    "beever_atlas.services.coreference_resolver",
    "beever_atlas.services.media_processor",
):
    logging.getLogger(_name).setLevel(logging.WARNING)
logging.getLogger("google.adk.runners").setLevel(logging.ERROR)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# Load plugins BEFORE importing project modules (embedded stores + Copilot LLM)
from plugins.loader import load_plugins
load_plugins()

# ── Formatting helpers ─────────────────────────────────────────────────────────

class _C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"
    CYAN = "\033[36m"; MAGENTA = "\033[35m"; BLUE = "\033[34m"


def _hdr(title: str) -> None:
    print(f"\n{_C.BOLD}{_C.CYAN}{'=' * 70}\n  {title}\n{'=' * 70}{_C.RESET}")

def _sub(title: str) -> None:
    print(f"\n  {_C.BOLD}{title}{_C.RESET}")

def _ok(msg: str) -> None:
    print(f"  {_C.GREEN}[OK]{_C.RESET} {msg}")

def _fail(msg: str) -> None:
    print(f"  {_C.RED}[FAIL]{_C.RESET} {msg}")

def _info(msg: str) -> None:
    print(f"  {_C.DIM}|{_C.RESET} {msg}")


# ── Main test ─────────────────────────────────────────────────────────────────

async def run_e2e(limit: int = 2, conversation_filter: str | None = None) -> None:
    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm import init_llm_provider, get_llm_provider
    from beever_atlas.services.batch_processor import BatchProcessor
    from beever_atlas.services.consolidation import ConsolidationService
    from beever_atlas.stores import StoreClients, init_stores, get_stores
    from beever_atlas.wiki.builder import WikiBuilder
    from beever_atlas.wiki.cache import WikiCache
    from plugins.sources.chatgpt.importer import _conv_to_messages

    t_total = time.monotonic()

    # ── 1. Config ──────────────────────────────────────────────────────────────
    _hdr("beever-atlas E2E Test  --  ChatGPT -> 4 Engines -> Wiki")

    settings = get_settings()
    _sub("Configuration")
    _info(f"LLM fast    : {settings.llm_fast_model}")
    _info(f"LLM quality : {settings.llm_quality_model}")
    _info(f"Vector store: {os.environ.get('WEAVIATE_BACKEND', 'weaviate')}")
    _info(f"Graph store : {settings.graph_backend}")
    _info(f"Mongo mode  : {os.environ.get('MONGODB_BACKEND', 'mongodb')}")
    _info(f"Embed model : {os.environ.get('COPILOT_EMBED_MODEL', 'text-embedding-3-small')}")

    # ── 2. Load ChatGPT history ────────────────────────────────────────────────
    _sub("Loading ChatGPT history")
    history_path = _ROOT / "chatgpt_history.json"
    if not history_path.exists():
        _fail(f"chatgpt_history.json not found at {history_path}")
        return

    conversations = json.loads(history_path.read_text(encoding="utf-8"))
    _info(f"Total conversations: {len(conversations)}")

    if conversation_filter:
        needle = conversation_filter.lower()
        conversations = [
            c for c in conversations
            if needle in c.get("id", "").lower() or needle in c.get("title", "").lower()
        ]
        _info(f"After filter '{conversation_filter}': {len(conversations)} conversations")

    subset = conversations[:limit]
    _info(f"Processing: {len(subset)} conversations")

    for i, c in enumerate(subset, 1):
        _info(f"  [{i}] {c.get('title', 'Untitled')[:60]}  ({len(c.get('messages', []))} msgs)")

    # ── 3. Initialize stores and LLM provider ─────────────────────────────────
    _sub("Initializing stores")
    stores = StoreClients.from_settings(settings)
    init_stores(stores)
    await stores.startup()
    init_llm_provider(settings)

    # Debug: log actual store types
    _info(f"vector store type : {type(stores.weaviate).__name__}")
    _info(f"graph store type  : {type(stores.graph).__name__}")
    _info(f"mongo backend     : {type(stores.mongodb).__name__}")

    provider = get_llm_provider()
    _ok(f"fact_extractor → {provider.get_model_string('fact_extractor')}")
    _ok(f"entity_extractor → {provider.get_model_string('entity_extractor')}")
    _ok(f"wiki_compiler → {provider.get_model_string('wiki_compiler')}")
    _ok(f"summarizer → {provider.get_model_string('summarizer')}")

    try:
        # ── 4. Ingestion pipeline ──────────────────────────────────────────────
        _hdr("Phase 1: Ingestion Pipeline (ChatGPT → All Stores)")
        processor = BatchProcessor()

        ingested = []
        for i, conv in enumerate(subset, 1):
            conv_id = conv.get("id", f"test-conv-{i}")
            title = conv.get("title") or "Untitled"
            messages = _conv_to_messages(conv)

            if not messages:
                _info(f"[{i}] Skipping '{title[:50]}' — no messages")
                continue

            _sub(f"[{i}/{len(subset)}] '{title[:55]}' ({len(messages)} messages)")
            t0 = time.monotonic()

            try:
                result = await processor.process_messages(
                    messages=messages,
                    channel_id=conv_id,
                    channel_name=title,
                    sync_job_id=f"e2e-test-{conv_id[:8]}",
                )
                elapsed = time.monotonic() - t0
                _ok(f"facts={result.total_facts}  entities={result.total_entities}"
                    f"  errors={len(result.errors)}  ({elapsed:.1f}s)")

                if result.errors:
                    for err in result.errors[:3]:
                        _info(f"  error: {err[:120]}")

                if result.total_facts > 0:
                    ingested.append((conv_id, title, result.total_facts, result.total_entities))
                else:
                    _info("  0 facts extracted — skipping consolidation for this conv")

            except Exception as exc:
                _fail(f"Ingestion failed: {exc}")
                import traceback
                traceback.print_exc()
                continue

        if not ingested:
            _fail("No conversations were successfully ingested. Aborting.")
            return

        # ── 5. Consolidation (required for wiki generation) ────────────────────
        _hdr("Phase 2: Consolidation (clusters + channel summary)")
        consolidation_svc = ConsolidationService(
            weaviate=stores.weaviate,
            settings=settings,
            graph=stores.graph,
        )

        consolidated = []
        for conv_id, title, n_facts, n_entities in ingested:
            _sub(f"Consolidating '{title[:50]}'")
            t0 = time.monotonic()
            try:
                cr = await consolidation_svc.on_sync_complete(
                    channel_id=conv_id,
                    channel_name=title,
                )
                elapsed = time.monotonic() - t0
                if cr.errors:
                    _info(f"  consolidation errors: {cr.errors[:2]}")
                _ok(
                    f"clusters created={cr.clusters_created}  updated={cr.clusters_updated}"
                    f"  summaries={cr.summaries_generated}  ({elapsed:.1f}s)"
                )
                # Check if a channel summary was generated
                summary = await stores.weaviate.get_channel_summary(conv_id)
                if summary:
                    _ok(f"Channel summary: {(summary.text or '')[:100]}…")
                    consolidated.append((conv_id, title))
                else:
                    _info("  No channel summary yet (too few clusters — trying full reconsolidate)")
                    cr2 = await consolidation_svc.full_reconsolidate(
                        channel_id=conv_id,
                        channel_name=title,
                    )
                    _info(f"  Full reconsolidate: clusters={cr2.clusters_created}  summaries={cr2.summaries_generated}")
                    summary2 = await stores.weaviate.get_channel_summary(conv_id)
                    if summary2:
                        _ok(f"Channel summary (after full): {(summary2.text or '')[:100]}…")
                        consolidated.append((conv_id, title))
                    else:
                        _info("  Still no channel summary — wiki step will be skipped for this conv")

            except Exception as exc:
                _fail(f"Consolidation error: {exc}")
                import traceback
                traceback.print_exc()

        # ── 6. Query all 4 engines ─────────────────────────────────────────────
        _hdr("Phase 3: Query All 4 Storage Engines")

        target_conv_id, target_title = ingested[0][0], ingested[0][1]
        _sub(f"Target channel: '{target_title[:55]}'  ({target_conv_id})")

        # ── 6a. Vector (semantic search) ───────────────────────────────────────
        _sub("6a. Vector Store — Semantic Search")
        # Use title-derived query so the search is relevant regardless of topic
        query_texts = [
            target_title,
            f"key findings about {target_title}",
            "what was decided or recommended",
        ]
        from plugins.llms.copilot._embedder import embed_texts
        for q in query_texts:
            try:
                vecs = await embed_texts([q])
                hits = await stores.weaviate.semantic_search(
                    query_vector=vecs[0],
                    channel_id=target_conv_id,
                    limit=3,
                    threshold=0.2,
                )
                _info(f"  query: '{q[:55]}' → {len(hits)} hits")
                for h in hits[:2]:
                    fact = h.get("fact") if isinstance(h, dict) else h
                    score = h.get("similarity_score") if isinstance(h, dict) else getattr(h, "score", "?")
                    text = (fact.get("memory_text", "") if isinstance(fact, dict) else getattr(fact, "memory_text", "")) or ""
                    score_str = f"{score:.4f}" if isinstance(score, float) else str(score)
                    _info(f"    [{score_str}] {text[:80]}...")
            except Exception as exc:
                _fail(f"Semantic search error: {exc}")

        # ── 6b. BM25 (FTS5 keyword search) ────────────────────────────────────
        _sub("6b. BM25 / FTS5 — Keyword Search")
        bm25_queries = ["how", "error", "using"]
        for q in bm25_queries:
            try:
                hits = await stores.weaviate.bm25_search(
                    query=q,
                    channel_id=target_conv_id,
                    limit=3,
                )
                _info(f"  bm25 query: '{q}' → {len(hits)} hits")
                for h in hits[:2]:
                    text = getattr(h, "text", "") or ""
                    _info(f"    {text[:80]}…")
            except AttributeError:
                _info("  bm25_search not available on this store (skipping)")
            except Exception as exc:
                _fail(f"BM25 error: {exc}")

        # ── 6c. Graph store ────────────────────────────────────────────────────
        _sub("6c. Graph Store — Entities & Relationships")
        try:
            all_entities = await stores.graph.list_entities(channel_id=target_conv_id, limit=50)
            persons = [e for e in all_entities if getattr(e, "type", "").lower() == "person"]
            techs = [e for e in all_entities if getattr(e, "type", "").lower() == "technology"]
            decisions = await stores.graph.get_decisions(target_conv_id)

            _info(f"  total entities : {len(all_entities)}")
            _info(f"  persons        : {len(persons)}")
            _info(f"  technologies   : {len(techs)}")
            _info(f"  decisions      : {len(decisions)}")

            for e in persons[:3]:
                _info(f"    person: {getattr(e, 'name', str(e))}")
            for e in techs[:3]:
                _info(f"    tech: {getattr(e, 'name', str(e))}")
            for e in all_entities[:5]:
                _info(f"    [{getattr(e, 'type', '?')}] {getattr(e, 'name', str(e))[:50]}")

        except Exception as exc:
            _fail(f"Graph query error: {exc}")

        # ── 6d. DocDB (MongoDB mock) ───────────────────────────────────────────
        _sub("6d. DocDB — MongoDB Mock")
        try:
            facts_count = await stores.weaviate.count_facts(target_conv_id)
            clusters = await stores.weaviate.list_clusters(target_conv_id)
            _info(f"  facts in vector store : {facts_count}")
            _info(f"  clusters in docdb     : {len(clusters)}")
            for cl in clusters[:3]:
                label = getattr(cl, "title", None) or getattr(cl, "label", "?")
                member_count = getattr(cl, "member_count", "?")
                _info(f"    cluster '{label}'  members={member_count}")

            # Check MongoDB sync state and outbox
            mongo_state = await stores.mongodb.get_channel_sync_state(target_conv_id)
            _info(f"  channel sync state    : {'found' if mongo_state else 'none'}")

        except Exception as exc:
            _fail(f"DocDB query error: {exc}")

        # ── 7. Wiki generation ─────────────────────────────────────────────────
        _hdr("Phase 4: Wiki Generation (Copilot LLM)")

        if not consolidated:
            _fail("No channels were consolidated — cannot generate wiki")
            _info("Hint: need more facts / clusters. Try --limit 2 with a richer conversation.")
        else:
            wiki_cache = WikiCache(settings.mongodb_uri)
            for conv_id, title in consolidated[:1]:
                _sub(f"Generating wiki for '{title[:50]}'")
                t0 = time.monotonic()
                try:
                    builder = WikiBuilder(
                        weaviate_store=stores.weaviate,
                        graph_store=stores.graph,
                        wiki_cache=wiki_cache,
                    )
                    wiki = await builder.generate_wiki(channel_id=conv_id)
                    elapsed = time.monotonic() - t0
                    _ok(f"Wiki generated in {elapsed:.1f}s")

                    # Print the overview page
                    pages = getattr(wiki, "pages", {})
                    if isinstance(pages, dict):
                        for page_name, page_content in list(pages.items())[:3]:
                            _sub(f"  Wiki page: {page_name}")
                            content = getattr(page_content, "content", None) or str(page_content)
                            print()
                            for line in content.splitlines()[:30]:
                                print(f"    {line}")
                            if len(content.splitlines()) > 30:
                                _info(f"    … ({len(content.splitlines())} lines total)")
                            print()
                    else:
                        _info(f"  wiki.pages type: {type(pages)}")
                        _info(f"  wiki response: {str(wiki)[:300]}")

                except ValueError as exc:
                    if "no consolidated data" in str(exc).lower():
                        _fail(f"Wiki failed: {exc}")
                        _info("Channel summary exists but wiki gatherer still failed.")
                        _info("Check WikiDataGatherer.gather() → get_channel_summary().")
                    else:
                        _fail(f"Wiki error: {exc}")
                        import traceback
                        traceback.print_exc()
                except Exception as exc:
                    _fail(f"Wiki error: {exc}")
                    import traceback
                    traceback.print_exc()

    finally:
        await stores.shutdown()

    # ── Summary ────────────────────────────────────────────────────────────────
    elapsed_total = time.monotonic() - t_total
    _hdr(f"E2E Test Complete  •  Total time: {elapsed_total:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2,
                        help="Max conversations to process (default: 2)")
    parser.add_argument("--conversation", default=None, metavar="FILTER",
                        help="Filter by partial conversation ID or title")
    args = parser.parse_args()
    asyncio.run(run_e2e(limit=args.limit, conversation_filter=args.conversation))


if __name__ == "__main__":
    main()
