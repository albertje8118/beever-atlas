# chatgpt_copilot plugin

A self-contained extension for **beever-atlas** that adds:

1. **GitHub Copilot API LLM support** — use `copilot/<model>` strings (e.g. `copilot/gpt-4o`) to call models through your existing GitHub Copilot subscription. No separate PAT needed — your `gh` CLI login is sufficient.
2. **GitHub Models API support** — use `github/<model>` strings as an alternative, authenticated via a `GITHUB_TOKEN` PAT.
3. **ChatGPT history extractor** — fetch your ChatGPT conversations from a live Edge session via CDP.
4. **ChatGPT history importer** — feed those conversations into the beever-atlas RAG pipeline.

---

## GitHub Copilot API vs GitHub Models API

| | `copilot/<model>` | `github/<model>` |
|---|---|---|
| **Auth** | `gh auth token` (`gho_`) — no separate PAT | GitHub PAT (`GITHUB_TOKEN`) |
| **Endpoint** | `api.githubcopilot.com` | `models.inference.ai.azure.com` |
| **Models** | Your Copilot subscription (GPT-5.5, Claude Opus 4.7, Gemini 2.5, etc.) | GitHub Marketplace public models |
| **Setup** | Just `gh auth login` (already done) | Create a new PAT |

> **Note:** The `github/copilot-sdk` package is NOT what this uses — that SDK is for building Copilot CLI agents (subprocess/JSON-RPC protocol). This plugin uses the Copilot REST API directly.

---

## Why a plugin?

beever-atlas is a fork of an upstream repo.  Rather than patching the upstream
source files (which creates merge conflicts on every rebase), this plugin
monkey-patches the relevant modules at startup — zero upstream files touched.

When the upstream releases an update:

```bash
git pull upstream/main       # may have changes in src/beever_atlas/
# plugins/ directory is unaffected — re-run tests to verify
uv run python -m pytest tests/test_model_resolver.py -q
```

---

## Setup

### 1. Start the app with plugins

Instead of `uvicorn beever_atlas.server.app:app`, use:

```bash
uvicorn start_with_plugins:app --reload
# or with uv:
uv run uvicorn start_with_plugins:app --reload
```

### 2. GitHub Copilot API (recommended)

You only need to be logged in to the GitHub CLI — no separate token required:

```bash
gh auth login   # if not already authenticated
```

List available models:

```bash
uv run python -m plugins.chatgpt_copilot.list_models
```

Then set a `copilot/` model in `.env`:

```env
LLM_FAST_MODEL=copilot/gpt-4o-mini
LLM_QUALITY_MODEL=copilot/gpt-4o
```

The token is resolved automatically in this priority order:
1. `COPILOT_GITHUB_TOKEN` env var
2. `GH_TOKEN` env var
3. `GITHUB_TOKEN` env var
4. `gh auth token` (auto-detected from the GitHub CLI)

### 3. GitHub Models API (alternative, requires PAT)

Add your GitHub personal access token to `.env`:

```env
GITHUB_TOKEN=ghp_...
```

Then set a `github/` model:

```env
LLM_FAST_MODEL=github/gpt-4o-mini
LLM_QUALITY_MODEL=github/gpt-4o
```

Full model list: <https://github.com/marketplace/models>

---

## ChatGPT history extraction

### Step 1 — Launch Edge with remote debugging

Close all Edge windows, then run:

```powershell
Start-Process "msedge" -ArgumentList `
  "--remote-debugging-port=9222 --remote-allow-origins=* --restore-last-session"
```

### Step 2 — Log in to ChatGPT

Navigate to <https://chatgpt.com> and sign in.

### Step 3 — Extract the Bearer token

In Edge DevTools console (F12 on the chatgpt.com tab):

```js
fetch("/api/auth/session").then(r=>r.json()).then(d=>console.log(d.accessToken))
```

Save the token to `chatgpt_token.txt` in the project root.

### Step 4 — Fetch history

```bash
python fetch_chatgpt.py
# or directly:
python -m plugins.chatgpt_copilot.chatgpt.fetch
```

This saves all conversations to `chatgpt_history.json`.

### Step 5 — Import into beever-atlas (optional)

```bash
# Dry-run preview (no writes):
uv run python -m plugins.chatgpt_copilot.chatgpt.importer

# Actual import (requires docker compose up + API keys):
uv run python -m plugins.chatgpt_copilot.chatgpt.importer --ingest

# Import one conversation:
uv run python -m plugins.chatgpt_copilot.chatgpt.importer --ingest --conversation "My Project"
```

---

## Docker

The standard `Dockerfile` only copies `src/`.  To include the plugin, use
`Dockerfile.plugins` (provided at the project root):

```bash
docker build -f Dockerfile.plugins -t beever-atlas-with-plugins .
docker run -e COPILOT_GITHUB_TOKEN=gho_... -p 8000:8000 beever-atlas-with-plugins
```

Or override the CMD in your `docker-compose.override.yml`:

```yaml
services:
  app:
    volumes:
      - ./plugins:/app/plugins:ro
      - ./start_with_plugins.py:/app/start_with_plugins.py:ro
    environment:
      COPILOT_GITHUB_TOKEN: "${COPILOT_GITHUB_TOKEN}"
    command:
      - uvicorn
      - start_with_plugins:app
      - --host
      - "0.0.0.0"
      - --port
      - "8000"
```

---

## Re-applying after an upstream update

After pulling upstream changes, verify the plugin still works:

```bash
uv run python -m pytest tests/test_model_resolver.py -q
```

If `_validate_model_resolution` changed upstream, update the `else` branch in
`plugins/chatgpt_copilot/_llm_patch.py` → `_patch_provider()` to mirror the
new upstream validation logic.  The `copilot/`, `github/`, and `ollama_chat/`
intercept branches stay the same.
