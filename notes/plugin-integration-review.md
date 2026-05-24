# Beever Atlas Plugin Integration Review

Date: 2026-05-02

## Executive Summary

The plugins are functional in the explicit plugin startup path. Because this project is a fork and the goal is to keep upstream changes mergeable, that separate plugin path is not a flaw by itself. It is the right direction.

In practical terms: `plugins` attach correctly when the app is started through `start_with_plugins.py`. They do not attach when using the normal upstream path that runs `beever_atlas.server.app:app` directly. For this fork, the right fix is not to modify upstream-owned app files. The right fix is to make the plugin bootstrap, plugin docs, and plugin-side wrappers clearer and more complete.

The plugin design is promising and useful: it adds an embedded SQLite/mongomock local mode, Copilot/GitHub model support, and ChatGPT history ingestion. The main risk is that most of this works by monkey-patching core modules before import. That makes startup order, plugin-side tests, and clear fork-safe documentation very important.

## Fork-Safe Constraint

This repository is a fork of another main repo. The customization strategy is intentional:

- Do not touch original upstream files unless there is no practical plugin-side alternative.
- Keep custom behavior inside `plugins/` or in clearly separate fork-owned files outside upstream-owned folders.
- Prefer plugin bootstraps, wrappers, adapters, monkey patches, and external notes/docs over direct edits to `src/`, `web/`, `bot/`, or upstream docs.
- Docker/image integration is not needed for now and should be treated as deferred.
- Recommendations below are framed around keeping future upstream updates easy to merge.

## What I Reviewed

I first reviewed source/scripts before documentation, then read documentation afterward to compare intent with implementation. Focus areas:

- Backend startup and lifecycle: `src/beever_atlas/server/app.py`
- Settings and environment handling: `src/beever_atlas/infra/config.py`
- Store construction and scheduler lifecycle: `src/beever_atlas/stores/__init__.py`, `src/beever_atlas/services/scheduler.py`
- LLM model resolution and model settings API: `src/beever_atlas/llm/model_resolver.py`, `src/beever_atlas/llm/provider.py`, `src/beever_atlas/api/models.py`
- Plugin startup and plugin packages: `start_with_plugins.py`, `plugins/loader.py`, `plugins/stores/embedded`, `plugins/llms/copilot`, `plugins/sources/chatgpt`
- Deployment paths were checked only to understand current behavior. Docker changes are not needed now.
- Frontend model settings UI: `web/src/hooks/useAgentModels.ts`, `web/src/components/settings/AgentModelSettings.tsx`, `web/src/components/settings/AgentModelRow.tsx`, `web/src/lib/types.ts`
- Test coverage under `tests`
- Main documentation and configuration examples

## Current Plugin Architecture

Plugin startup is controlled by `start_with_plugins.py`:

1. Adds project root and `src` to `sys.path`.
2. Loads `.env`.
3. Calls `plugins.loader.load_plugins()`.
4. Imports `beever_atlas.server.app:app`.

`plugins/loader.py` loads a hardcoded plugin list in this order:

1. `plugins.stores.embedded`
2. `plugins.llms.copilot`
3. `plugins.sources.chatgpt`

That order matters. The embedded store plugin must patch stores and scheduler before the app lifespan creates `StoreClients` and `SyncScheduler`. The Copilot plugin must patch model resolver/provider before model validation. The ChatGPT plugin patches scheduler startup to add a recurring job.

## What Works

### Plugin Activation Works In The Plugin Entry Point

When `load_plugins()` is called before app import, the patches apply. A runtime probe confirmed:

- `GRAPH_BACKEND=sqlite` is rewritten to `GRAPH_BACKEND=none` with `_SQLITE_GRAPH_OVERRIDE=1`.
- `SyncScheduler` uses `SQLAlchemyDataStore` instead of MongoDB APScheduler storage.
- `copilot/gpt-4o` and `github/gpt-4o` pass backend model validation after plugin activation.

### Embedded Store Plugin Covers The Main External Store Dependencies

The embedded plugin has replacements or fallbacks for the major services:

- MongoDB: `MONGODB_BACKEND=mock` patches Motor to use a singleton `mongomock_motor.AsyncMongoMockClient`.
- Weaviate: `WEAVIATE_BACKEND=null` injects `SQLiteVectorStore` and `SQLiteQAHistoryStore`, with null-store fallback.
- Graph store: `GRAPH_BACKEND=sqlite` injects `SQLiteGraphStore` while making core settings see `GRAPH_BACKEND=none`.
- Scheduler: patches `SyncScheduler.__init__` to use SQLite via APScheduler SQLAlchemy datastore.

This is a useful local/dev mode. It can allow Beever Atlas to run without MongoDB, Weaviate, or Neo4j if the plugin entry point is used.

### Copilot/GitHub Model Patch Is Connected To Core Validation

The LLM plugin patches:

- `beever_atlas.llm.model_resolver.resolve_model_object`
- `beever_atlas.llm.model_resolver.validate_model_string`
- `beever_atlas.llm.provider._validate_model_resolution`

This means model strings like `copilot/<model>` and `github/<model>` can be accepted by the backend once plugins are loaded.

### ChatGPT Source Plugin Is Attached To The Scheduler

The ChatGPT plugin patches `SyncScheduler.startup()` and registers an interval job for `run_chatgpt_sync`. It also keeps per-conversation ingestion state in SQLite to skip unchanged conversations.

## High Priority Findings

### 1. Default Startup Does Not Load Plugins

Severity: High

Normal app startup imports `beever_atlas.server.app:app` directly. That path never calls `load_plugins()`. The plugin patches only run through `start_with_plugins.py`.

Evidence:

- `start_with_plugins.py` is the only startup file that calls `load_plugins()` before importing the app.
- The normal upstream runtime remains unmodified, which is good for fork maintenance.

Impact:

Even if `.env` contains `GRAPH_BACKEND=sqlite`, `WEAVIATE_BACKEND=null`, or `MONGODB_BACKEND=mock`, those settings only work when the plugin bootstrap runs first.

Fork-safe recommendation:

- Keep upstream app startup untouched.
- Treat `start_with_plugins.py` as the official fork/plugin entrypoint.
- Add plugin-owned documentation under `notes/` or `plugins/README.md` that says plugin mode must run `uvicorn start_with_plugins:app`.
- Consider adding a plugin-side smoke command or wrapper script outside upstream-owned folders, for example `plugins/run_with_plugins.py`.

### 2. Plugin Runtime Needs Its Own Documentation And Guardrails

Severity: High

The plugin runtime is intentionally separate, but it is not yet documented as the primary fork workflow. That makes it easy to accidentally run the upstream entrypoint and wonder why plugin env vars do nothing.

Impact:

The plugin system can appear broken when the wrong startup command is used.

Fork-safe recommendation:

- Add `plugins/README.md` or `notes/plugin-mode.md` as the canonical plugin-mode guide.
- Add a plugin-side startup diagnostic that logs loaded plugins and important env flags.
- Add a plugin-side self-check command that verifies patches are active without requiring changes to core app files.

### 3. Root `fetch_chatgpt.py` Wrapper Is Broken

Severity: High

`fetch_chatgpt.py` imports a module path that does not exist:

```python
from plugins.chatgpt_copilot.chatgpt.fetch import main
```

The real module is:

```python
plugins.sources.chatgpt.fetch
```

Impact:

The documented/convenience command `python fetch_chatgpt.py` fails with `ModuleNotFoundError: No module named 'plugins.chatgpt_copilot'`.

Fork-safe recommendation:

- If `fetch_chatgpt.py` is fork-owned, change the import to `from plugins.sources.chatgpt.fetch import main`.
- If the root wrapper is considered upstream-owned, leave it alone and add a plugin-owned wrapper such as `python -m plugins.sources.chatgpt.fetch` or `plugins/sources/chatgpt/__main__.py`.
- Put corrected usage examples in plugin-owned docs rather than upstream docs.

### 4. Copilot/GitHub Models Are Not Exposed In API Or UI

Severity: High

The backend validation accepts `copilot/` and `github/` after plugin activation, but the available models endpoint and frontend UI only know about Gemini and Ollama.

Evidence:

- `src/beever_atlas/api/models.py` returns only `gemini`, `ollama`, and `ollama_connected` in `AvailableModelsResponse`.
- `web/src/lib/types.ts` models available providers as only `gemini`, `ollama`, and `ollama_connected`.
- `AgentModelRow.tsx` renders optgroups only for `Gemini (Cloud)` and `Local (Ollama)`.
- `AgentModelSettings.tsx` builds its model list from Gemini plus Ollama only.

Impact:

The plugin model support is usable only by manual API payloads or `.env` overrides. Users cannot discover or select Copilot/GitHub models in the app settings UI.

Fork-safe recommendation:

- Avoid editing `src/beever_atlas/api/models.py` or `web/` directly unless you decide to maintain a forked UI.
- Prefer plugin-side route patching: during plugin activation, register or override an additional available-models endpoint that includes `copilot` and `github` provider groups.
- If UI changes are needed, put them in a separate plugin-owned frontend overlay or document manual model strings for now.

### 5. Plugin Env Vars Are Outside The Typed Settings Model

Severity: Medium-High

The core `Settings` model knows `graph_backend` values `neo4j`, `nebula`, and `none`, but plugin mode depends on additional env vars read directly through `os.getenv`:

- `GRAPH_BACKEND=sqlite`
- `WEAVIATE_BACKEND=null`
- `MONGODB_BACKEND=mock`
- `BEEVER_SQLITE_DB_PATH`
- `BEEVER_SCHEDULER_DB_PATH`
- `CHATGPT_SYNC_INTERVAL_HOURS`
- `COPILOT_GITHUB_TOKEN`

Impact:

Configuration is invisible to validation, generated docs, settings dumps, and likely future admin UI. `GRAPH_BACKEND=sqlite` is not actually a core-supported value; the plugin rewrites it before settings validation becomes a problem.

Fork-safe recommendation:

- Do not edit core `Settings` unless absolutely necessary.
- Add a plugin-local settings parser, for example `plugins/settings.py`, that validates plugin env vars before patches run.
- Emit plugin startup diagnostics showing active plugin settings.
- Keep the current `GRAPH_BACKEND=sqlite` rewrite if it is the cleanest way to avoid upstream settings changes.

## Medium Priority Findings

### 6. ChatGPT Scheduled Sync Bypasses Core Sync Job Semantics

Severity: Medium

`run_chatgpt_sync()` calls `BatchProcessor.process_messages(...)` directly with a synthetic `sync_job_id` like `chatgpt-sched-<id>`. I did not see it create the corresponding core sync job record first.

Impact:

If `BatchProcessor` or downstream progress tracking expects a real sync job, scheduled ChatGPT ingestion can produce incomplete or inconsistent sync status. Errors might not be visible through the normal imports/sync UI.

Fork-safe recommendation:

- Keep the adapter in `plugins/sources/chatgpt`.
- Add a plugin-side bridge that creates or emulates the minimum sync job state expected by `BatchProcessor` before scheduled ingestion runs.
- Avoid changing the core sync runner unless a future upstream-compatible extension point appears.

### 7. ChatGPT Importer CLI Does Not Load Plugins

Severity: Medium

`plugins.sources.chatgpt.importer` loads `.env`, but it does not call `plugins.loader.load_plugins()`. If the importer is run directly, embedded store and Copilot patches may not activate.

Impact:

Running the importer with `MONGODB_BACKEND=mock`, `WEAVIATE_BACKEND=null`, or `GRAPH_BACKEND=sqlite` may still use the core external-store path.

Fork-safe recommendation:

- Call `load_plugins()` in the importer before importing core Beever Atlas services, because the importer itself is plugin-owned.
- Or provide a shared plugin-aware CLI bootstrap inside `plugins/` and have plugin CLIs use that.

### 8. Embedded Mongo Persistence Is Snapshot-Based And Exit-Dependent

Severity: Medium

The embedded Mongo replacement uses mongomock in memory and persists it by saving a JSON snapshot into SQLite at process exit via `atexit`.

Impact:

Hard kills, crashes, container restarts, or process termination paths that skip `atexit` can lose Mongo-side state. This matters for settings, sync status, chat history, shares, wiki cache/version metadata, and any store backed by MongoDB.

Fork-safe recommendation:

- Implement additional persistence inside `plugins/stores/embedded`.
- Save snapshots on plugin-controlled shutdown paths as well as `atexit`.
- Consider periodic snapshot flushes after writes, or replace high-value Mongo stores with plugin-local SQLite implementations instead of a global snapshot.

### 9. `SQLiteFileStore` Exists But Is Not Used

Severity: Medium

`plugins/stores/embedded/_sqlite_vector.py` includes `SQLiteFileStore`, but `_store_patch.py` injects `NullFileStore` when `MONGODB_BACKEND=mock`.

Impact:

File uploads/imported attachments are in-memory only in embedded mode, even though a persistent SQLite file store already exists.

Fork-safe recommendation:

- Inject `SQLiteFileStore` from `_store_patch.py` instead of `NullFileStore` once it satisfies the same methods used by core file APIs.
- Keep `NullFileStore` only as a fallback.

### 10. Hardcoded Plugin Order And Best-Effort Failure Handling Hide Integration Failures

Severity: Medium

`plugins/loader.py` uses a fixed list and logs activation exceptions without failing startup.

Impact:

This is friendly for experimentation but risky for deployment. If a required plugin fails, the app may continue booting with external stores or missing LLM support, and the user sees a partially configured system.

Fork-safe recommendation:

- Add plugin metadata in `plugins/loader.py`: name, dependency order, required/optional, enabled flag.
- Add `BEEVER_PLUGINS_REQUIRED=true` or per-plugin required settings to fail fast.
- Add plugin-side status reporting by registering routes during plugin activation if feasible, rather than editing core health routes.

### 11. Core Startup May Not Reload Persisted Model Overrides

Severity: Medium

`LLMProvider.reload_from_db()` exists and the model settings API calls it after updates. The app startup path initializes the provider with settings via `init_llm_provider(settings)`, but I did not see startup call `reload_from_db()`.

Impact:

Persisted per-agent model overrides in MongoDB may not be active immediately after app restart until a settings API path triggers reload.

Fork-safe recommendation:

- Do not edit `src/beever_atlas/server/app.py` for this unless you decide to maintain a core fork.
- Prefer a plugin-side startup hook or provider patch that loads persisted model overrides after stores/provider are initialized.

## Lower Priority / Cleanup Findings

### 12. Some Helper Code Shows Drift

Severity: Low-Medium

`_sqlite_vector.py` optionally imports `get_connection` from `_sqlite_db.py`, but `_sqlite_db.py` currently exposes only `get_db_path()` and `ensure_data_dir()`. This is handled by fallback code and is not fatal, but it suggests the helper design drifted.

Fork-safe recommendation:

- Either add the shared connection helper in `plugins/stores/embedded/_sqlite_db.py` or remove the optional import/fallback complexity from plugin code.

### 13. Plugin Code Has Limited Static Safety Because It Patches Private Internals

Severity: Low-Medium

The plugins patch private/protected internals such as `SyncScheduler.__init__`, `SyncScheduler.startup`, `provider._validate_model_resolution`, and `StoreClients.from_settings`.

Impact:

Core refactors can break plugins silently. This is acceptable for rapid prototyping, but it needs tests and visible status if the plugins are production-relevant.

Fork-safe recommendation:

- Because upstream core should stay untouched, keep extension points plugin-local for now: loader metadata, patch status checks, plugin settings validation, and adapter interfaces under `plugins/`.
- If upstream eventually accepts extension hooks, migrate patches to those hooks later.

## Documentation Drift

The upstream documentation mostly describes the normal external-service stack and standard startup path. That is acceptable for a fork, but the fork needs its own plugin documentation.

Key drifts:

- README/dev commands still point users toward normal upstream startup, not `start_with_plugins.py`.
- Docker docs align with the default Dockerfile. Docker is deferred for now.
- `.env.example` contains stale module references to `plugins.chatgpt_copilot...` while current code is under `plugins.sources.chatgpt` and `plugins.llms.copilot`.
- Plugin env vars are not documented as a cohesive mode.
- Some broader docs appear stale versus current code shape, such as pipeline stage descriptions and platform/source references.

Fork-safe docs to add:

- `plugins/README.md` as the canonical plugin guide, or `notes/plugin-mode.md` if you want notes-only documentation.
- Avoid editing upstream README/docs unless you intentionally maintain fork docs there.
- A plugin-mode guide should include:
  - Local command: `uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000`
  - Required/optional plugin env vars
  - Current limitations and data persistence behavior
   - The rule that customizations should stay in `plugins/` or other fork-owned paths.

## Test Coverage Gaps

I searched tests for plugin startup terms and did not find plugin-specific tests.

Recommended minimum tests:

1. Loader smoke test:
   - Set embedded env vars.
   - Call `load_plugins()`.
   - Assert `GRAPH_BACKEND` rewrite and `_SQLITE_GRAPH_OVERRIDE`.
2. Store patch test:
   - Build `StoreClients.from_settings()` after plugin load.
   - Assert `SQLiteGraphStore`, `SQLiteVectorStore`, `SQLiteQAHistoryStore`, and desired file store are injected.
3. Scheduler patch test:
   - Construct `SyncScheduler` after plugin load.
   - Assert datastore is `SQLAlchemyDataStore`.
4. LLM patch test:
   - Assert `validate_model_string("copilot/gpt-4o") is None`.
   - Assert `validate_model_string("github/gpt-4o") is None`.
5. ChatGPT wrapper test:
   - Assert `fetch_chatgpt.py` imports the correct module.
6. Plugin API contract test:
   - When plugin mode is active, plugin-owned model exposure includes `copilot`/`github` provider groups if route patching is added.

## Suggested Integration Roadmap

### Phase 1: Fix Broken And Hidden Paths Without Touching Upstream

1. Add or fix plugin-owned ChatGPT fetch entrypoints.
2. Add `plugins/README.md` or `notes/plugin-mode.md` with current module paths.
3. Document the plugin-mode startup command.
4. Add plugin startup logs listing active plugins.
5. Add a plugin-side self-check command.

### Phase 2: Make Plugins User-Visible

1. Extend the available-models API with generic provider groups.
2. Prefer doing this through plugin-side route patching rather than editing core API files.
3. Document manual `copilot/` and `github/` model strings until a plugin-owned UI overlay exists.
4. Add plugin settings/status API from plugin activation if feasible.

### Phase 3: Stabilize Plugin-Local Extension Points

1. Add plugin-local registries/factories where possible:
   - Store backend registry
   - Scheduler datastore factory
   - LLM model provider registry
   - Source sync adapter registry
2. Make plugin loading declarative and configurable.
3. Add required plugin failure behavior.
4. Keep these under `plugins/` unless upstream later provides official hooks.

### Phase 4: Improve Embedded Mode Persistence

1. Persist Mongo snapshot on store shutdown and optionally after writes.
2. Use `SQLiteFileStore` for files if compatible.
3. Add migration/version metadata for SQLite plugin tables.
4. Add backup/export/import commands for embedded mode.

## Bottom Line

The plugins are properly attached in the plugin-specific runtime. They are not attached to the upstream/default runtime, and for this fork that separation is intentional.

The next layer should be fork-safe: plugin-aware startup docs, plugin-local settings validation, plugin-owned route/status/model exposure where possible, tests, and persistence hardening. The goal is not to absorb these changes into upstream-owned folders; the goal is to make the plugin layer reliable enough that upstream updates can be merged without repeatedly reworking custom code.