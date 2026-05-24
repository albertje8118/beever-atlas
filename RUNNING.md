# Running beever-atlas

A personal knowledge graph that ingests ChatGPT conversation history and generates a wiki using GitHub Copilot as the LLM backend. No external database servers needed — everything runs locally via SQLite.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| [uv](https://docs.astral.sh/uv/) | latest | Python package manager |
| [Node.js](https://nodejs.org/) | ≥ 18 | For the frontend |
| [gh](https://cli.github.com/) | latest | GitHub CLI — used for Copilot token |

### GitHub Copilot access
Log in to GitHub CLI so the app can get a Copilot token automatically:
```bash
gh auth login
gh auth token   # should print a token
```
Or set the token explicitly in `.env`:
```
COPILOT_GITHUB_TOKEN=ghp_...
```

---

## 1. First-time setup

```bash
# 1. Install Python dependencies
uv sync

# 2. Install frontend dependencies
cd web && npm ci && cd ..

# 3. Copy environment file (already done if .env exists)
cp .env.example .env
```

Your `.env` is already configured for the **embedded / no-server** mode (SQLite only, no Neo4j, MongoDB, or Weaviate needed). The key settings that are already set:

```env
GRAPH_BACKEND=sqlite
WEAVIATE_BACKEND=null
MONGODB_BACKEND=mock
LLM_FAST_MODEL=copilot/gpt-5-mini
LLM_QUALITY_MODEL=copilot/gpt-5.4-mini
EMBED_MODEL=text-embedding-3-small   # via Copilot API
BEEVER_SQLITE_DB_PATH=.data/beever_atlas.db
```

---

## 2. Export ChatGPT history

Put your ChatGPT conversation export at the project root:

```
chatgpt_history.json   ← place file here
```

To get this file:
1. Go to **chatgpt.com → Settings → Data controls → Export data**
2. Download the ZIP, extract `conversations.json`
3. Rename it to `chatgpt_history.json` and place it in the project root

---

## 3. Run the backend

```bash
uv run uvicorn start_with_plugins:app --reload
```

The backend starts at **http://localhost:8000**

- API docs: http://localhost:8000/docs
- Swagger UI: http://localhost:8000/redoc

> **What `start_with_plugins.py` does:** loads all plugins in `plugins/` (SQLite stores, Copilot LLM, ChatGPT source, web UI patches) before starting the FastAPI app.

---

## 4. Run the frontend

In a **separate terminal**:

```bash
node plugins/web/run-vite.mjs dev
```

The frontend starts at **http://localhost:5173**

This wrapper is required in plugin mode. It injects the plugin-owned ChatGPT overlay into the frontend without modifying `web/src`.

---

## 5. Ingest ChatGPT history

Once both servers are running, trigger ingestion via the API or run the E2E test script:

### Option A — E2E test (ingest + query + generate wiki)
```bash
# Process 1 conversation (fast, ~5 min)
uv run python -X utf8 scripts/e2e_test.py --limit 1

# Process all conversations
uv run python -X utf8 scripts/e2e_test.py
```

### Option B — API trigger (once the UI is running)
Use the ChatGPT panel in the web UI:
1. Open http://localhost:5173
2. Go to **Settings** and choose **ChatGPT History** from **Add Connection**
3. Connect with either the browser session mode or cached history file mode
4. Select the conversations you want to materialize as channels
5. Use **Refresh Now** from the ChatGPT connection card when you want to resync

---

## 6. Summary of ports

| Service | URL | Command |
|---------|-----|---------|
| Backend API | http://localhost:8000 | `uv run uvicorn start_with_plugins:app --reload` |
| Frontend UI | http://localhost:5173 | `node plugins/web/run-vite.mjs dev` |
| API Docs | http://localhost:8000/docs | — |

---

## Troubleshooting

### `GET / HTTP/1.1" 404 Not Found`
The backend at `:8000` only serves `/api/...` and `/docs`. The web UI is served separately by Vite at `:5173`. Visit **http://localhost:5173** for the app.

### `ModuleNotFoundError: No module named 'beever_atlas'`
Always run backend with `uv run` (not plain `python`):
```bash
uv run uvicorn start_with_plugins:app --reload
```

### Copilot `Access to this endpoint is forbidden`
Your token may have expired. Re-authenticate:
```bash
gh auth login
```
Or check your `COPILOT_GITHUB_TOKEN` in `.env`.

### SQLite database reset
```bash
Remove-Item .data\beever_atlas.db   # Windows PowerShell
rm .data/beever_atlas.db            # Linux / macOS
```

### Rebuild frontend after plugin changes
```bash
node plugins/web/run-vite.mjs build
```

---

## Data flow

```
chatgpt_history.json
        │
        ▼
  Ingestion pipeline  (Copilot LLM)
        │
        ├──► SQLite Vector Store  (semantic search via embeddings)
        ├──► SQLite BM25 / FTS5   (keyword search)
        ├──► SQLite Graph Store   (entities & relationships)
        └──► MongoDB Mock / DocDB (fact documents & clusters)
                │
                ▼
        Wiki Compiler  (Copilot LLM)
                │
                ▼
        Wiki pages served at http://localhost:5173
```
