# chatgpt_copilot plugin

A self-contained extension for **beever-atlas** that adds:

1. **GitHub Models / Copilot LLM support** — use `github/<model>` strings (e.g. `github/gpt-4o-mini`) anywhere beever-atlas accepts a model name.
2. **ChatGPT history extractor** — fetch your ChatGPT conversations from a live Edge session via CDP.
3. **ChatGPT history importer** — feed those conversations into the beever-atlas RAG pipeline.

---

## Why a plugin?

beever-atlas is a fork of an upstream repo.  Rather than patching the upstream
source files (which creates merge conflicts on every rebase), this plugin
monkey-patches the relevant modules at startup — zero upstream files touched.

When the upstream releases an update:

```
git pull upstream/main       # may have changes in src/beever_atlas/
# plugins/ directory is unaffected — re-run tests to verify
uv run python -m pytest tests/test_model_resolver.py -q
```

---

## Setup

### 1. Start the app with plugins

Instead of `uvicorn beever_atlas.server.app:app`, use:

```bash
uvicorn start_with_plugins:app --host 0.0.0.0 --port 8000
# or with auto-reload:
uvicorn start_with_plugins:app --reload
# or with uv:
uv run uvicorn start_with_plugins:app --reload
```

### 2. GitHub Models (optional)

Add your GitHub personal access token to `.env`:

```env
GITHUB_TOKEN=ghp_...
```

No special scopes needed for public GitHub Models.
An active Copilot subscription is required for Copilot-specific model access.

Then set a `github/` model as your LLM tier in `.env`:

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
docker run -e GITHUB_TOKEN=... -p 8000:8000 beever-atlas-with-plugins
```

Or override the CMD in your `docker-compose.override.yml`:

```yaml
services:
  app:
    volumes:
      - ./plugins:/app/plugins:ro
      - ./start_with_plugins.py:/app/start_with_plugins.py:ro
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
new upstream validation logic.  The `github/` and `ollama_chat/` intercept
branches stay the same.
