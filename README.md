# Forge

**Repo:** https://github.com/ranjithpotlapelly/forge

A self-hosted repo copilot: it indexes a codebase, answers deep questions with
file+line citations, and — with your approval — plans a fix, edits the code,
runs the tests, and opens a pull request.

Built on a swappable-by-design architecture: every layer (model, retrieval,
state, tools, tracing) sits behind a small interface, so the `$0` local stack
upgrades to hosted services one file at a time.

> **Setting this up on a teammate's machine?** [docs/TEAM_SETUP.md](docs/TEAM_SETUP.md)
> is the condensed, Docker-first version of everything below — 10-15
> minutes, no architecture explanation. Come back here for the *why*.

---

### 1. Install Ollama and start it
Download from https://ollama.com/download, install, then in a terminal:
```
ollama serve
```
Leave this running. Ollama listens on http://localhost:11434.

### 2. Pull the models
```
ollama pull qwen3:8b            # primary reasoning model (`llm`) (~5 GB)
ollama pull qwen3:4b            # faster model for the Q&A path (`answer_model`) (~2.5 GB)
ollama pull nomic-embed-text    # embeddings for RAG (light)
```
`qwen3:4b` isn't just "for dev iteration" — `config.yaml`'s `answer_model`
section uses it for every live Q&A answer by default (Phase 23), separately
from `llm` (`qwen3:8b`, used for planning/task classification). Both are
pulled up front for that reason, not as an either/or choice.

Skip `qwen3-coder` — `code_model` (the code-edit step) defaults to a hosted,
OpenAI-compatible endpoint in the checked-in `config.yaml`, not local Ollama.
Only pull `qwen3-coder` if you revert `code_model` back to `adapter: ollama`
(see "Upgrade path" below).

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
Leave `GITHUB_TOKEN` blank for now — it's needed for `open_pr`/`fetch_issue`
(Phase 13/18), not for indexing or Q&A.

`CODE_MODEL_NAME`/`CODE_MODEL_FALLBACK_1`/`CODE_MODEL_FALLBACK_2`/
`CODE_MODEL_BASE_URL`/`CODE_MODEL_API_KEY` are a different matter: unlike
everything else in this file, they're **not optional-to-leave-blank**.
`config.yaml`'s checked-in `code_model:` block already points at a hosted,
OpenAI-compatible endpoint (`adapter: openai_compatible`) rather than local
Ollama — see the Upgrade path section below — so the code-edit step of the
task path won't work until these are filled in with real values. `llm`,
`answer_model`, and `embeddings` all stay local/free regardless.

### 5. Verify the scaffold (Phase 1)
```
python -m app.smoke_test
```
This checks that config loads, Ollama is reachable, and your models are present.

### 6. Start everything
```
powershell -File scripts\run-stack.ps1
```
Run from the repo root. This is the current, single entry point — it brings
up Ollama (native host process; starts `ollama serve` if it isn't already
running), Phoenix (Docker container, tracing UI: http://localhost:6006), and
Forge itself (Docker container, chat UI: http://localhost:8010), verifying
each is actually reachable before starting the next. It also tears down and
recreates the stack cleanly on every run (`docker compose down` first), and
kills orphaned `llama-server.exe` processes Ollama itself has lost track of.

Two things worth knowing about *why* it's built this way, not just *how* to
run it:

- **Phoenix is a container, not a native process.** The native
  `arize-phoenix` pip package needs a C++ toolchain to build one of its
  dependencies (`sqlean-py`, no prebuilt wheels) — not a given on every
  CPU-only Windows box.
- **Forge itself is a container too (Phase 9), not `python -m
  app.run_chainlit`.** An earlier version of this doc had you run Chainlit
  natively on port 8501 with a `nest_asyncio` workaround — that's gone.
  Everything now runs in Docker on port **8010** (not Chainlit's real
  default, 8000 — already taken by an unrelated project's container on this
  machine; change it in `docker-compose.yml` if 8000 is free on yours).
  `app/run_chainlit.py` still exists and still works for running Chainlit
  natively if you want to debug outside Docker, but it's no longer the
  documented path.

`.data/` (Chroma index, SQLite store/checkpoints, MCP workspace) lives in a
**named volume** (`forge_data`), not a bind mount — SQLite's file locking
doesn't work reliably over Docker Desktop's Windows bind-mount filesystem
translation and the container crashed on startup (`disk I/O error`) until
this was switched. Tradeoff: it's not directly browsable from Windows.

The Dockerfile builds with `uv`, not `pip` — `pip install -r requirements.txt`
was timing out against a flaky PyPI connection during this build; `uv` is
faster and retries more robustly. `requirements.txt` was also trimmed of
`llama-index`/`tree-sitter` (Phase 3 went a different route: chromadb + raw
Ollama embeddings, never used) and the full `arize-phoenix` package (Phase 6
already replaced it with `arize-phoenix-otel` — the full package still needs
a C++ toolchain to build `sqlean-py`, same blocker as Phase 6, just on Linux
instead of Windows). It also installs `nodejs`/`npm`/`chromium` (with
`CHROME_BIN` set) so the task path's `run_tests` tool can run a JS/TS
project's own `npm test` inside the sandbox, alongside the Maven/Gradle/
pytest support it already had.

### 7. Index a codebase and ask a question

Type in the chat, once Forge is running:
```
/index <path>
```
`<path>` must be reachable *inside the container* — an in-repo path (e.g.
`/index .`) works as-is; a path outside the repo needs a bind mount added to
`docker-compose.yml`'s `forge` service first (`- C:/some/host/path:/mnt/
host_projects:ro`), then `/index /mnt/host_projects/...`. Then just ask a
question — the answer streams in with expandable citation chips underneath.

See [WORKFLOW.md](WORKFLOW.md) Section 11 for the full walkthrough,
including how to trigger the plan/edit/test/commit/push/PR task path from
the same chat.

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

## Upgrade path: code_model already points at a hosted API (Phase 22)

Local CPU inference caps answer and patch quality, and the code-edit step
(`code_model`) feels it most — it's the smallest, fastest local model, since
patches are generated far more often than plans. `adapters/llm_openai_compatible.py`
implements the same `core.llm.LLMClient` port `adapters/llm_ollama.py` does,
so swapping one for the other is a `config.yaml` edit — nothing in `core/`,
`product/`, or anywhere else a model is consumed changes.

**This isn't a hypothetical you still need to set up — `config.yaml`'s
checked-in `code_model:` block already uses it:**

```yaml
code_model:
  adapter: openai_compatible
  models:
    - ${CODE_MODEL_NAME}
    - ${CODE_MODEL_FALLBACK_1}
    - ${CODE_MODEL_FALLBACK_2}
  base_url: ${CODE_MODEL_BASE_URL}
  api_key: ${CODE_MODEL_API_KEY}
```

`models:` (a list, not a single `model:` string) is a server-side fallback
chain, sized for a provider like OpenRouter: `adapters/llm_openai_compatible.py`
tries `CODE_MODEL_NAME` first, then `FALLBACK_1`, then `FALLBACK_2`, moving
to the next on a rate limit, moderation block, downtime, or any other error
— no client-side retry logic needed. `app/wiring.py` drops blank entries
before building the list, so you can leave `FALLBACK_1`/`FALLBACK_2` empty
if you don't want fallbacks and just fill in `CODE_MODEL_NAME`.

To make this actually work, fill in `.env` (see Step 4 above):
```
CODE_MODEL_API_KEY=sk-or-v1-...
CODE_MODEL_BASE_URL=https://openrouter.ai/api/v1
CODE_MODEL_NAME=<a model slug your provider serves>
CODE_MODEL_FALLBACK_1=
CODE_MODEL_FALLBACK_2=
```
(Substitute any provider that mirrors the OpenAI `/chat/completions` shape
— OpenRouter, OpenAI itself, etc.)

`llm:` (reasoning/planning) and `embeddings:` stay on local Ollama —
`code_model` is the only slot this repo currently ships pointed remote. The
same adapter works for `llm:` too (any slot typed `core.llm.LLMClient`), if
you'd rather upgrade reasoning quality, or revert `code_model` to Ollama by
changing `adapter: openai_compatible` back to `adapter: ollama` and adding
a `model:` field (e.g. `qwen3-coder`) instead of `models:`.

---

## Optional add-ons

Both off by default — nothing about existing behavior changes unless you
opt in. Neither touches `core/` or any protected file; each is a sibling
adapter behind an existing port.

**Slack notifications** (`adapters/tool_slack.py`) — a CPU-bound task can
take minutes; set `SLACK_WEBHOOK_URL` in `.env` (a Slack Incoming Webhook
URL) and Forge posts a short status line ("task complete", "PR opened:
&lt;url&gt;") at the end of a run. Leave it blank and `notify_slack` is a
silent no-op — a failed post is caught and logged, never raised, so it can't
fail a task either way.

**Qdrant retriever** (`adapters/retriever_qdrant.py`) — an alternative to
the default embedded Chroma, behind the same `core.retriever.Retriever`
port, same skeleton-index contract (full chunk text + path/start_line/
end_line/symbol metadata), same Ollama embedder. Switch to it with:
```yaml
retriever:
  adapter: qdrant       # was: chroma
  qdrant_url: ${QDRANT_URL}       # a running server, e.g. docker run qdrant/qdrant
  qdrant_path: ./.data/qdrant     # or: embedded/on-disk mode, no server needed (used when qdrant_url is blank)
```
Re-index after switching — Chroma and Qdrant don't share data. `adapter:
chroma` stays the default; nothing needs to change unless you flip this.

---

## Retrieval quality eval

`eval/dataset.yaml` + `eval/run.py` — a fixed set of questions with
known-correct files/symbols for whatever's currently indexed, scored
automatically: retrieval has a definite right answer (did the correct
file/symbol come back?), unlike judging answer prose, which needs an LLM
judge or a human and comes later.

```
python -m eval.run                          # per-case PASS/FAIL table + hit@k / MRR / precision@k
python -m eval.run --k 3                    # override top-k
python -m eval.run --min-hit-at-k 0.8       # regression-gate threshold (default: 0.7)
python -m eval.run --compare --compare-k 1  # side-by-side: current top_k vs. k=1
python -m eval.run --compare --compare-config config.staging.yaml  # before/after a chunking change
```

Exits non-zero when `hit@k` drops below `--min-hit-at-k` — that's the exit
code `.github/workflows/retrieval-eval.yml` gates on (see below). Add cases
to `eval/dataset.yaml` as the indexed repo grows or a real question exposes
a gap — each is just a `question` + `expect_files` (+ optional
`expect_symbols`) entry; see the comment block at the top of that file.

**CI gate**: `.github/workflows/retrieval-eval.yml` runs `python -m
eval.run` on every push/PR to `main` (plus manual dispatch), on a
**self-hosted runner only** — there's no GitHub-hosted-runner path here,
since the Chroma index, the lexical FTS5 DB, and `.env` are all gitignored
local state (`.data/*`, see `.gitignore`) that only exists on a machine
already running `ollama serve` with a repo indexed; a stock `ubuntu-latest`
runner starts with none of it and there's no cheap way to rebuild it fresh
on every run. Register a self-hosted runner on this machine (repo Settings
→ Actions → Runners → New self-hosted runner) for the workflow to actually
execute — until then the jobs just queue.

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
(not tree-sitter for Python — dropped in Phase 9 as an unused dependency,
and `ast` already does the job for Python). Tree-sitter came back later for
JavaScript, TypeScript, and Java once the project's actual daily-driver
target repo became an Angular/TypeScript app (Go was left on whole-file
chunking — not an active target repo):
`adapters/ingest_fs.py`'s `_ts_chunks` gives them the same per-method chunk
granularity Python has, walking into class/interface bodies since a typical
Angular component or Java class puts nearly all its code inside one class
per file — chunking only at class granularity wouldn't have actually fixed
anything for those two languages specifically.

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
(Java/Spring analogies, end-to-end request flows, diagrams), including
Section 13 for what still differs from the original design plan and why.
