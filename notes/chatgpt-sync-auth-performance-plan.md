# ChatGPT Sync Error, Performance, And Auth Plan

Date: 2026-05-03

## Scope

This note covers three current issues with the ChatGPT source plugin:

1. The UI still shows `Access to this endpoint is forbidden` during channel sync.
2. Sync is too slow for small ChatGPT conversations, currently around 3-5 minutes per channel.
3. ChatGPT auth/history fetch is not acceptable because it depends on a remote-debug browser and private ChatGPT web APIs.

## Current State Reviewed

Important files:

- `plugins/llms/copilot/_llm_patch.py`
- `plugins/sources/chatgpt/_service.py`
- `plugins/sources/chatgpt/_session.py`
- `plugins/web/_api_patch.py`
- `plugins/web/runtime/chatgpt-overlay.ts`
- `src/beever_atlas/services/batch_processor.py`
- `src/beever_atlas/services/sync_runner.py`
- `src/beever_atlas/agents/ingestion/pipeline.py`
- `src/beever_atlas/agents/ingestion/preprocessor.py`
- `src/beever_atlas/services/coreference_resolver.py`
- `src/beever_atlas/models/sync_policy.py`

The failing channel shown in the screenshot is:

- Channel: `69eb027e-e98c-8320-a0e0-15ac813251f2`
- Name: `PDF to Markdown Table`
- Messages: 11
- Last failed job: `720a14ab-8adc-41e8-b5d3-3a08d9b39c5e`
- Last failed job time: `2026-05-02T12:15:56Z` to `2026-05-02T12:19:01Z`
- Error: `litellm.APIError: OpenAIException - Access to this endpoint is forbidden`

## Finding 1: The Screenshot Shows A Stored Failed Job

The UI is replaying the most recent sync status for that channel. The job is already completed with status `failed`, not currently running.

The stored telemetry says:

| Metric | Value |
|---|---:|
| Total messages | 11 |
| Total batches | 2 |
| Completed batches | 1 |
| Failed batch | 1 |
| Successful batch | 2 |
| Successful batch facts | 5 |
| Successful batch entities | 10 |
| Successful batch relationships | 11 |

The successful batch proves `copilot/gpt-5-mini` can run the pipeline. The failure is request-shape or prompt-specific, not a total model outage.

## Finding 2: Copilot 403 Has Multiple Causes

Direct Copilot API probes showed these behaviors for `gpt-5-mini`:

| Request shape | Result |
|---|---:|
| `response_format={"type":"json_object"}` | 403 |
| `response_format={"type":"json_schema", ...}` | 200 |
| no `response_format`, no token cap | 403 in the probe |
| `max_tokens=8192` | 200 |
| `max_tokens=63000` | 403 in one plain request probe |
| long prompt | can return 403 |

This means the earlier fix was necessary but incomplete.

Already fixed in the current file:

- `plugins/llms/copilot/_llm_patch.py` now strips only `response_format.type == "json_object"`.
- It preserves `json_schema`, which Copilot accepts and ADK uses for OpenAI-compatible models.

Remaining issues:

- The running backend may still have the old monkey patch loaded until restart.
- The pipeline can still produce Copilot request shapes that 403, especially large prompts or oversized generation settings.
- Current error reporting does not record request-shape diagnostics, so all these cases look like the same generic Terms of Service 403 in the UI.

## Finding 3: The PDF Channel Is Not Actually Small For The LLM

Although the channel has only 11 ChatGPT messages, the content includes PDF/file extraction text, generated code, file citation tokens, and long assistant output.

The adaptive batcher split 11 messages into 2 batches. Batch 1 retained 8 messages, but those messages include long PDF-derived content and ChatGPT file metadata. Batch 2 retained 3 messages and succeeded.

The high-risk content in batch 1 includes text such as:

- `Make sure to include filecite...`
- parsed PDF page text
- rendered-file guidance
- generated Python/docx code
- long timetable/training outline output

For ingestion, this content should be normalized before it reaches fact/entity extraction. Right now too much raw ChatGPT runtime/file scaffolding is sent to the LLM.

## Finding 4: Main Performance Bottlenecks

The failed job still recorded useful timings from the successful batch:

| Stage | Observed seconds |
|---|---:|
| Preprocessor | 25.3 |
| Fact extractor | 27.62 |
| Entity extractor | 2.11 |
| Embedder | 50.69 |
| Validator | 0.97 |
| Persister | 0 |
| Batch wall clock | 170.93 |

Primary bottlenecks:

1. Preprocessor is not purely local in practice.
   - `PreprocessorAgent` calls `resolve_coreferences()` when `COREF_ENABLED` is on.
   - That creates another LLM call before extraction.
   - For ChatGPT conversations, this is often unnecessary because each conversation is already coherent and ordered.

2. Embedding is unexpectedly expensive.
   - `embedder` took about 50 seconds for the successful batch.
   - The plugin has a Copilot embedder patch, but the UI still reports `jina-embeddings-v4` in some activity logs, so the runtime model label and actual backend should be verified.

3. Fact extraction and entity extraction are separate LLM calls.
   - They run in parallel, but each pays model latency and quota risk.
   - For small ChatGPT conversations, a single combined extraction call could be faster and easier to retry.

4. ChatGPT sync currently rematerializes selected conversations.
   - `materialize_selected_conversations()` deletes imported messages for a selected channel and inserts the conversation messages again.
   - Manual plugin sync calls `runner.start_sync(... sync_type="full")` for selected channels.
   - This means unchanged conversations can still pay the full ingestion cost.

5. Timing telemetry has a double-counting bug.
   - `BatchProcessor` computes `duration_seconds = sum(batch_stage_timings.values())`.
   - `batch_stage_timings` includes both individual stages and `batch_wall_clock_s`.
   - So displayed batch duration can overstate real wall-clock time.

## Target Performance Model

The ideal user experience is:

- Under 1 minute to make a selected ChatGPT conversation usable.
- Wiki can appear quickly from a lightweight first pass.
- Rich graph/entity enrichment can continue asynchronously.
- Re-sync of unchanged conversations should finish in seconds.

To reach that, the pipeline should be split into two modes.

### Fast Mode: User-Visible Ingestion

Goal: make the channel searchable and wiki-ready in under 60 seconds.

Use this path for ChatGPT by default:

1. Normalize ChatGPT messages.
2. Strip ChatGPT runtime artifacts and file scaffolding.
3. Hash every message and skip unchanged messages.
4. Skip coreference resolution by default.
5. Run one combined LLM extraction pass for facts plus key entities.
6. Embed only extracted facts.
7. Persist facts and minimal entities.
8. Mark the channel usable.
9. Generate a lightweight wiki from facts.

### Rich Mode: Background Enrichment

Goal: improve graph quality without blocking the user.

Run later or manually:

1. Full entity extraction.
2. Cross-batch validation.
3. Graph writes.
4. Contradiction detection.
5. Full wiki regeneration.

## Recommended Fixes For The 403 Error

### Immediate

1. Restart the backend after `_llm_patch.py` changes.
   - The monkey patch is applied at import time; running servers keep the old function in memory.

2. Retry the failed channel.
   - The screenshot is a stored failed job. It will stay visible until a newer successful job replaces it.

3. Add Copilot request-shape telemetry.
   - Log model, stage, batch number, `response_format.type`, prompt character count, estimated prompt tokens, `max_tokens`, and whether tools are present.
   - Do not log prompt text or tokens.

4. Classify Copilot 403 causes.
   - `json_object_forbidden`
   - `prompt_too_large_or_rejected`
   - `missing_or_invalid_request_cap`
   - `rate_or_concurrency_limit`
   - `unknown_forbidden`

5. Add safe fallback retry for Copilot 403.
   - If a 403 happens on a large batch, retry with a smaller batch.
   - If it happens with `json_object`, strip it and retry.
   - If it happens without token cap, retry with an explicit cap.
   - If it happens with very high output cap, retry with a Copilot-specific lower cap.

### Model Request Policy

Set Copilot-specific request limits instead of reusing Gemini limits:

- Fact/entity extraction: start with `max_tokens` around 8192 to 12000.
- Reduce prompt batch size for ChatGPT file-heavy conversations.
- Preserve `json_schema` where possible.
- Never send `json_object` to Copilot.

## Recommended Performance Plan

### Phase 1: Quick Wins

1. Disable coreference resolution for ChatGPT channels by default.
   - Add a per-source or per-policy flag such as `skip_coreference_resolution`.
   - Expected saving from observed job: about 25 seconds per batch.

2. Strip ChatGPT export artifacts before batching.
   - Remove `filecite` tokens.
   - Remove ChatGPT system/tool scaffolding.
   - Collapse generated code blocks over a threshold into summaries.
   - Cap per-message text for ingestion while preserving the full raw message in `imported_messages`.

3. Fix duration telemetry.
   - Store `batch_wall_clock_s` separately.
   - Do not add it into `duration_seconds` with individual stages.

4. Verify embedding backend.
   - Ensure the activity label shows the actual embedding backend.
   - Measure embedding API latency separately from limiter wait.

5. Add message hashing.
   - Store `chatgpt_message_hash` per imported message.
   - Skip unchanged messages and unchanged conversations.

### Phase 2: ChatGPT Fast Path

1. Add a ChatGPT-specific ingestion preset.
   - `preset="chatgpt-fast"`
   - `skip_coreference_resolution=True`
   - `skip_entity_extraction=True` for the first pass, or replace with combined extraction.
   - `skip_graph_writes=True` for the first pass.
   - Smaller prompt/token caps for Copilot.

2. Add a combined extractor for ChatGPT.
   - One LLM call returns facts, important entities, and relationships.
   - This replaces separate fact and entity calls for fast mode.

3. Generate lightweight wiki first.
   - Use facts and conversation title only.
   - Full graph-aware wiki runs in background.

4. Make sync incremental by default.
   - Do not delete and reinsert all imported messages when only a few messages changed.
   - If conversation `updated` timestamp and message hashes are unchanged, skip ingestion entirely.

### Phase 3: Background Rich Enrichment

1. Queue entity graph enrichment after fast sync completes.
2. Run cross-batch validation in the background.
3. Regenerate full wiki after enrichment.
4. Surface progress as `Fast sync complete` and `Rich enrichment running`.

## Auth And ChatGPT History Download Findings

The current implementation in `plugins/sources/chatgpt/_session.py`:

- launches Edge or Chrome with `--remote-debugging-port=9222`;
- uses a dedicated temporary browser profile;
- checks `/api/auth/session` inside the ChatGPT tab;
- calls private `/backend-api/conversations`, `/backend-api/conversation/{id}`, and project endpoints;
- extracts an access token from the browser page context.

This is fragile and not professional for a product workflow:

- It is effectively DevTools/CDP automation.
- It does not reuse the user's normal browser SSO profile.
- It depends on private ChatGPT web APIs that can change without notice.
- It can break because of browser profile isolation, anti-automation changes, or session/token changes.
- It asks the app to handle browser session material indirectly.

Official OpenAI documentation currently supports ChatGPT data export through ChatGPT Settings/Data Controls or the Privacy Portal. The export arrives as a downloadable ZIP and includes chat history. The download link expires after 24 hours.

## Better Professional Auth/Import Approach

There does not appear to be a public OAuth scope for third-party apps to read a consumer ChatGPT user's conversation history directly. Therefore, the professional approach should avoid pretending there is a normal OAuth connector when the platform does not expose one.

Recommended product design:

### Primary: Official Export Import

Use OpenAI's official data export as the source of truth.

Flow:

1. User clicks `Connect ChatGPT` in Beever Atlas.
2. Beever opens the system browser to ChatGPT Data Controls or the OpenAI Privacy Portal.
3. User signs in with their normal ChatGPT account or SSO in the browser.
4. User requests `Export Data`.
5. User downloads the ZIP from OpenAI email.
6. Beever imports the ZIP directly or watches a configured import folder.
7. Beever parses `conversations.json` and registers conversations as channels.

This is not OAuth, but it is official, auditable, and does not touch cookies or browser tokens.

### SSO-Like User Experience

Keep the SSO feel without unsafe token extraction:

- `Open ChatGPT Export` button opens the normal browser.
- `Import Export ZIP` button accepts the official ZIP.
- Optional `Watch Downloads` mode detects new OpenAI export ZIPs locally.
- Optional email/drop-folder workflow lets the user save the export ZIP into a Beever inbox folder.
- Show a clear connection status: `Export imported`, `Last export date`, `Conversations found`, `New conversations since last import`.

### Enterprise Option

For ChatGPT Enterprise, investigate official admin/compliance export options separately.

If an organization has an official compliance/export API contract, implement it as a separate connector:

- admin-configured service credential;
- tenant/workspace scoped;
- no browser scraping;
- scheduled incremental import;
- audit logs.

This should be separate from consumer Plus/Pro import because the security model is different.

## Auth Implementation Plan

### Phase 1: Replace CDP With Export ZIP Import

1. Add backend endpoint: `POST /api/plugins/chatgpt/import-export`.
2. Accept `.zip` or extracted `conversations.json`.
3. Parse OpenAI export format into the existing normalized conversation model.
4. Store the original export metadata: export date, source filename, import hash.
5. Register or update the `ChatGPT History` connection.
6. Show conversations in the existing channel picker.

### Phase 2: Add Guided Export UX

1. Replace `Launch Browser` with `Open ChatGPT Export`.
2. Add import area for ZIP drag/drop.
3. Add `Watch folder` option for local development/desktop use.
4. Add user-facing status and troubleshooting.

### Phase 3: Incremental Import From Repeated Exports

1. Hash every conversation and message.
2. Skip unchanged conversations.
3. Add new messages only.
4. Keep deleted/archived state separately.
5. Trigger fast sync only for changed conversations.

### Phase 4: Optional Enterprise Connector

1. Research official Enterprise export/compliance API availability.
2. Add a separate connector type if supported.
3. Keep this separate from consumer export import.

## Proposed Work Order

1. Stabilize Copilot requests.
   - Restart backend.
   - Add request-shape telemetry.
   - Add Copilot-specific output caps.
   - Add 403 fallback retry with smaller prompt batches.

2. Fix ChatGPT preprocessing.
   - Strip export/runtime artifacts.
   - Cap file-heavy messages.
   - Skip coreference for ChatGPT by default.

3. Add ChatGPT fast ingestion preset.
   - Make channels usable in under 1 minute.
   - Defer rich graph enrichment.

4. Make sync incremental.
   - Hash messages and conversations.
   - Skip unchanged content.
   - Avoid delete-and-reinsert for unchanged selected conversations.

5. Replace CDP auth/fetch with official export ZIP import.
   - Keep browser SSO in the user's browser.
   - Beever imports only the official export file.

## Research: Downloading ChatGPT Sessions Without Export And Without DevTools

### Research Date: 2026-05-03

### What Was Checked

All current official OpenAI developer APIs were reviewed against the question:
> Can a third-party app read a consumer chatgpt.com user's conversation history without using the official export ZIP, and without CDP/DevTools automation?

Sources checked:
- `developers.openai.com/api/docs` — full Responses API and Connectors/MCP docs
- `developers.openai.com/apps-sdk` — ChatGPT Apps SDK and Auth
- `developers.openai.com/api/docs/guides/developer-mode` — ChatGPT Developer Mode
- `developers.openai.com/api/docs/guides/tools-connectors-mcp` — MCP and Connectors

### Finding: No Official API Exists For Reading chatgpt.com Consumer Conversation History

OpenAI currently provides four developer-facing surfaces. None of them expose read access to a chatgpt.com consumer account's conversation history:

| Surface | Direction | What it Does | Read ChatGPT history? |
|---|---|---|---|
| OpenAI Responses API (LLM calls) | App → OpenAI | Call GPT models | No |
| MCP and Connectors | ChatGPT model → external services | GPT reads Gmail, Dropbox, Drive, etc. | No — inverted direction |
| ChatGPT Apps SDK + MCP server | ChatGPT → your MCP server | Your service is called as a tool from inside ChatGPT | No — inverted direction |
| ChatGPT Developer Mode | ChatGPT → your MCP server | Full MCP client for Plus/Pro/Business/Enterprise | No — inverted direction |

The MCP/Connectors API is specifically for giving the OpenAI model access to **external** services (Dropbox, Gmail, Google Calendar, Google Drive, Microsoft Teams, Outlook Calendar, Outlook Email, SharePoint). It does not expose a connector for ChatGPT conversation history.

The ChatGPT Apps SDK and Developer Mode let developers build tools that run **inside** ChatGPT — so ChatGPT can call your service, but your service cannot poll ChatGPT for conversation history.

There is no public OAuth scope or endpoint for a third-party application to pull a chatgpt.com consumer user's conversation history.

### The Only Viable Non-Export, Non-CDP Approach: Browser Extension

A **browser extension** (WebExtension API — Chrome/Edge/Firefox) is architecturally different from CDP in several important ways:

| Property | CDP / DevTools (current) | Browser Extension (proposed) |
|---|---|---|
| Requires `--remote-debugging-port` | Yes — external process | No — runs inside the user's browser |
| Requires separate/temporary browser profile | Yes — dedicated profile | No — uses user's real browser and profile |
| User's real chatgpt.com session | Simulated through CDP | Automatically available via cookies |
| User consent | Implicit (CDP is transparent) | Explicit (one-time extension install) |
| Works with user's SSO / enterprise login | Sometimes not | Yes — reuses existing login |
| Distribution | No distribution path | Chrome Web Store / Edge Add-ons |
| Fragility | High (CDP port, port conflicts, profile races) | Lower (standard browser APIs) |

**How the extension approach works:**

1. User installs the Beever Atlas companion extension from Chrome Web Store or Edge Add-ons.
2. Extension requests permission for `chatgpt.com` domain.
3. Extension background service worker calls `fetch()` for ChatGPT's internal web APIs (`/backend-api/conversations`, `/backend-api/conversation/{id}`). Because the extension runs in the user's browser, the session cookie is automatically attached — no token extraction needed.
4. Extension sends the conversation data to the local Beever Atlas backend via `http://localhost:8000/api/plugins/chatgpt/push-history`.
5. Backend receives the data and materializes conversations into channels.

**Why this is the industry standard approach** — tools like ChatGPT Exporter, SaveMyChats, and similar apps all use this pattern. They are installable extensions that use the user's live session to read the same chatgpt.com internal APIs without CDP.

### Important Caveat

The browser extension still calls the same private chatgpt.com backend APIs (`/backend-api/conversations`) that the CDP approach calls. The difference is the mechanism:
- CDP: external process with a remote debugging port
- Extension: runs inside the user's browser, uses real session, no remote debugging port

OpenAI has not published these APIs as stable. They can change. However, these APIs have been stable since 2023 and are used by many popular community tools.

### Supplementary: ChatGPT Share Links

ChatGPT conversations can be shared as public URLs:
```
https://chatgpt.com/share/{uuid}
```
Beever could also accept share links as a clean supplementary import method:
- User copies a share link from ChatGPT (`Share → Copy Link`)
- User pastes link into Beever
- Beever fetches the public page and parses the conversation

This requires no auth, no extension, no APIs. It only works for conversations the user explicitly shares, so it cannot provide bulk history access.

### Proposed New Auth Architecture

Replace the current CDP flow with:

**Primary**: Browser extension that runs in the user's real browser.
**Secondary**: Share link import for individual conversations.
**Fallback**: Official export ZIP import (already planned above).

The extension replaces `_session.py`'s `launch_debug_browser()` and `export_history_from_browser()` with a push model: the extension calls Beever's backend directly.

### Browser Extension Implementation Plan

#### New Files

```
plugins/sources/chatgpt/extension/
    manifest.json              # Chrome/Edge Manifest V3
    background.js              # Service worker: fetch + push to Beever
    content_script.js          # Optional: DOM helper on chatgpt.com
    icons/
        icon16.png
        icon48.png
        icon128.png
    popup/
        popup.html
        popup.js               # Show sync status, trigger sync
```

#### Backend

New route (replaces/supplements `/fetch-history`):
```
POST /api/plugins/chatgpt/push-history
Body: { conversations: [...] }   # same format as current chatgpt_history.json
```

Extension authenticates to Beever with a local API key (generated on connection setup, stored in `chrome.storage.local`).

#### manifest.json (key permissions)

```json
{
  "manifest_version": 3,
  "name": "Beever Atlas — ChatGPT Sync",
  "version": "1.0.0",
  "permissions": ["storage", "alarms"],
  "host_permissions": [
    "https://chatgpt.com/*",
    "http://localhost:8000/*"
  ],
  "background": { "service_worker": "background.js" },
  "action": { "default_popup": "popup/popup.html" }
}
```

No `cookies` permission needed. The `host_permissions` entry for `chatgpt.com` is sufficient to make authenticated `fetch()` calls from the service worker because the user's cookies are sent automatically by the browser.

#### background.js (core logic)

```javascript
async function fetchAndPushHistory() {
  // Fetch conversation list
  const convResp = await fetch("https://chatgpt.com/backend-api/conversations?offset=0&limit=100", {
    credentials: "include"
  });
  if (!convResp.ok) throw new Error("ChatGPT session expired or not logged in");
  const { items } = await convResp.json();

  // Fetch full detail for each conversation
  const conversations = await Promise.all(
    items.map(async (item) => {
      const detailResp = await fetch(
        `https://chatgpt.com/backend-api/conversation/${item.id}`,
        { credentials: "include" }
      );
      return detailResp.ok ? detailResp.json() : null;
    })
  );

  // Push to Beever Atlas backend
  const { beeeverApiKey } = await chrome.storage.local.get("beeeverApiKey");
  await fetch("http://localhost:8000/api/plugins/chatgpt/push-history", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Beever-Key": beeeverApiKey
    },
    body: JSON.stringify({ conversations: conversations.filter(Boolean) })
  });
}
```

#### UI Changes

Replace `chatgpt-overlay.ts` `Launch Browser` step with:
1. `Install Extension` link (opens Chrome Web Store / Edge Add-ons page).
2. `Sync Now` button that sends a message to the extension's service worker.
3. Connection status: `Extension active`, `Last synced`, `Conversations found`.

## Acceptance Criteria

### Error Handling

- A failed Copilot request shows a classified cause, not only a generic 403.
- The UI shows model, stage, batch, and retry action.
- `json_object` is never sent to Copilot.
- Large-prompt 403 retries with smaller batches.

### Performance

- Unchanged ChatGPT conversation sync finishes in under 10 seconds.
- Changed small conversation fast sync finishes in under 60 seconds.
- Rich enrichment can exceed 60 seconds but must run in the background.
- UI distinguishes `usable` from `fully enriched`.

### Auth/Import

- No DevTools/CDP requirement.
- No cookie or access-token extraction from ChatGPT browser sessions.
- User can connect by using normal ChatGPT login/SSO and importing the official export ZIP.
- Repeated exports update only changed conversations.
