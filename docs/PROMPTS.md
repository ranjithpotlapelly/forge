# Forge build prompts — spec and implementation status

This is the prompt-by-prompt spec used to build Forge incrementally in an IDE
assistant (Claude in IntelliJ), plus a cross-check of what's actually landed
in this repo as of 2026-07-31. The original prompt text is preserved verbatim
below the status table so it stays usable as a script if the project (or a
fork of it) needs to replay any step.

## Status legend

- ✅ **Done** — implemented, matches the prompt's requirements
- ⚠️ **Partial / diverged** — implemented differently than specified, or spec'd requirement was dropped later for a stated reason
- ❌ **Not implemented** — no corresponding code in the repo

## Cross-check summary

| # | Prompt | Status | Evidence / notes |
|---|--------|--------|-------------------|
| 0 | Session primer | N/A | Instructions only, nothing to implement |
| 1 | Restore Java parsing (tree-sitter) | ✅ Done (JS/TS/Java, not just Java) | `adapters/ingest_fs.py`'s `_ts_chunks`/`_GRAMMAR_BY_EXT` reinstate tree-sitter for JavaScript, TypeScript (incl. `.tsx`), and Java — one chunk per method/function via `child_by_field_name("name")` (confirmed avoids the return-type-vs-method-name pitfall this prompt called out). Go was also implemented and then deliberately removed (2026-07-31): not an active target repo, so it stays on whole-file chunking rather than carrying an unused grammar. Two real-repo bugs fixed beyond the original spec, both caught only by testing against the actual target repo: `export class Foo {}` (an `export_statement` wrapping the real declaration — virtually all real Angular/TS code) wasn't matching at all until unwrapped, and Angular's functional-guard/interceptor pattern (`export const x = (...) => {...}`) needed its own extraction path since it's a `lexical_declaration`, not a `function_declaration`. Leading JSDoc/Javadoc comments are included in each chunk (they sit as a separate preceding sibling node, unlike a Python docstring which is inside the function body already) — this surfaced and led to fixing a real bug in `adapters/retriever_fts.py`'s `_signature()` (see below). `.xml`/`.properties` whole-file indexing (part of the original ask) is still **not done** — small, independent follow-up. **One narrow open item**: after the `_signature()` fix, `app/ingest_smoke_test.py`'s auth-interceptor query still ranks a different chunk first, because RRF gives it an exact fused-score tie with the correct answer and `adapters/retriever_hybrid.py`'s tie-break rule favors lexical rank over semantic score — a deliberate design choice (correct for identifier-shaped queries) that just doesn't suit this natural-language one. Left as-is; changing the tie-break risks regressing the case it was built for. |
| 2 | Move to a 32K-context model | ✅ Done | `config.yaml`: `llm.model: qwen3:8b`, `num_ctx: 16384`. `adapters/llm_ollama.py` always sends `num_ctx`/`keep_alive` in the request (verified via debug logging of the actual payload) and strips `<think>...</think>` in both `generate()` and `stream()`. |
| 3 | Remove config/schema for features that don't exist | ✅ Done (then legitimately reversed) | `open_pr` was removed from `require_approval_for`, `ProposedPR`/`PlanStep` kept with a comment. Prompt 9 later built the real `open_pr` tool, so it's back in `require_approval_for` — correct end state, comment in `product/schema.py` is now slightly stale ("not wired to anything yet" — it is, via `plan_node`). |
| 4 | Build the test-runner tool | ✅ Done | `adapters/mcp_servers/workspace_server.py`: `run_tests()`, build-system auto-detection (`_detect_build_steps`), timeout, truncated output. |
| 5 | Build the plan node | ✅ Done | `product/planning.py`, `product/prompts/plan.txt` (prompt kept out of Python as required), `plan_node` in `adapters/engine_langgraph.py`. |
| 6 | Gate the plan behind human approval | ✅ Done (superseded by #15) | `approval_gate_node` — now built on `interrupt()` rather than the original synchronous callback, since #15 upgraded it. |
| 7 | Build the edit node | ✅ Done | `product/code_edit.py`, `edit_node` in the graph. |
| 8 | Wire the test-and-retry loop | ✅ Done | `test_node` → `route_after_test` → `bump_attempt`/`commit`/`give_up`, capped via graph state (not a global). |
| 9 | Add the open_pr tool | ✅ Done | `adapters/github_pr.py`, `open_pr_node`, `pr_approval_gate_node`; `open_pr` is back in `require_approval_for`. |
| 10 | Add lexical search alongside semantic | ✅ Done | `adapters/retriever_fts.py` (SQLite FTS5) + `adapters/retriever_hybrid.py`, sitting behind the same `core.retriever.Retriever` port — `app/ask.py` untouched. |
| 11 | Chat UI: stream, cite, index | ✅ Done | `app/chainlit_app.py`: `_stream_tokens`, expandable citation elements, `/index` (plus `--changed` and `--clear`, beyond spec), `/help`. |
| 12 | Wire plan/edit/test loop into the UI | ✅ Done | Intent routing (`classify_intent`), `ProposedPR` rendered as a card, Approve/Edit/Reject actions, live progress messages, diff rendering, separate PR approval gate. `product/approval.py` contract preserved for non-UI callers. |
| 13 | GitHub issue-fetch tool | ✅ Done | `adapters/github_issue.py`, `#<number>` detection + fetch/skip actions in the UI. |
| 14 | Real task and run-history schema | ✅ Done | `core/run_history.py` (port), `adapters/run_history_sqlite.py`, `app/history.py` CLI. |
| 15 | Make approval durable (interrupt + checkpoint) | ✅ Done | `interrupt()` used at `approval_gate_node` and `pr_approval_gate_node`; `app/resume.py` for CLI resume by `thread_id`. |
| 16 | Conversation history and resume in the UI | ✅ Done | `/history` command, per-thread Resume/Delete actions, Q&A transcript replay, task-thread resume. |
| 17 | Hosted-model adapter (upgrade path) | ✅ Done | `adapters/llm_openai_compatible.py`, registered as `code_model` slot in `app/factory.py`/`app/wiring.py`; `core/`, `product/`, `app/ask.py` untouched. Went further than spec'd: server-side fallback chain across multiple models. |
| 18 | Performance tuning | ✅ Done | `config.yaml`: `answer_num_ctx`, `edit_num_ctx`, `answer_model` (qwen3:4b), `keep_alive`, `show_timing`, `answer_top_k`, `answer_max_expanded`. `app/bench.py` exists for A/B timing. |
| 19 | Slack notifications | ✅ Done | `adapters/tool_slack.py`, default-off via `SLACK_WEBHOOK_URL`, wired as `notify_tool` in `open_pr_node`. |
| 20 | Qdrant vector store option | ✅ Done | `adapters/retriever_qdrant.py`, same `Retriever` port/skeleton-index contract, `chroma` stays the default in `config.yaml`. |
| 21 | Docker packaging | ✅ Done | `docker-compose.yml` + `Dockerfile`: Ollama stays native (`host.docker.internal`), named volume `forge_data` (not a Windows bind mount, per the documented SQLite-locking lesson). |
| 22 | DuckDB analytics | ❌ **Not implemented** | No `app/stats.py`, no `duckdb` in `requirements.txt`/`requirements.lock`. Marked lowest-priority/optional in the original doc ("do it last or skip it") — looks skipped rather than missed. |
| 23 | Retrieval-quality eval harness | ✅ Done | `eval/dataset.yaml` (11 cases against the indexed rag-frontend-angular-v2 repo), `eval/run.py` (`python -m eval.run`, `--k`, `--min-hit-at-k`, `--compare`/`--compare-k`/`--compare-config`), wired as a CI gate in `.github/workflows/retrieval-eval.yml` (self-hosted runner — reuses the machine's already-running Ollama and already-indexed `.data/chroma`, since that state is gitignored and not reproducible on a stock hosted runner). `core/`, `product/code_source.py`, `product/approval.py`, the existing adapters, and `app/ask.py` untouched — verified via `git diff --stat`. |

### Net: 22 of 23 done, 1 skipped

- **Only real gap left:** Prompt 22 (DuckDB analytics) was never built. It was flagged as the lowest-value item in the original doc, so this may be an intentional skip rather than an oversight — confirm before treating it as backlog.
- **Prompt 1 landed (2026-07-31)**, extended beyond its original Java-only scope to also cover JS/TS, since the project's daily-driver repo is Angular/TypeScript (Go was built too, then deliberately dropped as unused). See the plan at the time: "Reinstate tree-sitter for JS/TS/Go/Java symbol chunking". `.xml`/`.properties` indexing remains a small, separate follow-up if Java/Spring config-file awareness is still wanted. The one open thread from this change is the RRF tie-break nuance noted in the table above — narrow, well-diagnosed, deliberately left alone rather than risking a regression to the case the current tie-break rule was built for.

---

# Original prompt document

Everything below this line is the prompt set as originally written, for use
one prompt at a time in an IDE assistant session. See the status table above
for what's already landed in this repo.

## Prompt 0 — Session primer (paste this first, once per session)

> **In plain terms:** Sets the ground rules so the IDE assistant respects the architecture and doesn't quietly break the design.

```
You are working on "Forge", a self-hosted repo copilot in Python. Read the repo
before changing anything. Key architecture rules you must follow:

- core/ contains PORT INTERFACES only (Protocol classes). No vendor imports, no
  domain logic. Never add a dependency here.
- adapters/ contains implementations of those ports (Ollama, Chroma, SQLite,
  MCP). Adapters may import vendors. Adapters must not import product/.
- product/ contains business logic (code chunking, prompts, approval, schema).
  It must NEVER import a vendor library directly — only core/ interfaces.
- app/factory.py is the ONLY place that maps a config name to a concrete class.
- config.yaml is the composition root.

Dependency direction: product/ -> core/ <- adapters/, wired by app/factory.py.

Before you write code: summarise back to me what each layer currently contains
and which files you plan to touch. Do not start editing until I confirm.
```

---

## Prompt 1 — Restore Java parsing (do this first)

> **In plain terms:** Teaches Forge to read Java code (and XML config) so it can understand your actual project.

```
Forge currently chunks code with Python's stdlib `ast` module, which only parses
Python. The target codebase is Java, so indexing it produces zero symbols.

Task: replace the stdlib-ast chunker with tree-sitter, supporting Java first.

Requirements:
- pip install tree-sitter tree-sitter-java  (use the single-language wheel, NOT
  tree-sitter-language-pack — that one fails to build on Windows)
- Extract these Java node types as symbols: class_declaration,
  interface_declaration, enum_declaration, method_declaration,
  constructor_declaration
- CRITICAL: get the symbol name via node.child_by_field_name("name"). Do NOT
  scan for the first identifier child — in Java the return type comes before the
  method name, so `public UserDetails loadUserByUsername(...)` would be indexed
  as "UserDetails" instead of the method name.
- Keep the existing metadata contract unchanged: path, module, language, kind,
  symbol, parent, start_line, end_line, hash, citation
- Keep Python support working via tree-sitter as well
- Also index .xml and .properties files whole (one chunk each) — Spring bean
  wiring and pom.xml carry real architecture in Java projects
- Keep the skeleton-index design: embed the signature/javadoc summary, store
  path + line range as a pointer, read real code from disk on demand

Acceptance check: index this repo and confirm non-zero Java symbols:
  git clone https://github.com/ranjithpotlapelly/tinywebgears-samples C:\code\tinywebgears
  python -m app.index C:\code\tinywebgears --reset
Expected: roughly 180 files and 600+ symbols. Then confirm the method name is
correct, not the return type:
  python -m app.ask --retrieve-only "load user by username"
It must cite UmsUserDetailsService.java.
```

---

## Prompt 2 — Move to a 32K-context model

> **In plain terms:** Gives Forge a bigger, faster brain that can hold more code in mind at once.

```
config.yaml currently points at llama3, which has an 8K context window. The
plan/edit/test loop needs more: the edit step must hold the target file, the
plan, and test failure output in one prompt.

Task:
1. Tell me the exact command to pull qwen3:8b with Ollama.
2. Update config.yaml: llm.model -> qwen3:8b, and set num_ctx to 16384.
3. Check adapters/llm_ollama.py actually sends num_ctx in the options block of
   the /api/chat payload. Ollama silently falls back to a small default if it
   isn't passed explicitly — verify this, don't assume.
4. Qwen3 emits <think>...</think> reasoning blocks. Confirm both generate() and
   stream() strip them so users never see the scratchpad.

Acceptance check: python -m app.test_reasoning
Expected: streamed answer with no <think> tags visible.
```

---

## Prompt 3 — Remove config and schema that describe features that don't exist

> **In plain terms:** Cleans out half-built features so the app doesn't promise things it can't do.

```
Two things in this repo advertise capabilities that were never built:

1. config.yaml lists "open_pr" in forge.require_approval_for, but no open_pr
   tool exists anywhere.
2. product/schema.py defines ProposedPR and PlanStep dataclasses that are never
   imported by any module.

Task: first, search the repo and confirm both claims are true — show me the
grep results. Then:
- Remove "open_pr" from require_approval_for (we will add it back in a later
  step when the tool actually exists).
- LEAVE ProposedPR and PlanStep in place. They will be used by the planner we
  build in a later step. Add a short comment above them noting they are
  consumed by the plan node.

Acceptance check: grep the repo for open_pr and show me there are no remaining
references outside of comments or docs.
```

---

## Prompt 4 — Build the test-runner tool

> **In plain terms:** Lets Forge run your project's tests, so it can check whether a change actually works.

```
Forge has no way to run a project's test suite, so there is nothing for a
retry loop to check against. Build that tool now. Build it standalone and
testable — it is not wired into any graph yet.

Task: add a test-runner tool that implements the core.tools.Tool protocol.

Requirements:
- name: "run_tests", requires_approval: False (running tests is read-only in
  effect; it changes nothing outside the workspace)
- Runs inside .data/workspace/ only — never against the user's real checkout
- Auto-detects the build system: pom.xml -> "mvn -q test",
  build.gradle -> "gradle test", pytest.ini/pyproject.toml -> "pytest -q"
- Uses subprocess with a hard timeout (default 300s) and captures stdout+stderr
- Returns a structured result: {passed: bool, exit_code: int, output: str,
  duration_s: float}
- Truncates output to the last ~4000 characters. Full Maven output will blow the
  model's context window when this is fed into a retry prompt.
- Never uses shell=True

Acceptance check: write a small script that creates a temp project in
.data/workspace/ with one passing and one failing test, then runs the tool
against both and prints the results. Show me the output.
```

---

## Prompt 5 — Build the plan node

> **In plain terms:** Forge writes a step-by-step plan before touching anything — a proposal you review first.

```
Add a "plan" node to the LangGraph orchestration graph. Find the file that
defines the current 3-node graph (retrieve/answer/decline) and extend it.

Task: given a task description plus retrieved code context, produce a
structured plan.

Requirements:
- Output MUST be a validated ProposedPR from product/schema.py, containing
  title, branch, and a list of PlanStep (description, target_path, kind).
- Ask Ollama for JSON: pass format: "json" in the request body, and give the
  exact JSON schema in the system prompt.
- Validate the parsed JSON into the dataclasses. If parsing or validation fails,
  retry the model call ONCE with the parse error appended, then give up with a
  clear error. Do not loop indefinitely.
- Reject any plan whose target_path escapes .data/workspace/ — check for
  absolute paths and ".." traversal. This is a security boundary, not a nicety.
- The node only PLANS. It must not write files.
- Put the prompt text in product/prompts/ as a separate file, not inline in the
  Python.

Acceptance check: run the plan node against a real task on the indexed repo and
print the resulting ProposedPR. Show me the JSON and confirm every target_path
stays inside the workspace.
```

---

## Prompt 6 — Gate the plan behind human approval

> **In plain terms:** Nothing gets changed until you say yes — a clear approval gate on every edit.

```
Wire the existing approval mechanism (product/approval.py) in front of any node
that writes files.

Task: after the plan node produces a ProposedPR, require explicit human
approval before proceeding.

Requirements:
- Print the plan clearly: title, branch, and each step with its target path.
- Accept approve / reject. On reject, stop the run cleanly and report — do not
  raise an unhandled exception up to the user.
- The gate goes BEFORE the edit node. Nothing is written at the point the human
  is asked, so rejecting costs one model call and leaves the disk untouched.
- Keep the existing synchronous callback design for now. Do not convert to
  LangGraph interrupt() in this step.

Acceptance check: run a task and reject the plan. Confirm .data/workspace/ is
byte-for-byte unchanged afterwards.
```

---

## Prompt 7 — Build the edit node

> **In plain terms:** Forge makes the actual code changes it planned, one file at a time.

```
Add an "edit" node that applies an approved ProposedPR to the workspace.

Task: for each PlanStep, generate the new code and write it.

Requirements:
- IMPORTANT: do NOT ask the model for a unified diff. Small local models produce
  malformed diffs with wrong line offsets constantly. Instead, have the model
  rewrite the ENTIRE target method or class body, then splice it in using the
  start_line/end_line already stored in the index metadata.
- Read the current file content and pass it to the model as context.
- Write via the existing write_file tool so the approval policy still applies.
- Operate only inside .data/workspace/.
- Back up each file before modifying it, so a failed edit can be rolled back.
- After all steps, print a unified diff of what changed (generate the diff
  yourself with difflib for display — do not ask the model for it).

Acceptance check: run a trivial task end to end, for example "add a null check
at the start of loadUserByUsername". Show me the diff and confirm the file still
compiles/parses.
```

---

## Prompt 8 — Wire the test-and-retry loop

> **In plain terms:** If the tests fail, Forge tries again on its own — but stops after 3 tries instead of looping forever.

```
Connect the run_tests tool into the graph as a "test" node, with a bounded
retry edge back to the edit node.

Task: edit -> test -> (pass? commit : retry edit)

Requirements:
- On failure, feed the truncated test output back into the edit node as
  additional context so the next attempt knows what broke.
- Hard cap of 3 attempts. On the third failure, stop and report — never loop
  indefinitely. On CPU each cycle takes minutes.
- Track attempt count in the graph state, not in a module-level global.
- If the retry budget is exhausted, offer to roll back to the backups taken in
  the edit step.
- Log each attempt clearly: attempt number, what changed, why tests failed.

Acceptance check: create a task you know will fail tests first, and confirm the
loop retries and then stops at exactly 3 attempts rather than running forever.
```

---

## Prompt 9 — Add the open_pr tool

> **In plain terms:** Forge opens a pull request for you to review, exactly like a teammate would.

```
Forge can currently commit and push, but there is no pull-request step, so
changes reach the remote with no PR review gate. Close that gap.

Task: add an "open_pr" tool implementing core.tools.Tool.

Requirements:
- requires_approval: True. Add "open_pr" back into forge.require_approval_for
  in config.yaml — now that the tool actually exists.
- Reads GITHUB_TOKEN from the environment via the existing config loader. Never
  hardcode a token, never log it, never print it in error messages.
- Calls the GitHub REST API to open a PR from the working branch.
- Shows the full diff and the PR title/body to the human BEFORE the API call.
- On success, returns the PR URL.
- Fails with a clear message if the token is missing or lacks permission.

Acceptance check: run against a scratch repo you own. Confirm that rejecting at
the approval prompt makes no network call at all — verify this, don't assume it.
```

---

## Prompt 10 — Add lexical search alongside semantic (optional, quality)

> **In plain terms:** Forge finds code by exact name as well as by meaning, so searches are more accurate.

```
Retrieval is currently semantic-only. For code, many real questions are lexical
("where is validateToken called?"), and embeddings are weak at exact symbol
matching.

Task: add a lexical stage to the retriever and combine it with the semantic one.

Requirements:
- Build a SQLite FTS5 index over symbol names, signatures and file paths,
  populated during the existing indexing run.
- On query: run the lexical search, run the semantic search, then merge and
  re-rank. Reciprocal rank fusion is a good default.
- This must go BEHIND the existing core.retriever.Retriever interface. No caller
  changes. app/ask.py must not need editing.
- Keep it in the adapter layer.

Acceptance check: compare before/after on a query naming an exact symbol, e.g.
"UmsRememberMeServices". The exact match should rank first after this change.
```

---

## Prompt 11 — Make the chat UI stream, cite, and index

> **In plain terms:** The chat screen shows answers as they're typed and lets you click a source to see the real code behind it.

```
The Chainlit UI currently returns a single non-streamed answer plus a flat
Sources list. On CPU an answer takes 20-60 seconds, so a non-streamed reply
looks like the app has frozen. Fix the Q&A experience first, before wiring the
task flow.

Task: upgrade the Chainlit Q&A path.

Requirements:
- Stream the answer token by token using the existing LLMClient.stream(). The
  first token should appear within a few seconds.
- Show a "Retrieving..." step while search runs, so the wait is explained.
- Render each citation as an EXPANDABLE element: collapsed it shows
  path:start-end; expanded it shows the actual source lines, read from disk on
  demand via the existing read-lines helper. Do not store code in the UI layer.
- Add a /index slash command: `/index C:\path\to\repo` runs the same indexing
  pipeline as `python -m app.index`, with a progress indicator, and reports the
  file and symbol counts when done.
- Add /help listing the available commands.
- All of this stays in the UI layer. Do not duplicate retrieval or prompt-
  building logic that already exists — call into it.

Acceptance check: ask a question in the browser. Confirm tokens stream, that
clicking a citation reveals real code at the right line range, and that /index
works on a fresh repo.
```

---

## Prompt 12 — Wire the plan/edit/test loop into the UI

> **In plain terms:** You run the whole "fix this" process from the browser with simple Approve / Edit / Reject buttons.

```
The plan/edit/test loop is currently Python-only. Approving a plan means reading
a terminal, which makes the feature unusable in practice. Expose it in Chainlit.

Task: drive the full task flow from the browser.

Requirements:
- Detect task intent (e.g. "fix ...", "add ...", "refactor ...") versus a
  question, and route to the task graph.
- Render the ProposedPR as a readable card: title, branch, and each PlanStep
  with its target path.
- Replace the terminal approval callback with Chainlit ACTION BUTTONS:
  Approve / Edit / Reject. "Edit" lets the user type corrections and re-plans.
- Show live progress through the loop: editing file 2 of 3, running tests,
  attempt 2 of 3.
- Render the resulting diff with syntax highlighting.
- Show a second, separate approval before open_pr, displaying the final diff and
  PR title/body.
- IMPORTANT: product/approval.py must keep working for non-UI callers. Inject
  the UI approver rather than replacing the interface — the approval contract
  lives in product/, the buttons live in the UI layer.

Acceptance check: run a complete task from the browser without touching a
terminal. Then run the same task and press Reject; confirm .data/workspace/ is
unchanged.
```

---

## Prompt 13 — Add a GitHub issue-fetch tool

> **In plain terms:** Forge can read a GitHub issue directly, so you just say "fix issue #42".

```
Forge cannot read issues, so "fix issue #42" does not work — the task text has
to be pasted manually.

Task: add a read-only GitHub issue tool implementing core.tools.Tool.

Requirements:
- name: "fetch_issue", requires_approval: False (read-only, no side effects)
- Fetches issue title, body, labels and comments by number
- Repo owner/name come from config.yaml, not hardcoded
- Reads GITHUB_TOKEN via the existing config loader. Never log or print it,
  including in error messages and stack traces.
- Works on public repos without a token; fails with a clear message on private
  repos when the token is missing.
- Detect "#<number>" in a user message and offer to fetch it.

Acceptance check: fetch a real issue from a public repo and print the parsed
title and body. Then confirm the token never appears in any log output.
```

---

## Prompt 14 — Add a real task and run-history schema

> **In plain terms:** Forge keeps a history of everything it did and every decision you approved — a full audit trail.

```
State is currently a generic key-value table in .data/forge.db. There is no
schema for tasks or run history, so past runs cannot be listed, inspected or
resumed.

Task: add proper tables behind the existing core.store.StateStore port.

Requirements:
- Tables: `runs` (id, thread_id, kind, task_text, status, started_at, ended_at,
  error) and `run_steps` (id, run_id, node, status, detail, duration_ms).
- Record every graph node execution as a run_step.
- Record approval decisions explicitly: what was proposed, what the human chose,
  and when. This is your audit trail — it matters most for the write path.
- Extend the StateStore port ONLY if strictly necessary; prefer adding a
  narrow second port (e.g. RunHistory) over widening an existing interface.
- Add a CLI: `python -m app.history` listing recent runs, and
  `python -m app.history <run_id>` showing that run's steps.

Acceptance check: run one Q&A and one task, then show me the output of both
history commands, including the recorded approval decision.
```

---

## Prompt 15 — Make approval durable

> **In plain terms:** If your computer restarts mid-task, Forge picks up exactly where it left off.

```
Approval is currently a synchronous callback that raises on denial. Nothing
pauses and resumes, so a run cannot survive a restart while waiting on a human.
This is acceptable while a human initiates every tool call, but becomes the
blocker once the agent self-initiates them.

Task: convert the approval gate to LangGraph's interrupt() with checkpointing.

Requirements:
- Use interrupt() at the approval points so graph state is persisted to the
  existing .data/checkpoints.db and the run can be resumed later by thread_id.
- Resuming must continue from the checkpoint, not re-run prior nodes. Re-running
  edit or commit would duplicate side effects — verify this explicitly.
- Keep the product/approval.py interface stable for callers that do not run
  inside the graph.
- Add `python -m app.resume <thread_id>` to continue a paused run.

Acceptance check: start a task, stop at the approval prompt, KILL the process
entirely, restart, resume by thread_id, and approve. Confirm the edit is applied
exactly once and no earlier node re-executed.
```

---

## Prompt 16 — Conversation history and resume in the UI

> **In plain terms:** Past conversations are saved in a sidebar you can reopen and continue anytime.

```
thread_id is currently a per-browser-session UUID with no listing or resume UI,
even though the checkpointer underneath already supports it.

Task: expose conversation history in Chainlit.

Requirements:
- List past threads with a title (first user message, truncated) and timestamp.
- Clicking a thread resumes it with full context, including a task that was
  paused mid-approval.
- Add a way to delete a thread, removing both its checkpoints and its run
  history rows.
- Read this from the run-history tables added earlier — do not invent a second
  source of truth.

Acceptance check: hold two separate conversations, restart the app, and confirm
both appear and resume correctly with their context intact.
```

---

## Prompt 17 — Add a hosted-model adapter (the upgrade path)

> **In plain terms:** When you're ready for more speed and quality, Forge can switch to a paid AI service by changing one line — no rebuild.

```
Local CPU inference caps answer and patch quality. The code-edit step suffers
most. config.yaml already has a separate `code_model` slot for exactly this.
Prove the port design by making the swap a config change.

Task: add an OpenAI-compatible hosted adapter alongside the Ollama one.

Requirements:
- New file adapters/llm_openai_compatible.py implementing the SAME
  core.llm.LLMClient protocol, including streaming.
- Register it in app/factory.py under adapter name "openai_compatible".
- Base URL and API key come from .env via the existing config loader. Never
  hardcode, never log, never include the key in error messages.
- CONSTRAINT: do not change any file in core/, product/, or app/ask.py. If you
  find yourself needing to, stop and tell me — that means a port is leaking.
- Document in README how to point ONLY code_model at the hosted endpoint while
  llm and embeddings stay local and free.

Acceptance check: with code_model switched to the hosted adapter and llm still

on Ollama, run a full task. Confirm it works and show me the git diff proving
core/ and product/ were untouched.
```

## How to use these well

- **One prompt per session-chunk.** Run the acceptance check before the next one.
- **Make it show you the plan first.** Prompt 0 asks Claude to summarise before
  editing — hold it to that.
- **Commit after each green acceptance check.** If the next step breaks things,
  you have a clean point to return to.
- **If it wants to add a dependency**, ask why, and whether the port interface
  can absorb it in the adapter layer instead.
- **If it edits `core/`**, push back. That layer should almost never change —
  if it does, the design is drifting.
- **Order matters**: 1 and 2 remove hard blockers. 4 through 8 are the
  plan/edit/test loop and depend on each other. 9, 10, 13 and 17 are
  independent. 11 and 12 need the loop (4-8) to exist first. 16 needs 14 and 15.

### Suggested order if you want value soonest

1, 2, 11 -> a genuinely pleasant Q&A tool over your Java repo (biggest win per
hour of work). Then 4, 5, 6, 7, 8 for the loop, then 12 to make it usable in the
browser. Then 3, 9, 13, 14 to close the write path properly. Then 15, 16, 10 for
durability and quality. 17 whenever local speed stops being tolerable.

---

## Prompt 18 — Performance tuning: make answer latency configurable and fast

> **In plain terms:** Tunes Forge so answers come back in seconds instead of minutes, and lets you measure what's fastest on your machine.

```
GOAL
Answers currently take 2-3 minutes on this machine (AMD Ryzen 7 7735U, Radeon
680M integrated GPU, 16 GB shared RAM, Windows, CPU-first inference via Ollama).
That is far slower than expected for an 8B model; the target is 20-40 seconds.
The cause is almost certainly configuration (oversized context, cold-start model
reloads, oversized prompts), not the hardware ceiling. Do NOT rewrite the
inference engine. Make the existing knobs configurable and measurable, then
help me tune them.

ARCHITECTURE RULES (do not break these)
- adapters/llm_ollama.py is the ONLY file that talks to Ollama.
- core/ interfaces do not change. If you think they must, stop and explain why.
- app/factory.py is the only place that maps config to a concrete class.
- New behaviour must be driven by config.yaml, not hardcoded.
Before editing, list the files you will touch and show me the plan. Wait for my
go-ahead.

CHANGES TO MAKE

1) Per-call num_ctx, right-sized instead of maxed.
   - Ensure adapters/llm_ollama.py passes `num_ctx` inside the options block of
     the /api/chat payload on EVERY call (Ollama silently falls back to a small
     default if omitted, and a too-LARGE value wastes KV-cache memory and slows
     generation). Verify it is actually sent; do not assume.
   - Make num_ctx configurable per call, falling back to a config default.
   - Add two config values under llm:
        answer_num_ctx: 8192     # Q&A rarely needs more
        edit_num_ctx: 16384      # code-edit loop needs the headroom
     The answer path uses answer_num_ctx; the (future) edit path uses edit_num_ctx.

2) A separate, faster model for answers.
   - Add an `answer_model` slot in config.yaml, defaulting to qwen3:4b, while the
     main `llm.model` stays qwen3:8b for higher-quality work.
   - The answer node uses answer_model. Everything else keeps using llm.model.
   - This must go through app/factory.py (build_llm with a slot name), consistent
     with how the existing `code_model` slot already works. Do not special-case it.

3) Keep the model warm (kill cold-start reloads).
   - Ollama unloads a model after ~5 minutes idle by default; the next call then
     pays a full reload, which alone can cost minutes.
   - Send `keep_alive` in the Ollama request, configurable via llm.keep_alive
     (default "30m"). Confirm it appears in the request body.
   - Also document setting the OLLAMA_KEEP_ALIVE environment variable as an
     alternative, in README.

4) Trim the prompt on the answer path.
   - Add retriever.answer_top_k (default 5) used by the answer path, separate
     from the existing top_k. Fewer, higher-ranked chunks = faster prefill.
   - Cap how many retrieved chunks get expanded to full source before being put
     in the prompt (add answer_max_expanded, default 4). Rank first, expand only
     the top few, summarise or drop the rest.

5) Built-in timing so I can measure, not guess.
   - In the answer path, measure and log: retrieval time, prompt token estimate,
     time-to-first-token, total generation time, and tokens/second.
   - Print these as a compact one-line summary after each answer, e.g.:
        [timing] retrieve 0.4s | prompt ~2100 tok | first-token 4.2s | total 22.1s | 9.3 tok/s
   - Put this behind a config flag llm.show_timing (default true for now).

6) A benchmark script to A/B settings without editing code.
   - Add app/bench.py that runs a fixed question N times and reports median
     time-to-first-token, median total time, and tokens/second.
   - It must accept overrides so I can compare without touching config, e.g.:
        python -m app.bench --model qwen3:4b --num-ctx 8192 --top-k 5
        python -m app.bench --model qwen3:8b --num-ctx 16384 --top-k 8
   - Print a small table comparing runs in one invocation if multiple --model or
     --num-ctx values are given.

VULKAN (documentation only — do NOT change code for this)
- In README, add a short section: this machine's Radeon 680M cannot use ROCm on
  Windows, but Ollama has experimental Vulkan support for AMD iGPUs enabled with
  the environment variable OLLAMA_VULKAN=1 (restart Ollama afterward). Expected
  gain on a 680M is roughly 1.5-2x since an iGPU shares system memory bandwidth —
  worth trying, not transformative. Tell the reader to verify with `ollama ps`
  that the model shows GPU usage and is not reloading between calls.

ACCEPTANCE CHECKS
- Show me the git diff. Confirm core/ was not modified.
- Run: python -m app.bench --model qwen3:4b --num-ctx 8192 --top-k 5
        python -m app.bench --model qwen3:8b --num-ctx 16384 --top-k 8
  and paste both timing tables so we can see the trade-off.
- Ask one real question and show the [timing] line.
- Confirm keep_alive and num_ctx actually appear in the outgoing Ollama request
  (log the request body once at debug level to prove it).

After it runs, tell me which single setting helped most, and recommend a default
config for daily use on this machine.
```

---

# Additive prompts — complete the diagram WITHOUT disturbing what works

These add the valuable pieces still missing from the reference diagram (Slack,
Qdrant, Docker packaging, DuckDB analytics). Every one is **additive only**: it
adds files and optional config, and changes nothing about your existing,
working build unless you explicitly flip a switch.

## Additive primer — PASTE THIS BEFORE EACH ADDITIVE PROMPT BELOW

```
You are ADDING a capability to Forge without changing anything that already
works. This is additive-only. Read the repo first.

HARD RULES — do not violate:
- Do NOT modify core/ interfaces. New work goes behind the EXISTING ports.
- Do NOT modify these working files:
    product/code_source.py   (the tree-sitter code chunker)
    product/approval.py      (the human approval gates)
    adapters/llm_ollama.py, adapters/embed_ollama.py, adapters/retriever_chroma.py
    app/ask.py
- Do NOT change .data/workspace/ sandbox behaviour or its git identity.
- You MAY add NEW files (a new adapter, a new tool, a new CLI).
- You MAY add NEW keys to config.yaml and NEW branches in app/factory.py — but
  every existing config value keeps its current default, so default behaviour is
  byte-for-byte unchanged.
- Any new capability is OFF or unselected by default. The app must behave exactly
  as it does today unless I explicitly opt in via config.

Before editing: list the NEW files you will add and the exact config keys and
factory branches you will add. Confirm you will touch NONE of the protected
files. Wait for my go-ahead.

REGRESSION CHECK — run at the end of every task and show me the results:
- python -m app.smoke_test still passes.
- An existing code Q&A still returns a cited answer as before.
- `git diff --stat` shows the protected files above are UNCHANGED.
```

---

## Prompt 19 — Slack notifications (diagram box 5: Tool Use via MCP)

> **In plain terms:** Forge pings you on Slack when a long task finishes or a PR opens, so you don't have to sit watching a terminal.

```
Add an OPTIONAL Slack notifier. On CPU a task takes minutes, so a "done" ping is
genuinely useful. Additive only — follow the additive primer above.

Requirements:
- New file adapters/tool_slack.py implementing core.tools.Tool.
  name: "notify_slack", requires_approval: False. It posts a short STATUS line
  only (e.g. "task complete", "PR opened: <url>") — never source code.
- It sends to a Slack Incoming Webhook URL read from .env as SLACK_WEBHOOK_URL
  via the existing config loader. Never hardcode or log the URL.
- DEFAULT OFF: if SLACK_WEBHOOK_URL is not set, the tool is a silent no-op.
  Nothing about current behaviour changes for anyone without a webhook.
- Add an optional call at natural end points (task finished, PR opened) that
  invokes the notifier IF configured. Do not weave it into core graph logic in a
  way that could fail a run — a failed notification must never fail the task;
  catch and log.
- Register it in the tool registry / factory as a new entry.

Acceptance:
- With no SLACK_WEBHOOK_URL: run a task, confirm behaviour is identical to today
  and no error occurs.
- With a webhook set (I'll add it): run a task and confirm one status line posts.
- Show git diff proving the protected files are untouched.
```

---

## Prompt 20 — Qdrant vector store option (diagram box 3: RAG at scale)

> **In plain terms:** adds a bigger, on-disk vector store for large codebases, switchable in one config line — Chroma stays the default and nothing changes unless you flip it.

```
Add Qdrant as an ALTERNATIVE retriever behind the existing Retriever port. This
is both a diagram item and a proof that the port design works. Additive only —
follow the additive primer above.

Requirements:
- New file adapters/retriever_qdrant.py implementing the SAME
  core.retriever.Retriever protocol as the existing ChromaRetriever, with the
  SAME method signatures (add, search) and the SAME skeleton-index contract:
  it stores POINTERS (path + start_line + end_line + metadata), not code bodies,
  and supports the same metadata filtering (e.g. by module).
- Uses a local Qdrant (docker container or the embedded/local mode). Config keys
  under retriever: url or path, collection.
- Register "qdrant" as a new branch in app/factory.py build_retriever.
- CRITICAL: retriever.adapter stays "chroma" by DEFAULT. Do NOT modify
  retriever_chroma.py or the Retriever interface. This is a sibling file only.
- Reuse the existing embedder injection exactly as Chroma does.

Acceptance:
- With retriever.adapter: chroma (default) everything is byte-for-byte as today.
- Then I switch to qdrant, re-index the sample repo, and run the SAME query
  ("how does customauth authenticate a user"). It must return comparable results
  with correct citations.
- Show git diff proving retriever_chroma.py, core/, and app/ask.py are unchanged.
```

---

## Prompt 21 — Docker packaging for reproducible local run (diagram box 8: Deployment)

> **In plain terms:** packages the supporting services so the whole thing starts with one command and behaves the same on any machine — inference still runs natively on your own hardware.

```
Fill in the docker-compose.yml (currently a placeholder) to package the app's
SUPPORTING services for a reproducible local run. Additive only — follow the
additive primer above.

Requirements:
- Compose services: the Forge app, ChromaDB (if run as a service) and Phoenix.
- IMPORTANT: Ollama stays NATIVE on the host, not in a container — it needs host
  access to the integrated GPU (Vulkan) and containerising it wastes RAM on a
  16 GB machine. The app in the container reaches host Ollama via
  host.docker.internal. Document this.
- Use a NAMED docker volume for .data, NOT a Windows bind-mount — SQLite file
  locking fails over Docker Desktop's Windows bind-mount translation and the
  container will crash with "disk I/O error". (This lesson was already learned
  for Phoenix; apply it here.)
- Everything must ALSO still run natively without Docker exactly as today. Docker
  is an added option, not a replacement. Do not change how the app runs natively.
- Add a short README section: native run (unchanged) vs `docker compose up`.

Do NOT attempt to deploy to Cloudflare Workers or Hugging Face Spaces. Those free
tiers have no GPU and can't run this stack, and exposing local Ollama to a cloud
UI needs a tunnel with real security implications. If I later want remote access,
we'll discuss a tunnel separately. For now, Docker = reproducible LOCAL run only.

Acceptance:
- Native run still works unchanged (smoke test + a Q&A).
- `docker compose up` starts the services; the app reaches host Ollama; state
  persists across a container restart (the named-volume test).
```

---

## Prompt 22 — DuckDB analytics (diagram box 7: Data Layer) — OPTIONAL, low priority

> **In plain terms:** adds a small analytics view over your codebase and run history — symbol counts, most-edited files, run stats. Nice to have, not important.

```
Add an OPTIONAL, read-only analytics view using DuckDB. Honest note: this is the
lowest-value diagram item for a code tool — build it only if you want the Data
Layer box filled. Requires the run-history tables (Prompt 14) to exist first.
Additive only — follow the additive primer above.

Requirements:
- New file app/stats.py, a CLI: `python -m app.stats`.
- Uses DuckDB to run read-only queries by ATTACHING the existing SQLite files —
  do not create a new store, do not duplicate data, do not migrate anything.
- Reports: total symbols indexed per module/language (from the index metadata),
  and from run history: number of runs, pass/fail rate, average duration, and
  most-frequently-edited files.
- Strictly read-only. It must not write to .data/forge.db or the index.

Acceptance:
- `python -m app.stats` prints the metrics.
- Confirm .data/forge.db and the Chroma index are byte-for-byte unchanged after
  running it (read-only proof).
```

---

## Priority for the additive prompts

- **19 (Slack)** — highest everyday value; long CPU tasks make a "done" ping genuinely useful. Small and safe.
- **21 (Docker)** — worth it for reproducibility; the named-volume rule is the one thing to get right.
- **20 (Qdrant)** — build when a repo grows past a few hundred thousand symbols, OR now if you want to prove the port design end to end.
- **22 (DuckDB)** — optional, cosmetic for a code tool. Do it last or skip it.

Skipped on purpose: Cloudflare/HF deployment (no GPU, needs a tunnel — wrong fit
for local inference), and a generic database MCP tool (only useful if the target
app has a DB you want Forge to query — say so and we'll add it then).

---

## Prompt 23 — Retrieval-quality eval harness (regression gate)

> **In plain terms:** a fixed quiz with known right answers for the retriever — did the correct file/symbol come back — graded automatically so a chunking or config change can be proven better or worse instead of eyeballed, and wired into CI as a gate.

```
Add an evaluation harness that measures RETRIEVAL quality against a fixed set of
questions with known-correct answers. Additive only — follow the additive primer:
do not modify core/, product/code_source.py, product/approval.py, the existing
adapters, or app/ask.py. This is a new, self-contained tool.

WHY retrieval and not answer text: retrieval has a definite right answer (did the
correct file/symbol come back?), so it can be scored automatically. Judging prose
quality is subjective and comes later.

Requirements:
1. A dataset file eval/dataset.yaml — a list of cases, each:
     - question: "how does customauth authenticate a user"
       expect_files: ["UmsUserDetailsService.java"]     # substring match on cited path
       expect_symbols: ["loadUserByUsername"]           # optional, symbol-name match
   Seed it with 8-10 cases for the indexed repo. Make it easy to add more.

2. A runner: python -m eval.run
   - For each case, call the EXISTING retriever (retriever.search) and check
     whether the expected files/symbols appear in the top-k results.
   - Compute and print these standard metrics:
       * hit@k        — fraction of cases where at least one expected item is in top-k
       * MRR          — mean reciprocal rank of the first correct hit
       * precision@k  — of the top-k returned, fraction that are relevant
   - Print a per-case PASS/FAIL table AND the summary metrics.
   - Support --k to override top-k so I can see how metrics change with k.
   - Exit non-zero if hit@k drops below a configurable threshold (so it can act
     as a regression gate later).

3. A compare mode: python -m eval.run --compare
   - Runs the suite twice with two configs (e.g. two different top_k values, or
     before/after I change chunking) and prints a side-by-side metric table so I
     can see if a change helped or hurt.

4. Keep it dependency-light. No new heavy libraries; plain Python + the existing
   retriever is enough.

Acceptance check:
- python -m eval.run prints a per-case table and hit@k / MRR / precision@k.
- Deliberately worsen retrieval (e.g. set top_k=1) and confirm the metrics drop
  and the exit code flips — proving the harness actually measures quality.
- Show git diff proving the protected files are untouched.
```

Landed 2026-08-03: `eval/dataset.yaml` (11 cases against the indexed
rag-frontend-angular-v2 repo — real file/symbol pairs pulled from the live
Chroma index, not invented), `eval/run.py` (per-case PASS/FAIL table,
hit@k/MRR/precision@k, `--k`, `--min-hit-at-k` default 0.7, `--compare` with
`--compare-k`/`--compare-config`). Verified the harness actually discriminates:
at the configured `top_k=8`, hit@8=1.000 (exit 0); forcing `--k 1` drops
hit@1 to 0.636 and flips the exit code to 1.

Then wired as a CI gate: `.github/workflows/retrieval-eval.yml`, triggered on
push/PR to `main` plus manual dispatch, `runs-on: self-hosted`. No
GitHub-hosted-runner path exists for this job — the Chroma index, lexical
DB, and `.env` are all gitignored local state (see `.gitignore`), and Ollama
(embeddings) has to already be serving on the runner's machine; a stock
`ubuntu-latest` runner starts with none of that and there's no cheap way to
build it fresh on every run. The checkout step uses `clean: false` so the
default `git clean -ffdx` doesn't wipe `.venv/`/`.env`/`.data/` between runs
— only tracked files (the code under test) get updated.

**Open item:** no self-hosted runner is registered on `ranjithpotlapelly/forge`
yet (`gh api repos/.../actions/runners` → zero) — the workflow file alone
won't execute anything until one is registered on this machine (repo Settings
→ Actions → Runners → New self-hosted runner; a one-time interactive step,
left to be done by hand rather than automated).