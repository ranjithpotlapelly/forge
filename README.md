# Forge

**Repo:** https://github.com/ranjithpotlapelly/forge

A self-hosted repo copilot: it indexes a codebase, answers deep questions with
file+line citations, and — with your approval — plans a fix, edits the code,
runs the tests, and opens a pull request.

Built on a swappable-by-design architecture: every layer (model, retrieval,
state, tools, tracing) sits behind a small interface, so the `$0` local stack
upgrades to hosted services one file at a time.

---

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

## Upgrade path: pointing code_model at a hosted API (Phase 22)

Local CPU inference caps answer and patch quality, and the code-edit step
(`code_model`) feels it most — it's the smallest, fastest local model, since
patches are generated far more often than plans. `adapters/llm_openai_compatible.py`
implements the same `core.llm.LLMClient` port `adapters/llm_ollama.py` does,
so swapping one for the other is a `config.yaml` edit — nothing in `core/`,
`product/`, or anywhere else a model is consumed changes.

To move **only** the code-edit step to a hosted, OpenAI-compatible endpoint
(OpenAI itself, or any provider that mirrors its `/chat/completions` shape)
and keep `llm` (the primary reasoning model) and `embeddings` local and free:

1. Add your key to `.env` (already reserved there, blank by default):
   ```
   CODE_MODEL_API_KEY=sk-...
   CODE_MODEL_BASE_URL=https://api.openai.com/v1
   ```
2. In `config.yaml`, change **only** the `code_model:` block:
   ```yaml
   code_model:
     adapter: openai_compatible
     model: gpt-4o-mini
     base_url: ${CODE_MODEL_BASE_URL}
     api_key: ${CODE_MODEL_API_KEY}
   ```
3. Leave `llm:` and `embeddings:` exactly as they are. Nothing else changes —
   `product/code_edit.py` calls `code_model.generate()`/`.propose_edit()` the
   same way regardless of which adapter is behind it.

The same adapter works for `llm:` too (any slot typed `core.llm.LLMClient`),
if you'd rather upgrade reasoning quality instead of (or as well as) the
code-edit step.

---

## Performance tuning (Phase 23)

On this machine (Ryzen 7 7735U / Radeon 680M iGPU / 16 GB shared RAM,
CPU-first Ollama inference) a Q&A answer used to take 2-3 minutes. Config-only
fixes — `adapters/llm_ollama.py` still the only file that talks to Ollama, no
inference-engine changes:

- **`answer_model`** (`config.yaml`): the Q&A path (`app/chainlit_app.py`)
  uses a separate, smaller/faster model (default `qwen3:4b`) than `llm.model`
  (`qwen3:8b`, kept for planning/task work). `qwen3:8b` is a thinking model —
  its `<think>...</think>` reasoning (see `_strip_think` in
  `adapters/llm_ollama.py`) is often the single biggest latency cost on a
  simple lookup question, independent of context size.
- **`answer_num_ctx`** (default `8192`, vs. `llm.num_ctx: 16384`): every
  request now always sends an explicit `num_ctx` (`adapters/llm_ollama.py`
  logs the actual outgoing `options`/`keep_alive` at debug level) — Ollama
  silently falls back to its own small built-in default when it's omitted,
  and an oversized value burns KV-cache/generation time for no benefit on a
  short Q&A prompt.
- **`answer_top_k` / `answer_max_expanded`**: the answer path retrieves fewer
  chunks than the general `retriever.top_k` and expands only the
  best-ranked few to full source in the prompt (the rest get a path-only
  reference line) — see `build_answer_messages()` in
  `adapters/engine_langgraph.py`.
- **`llm.keep_alive`** (default `30m`): sent on every request so Ollama keeps
  the model loaded between turns instead of unloading it after its default
  ~5 minute idle timeout and paying a full reload on the next question.
  Server-side alternative (applies to every model, not just Forge's calls):
  set the `OLLAMA_KEEP_ALIVE` environment variable before `ollama serve`,
  e.g. `OLLAMA_KEEP_ALIVE=30m ollama serve`.
- **`llm.show_timing`** (default `true`): prints a one-line
  `[timing] retrieve 0.4s | prompt ~2100 tok | first-token 4.2s | total 22.1s | 9.3 tok/s`
  summary after each Chainlit answer.

### Benchmark script
```
python -m app.bench --model qwen3:4b --num-ctx 8192 --top-k 5
python -m app.bench --model qwen3:8b --num-ctx 16384 --top-k 8
```
Runs a fixed question N times per `--model`/`--num-ctx`/`--top-k`
combination (each flag is repeatable — pass a flag twice in one invocation
to get a side-by-side comparison table) and reports median time-to-first-
token, median total time, and tokens/second.

### Vulkan on the Radeon 680M (measured — this is the setting that actually helped)
The 680M can't use ROCm on Windows, but Ollama has experimental Vulkan
support for AMD iGPUs. This is an environment-variable change to the Ollama
server process, not a Forge config or code change — nothing in this repo
depends on it.

1. **Both env vars are required**, not just the one Ollama's docs lead with:
   ```
   OLLAMA_VULKAN=1
   OLLAMA_IGPU_ENABLE=1
   ```
   `OLLAMA_VULKAN=1` alone enumerates the iGPU but then drops it — the
   server log says so explicitly: `msg="dropping integrated GPU; to enable,
   set OLLAMA_IGPU_ENABLE=1"`. Without the second variable, Ollama silently
   falls back to CPU and everything below still applies unchanged.
2. Set both, then restart Ollama. On Windows the desktop tray app
   (`ollama app.exe`) needs the variables set as **persistent user
   environment variables** (System Properties → Environment Variables, or
   `setx OLLAMA_VULKAN 1` / `setx OLLAMA_IGPU_ENABLE 1` from a fresh
   terminal — `setx` only affects shells opened after it runs) and the tray
   app restarted; running `ollama serve` directly in a terminal that already
   has both `$env:` variables set works immediately, for testing.
3. Verify with `ollama ps` after a request: `PROCESSOR` should read `100%
   GPU`, not `100% CPU`. If it still says CPU, `OLLAMA_IGPU_ENABLE` didn't
   take — check the server startup log for the "dropping integrated GPU"
   line above.
4. Measured on this machine (`app/bench.py`, median of 3 runs, real
   Q&A question, hybrid retriever context):

   | Config | CPU only | Vulkan + iGPU enabled | Speedup |
   |---|---|---|---|
   | `qwen3:4b`, `num_ctx=8192`, `top_k=5` | 347.7s total, 1.6 tok/s | 146.2s total, 3.3 tok/s | ~2.4x |
   | `qwen3:8b`, `num_ctx=16384`, `top_k=8` | 301.0s total, 1.3 tok/s | 213.8s total, 1.9 tok/s | ~1.4x |

   A real gain, but not enough alone to hit a 20-40s target on this
   hardware — an iGPU shares system memory bandwidth with the CPU, so it
   isn't a discrete-GPU-sized win. Ollama's log also warned `AMD driver is
   too old. Update your AMD driver to enable GPU inference` on this
   machine — worth updating the AMD graphics driver to see if it unlocks
   more headroom, independent of anything above.

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
| 7 | Action (MCP tools + approval gates + PR flow) | done |
| 8 | Interface (Chainlit chat) | done |
| 9 | Deployment (Docker packaging) | done |
| 10 | Ingest source (structure-aware repo indexing) | done |
| 11 | Code-edit tool (dedicated code_model + gated write_file) | done |
| 12 | Commit/push tools (gated, sandboxed git identity) | done |
| 13 | `open_pr` tool (GitHub REST API, PR-review gate) | done |
| 14 | Task path (plan → approve → edit → test → retry ≤3 → commit) | done |
| 15 | Hybrid retriever (SQLite FTS5 lexical + semantic, RRF fusion) | done |
| 16 | Chainlit Q&A: streaming answers, expandable citations, `/index` | done |
| 17 | Chainlit task-flow UI: plan card, Approve/Edit/Reject, PR gate | done |
| 18 | `fetch_issue` tool (read-only GitHub issue fetch, `#N` detection in chat) | done |
| 19 | Run history (`runs`/`run_steps` tables, `core.run_history.RunHistory`, `python -m app.history`) | done |
| 20 | Approval gate on `interrupt()` + checkpoint (survives a restart), `python -m app.resume` | done |
| 21 | Conversation history in Chainlit (`/history`, resume/delete a thread) | done |
| 22 | Hosted (OpenAI-compatible) LLM adapter for `llm`/`code_model` | done |
| 23 | Q&A latency tuning (`answer_model`, per-path `num_ctx`, `keep_alive`, prompt trimming, `app/bench.py`) | done |

We build one phase per step, each testable on its own before the next.

Phase 10 fills a gap left open since Phase 3: `core/ingest.py` was scaffolded
in Phase 1 but never implemented — every earlier phase indexed 3 hardcoded
test documents instead of real code. `adapters/ingest_fs.py` now walks a
repo and yields real per-function/class chunks via Python's stdlib `ast`
(not tree-sitter — dropped in Phase 9 as an unused dependency, and `ast`
already does the job for the language this repo is written in).

Phase 11 fills the other gap `code_model` had sat reserved for since Phase
2: `product/code_edit.py` asks it for a full replacement file and applies it
through the existing approval-gated `write_file` MCP tool — no new approval
mechanism, just wiring. It only operates inside the sandboxed workspace
(`.data/workspace/`), same boundary Phase 7 set for `write_file` — it can't
touch the live Forge source tree.

Phase 12 adds `commit`/`push` to the same local MCP server, both gated the
same way `write_file` already was — `commit`/`push` were in
`forge.require_approval_for` since Phase 1, just unimplemented until now.
Both operate on `.data/workspace/`'s own git repo (a local-only identity, no
`--global` config touched).

Phase 13 adds `open_pr`, the one entry in that list left unbuilt since Phase
1. Unlike `commit`/`push` it isn't an MCP tool: `adapters/github_pr.py`
implements `core.tools.Tool` directly, prints the full diff plus the PR
title/body to the human, then calls the GitHub REST API
(`POST /repos/{owner}/{repo}/pulls`) with a `GITHUB_TOKEN` read once by
`app/config_loader.py` — the only place that ever touches the raw value.
Same approval gate as everything else in `forge.require_approval_for`, so a
denied `open_pr` never reaches the network (proven in
`app/open_pr_smoke_test.py` against a real local HTTP server, not just
reasoned about).

See [WORKFLOW.md](WORKFLOW.md) for the full architecture walkthrough
(Java/Spring analogies, end-to-end request flows, diagrams) and
[WORKFLOW_CHANGES.md](WORKFLOW_CHANGES.md) for what changed from the
original design plan.
