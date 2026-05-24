# ChatGPT Source And Channel Integration Plan

Date: 2026-05-02

## Purpose

Study how the web app connects platforms and channels today, compare that flow with the ChatGPT plugin implementation, and define a concrete plan to make ChatGPT usable through the app as a first-class data source instead of a hardcoded side panel.

## Files Reviewed

Frontend:

- `web/src/pages/SettingsPage.tsx`
- `web/src/components/settings/ConnectionWizard.tsx`
- `web/src/components/settings/FileImportWizard.tsx`
- `web/src/components/settings/ChatGPTSourcePanel.tsx`
- `web/src/hooks/useConnections.ts`
- `web/src/hooks/useFileImport.ts`
- `web/src/hooks/useChatGPTSource.ts`
- `web/src/pages/Channels.tsx`
- `web/src/pages/AskPage.tsx`

Backend core:

- `src/beever_atlas/api/connections.py`
- `src/beever_atlas/api/imports.py`
- `src/beever_atlas/api/channels.py`
- `src/beever_atlas/models/platform_connection.py`
- `src/beever_atlas/infra/channel_access.py`
- `src/beever_atlas/server/app.py`

Plugin side:

- `plugins/loader.py`
- `plugins/web/__init__.py`
- `plugins/web/_api_patch.py`
- `plugins/sources/chatgpt/__init__.py`
- `plugins/sources/chatgpt/importer.py`
- `plugins/sources/chatgpt/_scheduler_hook.py`

## How The App Connects A Real Source Today

### 1. The Settings UI is built around `PlatformConnection`

The main settings flow uses `useConnections()` and `GET /api/connections` as the source of truth.

- `SettingsPage.tsx` renders platform connections from `useConnections()`.
- `ConnectionWizard.tsx` creates a connection through `POST /api/connections`.
- `ManageChannelsDialog.tsx` and `useConnectionChannels()` use `GET /api/connections/{id}/channels`.
- Channel selection is persisted through `PUT /api/connections/{id}/channels`.

This means the app expects a connected integration to exist as a persisted backend record, not as a frontend-only panel.

### 2. A real connection has a lifecycle

`src/beever_atlas/api/connections.py` defines the normal contract:

1. Create a `PlatformConnection` row.
2. Validate/register the adapter with the bot bridge.
3. Discover channels for that connection.
4. Persist selected channels on the connection.
5. Trigger sync for newly selected channels.

The model is `PlatformConnection` in `src/beever_atlas/models/platform_connection.py`.

Important fields for app behavior:

- `platform`
- `display_name`
- `selected_channels`
- `status`
- `source`
- `owner_principal_id`

### 3. The rest of the app derives visibility from connections and channels

The broader UI does not know about custom source panels.

- `Channels.tsx` loads `GET /api/channels`.
- `AskPage.tsx` also loads `GET /api/channels` and only offers connected channels.
- `src/beever_atlas/api/channels.py` builds channel lists by iterating connected `PlatformConnection` rows and reading each connection's `selected_channels`.

If a source does not become a real connection and does not produce discoverable channels, it is invisible to the main product surfaces.

### 4. File Import is the key baseline

File Import is not bridge-backed, but it is still implemented as a first-class app source.

`src/beever_atlas/api/imports.py` does the important work:

- creates or reuses a synthetic `platform="file"` connection via `_ensure_file_connection()`
- adds the imported channel ID into `selected_channels`
- stores raw messages in `imported_messages`
- writes `channel_sync_state`
- logs activity so the channel appears as a real app entity

That is the strongest existing pattern for ChatGPT because ChatGPT is also not a bot bridge integration.

## How ChatGPT Is Implemented Today

### 1. The UI is hardcoded as a separate built-in panel

`SettingsPage.tsx` renders `ChatGPTSourcePanel` above the normal connection list under a separate "Built-in Sources" section.

`ChatGPTSourcePanel.tsx` does not use `useConnections()`. It uses `useChatGPTSource()`.

`useChatGPTSource.ts` calls two plugin-only endpoints:

- `GET /api/plugins/chatgpt/status`
- `POST /api/plugins/chatgpt/sync`

So the ChatGPT panel is not participating in the same frontend connection model as Slack, Discord, Teams, Mattermost, or File Import.

### 2. The backend is also a plugin-only side path

`plugins/web/_api_patch.py` registers a dedicated router under `/api/plugins/chatgpt`.

That router:

- reads the existence of `chatgpt_history.json`
- counts conversations from that file
- reads plugin-local ingestion state from `chatgpt_sync_state`
- queues `run_chatgpt_sync()` with `asyncio.create_task`

It does not:

- create a `PlatformConnection`
- expose channels through `GET /api/connections/{id}/channels`
- update `selected_channels`
- create sync job records through the normal sync APIs

### 3. The scheduler integration is global, not connection-scoped

`plugins/sources/chatgpt/_scheduler_hook.py` monkey-patches `SyncScheduler.startup()` and registers a single global APScheduler job.

That job:

- optionally refreshes `chatgpt_history.json`
- compares conversations against a plugin-specific SQLite table `chatgpt_sync_state`
- directly calls `BatchProcessor.process_messages(...)`

This job is not tied to:

- a connection record
- selected channels
- channel policy rows
- the normal sync runner contract

### 4. The importer bypasses the app's source/channel registration flow

`plugins/sources/chatgpt/importer.py` converts each conversation into message dictionaries and calls `BatchProcessor.process_messages(...)` directly.

Important details:

- `CHATGPT_PLATFORM = "chatgpt"`
- `channel_id = conversation id`
- `channel_name = conversation title`
- no `PlatformConnection` is created
- no `selected_channels` are updated
- no `imported_messages` copy is written
- no `channel_sync_state` write is visible in the plugin code
- no normal sync-job creation/completion path is used in the plugin code

Also, `PlatformConnection.platform` currently only allows:

- `slack`
- `discord`
- `teams`
- `telegram`
- `mattermost`
- `file`

There is no `chatgpt` platform in the core model today.

## Findings

### 1. ChatGPT is not a first-class connection in the product

This is the main problem.

The rest of the application is built around persisted `PlatformConnection` rows and app-discoverable channels. ChatGPT is implemented as a separate status card plus a background ingestion hook.

That is why it feels hardcoded: it is hardcoded.

### 2. The current ChatGPT UI does not support the app's normal user journey

Normal integrations follow this path:

1. connect source
2. discover channels
3. choose channels
4. see channels in Channels page
5. use them in Ask page

ChatGPT currently supports only:

1. show status
2. trigger background sync

There is no channel discovery or selection UX in the app.

### 3. ChatGPT state is stored in a parallel state model

The plugin uses:

- `chatgpt_history.json`
- plugin-local SQLite table `chatgpt_sync_state`

The app uses:

- `platform_connections`
- `selected_channels`
- `channel_sync_state`
- sync jobs
- imported/raw message storage for non-bridge content

These two models barely intersect.

### 4. ChatGPT conversations are not intentionally registered as usable app channels

`src/beever_atlas/api/channels.py` can show orphaned channels when sync state exists without a matching connection. But the ChatGPT plugin does not intentionally create the metadata that the app expects for a managed source.

The direct `BatchProcessor` path is not enough to make ChatGPT a proper app-visible channel flow.

### 5. Access control and ownership are also bypassed

The core app records `owner_principal_id` on connections and uses it in `assert_channel_access()`.

The current ChatGPT plugin path does not create owned connections, so it does not fit the app's connection ownership model.

### 6. Startup order is a hidden dependency

ChatGPT exists only when plugin bootstrap runs through `start_with_plugins.py` and `plugins.loader.load_plugins()`.

That is acceptable for plugin mode, but it makes the feature even more disconnected from the upstream app's normal source model.

## Recommended Design

## Recommendation Summary

Treat ChatGPT as a first-class built-in source connection that owns many conversation channels.

Do not keep the current model where ChatGPT is only a special status panel plus a scheduler patch.

### Target product behavior

The user flow should become:

1. Add Connection or Built-in Source -> ChatGPT History.
2. Configure the source file and sync behavior.
3. Discover conversations from the history file as selectable channels.
4. Save selected conversations to a real connection record.
5. Show those conversations in Channels and Ask like every other source.
6. Trigger sync per selected conversation through the normal app flow.

### Recommended backend shape

Represent ChatGPT as a source-backed connection, not as a bridge-backed connection.

That means one connection row for the source and many conversation channels under it.

Conceptually:

- connection: `ChatGPT History`
- platform/source type: `chatgpt`
- channels: one per conversation
- channel ID: conversation ID
- channel name: conversation title

### Recommended implementation principle

Reuse the File Import pattern wherever possible.

That means ChatGPT should do the same kinds of things File Import already does:

- create a synthetic connection record
- attach channel IDs to `selected_channels`
- persist raw messages for UI rendering
- write `channel_sync_state`
- integrate with normal channel visibility

### Why this is better than extending the current status panel

Improving the existing `/api/plugins/chatgpt/status` panel still leaves ChatGPT outside:

- `/api/connections`
- `/api/connections/{id}/channels`
- `/api/channels`
- channel ownership/access rules
- the Ask page picker

The root issue is not a missing button or an incomplete panel. The root issue is that ChatGPT is outside the app's source/channel model.

## Design Options

### Option A: Fastest stopgap

Model ChatGPT under the existing `platform="file"` synthetic connection and store metadata that marks channels as ChatGPT-originated.

Pros:

- minimal core changes
- easiest to implement in plugin mode
- quickly makes conversations visible in Channels and Ask

Cons:

- wrong platform identity in the product
- mixes two different source types under the same platform
- harder to maintain long term

### Option B: Proper design

Add `chatgpt` as a first-class source type in the connection model and support non-bridge connection providers.

Pros:

- clean model
- correct platform badges and filtering
- easier long-term maintenance
- clearer future path for more built-in sources

Cons:

- requires small core changes, not just plugin-side patches

### Recommendation

Use Option B as the target design.

If a quick functional result is needed first, ship Option A as a temporary compatibility layer and migrate to Option B immediately afterward.

## Implementation Plan

### Phase 1: Make ChatGPT app-visible with the least architectural change

Goal: make ChatGPT conversations show up as usable channels in the current app.

1. Add plugin-owned service code that reads `chatgpt_history.json` and returns a conversation inventory.
2. On first use, create a synthetic connection row for ChatGPT history.
3. Reuse the File Import pattern to persist raw messages for each selected conversation.
4. Write `channel_sync_state` for each selected conversation.
5. Add selected conversation IDs into the synthetic connection's `selected_channels`.
6. Ensure Channels page and Ask page can immediately see the conversations through `GET /api/channels`.

If Phase 1 must stay plugin-only, the lowest-risk version is to temporarily reuse `platform="file"` and distinguish ChatGPT by metadata and display name.

### Phase 2: Replace the hardcoded panel with a real connection flow

Goal: move ChatGPT into the same frontend model as other sources.

1. Replace `ChatGPTSourcePanel` with a ChatGPT connection wizard or source wizard.
2. Make the wizard list discovered conversations and let the user select which ones to ingest.
3. Store the result through the same connection refresh cycle used elsewhere so `useConnections()` becomes the source of truth.
4. Keep a lightweight source health/status view, but place it inside the connection card rather than as a separate global panel.

Frontend outcome:

- no more special `useChatGPTSource()` as the primary control path
- ChatGPT appears in the same settings list as other integrations
- management happens through a connection record, not a standalone card

### Phase 3: Make the backend model correct

Goal: stop pretending ChatGPT is a file import.

1. Extend `PlatformConnection.platform` to support `chatgpt`.
2. Update frontend platform typing and badges to include ChatGPT.
3. Introduce a distinction between bridge-backed sources and source-backed integrations.
4. Keep Slack, Discord, Teams, Telegram, and Mattermost on the bridge-backed path.
5. Keep File Import and ChatGPT on the source-backed path.

This is the clean point where the product becomes internally consistent.

### Phase 4: Replace plugin-only sync semantics with app-managed sync semantics

Goal: align ChatGPT sync with the rest of the product.

1. Stop treating ChatGPT sync as only a global scheduler hook.
2. Create normal sync records or a normal source-sync abstraction the UI can inspect.
3. Support manual sync per connection and, eventually, per selected conversation/channel.
4. Store last-sync information in the same state model the app already uses for channel readiness and progress.

This phase is important because the current `asyncio.create_task(run_chatgpt_sync())` path is operationally convenient but outside the app's main sync lifecycle.

### Phase 5: Access control and ownership

Goal: fit ChatGPT into the same security model as other integrations.

1. Stamp `owner_principal_id` on the ChatGPT connection record.
2. Ensure conversation channels are only admitted through the same channel access guard logic already used elsewhere.
3. Avoid plugin-local state that cannot be tied back to a connection owner.

## Concrete Refactor Targets

### Frontend

- Remove ChatGPT as the primary special case from `SettingsPage.tsx`.
- Replace `useChatGPTSource.ts` with connection-backed hooks, or reduce it to supplemental status only.
- Add a ChatGPT wizard similar in role to `FileImportWizard.tsx`.

### Backend core or plugin boundary

- Add a ChatGPT connection creation path.
- Add a conversation discovery path analogous to `GET /api/connections/{id}/channels`.
- Add a sync path that writes app-visible channel metadata.

### Plugin

- Keep file parsing and CDP fetch logic in `plugins/sources/chatgpt`.
- Move from "global sidecar sync" toward "connection-backed source adapter" behavior.
- Keep plugin bootstrap ownership if fork-safe separation is still the priority.

## Practical Next Step

If the goal is to make this usable quickly without a large rewrite, the best next implementation slice is:

1. create a synthetic ChatGPT connection
2. discover conversations from `chatgpt_history.json`
3. let the user select them in the app
4. persist raw messages and sync state using the File Import pattern
5. make them appear in `GET /api/channels`

That would solve the main product problem first: ChatGPT conversations would finally behave like channels the app can browse and ask over.

After that, the model can be cleaned up from temporary `file` semantics to a proper `chatgpt` platform/source type.

## ChatGPT Authentication Design

## Current Auth Behavior

The current fetch path in `plugins/sources/chatgpt/fetch.py` uses two things:

1. an already-authenticated `chatgpt.com` browser tab exposed through local Chrome DevTools Protocol on `http://localhost:9222`
2. a manually stored bearer token in `chatgpt_token.txt`

The script tells the user to extract the token from:

- `fetch("/api/auth/session").then(r=>r.json()).then(d=>console.log(d.accessToken))`

This means the current auth model is:

- manual
- browser-session dependent
- token-file based
- not integrated into the app UI

It works as an operator workflow, but it is not acceptable as a first-class product flow.

## Authentication Goal

The app should authenticate to the user's existing `chatgpt.com` web session without asking for their OpenAI password inside Beever Atlas and without persistently copying browser cookies into the backend database.

The desired product behavior is:

1. user clicks `Connect ChatGPT`
2. app checks whether a compatible local browser session is already authenticated
3. if yes, app establishes a local session bridge and fetches conversation data
4. if no, user signs in to `chatgpt.com` in their own browser
5. app retries discovery and then uses short-lived session material only for fetch operations

## Recommendation Summary

Use browser-session reuse as the primary authentication model.

Do not build app-managed username/password login for `chatgpt.com`.
Do not permanently ingest browser cookies into Atlas backend storage.
Do not treat ChatGPT authentication as if Atlas owns the identity flow.

The best design is:

- user authenticates on real `chatgpt.com` in their browser
- Atlas uses a local helper to access that authenticated session on the same machine
- Atlas requests an ephemeral access token or performs authenticated fetches through the browser context
- Atlas stores only connection metadata and health state, not durable ChatGPT session secrets

## Design Principles

### 1. Browser-first identity

The source of truth for ChatGPT auth should remain the user's browser session on `chatgpt.com`.

Reason:

- that is where the user already signs in
- that is where SSO, MFA, device trust, and session renewal already happen
- Atlas should not try to reproduce or intercept the full OpenAI auth flow

### 2. Local-only secret use

Any cookies or bearer tokens derived from the browser should be treated as local ephemeral credentials.

That means:

- use them only on the user's machine
- prefer memory over disk
- if persistence is required, store only encrypted short-lived metadata and rotate aggressively

### 3. User-mediated consent

Atlas should never silently scrape unrelated browser state.

The user should explicitly opt in to one of these:

- `Use existing browser session`
- `Open ChatGPT and sign in`
- `Upload exported history file`

### 4. Fallback-friendly architecture

Because this relies on browser behavior and private web APIs, the product must keep a fallback path.

That fallback is file import or manual fetch/export.

## Authentication Options

### Option 1: Reuse the existing authenticated browser session

Flow:

1. User chooses `Use existing browser session`.
2. Atlas desktop-local helper looks for a browser with remote automation enabled or launches a managed browser profile.
3. Helper finds an open `chatgpt.com` tab or opens one.
4. Helper executes `fetch("/api/auth/session")` inside that browser context.
5. Helper retrieves the short-lived access token or directly performs authenticated `fetch()` calls in that same browser context.
6. Atlas uses the resulting data to discover conversations and sync them.

Pros:

- best UX when the user is already signed in
- no password entry in Atlas
- naturally benefits from existing SSO and MFA
- aligns with how the current plugin already works conceptually

Cons:

- requires a local helper, browser automation, or extension bridge
- fragile if ChatGPT changes private frontend/backend behavior
- not suitable for a headless server-only deployment

### Option 2: Launch a dedicated browser profile for Atlas-managed session reuse

Flow:

1. User clicks `Connect ChatGPT`.
2. Atlas launches Edge or Chrome in a dedicated profile with remote debugging enabled.
3. User signs in to `chatgpt.com` in that browser window.
4. Atlas reuses that same profile for future fetches.
5. Atlas extracts session information only through the controlled profile.

Pros:

- avoids depending on the user's unrelated personal browser profile
- cleaner security boundary
- easier to support and document than arbitrary profile scraping

Cons:

- slightly more setup friction
- behaves more like a managed connector than a pure passive integration

### Option 3: Import browser cookies into Atlas backend

This means reading cookies from the browser profile and storing them in Atlas for direct HTTP requests.

This is not recommended.

Reasons:

- higher security risk
- difficult cookie lifecycle and session invalidation handling
- stronger coupling to browser internals
- makes Atlas a durable holder of third-party session secrets

### Option 4: Atlas-managed login or embedded password form

This means asking the user to enter OpenAI credentials directly into Atlas.

This should not be implemented.

Reasons:

- wrong trust boundary
- breaks MFA/SSO expectations
- high security and compliance risk
- more likely to fail as the web login flow changes

## Recommended Auth Architecture

### Preferred model

Use a local browser-session bridge with a managed browser profile option.

That means:

- primary path: reuse an existing authenticated browser session
- safer fallback path: launch a dedicated Atlas browser profile and let the user sign in there
- emergency fallback: file import or manual export

### Core components

1. ChatGPT connection record

Persist a `PlatformConnection`-like record for ChatGPT integration state:

- `platform = chatgpt`
- `display_name = ChatGPT History`
- `source = ui`
- `owner_principal_id`
- auth mode metadata only

Store metadata such as:

- `auth_mode = existing_browser | managed_browser | file_only`
- `browser_type = edge | chrome`
- `browser_profile_kind = existing | atlas_managed`
- `last_auth_check_at`
- `last_auth_status = connected | expired | missing_browser | reauth_required`

Do not store raw cookies or bearer tokens in this record.

2. Local session bridge

Add a plugin-owned local helper service or helper module that can:

- discover browser instances
- open or attach to a `chatgpt.com` tab
- evaluate JavaScript in that tab context
- run authenticated `fetch()` calls from inside the browser

This can reuse the same CDP idea as the current fetcher, but it should move behind an app-owned connector interface rather than a manual CLI script.

3. Ephemeral auth provider

Create a plugin service such as `ChatGPTSessionProvider` that exposes methods like:

- `check_session()`
- `ensure_authenticated_tab()`
- `get_access_token()`
- `fetch_json(path)`

Important rule:

- prefer `fetch_json(path)` executed inside the authenticated browser tab over exporting the access token whenever possible

That reduces token handling and keeps auth state inside the browser boundary.

4. Connection health API

Expose app-facing auth status endpoints such as:

- `POST /api/connections/chatgpt/connect`
- `POST /api/connections/chatgpt/reauth`
- `GET /api/connections/{id}/status`

These endpoints should report:

- browser found or not
- tab authenticated or not
- session valid or expired
- last successful token/session probe time

## Recommended End-to-End Auth Flow

### Flow A: Existing browser session

1. User selects `Connect ChatGPT`.
2. UI calls backend `connect` endpoint.
3. Backend local helper checks for supported browser automation endpoint.
4. If a `chatgpt.com` tab exists and `fetch("/api/auth/session")` returns an authenticated session, the connection is marked healthy.
5. Backend discovers conversations using in-browser authenticated requests.
6. User selects conversations to sync.

### Flow B: Existing browser installed but not authenticated

1. User selects `Connect ChatGPT`.
2. Helper opens `chatgpt.com` in a supported browser.
3. UI shows `Finish sign-in in your browser`.
4. Backend polls the local helper for session readiness.
5. When session becomes valid, discovery proceeds automatically.

### Flow C: Managed Atlas browser profile

1. User selects `Use Atlas browser profile`.
2. App launches a dedicated Edge or Chrome profile with remote debugging enabled.
3. User signs in there once.
4. Atlas reuses that profile for future imports and syncs.

### Flow D: Browser bridge unavailable

1. UI reports that live browser auth is unavailable.
2. User is offered `Upload history file` or `Retry with supported browser`.

## Security Model

### What Atlas may store

- connection metadata
- browser selection metadata
- last successful auth check timestamp
- conversation inventory metadata
- encrypted local path or profile reference if needed

### What Atlas should avoid storing

- OpenAI username or password
- durable raw session cookies
- long-lived bearer tokens in plaintext files
- tokens inside normal connection credentials storage unless there is no alternative

### If short-lived token persistence is unavoidable

If implementation constraints force temporary persistence, then:

- encrypt using the existing credential encryption path
- set aggressive expiry
- mark tokens non-exportable in logs and APIs
- never return them to the frontend
- rotate by reacquiring from browser session frequently

Even then, in-browser authenticated fetch remains preferable.

## SSO Discussion

Using `the web browser level where ChatGPT already authenticated` is the right direction.

In practice, for this product, that means session reuse rather than traditional enterprise SSO integration.

Why:

- Atlas is not the identity provider for ChatGPT
- ChatGPT web auth is already completed in the user's browser
- the product value is in reusing that trusted authenticated browser context

So the design should be framed as:

- browser session reuse
- not custom SSO protocol implementation inside Atlas

## UX Design For Auth

The ChatGPT connection wizard should include an auth step with three explicit choices:

1. `Use existing browser session`
2. `Open ChatGPT sign-in window`
3. `Use file import only`

Suggested auth statuses:

- `Connected via browser session`
- `Browser found, sign-in required`
- `Session expired, reconnect required`
- `Browser automation unavailable`
- `File import mode only`

Suggested user actions:

- `Check browser session`
- `Open ChatGPT`
- `Reconnect`
- `Continue with file import`

## Plugin Implementation Plan For Auth

### Phase A: Replace `chatgpt_token.txt`

Remove the manual token-file assumption from the main product flow.

Instead:

- create a `ChatGPTSessionProvider`
- acquire session information live from the browser
- keep `chatgpt_token.txt` only as a developer fallback if needed

### Phase B: Wrap current CDP logic behind a service boundary

Move browser interaction from `plugins/sources/chatgpt/fetch.py` into a reusable service with methods for:

- browser discovery
- tab discovery
- auth validation
- authenticated fetch

This turns the current one-off script into a real connector primitive.

### Phase C: Add auth-aware connection API

Add plugin endpoints for:

- connect
- auth status
- reconnect
- disconnect
- browser launch

These endpoints should update the ChatGPT connection record rather than a standalone plugin status panel.

### Phase D: Join auth and source discovery

Once auth succeeds:

- fetch conversation stubs
- present them as selectable channels
- persist selection through the connection record

### Phase E: Keep manual fallback

Keep file import and manual export as a supported fallback because browser automation against private web APIs is inherently brittle.

## Final Recommendation

Yes, reuse the existing authenticated browser session as the primary auth model.

Do it through a local browser bridge, not by scraping and permanently storing cookies in Atlas.

If a cleaner operational boundary is needed, support a dedicated Atlas-managed browser profile as the secondary mode.

Do not implement direct credential login inside Atlas.
Do not make `chatgpt_token.txt` the production auth mechanism.

The product should treat ChatGPT auth as `session reuse from the user's browser`, then convert that authenticated session into a first-class source connection and channel discovery flow.