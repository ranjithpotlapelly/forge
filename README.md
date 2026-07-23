# Forge

A self-hosted repo copilot: it indexes a codebase, answers deep questions with
file+line citations, and — with your approval — plans a fix, edits the code,
runs the tests, and opens a pull request.

Built on a swappable-by-design architecture: every layer (model, retrieval,
state, tools, tracing) sits behind a small interface, so the `$0` local stack
upgrades to hosted services one file at a time.

---

## Setup (Windows, CPU-first)

Your machine (Ryzen 7 7735U, 16 GB, integrated GPU) runs everything locally on
CPU. Inference is slow but fully functional — perfect for building and learning.

### 1. Install Ollama and start it
Download from https://ollama.com/download, install, then in a terminal:
```
ollama serve
```
Leave this running. Ollama listens on http://localhost:11434.

### 2. Pull the models
```
ollama pull qwen3:8b            # primary reasoning model (~5 GB)
ollama pull qwen3:4b            # faster model for dev iteration (~2.5 GB)
ollama pull qwen3-coder         # for the code-edit step (Phase 7)
ollama pull nomic-embed-text    # embeddings for RAG (light)
```
Tip: while developing, set `llm.model: qwen3:4b` in `config.yaml` for ~2-3x
faster turns; switch back to `qwen3:8b` to check final answer quality.

### 3. Python environment
Requires Python 3.11+.
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configuration
```
copy .env.template .env
```
Leave `GITHUB_TOKEN` blank for now — it's only needed in Phase 7.

### 5. Verify the scaffold (Phase 1)
```
python -m app.smoke_test
```
This checks that config loads, Ollama is reachable, and your models are present.

### 6. Start Phoenix (Phase 6, tracing)
```
docker compose up -d phoenix
```
The native `arize-phoenix` pip package needs a C++ toolchain to build one of
its dependencies (`sqlean-py`, no prebuilt wheels) — not a given on every
CPU-only Windows box, so Phoenix runs as a container instead. The rest of
Forge still runs natively. UI: http://localhost:6006.

### 7. Chat UI (Phase 8)
```
python -m app.run_chainlit run app/chainlit_app.py -w --port 8501
```
Use this instead of `chainlit run` directly. Chainlit's CLI unconditionally
calls `nest_asyncio.apply()` at import time, which breaks anyio's event-loop
detection on this Python version — static assets (JS/CSS/favicon) come back
503/500 and the page loads blank. `app/run_chainlit.py` neutralizes that patch
before Chainlit's CLI module loads. UI: http://localhost:8501.

Chainlit's real default port is 8000, but on this machine that's already
taken by an unrelated project's container — pass `--port` explicitly to
avoid it (or drop the flag if 8000 is free on yours).

### 8. Full stack in Docker (Phase 9)
```
docker compose up -d --build
```
Builds and runs Forge itself alongside Phoenix. Ollama still runs natively —
the container reaches it via `host.docker.internal` (set in
docker-compose.yml). UI: http://localhost:8010 (8000 is taken on this
machine — see above).

`.data/` (Chroma index, SQLite store/checkpoints, MCP workspace) lives in a
**named volume** (`forge_data`), not a bind mount — SQLite's file locking
doesn't work reliably over Docker Desktop's Windows bind-mount filesystem
translation and the container crashed on startup (`disk I/O error`) until
this was switched. Tradeoff: it's not directly browsable from Windows, and
it's a separate store from your native `.data/` — indexing done natively
doesn't show up in the container and vice versa. To index the same test
docs into the container's own volume: `docker compose exec forge python -m
app.retriever_smoke_test`.

The Dockerfile builds with `uv`, not `pip` — `pip install -r requirements.txt`
was timing out against a flaky PyPI connection during this build; `uv` is
faster and retries more robustly. `requirements.txt` was also trimmed of
`llama-index`/`tree-sitter` (Phase 3 went a different route: chromadb + raw
Ollama embeddings, never used) and the full `arize-phoenix` package (Phase 6
already replaced it with `arize-phoenix-otel` — the full package still needs
a C++ toolchain to build `sqlean-py`, same blocker as Phase 6, just on Linux
instead of Windows).

---

## Repo layout

```
core/       PORTS — interfaces only. No vendor imports, no domain logic.
adapters/   Infra implementations behind those ports (Ollama, Chroma, SQLite...).
product/    Forge's business logic (code models, prompts, workflow). No infra here.
app/        Composition root: loads config, wires adapters, runs the app.
```

The rule that keeps this upgradeable: **`product/` never imports a vendor, and
`core/` never imports `product/` or an adapter.** Dependencies point inward.
When a second product ever appears, `product/` becomes `products/forge/` and the
shared pieces are promoted into `core/` — no rewrite.

---

## Build roadmap

| Phase | Layer | Status |
|------:|-------|--------|
| 1 | Scaffold (structure, config, smoke test) | done |
| 2 | Reasoning (Ollama adapter) | done |
| 3 | Knowledge (code-aware RAG over Chroma) | done |
| 4 | Orchestration (LangGraph decision graph) | done |
| 5 | State (SQLite checkpointer + store) | done |
| 6 | Observability (Phoenix tracing) | done |
| 7 | Action (MCP tools + approval gates + PR flow) | in progress — tools + approval gate done, PR flow (commit/push/open_pr) pending |
| 8 | Interface (Chainlit chat) | done |
| 9 | Deployment (Docker packaging) | done |

We build one phase per step, each testable on its own before the next.
