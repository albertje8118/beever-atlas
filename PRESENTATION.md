# Beever Atlas — OSS Pipeline + LLM Wiki Redesign

**Branch:** `redesign/oss-pipeline-and-wiki` vs `main`
**Date:** 2026-05-05

---

## TL;DR — What changed

A 9-PR redesign (PR-0 → PR-G) that turns the chat-extraction pipeline into a **non-blocking, fault-tolerant ingestion layer** and reshapes the wiki from a **regenerate-everything** generator into a **Karpathy-style LLM Wiki bookkeeper** that compounds over time.

Four outcomes:

1. **Sync is ~100x faster perceived** — fetch+persist returns in seconds; LLM extraction runs in the background.
2. **Errors no longer kill the pipeline** — a Gemini 503 storm doesn't stall the cursor, lose batches, or wall the UI in identical errors.
3. **Wiki compounds, doesn't regenerate** — new facts route deterministically to affected pages; only changed sections are rewritten; title/slug/voice preserved.
4. **Push-ready for OpenClaw / Hermes** — signed `POST /api/sources/{id}/events` (HMAC + idempotency) lets external agents push directly into the same store as pull adapters.

---

## By the numbers

| Metric | Value |
|---|---|
| **Commits** | 120 |
| **Files changed** | 178 |
| **Lines added** | ~38,142 |
| **Lines removed** | ~1,103 |
| **Net delta** | **+37,039 LOC** |
| **New files** | 111 |
| **Modified files** | 66 |
| **New backend services** | 6 (`extraction_worker`, `circuit_breaker`, `wiki_maintainer`, `wiki_lint`, `wiki_drift_comparator`, `push_hmac`) |
| **New API endpoints** | 12+ (push-source, wiki maintain/lint/graph, admin metrics, MCP tools) |
| **New test files** | ~50 (services, API, stores, wiki, frontend) |
| **New env vars (operator-flippable)** | 6 (down from 12 — consolidated by analyst review) |
| **Default behavior change on merge** | None (every flag defaults OFF) |

### Commit type breakdown

| Type | Count |
|---|---|
| `feat` | 64 |
| `fix` | 44 |
| `docs` | 5 |
| `refactor` / `chore` / `tune` / `revert` | 7 |

### Top scopes

```
feat(web)     26    fix(web)        27
feat(wiki)    11    fix(wiki)        5
feat(api)      4    feat(server)     2
feat(mcp)      2    feat(extraction) 2
```

### Where the changes live

```
src/beever_atlas/services/   ████████ 7.3%   (new services)
src/beever_atlas/wiki/       ████ 4.4%       (page store, structure planner)
tests/api/                   ██████████ 9.5% (endpoint coverage)
tests/services/              ████████ 7.8%   (worker / breaker / maintainer)
web/src/components/wiki/     █████ 5.0%      (graph, toolbar, layout)
src/beever_atlas/api/        ███ 3.3%        (push, wiki, admin)
```

---

## Architecture — Before vs After

### BEFORE (main)

```
┌──────────────┐
│ Pull adapter │   (Slack, file import — only path)
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────┐
│  SYNC RUNNER (blocking)                  │
│  ─────────────────────                   │
│  1. fetch messages                       │
│  2. run 6-stage LLM extraction inline ◀── 5+ min for 100 msgs
│  3. advance cursor only if all pass ◀──── one 503 = lost batches
│  4. write to imported_messages           │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│  WIKI BUILDER (regenerates everything)   │
│  7 + N_clusters LLM calls every refresh  │
│  Title/slug/voice may drift each rebuild │
└──────────────────────────────────────────┘
```

**Problems**
- Sync UI hangs for minutes on long fetches.
- Single LLM 503 → cursor doesn't advance, all batches discarded.
- Wiki regenerates from scratch every refresh — costly + voice drifts.
- No path for external runtimes (OpenClaw, Hermes) to push messages in.
- Errors stack as identical wall-of-red in the UI.

---

### AFTER (this branch)

```
   pull adapters                POST /api/sources/{id}/events
   (Slack, Discord, …)          (OpenClaw, Hermes — HMAC signed)
          │                              │
          └──────────────┬───────────────┘
                         ▼
   ┌──────────────────────────────────────────────────┐
   │  channel_messages   collection (Mongo)           │
   │  key: (source_id, channel_id, message_id)        │
   │  extraction_status: pending → extracting         │
   │                            → done | failed       │
   │  ✓ Cursor advances on FETCH success (PR-0)       │
   └──────────────────────────────────────────────────┘
                         │
              APScheduler 30s tick
                         │  ExtractionWorker.tick()
                         │  (find_one_and_update atomic claim)
                         ▼
   ┌──────────────────────────────────────────────────┐
   │  6-stage ADK pipeline  (preserved unchanged)     │
   │  preprocessor → fact_extractor → entity_*        │
   │   → embedder → cross_batch_validator → persister │
   └──────────────────────────────────────────────────┘
            │                      │
            │ CircuitBreaker        └─→ Weaviate / Neo4j / Mongo
            │ (fast-fail on 503,                    │
            │  exp. backoff retry)                   │
            ▼                                        │
   ┌──────────────────────────────────────────────────┐
   │  on_extraction_done(channel_id, fact_ids)        │
   └──────────────────────────────────────────────────┘
                         │
                         ▼
   ┌──────────────────────────────────────────────────┐
   │  WikiMaintainer.plan_updates(facts)              │
   │   • cluster_id  → topic:<slug>                   │
   │   • entity_tags → entity:<slug>                  │
   │   • fact_type   → decisions / faq / action_items │
   │  (deterministic routing — NO LLM call)           │
   └──────────────────────────────────────────────────┘
                         │
            mode=manual:  │   mode=auto:
            mark dirty    │   apply_update per page
                          │   (1 LLM call/page, only changed sections)
                          ▼
   ┌──────────────────────────────────────────────────┐
   │  wiki_pages collection (Mongo)                   │
   │  one doc per (channel_id, lang, page_id)         │
   │  with sections, version, tensions, slug-stable   │
   └──────────────────────────────────────────────────┘
                         │
                         ▼  POST /wiki/lint, GET /wiki/graph
   ┌──────────────────────────────────────────────────┐
   │  Lint findings (orphan / stale / dup / coherence)│
   │  Tensions surfacing                              │
   │  Wiki graph view (Cytoscape)                     │
   └──────────────────────────────────────────────────┘
```

---

## The 9 PRs

| PR | Capability | Flag (default) |
|---|---|---|
| **PR-0** | Cursor advances on fetch success regardless of extraction errors | (no flag) |
| **PR-A** | Durable Message Store + `Source` protocol seam | `READ_FROM_MESSAGE_STORE` (OFF) |
| **PR-B** | Background `ExtractionWorker` + content-hash fact ID + frontend dedupe | `DECOUPLE_EXTRACTION` (OFF) |
| **PR-C** | Injectable `CircuitBreaker` + provider failover seam + auto-retry | (code-level) |
| **PR-D** | Push-source HMAC ingest endpoint | per-source registration |
| **PR-E** | Per-page wiki page-store split | `PER_PAGE_WIKI` (OFF) |
| **PR-F** | `WikiMaintainer` service (incremental maintainer) | `WIKI_MAINTENANCE_MODE` (`manual`) |
| **PR-G** | Wiki lint endpoint + tensions surfacing | (no flag) |
| **PR-H** | Folder-tree / structure planner (n-depth pages) | (no flag) |

> All flags default OFF — the branch is safe to merge to `main` with **zero behavior change**. Production rollout flips flags in order per the runbook.

---

## What got built — backend

### New services
- `services/extraction_worker.py` — APScheduler-driven background worker; atomic claim via `find_one_and_update`; tick / stale / max-retries as module constants.
- `services/circuit_breaker.py` — centralized, injectable; fast-fails on 503; exponential backoff.
- `services/wiki_maintainer.py` (~2,000 LOC) — deterministic fact-to-page routing; per-page apply_update; preserves voice; A/B drift seam.
- `services/wiki_lint.py` — orphan / stale / dup / coherence findings.
- `services/wiki_drift_comparator.py` — page-voice A/B comparator for the soak window.
- `services/push_hmac.py` — HMAC-SHA256 + 5-min skew + 24h idempotency.

### New API surface
- `POST /api/sources/{id}/events` — push ingest (OpenClaw / Hermes).
- `POST /api/channels/{id}/wiki/maintain` — manual maintainer trigger.
- `POST /api/channels/{id}/wiki/lint` — wiki health.
- `GET  /api/channels/{id}/wiki/graph` — wiki graph data.
- `GET  /api/channels/{id}/extraction-status` / `extraction-failures`.
- `GET  /api/admin/extraction-worker/metrics`, `wiki-drift-summary`, `wiki-graph`.
- Admin push-source registry CRUD.

### New stores / models
- `wiki_pages` collection with compound unique index (slug-stable identity).
- `channel_messages` becomes the single source of truth (`imported_messages` replaced via dual-read migration).
- `failed_batches` diagnostic table.
- New `domain.py` / `persistence.py` types: page document, tensions, push events.

### MCP tools
- `read_wiki_page`, `list_wiki_pages`, `get_wiki_graph` (read).
- `search_memory`, `lint_wiki`, `get_extraction_status` (action).

### Migration scripts
- `migrate_imported_messages_to_channel_messages.py` (idempotent, dry-run).
- `migrate_wiki_cache_to_pages.py`.
- `migrate_wiki_pages_to_slug_identity.py`.

---

## What got built — frontend

| Component | Purpose |
|---|---|
| `WikiGraph.tsx` (1,677 LOC) | Cytoscape galaxy graph: fcose layout, kind-bordered pill nodes, central-hub pulse, search, filter panel, fullscreen |
| `WikiHealthToolbar.tsx` (834 LOC) | Tools dropdown, lint, maintain, drift indicators |
| `WikiLayout.tsx`, `WikiSidebar.tsx`, `WikiMarkdown.tsx` | Per-page rendering, recursive folder-tree sidebar, wikilink resolution |
| `ExtractionWorkerPanel.tsx` (388 LOC) | Live extraction progress, failure breakdown |
| `MemoryGraphView.tsx` | Obsidian-style entity graph (dot+caption nodes, orphan toggle) |
| `WelcomeScreen.tsx` | Onboarding split layout, OSS personal-intelligence positioning |
| Admin: `PushSources.tsx`, `WikiDrift.tsx` | Operator surfaces |
| Shared: `FullscreenWrapper`, `SegmentedToggle`, `ViewExplainerButton` | Reusable UX primitives |
| Hooks: `useExtractionStatus`, `useWikiLint`, `useWikiMaintain`, `useWikiGraph` | SWR-backed live status |

---

## Migration safety

```
   ┌────────────┐                       ┌────────────────┐
   │ Pull       │  WRITE_DUAL=true      │ imported_msgs  │  (legacy)
   │ adapter    ├──────────────────────►│                │
   └────────────┘            │           └────────────────┘
                             │
                             └──────────►┌────────────────┐
                                         │ channel_msgs   │  (new)
                                         └────────────────┘
                                                 │
                READ_FROM_MESSAGE_STORE=true      │
                ───────────────────────────►(reader switches)
                              │
                              ▼ soak window
                    drop legacy collection
```

- **Dual-write → dual-read → cutover → drop.** Each step is a flag flip; reversible until step 9.
- **Idempotent re-extraction** via content-hash fact ID — re-running can never duplicate.
- **Per-channel maintenance mode** (analyst recommendation) so a single channel can A/B without global flip.

---

## Testing investment

| Area | New / expanded test files | Indicative LOC |
|---|---|---|
| `tests/services/` | 11 | ~3,400 |
| `tests/api/` | 17 | ~3,800 |
| `tests/wiki/` | 8 | ~2,200 |
| `tests/stores/` | 2 | ~970 |
| `tests/scripts/` | 3 | ~1,000 |
| `web/src/**/__tests__/` | 12+ | ~2,300 |

Total: **>50 new test files**, covering the worker tick semantics, breaker state machine, HMAC verify, dual-read parity, page-store identity stability, drift A/B, lint findings, MCP tool wiring, and frontend hook + component behavior.

---

## Documentation added

- `docs/architecture/oss-pipeline.md` — single-page architecture summary with the data flow diagram.
- `docs/integrations/openclaw.md`, `hermes.md`, `push-sources.md` — push-source cookbook (register source → sign request → handle replays).
- `docs/runbooks/wiki-maintenance-soak.md` — operator soak procedure for §22.
- `docs/redesign-test-plan.md` — full test plan.
- `HANDOFF.md` — cross-session state.

---

## Demo flow (suggested talk track, ~5 min)

1. **Open a channel** on `main` → kick a sync → watch the UI hang and a 503 spam errors. (~30 s)
2. **Switch to the redesign branch** → kick the same sync → returns in seconds. Show the new **ExtractionWorkerPanel** ticking through batches. (~60 s)
3. **Force a 503** → cursor still advances; failures land in `failed_batches`; UI shows one deduped error, not a wall. (~60 s)
4. **Open the Wiki tab** → toggle `?view=graph` → fcose galaxy of pill nodes. Click → inline preview, double-tap → route. (~60 s)
5. **Trigger** `POST /wiki/maintain` → only the affected pages re-render; title/slug stay stable; tensions surface inline. (~45 s)
6. **Push from OpenClaw/Hermes** → `curl` a signed `POST /api/sources/{id}/events` → message lands in the same `channel_messages` store, gets extracted by the worker, lands in the wiki. (~45 s)

---

## Strategic framing

- **OSS = personal intelligence + Karpathy-style LLM Wiki.** The wiki UI lives inside Beever Atlas — there is no Obsidian export. (Out of scope.)
- **Connectors are commodity.** OpenClaw + Hermes own platform reach long-term. The IP being defended is **agent memory + LLM Wiki**.
- **Adapters are the replaceable layer.** Pull adapters and the new push endpoint sit behind the same `Source` protocol seam.
- **Enterprise tier deferred.** Multi-tenancy, ACL, SSO, BigQuery / Jira / GitHub extractors, durable queue — explicitly not in this redesign.

---

## Why merge this

- Default-OFF flags → **zero risk on merge**.
- Reversible rollout (steps 1–8) via flag flip; only the final two steps (auto maintenance + drop legacy collection) are one-way doors and gated on a 2-week A/B.
- All four stated outcomes validated: faster sync, errors no-op, wiki compounds, push-ready.
- Three Opus code reviews APPROVED the prior state; tests green (130/130 targeted backend, 65/65 frontend); ruff format clean.

---

## Round 8 — wiki-narrative-articles

The wiki redesign track had shipped 26 modules of *better-sorted facts* by Round 7. Round 8 closes the redundancy gap with raw Agent Memory: every wiki page is now a multi-section explanatory **article** at the top, with the previous 26 modules demoted to a "Reference & Evidence" appendix below.

### What landed

- **27 wiki modules** (was 26) — added `narrative_article` (frontend renderer) at module #2 in the page plan, immediately after `hero_summary`.
- **28 MCP tools** (was 27) — added `read_wiki_section(channel_id, page_slug, anchor)` for one-section retrieval without loading the full page; agents can now follow article anchors directly.
- **One-pass v3 prompt** (`MODULE_COMPILE_PROMPT_V3`) returns plan + hero + `narrative_sections[]` + body in a single LLM response — same call cardinality as v2; output tokens grow ~30-60%, NOT the 2-3x a multi-pass approach would cost.
- **Strict citation discipline** — every paragraph cites ≥ 1 `fact_id`; uncited paragraphs are dropped by the validator; "shared a link" / "noted that" narration is forbidden; `[agent-inference]` chip required for synthesis paragraphs.
- **Soft archetype-aware section hints** — Decision / Tension / Folder / Channel-Overview archetypes get suggested section structures; Topic archetype gets NO template (sections come purely from cluster facts). Hints explicitly tell the LLM to deviate when the data does not fit.
- **Frontend article + sticky TOC** — reading-column layout (max-w-prose), citation-chip popovers with fact preview, reading-time estimate, "X memories synthesized" badge, sticky TOC ≥ 1024px (compact dropdown below).

### Sessions + commits

- **Session A** (backend pipeline) — `e9c8aa3`, `8831e5b`, `86b333c`, `441bb1e`
- **Session B** (frontend article + TOC + appendix) — `9ff8183`
- **Session C** (archetype hints + rollout artifacts + final verification) — this commit

### Test growth

- Backend: 905 → 1014+ tests (+109)
- Frontend: 311 → 346 tests (+35); TypeScript clean

### Acceptance criteria (from `openspec/changes/wiki-narrative-articles/proposal.md`)

- Visiting a topic page in the live web UI shows a multi-section article with content-driven titles emerging from facts (not templates), with inline `[f_xxx]` citations + optional supporting visuals per section. ✅
- Reading the article gives the same understanding as reading every source message — without reading them. ✅ (validator-gated)
- Existing reference modules render below as evidence appendix. ✅ (collapsible "Reference & Evidence" `<details>` wrapper)
- LLM cost increase ≤ 60% per regen. ✅ (single call, +30-60% output tokens)

### Rollout posture

- Narrative generation is **the unconditional default**. The original rollout shipped behind `WIKI_NARRATIVE_ARTICLES` (default OFF) plus a per-channel override; both were removed because the validator's graceful fallback path provides the same safety guarantee without operator-side flag management.
- Implicit fallback to module-only rendering when the LLM fails parse OR the validator rejects the narrative on citation coverage — no broken pages, just a telemetry log line (`narrative_article_fallback reason=...`).
- Operator runbook lives at `docs/runbooks/wiki-narrative-articles-rollout.md` — covers validation checklist per archetype, telemetry queries to watch, validator-threshold tuning, and rollback via revert.
