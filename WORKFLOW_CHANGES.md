# What changed from the original plan, and why

`WORKFLOW.md` replaces an earlier `WORKFLOW.pdf` written as a design plan
before most of the system existed. This is every place the two diverge.

- **Format: PDF → Markdown.** A binary PDF doesn't diff or review in a PR,
  and drifts out of sync fast — this rewrite is the proof.
- **Reasoning model: Qwen3 → Llama 3 family.** Qwen3 was never pulled on
  this machine; `config.yaml` runs `llama3:latest` and `llama3.2:1b`.
- **Knowledge layer: LlamaIndex + Chroma → raw Ollama embeddings + Chroma.**
  LlamaIndex sat in `requirements.txt` unused; Phase 9 removed it as dead
  weight.
- **Chunking: tree-sitter → stdlib `ast`.** Same structure-aware,
  per-function/class result, no extra dependency (Phase 10).
- **Retrieval: two-stage hybrid (lexical + semantic) → semantic-only.** No
  SQLite FTS5 stage was ever built.
- **Orchestration: `route/retrieve/answer/plan/edit/test` graph → 3-node
  graph (`retrieve/answer/decline`).** The code-edit/commit/push tools are
  standalone functions, not graph nodes.
- **Approval gate: LangGraph `interrupt()` + resumable checkpoint → plain
  synchronous callback (`product/approval.run_tool`).** Real and tested, but
  not restart-durable — a denied call just raises, nothing pauses-and-resumes.
- **Action tools: GitHub + filesystem + shell MCP tools → filesystem + git
  only.** One local MCP server (`read_file`, `write_file`, `commit`,
  `push`). No GitHub issue-fetch tool, no shell/test-runner tool.
- **`open_pr`: planned → not built.** The one entry in
  `forge.require_approval_for` (set since Phase 1) nothing exists for yet.
- **Test-and-retry loop on failure: planned → not built.** No test runner
  exists, so there's nothing to retry against.
- **State: one SQLite file (checkpoints + tasks/run history) → two files.**
  `.data/forge.db` (generic key-value `StateStore`) and
  `.data/checkpoints.db` (LangGraph's own checkpointer) are separate, and
  there's no dedicated tasks/run-history schema — just the generic KV table.
- **`.data/workspace/` — missing from the original plan entirely.** Didn't
  exist until Phase 7/12; it's the sandbox `write_file`/`commit`/`push`
  operate in, with its own local git identity.
- **Observability: native `phoenix serve` → Docker container.** The native
  `arize-phoenix` pip package depends on `sqlean-py`, which has no prebuilt
  wheels anywhere and needs a C++ toolchain this machine doesn't have.
- **Deployment: bind-mounted `.data/` → named Docker volume.** SQLite's file
  locking doesn't work reliably over Docker Desktop's Windows bind-mount
  translation; the container crashed on startup (`disk I/O error`) until
  this changed.
- **Chat UI: streaming answers, `/index` command, approval buttons, citation
  expansion, PR review → single non-streamed answer + a Sources list, no
  slash commands, no approval UI, no citation expansion.** Only the Q&A path
  is wired into Chainlit; everything else is Python-only today.
- **Sidebar conversation history: planned → not built.** `thread_id` is a
  per-browser-session UUID with no listing/resume UI, even though the
  checkpointer underneath could support one.
- **Phase count: 9 → 12.** Ingest source (10), code-edit tool (11), and
  commit/push tools (12) were added after the original 9-phase plan was
  written.
- **`ProposedPR`/`PlanStep` dataclasses: planned to be used by a planning
  step → dead code.** Defined in `product/schema.py`, never imported
  anywhere.
