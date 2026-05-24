# Plugin Mode Working Rules

Date: 2026-05-02

## Purpose

This project is a fork of an upstream Beever Atlas repository. Custom behavior should be implemented as a plugin layer so upstream updates can be merged with minimal conflict.

## Boundary Rules

- Do not edit upstream-owned files unless there is no practical plugin-side alternative.
- Put customizations inside `plugins/` whenever possible.
- If a customization does not belong inside `plugins/`, put it in a clearly fork-owned path outside upstream-owned folders.
- Treat `src/`, `web/`, `bot/`, upstream docs, and default Docker files as upstream-owned by default.
- Docker/image changes are deferred for now.

## Runtime Rule

Plugin behavior only activates when the plugin bootstrap runs before the main app import.

Use:

```powershell
uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000
```

Avoid using the upstream entrypoint for plugin mode:

```powershell
uvicorn beever_atlas.server.app:app --host 0.0.0.0 --port 8000
```

That upstream entrypoint is useful as a baseline, but it will not activate `plugins.loader.load_plugins()`.

## Preferred Implementation Pattern

Use plugin-owned mechanisms first:

- `plugins/loader.py` for activation order and plugin metadata.
- Plugin-local settings validation before patches run.
- Monkey patches or adapters that apply before core services are constructed.
- Plugin-side CLI wrappers and self-check commands.
- Plugin-owned FastAPI route registration if extra API surface is needed.
- Plugin-local tests that prove patches still match upstream internals after an update.

## Update Workflow

See `fork-sync.md` for the full rebase-based sync strategy.

After every upstream sync:

1. Run plugin smoke tests or self-checks.
2. Verify `start_with_plugins.py` still loads all plugins.
3. Verify store, scheduler, LLM, and ChatGPT patches still apply.
4. Fix only the plugin layer unless an upstream edit is explicitly accepted.

## Current Plugin Areas

- `plugins/stores/embedded`: SQLite/mongomock local store replacements.
- `plugins/llms/copilot`: `copilot/` and `github/` model support.
- `plugins/sources/chatgpt`: ChatGPT history fetch/import/scheduled ingestion.

## Guiding Principle

The plugin layer should feel reliable and first-class for this fork while staying removable, reviewable, and isolated from upstream-owned code.