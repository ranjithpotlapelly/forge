# What changed from the original plan, and why

`WORKFLOW.md` replaces an earlier `WORKFLOW.pdf` written as a design plan
before most of the system existed. This document was originally written
right after Phase 12 and only compared the plan against that snapshot. It's
now updated through **Phase 23** — and the honest summary is: most of what
looked like a divergence at Phase 12 has since been built and now matches
the original plan. What's left are a handful of permanent architectural
decisions, plus one genuinely new divergence that emerged *after* the plan
was written and was never in it at all.

## Permanent divergences (still true today)

- **Format: PDF → Markdown.** A binary PDF doesn't diff or review in a PR,
  and drifts out of sync fast — this file's own history is proof either way
  (see the note at the bottom on how badly it drifted before this rewrite).
- **Knowledge layer: LlamaIndex + Chroma → raw Ollama embeddings + Chroma.**
  LlamaIndex sat in `requirements.txt` unused; Phase 9 removed it as dead
  weight.
- **Chunking: tree-sitter → stdlib `ast`.** Same structure-aware,
  per-function/class result for Python, no extra dependency (Phase 10).
  Still `ast`-only today — non-Python languages (`.js`/`.ts`/`.go`) get
  whole-file chunks, no per-symbol chunker was ever added for them.
- **State: one SQLite file (checkpoints + tasks/run history) → two files.**
  Still two files today: `.data/forge.db` (now holding `run_history`'s
  `runs`/`run_steps` tables, Phase 19 — see below) and
  `.data/checkpoints.db` (LangGraph's checkpointer). The plan's single-file
  vision was never implemented; a dedicated run-history *schema* was
  (Phase 19), just not merged into the checkpoint file.
- **`.data/workspace/` — missing from the original plan entirely.** Didn't
  exist until Phase 7/12; it's the sandbox `write_file`/`commit`/`push`
  (and, since Phase 14, the whole task path) operate in, with its own local
  git identity. Still there, still the same role, just far more load-bearing
  now that the full task path runs against it.
- **Observability: native `phoenix serve` → Docker container.** The native
  `arize-phoenix` pip package depends on `sqlean-py`, which has no prebuilt
  wheels and needs a C++ toolchain this machine doesn't have.
- **Deployment: bind-mounted `.data/` → named Docker volume.** SQLite's file
  locking doesn't work reliably over Docker Desktop's Windows bind-mount
  translation; the container crashed on startup (`disk I/O error`) until
  this changed.

## Divergences that have since been resolved (now match the plan)

Everything below was flagged as "planned but not built" or "diverged from
the plan" as of Phase 12. All of it now exists:

- **Reasoning model.** The plan specified Qwen3; Phases 1–21 actually ran
  the Llama 3 family instead (Qwen3 was never pulled on this machine at the
  time). As of Phase 22/23, `config.yaml`'s `llm`/`answer_model` run Qwen3
  again (`qwen3:8b`/`qwen3:4b`) — coincidentally back in line with the
  original plan, though nothing in the code indicates this was a deliberate
  "return to the plan" rather than just a model-availability/performance
  call made independently.
- **Retrieval: semantic-only → hybrid.** The plan wanted lexical + semantic
  fusion; Phase 12 had semantic-only, "no SQLite FTS5 stage was ever built."
  Phase 15 built exactly that (`adapters/retriever_fts.py` +
  `adapters/retriever_hybrid.py`, RRF fusion).
- **Orchestration: standalone functions → full graph, for the task path.**
  The plan wanted one graph covering everything (`route/retrieve/answer/
  plan/edit/test`); Phase 12 had a 3-node Q&A graph plus standalone
  code-edit/commit/push functions with no graph, no checkpoint. Phase 14/17
  built the task path as real graph nodes (`prepare_workspace` →
  `task_retrieve` → `plan` → `approval_gate` → `edit` → `test` → `commit` →
  `push` → `pr_gate` → `open_pr`, ~19 nodes total including the Q&A side —
  see `WORKFLOW.md` Section 5). The one thing the plan didn't anticipate:
  Q&A itself now *bypasses* this graph for a different reason — see "A new
  divergence" below.
- **Approval gate: synchronous callback → restart-durable `interrupt()`.**
  The plan wanted a resumable, checkpointed pause; Phase 12 had "a denied
  call just raises, nothing pauses-and-resumes." Phase 20 built real
  `langgraph.types.interrupt()` gates, checkpointed to
  `.data/checkpoints.db` — a paused task now survives a restart and resumes
  from a different process (`python -m app.resume`) or browser session
  (`/history` → Resume).
- **Action tools: filesystem + git only → + GitHub issue-fetch, + a test
  runner.** Phase 12 had neither. Phase 18 added `fetch_issue` (read-only,
  `#N` detection in chat) and Phase 13 added `open_pr` — both implemented as
  direct REST adapters (`core.tools.Tool`), not literal MCP tools, for the
  same reason: keeping non-MCP HTTP calls out of the MCP server's stdio
  process, which would otherwise corrupt its JSON-RPC channel. A real test
  runner (`run_tests`, auto-detecting Maven/Gradle/pytest/npm) exists as an
  actual MCP tool on the local filesystem+git server.
- **Test-and-retry loop on failure: planned → built.** Phase 14 built it,
  capped at 3 attempts, each retry fed the previous failure's output.
- **Chat UI: single answer + Sources list → streaming, `/index`, approval
  buttons, citation expansion, PR review.** All five were "planned → not
  built" at Phase 12. Phase 16 added streaming answers, `/index`, and
  citation expansion; Phase 17 added the plan card with Approve/Edit/Reject
  buttons and the PR-review gate.
- **Sidebar conversation history: planned → built, as `/history`.** Phase 21
  built resume/delete for past threads — via a slash command rather than a
  literal sidebar widget (Chainlit doesn't offer custom sidebar chrome), but
  functionally the same capability the plan described.
- **`ProposedPR`/`PlanStep` dataclasses: dead code → in active use.** At
  Phase 12 these were defined in `product/schema.py` and never imported.
  `product/planning.py`'s `make_plan()` now constructs them for every task,
  and they're what the plan card renders.

## A new divergence the original plan never anticipated

**The Q&A path bypasses the engine/graph entirely (Phase 16, refined in
Phase 23) — for reasons that didn't exist when the plan was written.**
`app/chainlit_app.py` calls `Retriever.search`/`has_context`/`decline`/
`build_answer_messages`/`LLMClient.stream` directly rather than invoking
`Engine.ask()` or the compiled graph, because streaming a response requires
it (`Engine.ask()` only returns a finished, non-streamed `Answer`), and
because `Engine.ask()` always re-runs `classify_intent`, which the caller
has already done. Phase 23 went further and gave Q&A answers their own,
smaller/faster model (`answer_model`, separate from `llm`) plus per-path
`num_ctx` tuning — none of which the original plan or the Phase-12 snapshot
of this document ever mentioned, since neither streaming latency nor a
second model slot was a concern at that point. See `WORKFLOW.md` Section 4
for the full mechanics.

## Phase count

**9 → 23.** The original plan described 9 phases; by the time this document
was first written it was already 12; it's 23 now. See `WORKFLOW.md` Section
7 for what each one built.

## A note on why this rewrite happened

This document sat frozen at Phase 12 for eleven more phases, describing as
"not built" several major features (`open_pr`, the test-retry loop, the
entire approval-gate durability model, most of the chat UI) that had long
since shipped — while `WORKFLOW.md`'s own intro linked here as "what
changed since the original plan." A reader following that link after
reading an accurate `WORKFLOW.md` would have landed in a document
contradicting nearly everything they'd just read. Keep this one current the
same way `WORKFLOW.md`'s Recommendations section asks for that document —
don't let it silently stop being updated once "the interesting changes"
feel like they're over.
