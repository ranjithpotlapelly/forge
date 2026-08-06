# Recreating Forge on your machine (Docker path)

For teammates spinning Forge up fresh via Docker. This is the short,
step-by-step version — for *why* things are built this way, see
[`README.md`](../README.md) and [`WORKFLOW.md`](../WORKFLOW.md).

You'll have Forge's chat UI answering questions about a codebase in about
10-15 minutes (most of that is pulling model weights).

---

## Before you start

- **Docker Desktop**, installed and running (Windows/Mac: WSL2 or Hyper-V
  backend enabled). Verify with `docker info` — if it errors, start Docker
  Desktop and wait ~30-60s for its daemon to come up before retrying.
- **Ollama**, installed from https://ollama.com/download. **This stays
  native on your host, never in a container.** It needs direct access to
  your GPU (Vulkan/Metal/CUDA depending on platform) for reasonable
  inference speed, and containerizing it just adds RAM overhead for no
  benefit — the app in Docker reaches it over `host.docker.internal`
  instead.
- **Git**, to clone the repo.

## 1. Start Ollama and pull the models

```
ollama serve
```
Leave this running (or let the Ollama tray app manage it). Then, in another
terminal:
```
ollama pull qwen3:8b
ollama pull qwen3:4b
ollama pull nomic-embed-text
```
~8 GB total. `qwen3-coder` is **not** needed — the checked-in `config.yaml`
points the code-edit step at a hosted API by default (see step 3).

## 2. Clone the repo

```
git clone https://github.com/ranjithpotlapelly/forge.git
cd forge
```

## 3. Configure `.env`

```
copy .env.template .env          # Windows
cp .env.template .env            # Mac/Linux
```

Open `.env` and fill in:

| Variable | Required? | Notes |
|---|---|---|
| `OLLAMA_HOST` | No | Ignored by the Docker path — `docker-compose.yml` points the container at `host.docker.internal:11434` for you |
| `PHOENIX_ENDPOINT` | No | Same — `docker-compose.yml` points it at the `phoenix` container service by name |
| `FORGE_REPO_PATH` | Only for the task path | Also ignored by the Docker path (see "task path" section below) — Q&A/`/index` don't need it, but leaving it blank breaks `prepare_workspace`, `python -m app.index` with no argument, and `graph_expand`'s context reads |
| `GITHUB_TOKEN` | Only for `open_pr`/`fetch_issue` | Leave blank to start; Q&A and indexing don't need it |
| `CODE_MODEL_API_KEY` / `CODE_MODEL_BASE_URL` / `CODE_MODEL_NAME` | Only for the task path's code-edit step | `config.yaml`'s `code_model:` block already points at a hosted, OpenAI-compatible endpoint (e.g. OpenRouter) — Q&A works fine without these, but editing code won't. See README's "Upgrade path" section |
| `SLACK_WEBHOOK_URL` | No, optional | A Slack Incoming Webhook — if set, Forge posts a short "task complete"/"PR opened" ping. Leave blank and it's silently disabled |
| `QDRANT_URL` | No, optional | Only matters if you also flip `retriever.adapter` to `qdrant` in `config.yaml` (see below) — irrelevant with the default Chroma retriever |

**Minimum to get Q&A working: leave everything blank except pulling the
models in step 1.** Everything else is opt-in.

## 4. Bring the stack up

```
docker compose up -d --build
```
First run builds the image (installs git/nodejs/chromium for the task
path's `run_tests` tool — a few minutes). Subsequent runs are fast.

Windows users can instead run `powershell -File scripts\run-stack.ps1` from
the repo root — it does the same `docker compose up`, but also starts
Ollama for you if it isn't already running, and verifies each service
(Ollama → Phoenix → Forge) actually answers before starting the next,
failing fast with a clear message instead of a silent hang.

## 5. Verify it's up

```
docker compose ps
```
Both `forge-forge-1` and `forge-phoenix-1` should show `Up`. Then:

- **Forge chat UI**: http://localhost:8010
- **Phoenix tracing UI**: http://localhost:6006

If the chat loads but nothing responds, the container usually can't reach
Ollama — check `docker compose logs forge` and confirm `ollama serve` is
actually running on the host (`ollama ps` in another terminal).

## 6. Try it

In the chat:
```
/index .
```
This indexes Forge's own codebase (no config changes needed — it's already
inside the container). Once it finishes, ask a question, e.g. *"how does
the approval gate work?"* — the answer streams in with clickable
`path:line` citations.

---

## If you want the task path (edit/test/commit/PR) working too

The steps above get Q&A working against Forge's own code with zero
customization. The task path (propose a plan → edit → test → commit → push
→ open a PR) targets a *different* repo — `forge.repo_path` in
`config.yaml` — and that value is machine-specific. To point it at your own
project:

1. Add a read-only bind mount for your repo's parent folder in
   `docker-compose.yml`'s `forge` service, under `volumes:`:
   ```yaml
   - /path/on/your/host:/mnt/host_projects:ro
   ```
   (Windows: `C:/Users/you/projects:/mnt/host_projects:ro`.)
2. Set `FORGE_REPO_PATH` in `docker-compose.yml`'s `environment:` block
   (next to `OLLAMA_HOST`/`PHOENIX_ENDPOINT`) to the container-side path,
   e.g. `/mnt/host_projects/your-repo` — `config.yaml` just reads this via
   `${FORGE_REPO_PATH}`, same as those two. (For a native, non-Docker run,
   set the same variable in `.env` instead, to your real host path.)
3. `docker compose up -d --build` again to pick up the change.

Two things worth knowing: `/index` and `forge.repo_path` are independent —
indexing a path only affects Q&A, it doesn't retarget the task path (see
`WORKFLOW.md` Section 1). And **push from inside Forge will fail if that
mount is read-only** (`:ro`) — Forge clones `repo_path` into its own
sandbox and commits fine there, but can't push back into a read-only
source. Either drop `:ro` for a repo you're OK letting Forge push to
directly, or treat `push`/`open_pr` as a manual step you run yourself once
tests pass in the sandbox.

## Common gotchas

| Symptom | Fix |
|---|---|
| `docker info` errors / `docker compose up` can't connect to the daemon | Docker Desktop isn't running yet — start it and wait 30-60s before retrying |
| Container starts then immediately shows `Dead`/name conflicts after a failed first attempt | `docker compose down` then `docker compose up -d` again — clears stale state from an interrupted build |
| `disk I/O error` from SQLite inside the container | Don't bind-mount `.data/` — it must stay the named volume (`forge_data`) already set up in `docker-compose.yml`. SQLite's file locking doesn't survive Docker Desktop's Windows bind-mount translation |
| Chat loads, nothing ever responds | Ollama isn't reachable from the container — confirm `ollama serve` is running natively on the host, not in Docker |
| Want to stop everything | `docker compose down` — the named volume (and everything indexed) survives; `docker compose up -d` picks up right where you left off |

For anything not covered here — retrieval quality issues, latency tuning,
approval-gate/resume flows — see `WORKFLOW.md` Section 11 ("How to
actually use Forge today") and Section 12's troubleshooting table.
