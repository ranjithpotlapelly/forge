"""Adapter (Phase 4/5/14): implements core.engine.Engine via a LangGraph
StateGraph, checkpointed to SQLite so graph state survives across process
runs.

The graph's node logic lives here too, not in product/ — LangGraph's
add_node/add_conditional_edges wiring and the node functions it calls are
inseparable enough that splitting them across adapters/ and product/ meant
this adapter had to import product/, breaking the dependency direction
(adapters/ -> core/ <- product/). Keeping node logic here costs a bit of
vendor coupling in these functions; core/engine.py stays the vendor-free port
everything else depends on.

Phase 14 (task path) still needed real product/ logic (make_plan, edit_file)
that's too substantial to duplicate here. Rather than import it directly —
the same violation — app/wiring.py (the composition root, which is meant to
know about every layer) imports it and injects it into this class as a plain
callable. This file never has a `from product...` import anywhere.

Phase 17 threads that same rule through the plan-approval and PR-approval
gates: config["configurable"]["approve"] is expected to return something
duck-typed like product.approval.PlanDecision (a `.decision` /
`.feedback` pair) -- this file reads those two attributes but never imports
the dataclass itself, so its own fallback default (used when a caller passes
no approve callback at all) is a plain types.SimpleNamespace with the same
two attributes, not a real PlanDecision.

Phase 19 instruments every node in _build() with _instrumented(), which
records a core.run_history.RunHistory step around each one (thread_id/run_id
live in config["configurable"], same channel as on_progress above).
approval_gate and pr_gate get a richer detail payload (_step_detail) --
what was proposed and what the human decided -- since that's the audit
trail the run history exists for; every other node gets a generic
ok/error record. RunHistory is a plain core/ port, so importing it here
doesn't touch the product/ rule above.

Phase 20 replaces the synchronous config["configurable"]["approve"]/
["approve_pr"] callbacks approval_gate_node/pr_approval_gate_node used to
read with langgraph.types.interrupt() calls. A callback that blocks a thread
waiting on a human can't survive that thread's process dying; interrupt()
instead checkpoints the run to .data/checkpoints.db and returns control
immediately -- resuming (possibly in a different process entirely, see
app/resume.py) is Command(resume=value) against the same thread_id, and
LangGraph re-executes only the interrupted node, never anything upstream of
it (verified empirically, not just assumed, before writing this).

The payload passed to interrupt() must be a plain, checkpoint-safe dict (no
ProposedPR objects, still no product/ import) -- LangGraphEngine._run_graph
reconstructs a SimpleNamespace with the same shape from that dict before
calling a caller's approve()/approve_pr(), so existing callers (Chainlit,
every task-flow smoke test) that expect a plan object with .title/.branch/
.steps[i].kind attributes keep working completely unchanged.
"""
from __future__ import annotations
import inspect
import json
import re
import sqlite3
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, TypedDict
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import StateGraph, END
from langgraph.types import Command, RunnableConfig, interrupt
from opentelemetry import trace
from core.llm import LLMClient
from core.retriever import Retriever
from core.run_history import RunHistory
from core.tools import Tool
from core.types import Answer, Chunk, Message

_tracer = trace.get_tracer(__name__)

MIN_RELEVANCE = 0.2  # below this, treat retrieval as "no relevant context"

def _emit(config: RunnableConfig, text: str) -> None:
    """Best-effort progress notification -- on_progress is optional in
    configurable (CLI/test callers rarely supply one), so this never raises
    just because nobody's listening."""
    config["configurable"].get("on_progress", lambda *_: None)(text)

# --- run-history instrumentation (Phase 19) ---------------------------------

def _step_detail(node_name: str, state: GraphState, result: GraphState) -> str | None:
    """Richer, explicit audit detail for the two approval nodes -- what was
    proposed and what the human chose -- vs. a generic per-node record for
    everything else. Returns None to fall back to a plain status-only record."""
    if node_name == "approval_gate":
        plan, decision = state.get("plan"), result.get("plan_decision")
        if plan is None or decision is None:
            return None
        return json.dumps({
            "proposed": {
                "title": plan.title, "branch": plan.branch,
                "steps": [{"kind": s.kind, "target_path": s.target_path, "description": s.description} for s in plan.steps],
            },
            "decision": decision,
            "feedback": result.get("plan_feedback"),
        })
    if node_name == "pr_gate":
        plan, approved = state.get("plan"), result.get("pr_approved")
        if plan is None or approved is None:
            return None
        return json.dumps({
            "proposed": {"title": plan.title, "body": plan.body, "diff_chars": len(state.get("edit_diff") or "")},
            "decision": "approve" if approved else "reject",
        })
    return None

def _instrumented(node_name: str, fn: Callable[..., GraphState]) -> Callable[..., GraphState]:
    """Wraps a node so every execution is recorded as a run_step, regardless
    of whether the underlying node function itself accepts `config` -- this
    wrapper always does, so LangGraph always injects it here, and calls the
    wrapped fn either way based on its own signature."""
    accepts_config = "config" in inspect.signature(fn).parameters

    def wrapped(state: GraphState, config: RunnableConfig) -> GraphState:
        run_history: RunHistory | None = config["configurable"].get("run_history")
        run_id = config["configurable"].get("run_id")
        start = time.monotonic()
        try:
            result = fn(state, config=config) if accepts_config else fn(state)
        except GraphBubbleUp:
            # interrupt() (or a cooperative drain) -- not a failure, just a
            # pause. Nothing to record: this node hasn't actually finished,
            # so there's no result yet to describe. It gets recorded normally
            # when it finally returns on a later resume.
            raise
        except Exception as e:
            if run_history and run_id:
                run_history.record_step(run_id, node_name, "error", str(e), (time.monotonic() - start) * 1000)
            raise
        if run_history and run_id:
            detail = _step_detail(node_name, state, result)
            run_history.record_step(run_id, node_name, "ok", detail, (time.monotonic() - start) * 1000)
        return result

    return wrapped

# --- intent classification -------------------------------------------------
# Two fast, no-model-call heuristics cover the common cases in both
# directions; a model call (~10-30s on CPU) is the last resort, not the
# default for every plain question. Every existing Q&A smoke-test query
# ("What function...", "how does...") matches _QUESTION_RE and never pays
# that cost.
_TASK_VERB_RE = re.compile(r"^\s*(fix|add|refactor|implement|rename)\b", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#\d+")
_QUESTION_RE = re.compile(
    r"^\s*(what|how|why|where|when|who|which|is|are|does|do|can|could|should)\b",
    re.IGNORECASE,
)

class GraphState(TypedDict, total=False):
    # Q&A path
    question: str
    chunks: list[Chunk]
    answer: Answer
    # shared
    intent: str
    # task path
    task: str
    task_files: list[Chunk]
    workspace_branch: str
    plan: Any  # product.schema.ProposedPR — untyped here so this file never imports product/
    plan_decision: str  # "approve" | "edit" | "reject", mirrors PlanDecision.decision
    plan_feedback: str
    approved: bool
    rejection_reason: str
    edit_results: list[dict]
    edit_diff: str
    backups: dict[str, str]
    attempt: int
    test_result: dict
    test_history: list[dict]
    commit_result: dict
    push_result: dict
    pr_approved: bool
    pr_rejection_reason: str
    pr_result: dict
    gave_up: bool
    rolled_back_paths: list[str]

# --- Q&A nodes (unchanged since Phase 4) ------------------------------------

def retrieve(state: GraphState, retriever: Retriever, k: int = 8) -> GraphState:
    return {"chunks": retriever.search(state["question"], k=k)}

def has_context(state: GraphState) -> str:
    """Decision: route to 'answer' if retrieval found relevant chunks, else 'decline'."""
    chunks = state.get("chunks") or []
    if chunks and chunks[0].score is not None and chunks[0].score >= MIN_RELEVANCE:
        return "answer"
    return "decline"

def build_answer_messages(
    question: str, chunks: list[Chunk], max_expanded: int | None = None,
) -> list[Message]:
    """The Q&A prompt: shared by the `answer` node (llm.generate, below) and
    the Chainlit UI's streaming path (llm.stream) so both ask the model the
    exact same question the exact same way -- one place defines it.

    Phase 23 (perf tuning): chunks are assumed already ranked best-first by
    the retriever. When max_expanded is given and there are more chunks than
    that, only the top max_expanded get their full body in the prompt; the
    rest contribute a path-only reference line -- trims prefill without an
    extra LLM call to summarize them.
    """
    if max_expanded is not None and len(chunks) > max_expanded:
        expanded, referenced = chunks[:max_expanded], chunks[max_expanded:]
    else:
        expanded, referenced = chunks, []
    context = "\n\n".join(
        f"[{c.metadata.get('path', '?')}:{c.metadata.get('start_line', '?')}]\n{c.content}"
        for c in expanded
    )
    if referenced:
        refs = "\n".join(
            f"[{c.metadata.get('path', '?')}:{c.metadata.get('start_line', '?')}] (not expanded)"
            for c in referenced
        )
        context = f"{context}\n\nOther possibly relevant locations:\n{refs}"
    return [
        Message(role="system", content=(
            "Answer the question using only the provided code context. "
            "Cite sources inline as [path:line]."
        )),
        Message(role="user", content=f"Context:\n{context}\n\nQuestion: {question}"),
    ]

def answer(state: GraphState, llm: LLMClient) -> GraphState:
    chunks = state["chunks"]
    messages = build_answer_messages(state["question"], chunks)
    return {"answer": Answer(text=llm.generate(messages), citations=chunks)}

def decline(state: GraphState) -> GraphState:
    return {"answer": Answer(text="I don't have enough indexed context to answer that.", citations=[])}

# --- shared entry: classify question vs. task -------------------------------

def classify_intent(state: GraphState, llm: LLMClient) -> GraphState:
    text = state.get("question", "")
    if _TASK_VERB_RE.match(text) or _ISSUE_REF_RE.search(text):
        return {"intent": "task", "task": text}
    if _QUESTION_RE.match(text) or text.rstrip().endswith("?"):
        return {"intent": "question"}
    messages = [
        Message(role="system", content=(
            "Classify the user's message as exactly one word: 'task' if it asks "
            "to change code (fix/add/refactor/implement/rename something), or "
            "'question' if it asks about existing code. Respond with exactly one word."
        )),
        Message(role="user", content=text),
    ]
    verdict = llm.generate(messages).strip().lower()
    if "task" in verdict:
        return {"intent": "task", "task": text}
    return {"intent": "question"}

def route_intent(state: GraphState) -> str:
    return state.get("intent", "question")

# --- task-path nodes ---------------------------------------------------------

def prepare_workspace_node(
    state: GraphState, retriever: Retriever, repo_path: str, prepare_tool: Tool,
) -> GraphState:
    probe = retriever.search(state.get("task", ""), k=1)
    if not probe:
        raise RuntimeError(
            "No indexed content found — index a repo (e.g. `python -m "
            "app.ingest_smoke_test`) before starting a task."
        )
    result = json.loads(prepare_tool.run(repo_path=repo_path))
    return {"workspace_branch": result["branch"]}

def task_retrieve(state: GraphState, retriever: Retriever, workspace: Path, k: int = 8) -> GraphState:
    """Whole files, not top-k symbol fragments — editing a method safely
    requires the surrounding file. Same Retriever port as Q&A, different use
    of its results: dedupe hits to unique paths, then read each file whole
    from the (now-populated) workspace rather than embedding fragment text."""
    hits = retriever.search(state.get("task", ""), k=k)
    seen: list[str] = []
    for c in hits:
        path = c.metadata.get("path")
        if path and path not in seen:
            seen.append(path)
    files: list[Chunk] = []
    for path in seen:
        file_path = workspace / path
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(Chunk(content=content, metadata={"path": path}))
    return {"task_files": files}

def plan_node(state: GraphState, config: RunnableConfig, plan_fn: Callable[..., Any], llm: LLMClient) -> GraphState:
    feedback = state.get("plan_feedback")
    _emit(config, "Revising plan..." if feedback else "Generating plan...")
    plan = plan_fn(llm=llm, task=state.get("task", ""), files=state.get("task_files", []), feedback=feedback)
    return {"plan": plan}

def approval_gate_node(state: GraphState) -> GraphState:
    """Everything before interrupt() re-runs on resume (LangGraph replays a
    node from its own top) -- printing again is harmless, which is exactly
    why nothing here does anything less idempotent than that before the
    interrupt() call. The payload is a plain dict (checkpoint-safe, no
    ProposedPR/product import); LangGraphEngine._run_graph reconstructs an
    attribute-accessible plan object from it before calling a caller's
    approve(), and passes that caller's return value straight through as the
    resume value -- so what interrupt() returns here is duck-typed exactly
    like the old config["configurable"]["approve"](plan) return was."""
    plan = state["plan"]
    print("\n=== Proposed plan ===")
    print(f"Title:  {plan.title}")
    print(f"Branch: {plan.branch}")
    print("Steps:")
    for i, step in enumerate(plan.steps, 1):
        print(f"  {i}. [{step.kind}] {step.target_path} - {step.description}")
    print()
    decision = interrupt({
        "kind": "plan_approval",
        "plan": {
            "title": plan.title, "branch": plan.branch, "body": plan.body,
            "steps": [{"kind": s.kind, "target_path": s.target_path, "description": s.description} for s in plan.steps],
        },
    })
    if decision.decision == "approve":
        return {"approved": True, "plan_decision": "approve"}
    if decision.decision == "edit":
        print(f"Revision requested: {decision.feedback}\n")
        return {"plan_decision": "edit", "plan_feedback": decision.feedback}
    return {"approved": False, "plan_decision": "reject", "rejection_reason": "plan rejected by human"}

def route_after_approval(state: GraphState) -> str:
    decision = state.get("plan_decision")
    if decision == "approve":
        return "proceed"
    if decision == "edit":
        return "revise"
    return "rejected"

def rejected_node(state: GraphState) -> GraphState:
    return {}  # rejection_reason already set by approval_gate_node

def edit_node(
    state: GraphState, config: RunnableConfig, apply_plan_fn: Callable[..., Any], code_model: LLMClient,
    retriever: Retriever, read_tool: Tool, write_tool: Tool,
) -> GraphState:
    """Applies the already-approved plan. The human decision already
    happened at approval_gate — writes here go through write_file (so
    forge.require_approval_for policy still formally applies to every write)
    but with an always-approve callback, since re-prompting per file would
    mean the "one decision covers the whole plan" approval never actually
    finishes anything.

    attempt/backups/test_result are read from state, not module-level
    globals, so multiple concurrent runs (or a resumed checkpoint) never
    share retry state with each other."""
    plan = state["plan"]
    attempt = state.get("attempt", 1)
    backups = state.get("backups")
    test_result = state.get("test_result")
    test_feedback = test_result["output"] if test_result and not test_result.get("passed") else None

    print(f"\n=== Edit attempt {attempt}/3 ===")
    if test_feedback:
        print(f"Retrying with test failure feedback ({len(test_feedback)} chars of output)")
    for step in plan.steps:
        print(f"  - [{step.kind}] {step.target_path}: {step.description}")

    try:
        outcome = apply_plan_fn(
            code_model, retriever, read_tool, write_tool, plan,
            backups=backups, attempt=attempt, test_feedback=test_feedback,
            on_progress=lambda text: _emit(config, text),
        )
    except Exception as e:  # noqa: BLE001 — EditFailure or similar; can't import the specific type (adapters/ -> product/)
        print(f"Edit attempt {attempt} failed outright: {e}")
        _emit(config, f"Edit attempt {attempt} failed outright: {e}")
        return {"edit_results": [{"status": "failed", "detail": str(e)}], "edit_diff": ""}
    print(f"Edit attempt {attempt} wrote {len([r for r in outcome['results'] if r['status'] == 'ok'])} file(s)")
    return {"edit_results": outcome["results"], "edit_diff": outcome["diff"], "backups": outcome["backups"]}

def test_node(state: GraphState, config: RunnableConfig, run_tests_tool: Tool) -> GraphState:
    attempt = state.get("attempt", 1)
    _emit(config, f"Running tests (attempt {attempt}/3)...")
    try:
        test_result = json.loads(run_tests_tool.run())
    except RuntimeError as e:
        if "no recognized build system" not in str(e):
            raise
        # Nothing to verify isn't a failure — plenty of real tasks target a
        # repo with no test suite at all. Treat it like a pass rather than
        # crashing the graph or burning a retry attempt on it.
        test_result = {"passed": True, "exit_code": 0, "output": "no test framework detected in the workspace", "duration_s": 0.0}
    history = state.get("test_history", []) + [{
        "attempt": attempt, "passed": test_result["passed"], "exit_code": test_result["exit_code"],
    }]
    print(f"\n=== Test attempt {attempt}/3 ===")
    print(f"Passed: {test_result['passed']} (exit_code={test_result['exit_code']}, {test_result['duration_s']}s)")
    if not test_result["passed"]:
        print(f"Why it failed (last part of output):\n{test_result['output'][-1500:]}")
    _emit(config, f"Attempt {attempt}/3: {'passed' if test_result['passed'] else 'failed'}")
    return {"test_result": test_result, "test_history": history}

def route_after_test(state: GraphState) -> str:
    if state.get("test_result", {}).get("passed"):
        return "commit"
    if state.get("attempt", 1) >= 3:
        return "give_up"
    return "retry"

def bump_attempt_node(state: GraphState) -> GraphState:
    return {"attempt": state.get("attempt", 1) + 1}

def commit_node(state: GraphState, config: RunnableConfig, commit_fn: Callable[..., Any], commit_tool: Tool) -> GraphState:
    plan = state["plan"]
    _emit(config, "Committing...")
    try:
        detail = commit_fn(commit_tool, plan.title, plan.body)
        print(f"\n=== Committed ===\n{detail}")
        _emit(config, "Committed.")
        return {"commit_result": {"status": "ok", "detail": detail}}
    except Exception as e:  # noqa: BLE001 — can't import the specific commit-tool exception type here
        print(f"\n=== Commit failed ===\n{e}")
        _emit(config, f"Commit failed: {e}")
        return {"commit_result": {"status": "error", "detail": str(e)}}

def push_node(state: GraphState, config: RunnableConfig, push_fn: Callable[..., Any], push_tool: Tool) -> GraphState:
    branch = state.get("workspace_branch", "main")
    _emit(config, f"Pushing {branch}...")
    try:
        detail = push_fn(push_tool, branch)
        print(f"\n=== Pushed ===\n{detail}")
        _emit(config, "Pushed.")
        return {"push_result": {"status": "ok", "detail": detail}}
    except Exception as e:  # noqa: BLE001 — can't import the specific push-tool exception type here
        print(f"\n=== Push failed ===\n{e}")
        _emit(config, f"Push failed: {e}")
        return {"push_result": {"status": "error", "detail": str(e)}}

def route_after_push(state: GraphState) -> str:
    return "pr_gate" if state.get("push_result", {}).get("status") == "ok" else "push_failed"

def push_failed_node(state: GraphState) -> GraphState:
    return {}  # push_result already carries the error detail

# --- second, separate approval: opening the PR is a distinct, externally
# visible act from "do the approved edit" (commit/push), so it gets its own
# gate rather than riding on the plan approval from earlier in the run -----

def pr_approval_gate_node(state: GraphState) -> GraphState:
    plan = state["plan"]
    diff = state.get("edit_diff", "")
    print("\n=== Ready to open a pull request ===")
    print(f"Title:  {plan.title}")
    print(f"Branch: {state.get('workspace_branch')}")
    print(f"Diff:\n{diff}\n")
    approved = interrupt({"kind": "pr_approval", "title": plan.title, "body": plan.body, "diff": diff})
    if not approved:
        return {"pr_approved": False, "pr_rejection_reason": "PR rejected by human"}
    return {"pr_approved": True}

def route_after_pr_approval(state: GraphState) -> str:
    return "open_pr" if state.get("pr_approved") else "pr_rejected"

def pr_rejected_node(state: GraphState) -> GraphState:
    return {}  # pr_rejection_reason already set by pr_approval_gate_node

def open_pr_node(state: GraphState, config: RunnableConfig, open_pr_fn: Callable[..., Any], open_pr_tool: Tool) -> GraphState:
    plan = state["plan"]
    head = state.get("workspace_branch", "main")
    _emit(config, "Opening pull request...")
    try:
        url = open_pr_fn(open_pr_tool, plan.title, plan.body, "main", head)
        print(f"\n=== PR opened ===\n{url}")
        _emit(config, f"PR opened: {url}")
        return {"pr_result": {"status": "ok", "url": url}}
    except Exception as e:  # noqa: BLE001 — can't import the specific open_pr-tool exception type here
        print(f"\n=== open_pr failed ===\n{e}")
        _emit(config, f"open_pr failed: {e}")
        return {"pr_result": {"status": "error", "detail": str(e)}}

def give_up_node(state: GraphState, rollback_fn: Callable[..., Any], write_tool: Tool) -> GraphState:
    backups = state.get("backups") or {}
    rolled_back = rollback_fn(write_tool, backups)
    print(f"\n=== Giving up after {state.get('attempt', 1)} attempts ===")
    print(f"Rolled back {len(rolled_back)} file(s) to their pre-edit content: {rolled_back}")
    return {"gave_up": True, "rolled_back_paths": rolled_back}

class LangGraphEngine:
    def __init__(
        self,
        llm: LLMClient,
        retriever: Retriever,
        k: int = 8,
        checkpoint_path: str | None = None,
        code_model: LLMClient | None = None,
        read_tool: Tool | None = None,
        write_tool: Tool | None = None,
        prepare_tool: Tool | None = None,
        run_tests_tool: Tool | None = None,
        commit_tool: Tool | None = None,
        push_tool: Tool | None = None,
        open_pr_tool: Tool | None = None,
        plan_fn: Callable[..., Any] | None = None,
        apply_plan_fn: Callable[..., Any] | None = None,
        commit_fn: Callable[..., Any] | None = None,
        push_fn: Callable[..., Any] | None = None,
        open_pr_fn: Callable[..., Any] | None = None,
        rollback_fn: Callable[..., Any] | None = None,
        repo_path: str = ".",
        workspace: str | Path = "./.data/workspace",
        run_history: RunHistory | None = None,
    ):
        self.llm, self.retriever, self.k = llm, retriever, k
        self.code_model = code_model
        self.read_tool, self.write_tool, self.prepare_tool = read_tool, write_tool, prepare_tool
        self.run_tests_tool, self.commit_tool = run_tests_tool, commit_tool
        self.push_tool, self.open_pr_tool = push_tool, open_pr_tool
        self.plan_fn, self.apply_plan_fn = plan_fn, apply_plan_fn
        self.commit_fn, self.push_fn, self.open_pr_fn = commit_fn, push_fn, open_pr_fn
        self.rollback_fn = rollback_fn
        self.repo_path = repo_path
        self.run_history = run_history
        self.workspace = Path(workspace).resolve()
        self._checkpointer = self._build_checkpointer(checkpoint_path)
        self._graph = self._build()

    def _build_checkpointer(self, checkpoint_path: str | None):
        if not checkpoint_path:
            return None
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        # Graph state carries our own value types; allowlist them explicitly so
        # checkpointing keeps working once langgraph starts rejecting
        # unregistered types by default. These are string module/class
        # references for the serializer's allowlist, not imports.
        serde = JsonPlusSerializer(allowed_msgpack_modules=[
            ("core.types", "Chunk"), ("core.types", "Answer"),
            ("product.schema", "ProposedPR"), ("product.schema", "PlanStep"),
        ])
        saver = SqliteSaver(conn, serde=serde)
        saver.setup()
        return saver

    def _build(self):
        g = StateGraph(GraphState)
        g.add_node("classify_intent", _instrumented("classify_intent", partial(classify_intent, llm=self.llm)))
        g.add_node("retrieve", _instrumented("retrieve", partial(retrieve, retriever=self.retriever, k=self.k)))
        g.add_node("answer", _instrumented("answer", partial(answer, llm=self.llm)))
        g.add_node("decline", _instrumented("decline", decline))
        g.add_node("prepare_workspace", _instrumented("prepare_workspace", partial(
            prepare_workspace_node, retriever=self.retriever,
            repo_path=self.repo_path, prepare_tool=self.prepare_tool,
        )))
        g.add_node("task_retrieve", _instrumented("task_retrieve", partial(
            task_retrieve, retriever=self.retriever, workspace=self.workspace, k=self.k,
        )))
        g.add_node("plan", _instrumented("plan", partial(plan_node, plan_fn=self.plan_fn, llm=self.llm)))
        g.add_node("approval_gate", _instrumented("approval_gate", approval_gate_node))
        g.add_node("rejected", _instrumented("rejected", rejected_node))
        g.add_node("edit", _instrumented("edit", partial(
            edit_node, apply_plan_fn=self.apply_plan_fn, code_model=self.code_model,
            retriever=self.retriever, read_tool=self.read_tool, write_tool=self.write_tool,
        )))
        g.add_node("test", _instrumented("test", partial(test_node, run_tests_tool=self.run_tests_tool)))
        g.add_node("bump_attempt", _instrumented("bump_attempt", bump_attempt_node))
        g.add_node("commit", _instrumented("commit", partial(commit_node, commit_fn=self.commit_fn, commit_tool=self.commit_tool)))
        g.add_node("push", _instrumented("push", partial(push_node, push_fn=self.push_fn, push_tool=self.push_tool)))
        g.add_node("push_failed", _instrumented("push_failed", push_failed_node))
        g.add_node("pr_gate", _instrumented("pr_gate", pr_approval_gate_node))
        g.add_node("pr_rejected", _instrumented("pr_rejected", pr_rejected_node))
        g.add_node("open_pr", _instrumented("open_pr", partial(open_pr_node, open_pr_fn=self.open_pr_fn, open_pr_tool=self.open_pr_tool)))
        g.add_node("give_up", _instrumented("give_up", partial(give_up_node, rollback_fn=self.rollback_fn, write_tool=self.write_tool)))

        g.set_entry_point("classify_intent")
        g.add_conditional_edges("classify_intent", route_intent, {"question": "retrieve", "task": "prepare_workspace"})

        g.add_conditional_edges("retrieve", has_context, {"answer": "answer", "decline": "decline"})
        g.add_edge("answer", END)
        g.add_edge("decline", END)

        g.add_edge("prepare_workspace", "task_retrieve")
        g.add_edge("task_retrieve", "plan")
        g.add_edge("plan", "approval_gate")
        g.add_conditional_edges("approval_gate", route_after_approval, {"proceed": "edit", "revise": "plan", "rejected": "rejected"})
        g.add_edge("rejected", END)

        g.add_edge("edit", "test")
        g.add_conditional_edges("test", route_after_test, {"commit": "commit", "retry": "bump_attempt", "give_up": "give_up"})
        g.add_edge("bump_attempt", "edit")
        g.add_edge("give_up", END)

        g.add_edge("commit", "push")
        g.add_conditional_edges("push", route_after_push, {"pr_gate": "pr_gate", "push_failed": "push_failed"})
        g.add_edge("push_failed", END)
        g.add_conditional_edges("pr_gate", route_after_pr_approval, {"open_pr": "open_pr", "pr_rejected": "pr_rejected"})
        g.add_edge("pr_rejected", END)
        g.add_edge("open_pr", END)

        return g.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _plan_namespace(plan_dict: dict) -> SimpleNamespace:
        """Reconstructs an attribute-accessible plan object (.title/.branch/
        .body/.steps[i].kind/.target_path/.description) from interrupt()'s
        plain-dict payload, so an approve() callback written against the old
        ProposedPR-shaped callback (Chainlit, every task-flow smoke test)
        doesn't need to change at all."""
        steps = [SimpleNamespace(**s) for s in plan_dict["steps"]]
        return SimpleNamespace(title=plan_dict["title"], branch=plan_dict["branch"], body=plan_dict["body"], steps=steps)

    def _run_graph(self, initial_input: Any, config: dict, approve, approve_pr) -> dict:
        """Invoke the graph and resolve any approval interrupt it hits via
        approve/approve_pr, looping until the graph reaches END -- or, if no
        callback is available to resolve a given interrupt, returning the
        paused result as-is (result["__interrupt__") for a caller to resume
        later, possibly in a different process (see resume_task/app/resume.py).

        initial_input is {"question": ..., ...} to start a fresh run, or None
        to continue whatever thread_id's checkpoint already has pending."""
        result = self._graph.invoke(initial_input, config=config)
        while result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            kind = payload.get("kind")
            if kind == "plan_approval" and approve is not None:
                decision = approve(self._plan_namespace(payload["plan"]))
                result = self._graph.invoke(Command(resume=decision), config=config)
            elif kind == "pr_approval" and approve_pr is not None:
                approved = bool(approve_pr(payload["diff"], payload["title"], payload["body"]))
                result = self._graph.invoke(Command(resume=approved), config=config)
            else:
                return result  # nobody to resolve this right now -- stay paused
        return result

    def ask(self, question: str, thread_id: str = "default") -> Answer:
        with _tracer.start_as_current_span("engine.ask") as span:
            span.set_attribute("engine.thread_id", thread_id)
            span.set_attribute("engine.question", question)
            run_id = self.run_history.start_run(thread_id, "qa", question) if self.run_history else None
            config = {"configurable": {
                "thread_id": thread_id,
                "on_progress": lambda *_: None,
                "run_history": self.run_history,
                "run_id": run_id,
            }}
            # A plain question should never reach the task path's approval
            # gates (classify_intent routes those to prepare_workspace, not
            # retrieve) -- but if it somehow did, auto-reject immediately
            # rather than leave a supposedly-synchronous ask() call paused
            # with no way for this caller to resolve it.
            reject_plan = lambda _plan: SimpleNamespace(decision="reject", feedback=None)
            reject_pr = lambda *_: False
            try:
                result = self._run_graph({"question": question}, config, reject_plan, reject_pr)
            except Exception as e:
                if self.run_history and run_id:
                    self.run_history.finish_run(run_id, "failed", str(e))
                raise
            if self.run_history and run_id:
                self.run_history.finish_run(run_id, "completed")
            answer_ = result["answer"]
            span.set_attribute("engine.answer.citation_count", len(answer_.citations))
            return answer_

    def run_task(
        self,
        task: str,
        thread_id: str = "default",
        approve: Callable[[Any], Any] | None = None,
        approve_pr: Callable[[str, str, str], bool] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        """approve receives a plan object (.title/.branch/.body/.steps[i].kind/
        .target_path/.description) and returns a duck-typed PlanDecision
        (product.approval.PlanDecision, or anything with the same
        .decision/.feedback shape). approve_pr receives (diff, title, body)
        for the second, PR-specific gate and returns a plain bool.

        If neither is given, hitting an approval gate pauses the run (real
        interrupt() + checkpoint to .data/checkpoints.db) and this returns
        immediately with result["__interrupt__"] set, instead of blocking --
        continue it later, in this process or a fresh one, with resume_task()
        or `python -m app.resume <thread_id>`."""
        with _tracer.start_as_current_span("engine.run_task") as span:
            span.set_attribute("engine.thread_id", thread_id)
            span.set_attribute("engine.task", task)
            run_id = self.run_history.start_run(thread_id, "task", task) if self.run_history else None
            config = {"configurable": {
                "thread_id": thread_id,
                "on_progress": on_progress or (lambda *_: None),
                "run_history": self.run_history,
                "run_id": run_id,
            }}
            try:
                result = self._run_graph({"question": task, "attempt": 1}, config, approve, approve_pr)
            except Exception as e:
                if self.run_history and run_id:
                    self.run_history.finish_run(run_id, "failed", str(e))
                raise
            if result.get("__interrupt__"):
                span.set_attribute("engine.paused", True)
                return result  # run_history row stays 'running' -- resume_task() finishes it
            if self.run_history and run_id:
                self.run_history.finish_run(run_id, "completed")
            span.set_attribute("engine.approved", bool(result.get("approved")))
            span.set_attribute("engine.attempts", result.get("attempt", 1))
            return result

    def resume_task(
        self,
        thread_id: str,
        approve: Callable[[Any], Any] | None = None,
        approve_pr: Callable[[str, str, str], bool] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Continue a run sitting at an approval interrupt for thread_id --
        loaded purely from the checkpointer, so this works from a brand new
        LangGraphEngine instance in a brand new process; nothing about the
        original run_task() call needs to still be alive. Raises RuntimeError
        if thread_id has no paused run to resume."""
        with _tracer.start_as_current_span("engine.resume_task") as span:
            span.set_attribute("engine.thread_id", thread_id)
            peek_config = {"configurable": {"thread_id": thread_id}}
            snapshot = self._graph.get_state(peek_config)
            if not snapshot.next:
                raise RuntimeError(
                    f"no paused run found for thread_id={thread_id!r} -- "
                    "it may have already finished, failed, or never started"
                )
            run_id = self.run_history.find_running_run(thread_id) if self.run_history else None
            config = {"configurable": {
                "thread_id": thread_id,
                "on_progress": on_progress or (lambda *_: None),
                "run_history": self.run_history,
                "run_id": run_id,
            }}
            try:
                result = self._run_graph(None, config, approve, approve_pr)
            except Exception as e:
                if self.run_history and run_id:
                    self.run_history.finish_run(run_id, "failed", str(e))
                raise
            if result.get("__interrupt__"):
                span.set_attribute("engine.paused", True)
                return result  # paused again at the next gate (e.g. pr_gate after approving the plan)
            if self.run_history and run_id:
                self.run_history.finish_run(run_id, "completed")
            span.set_attribute("engine.approved", bool(result.get("approved")))
            return result

    def delete_thread(self, thread_id: str) -> None:
        """Delete everything for a thread (Phase 21): its checkpoints (if a
        checkpointer is configured -- SqliteSaver.delete_thread(), a native
        LangGraph method, not something reimplemented here) and its
        run_history rows (runs + run_steps). Two separate kinds of per-thread
        state, deliberately kept in separate stores (see core/run_history.py's
        module docstring); this is the one place that knows to clear both."""
        if self._checkpointer is not None:
            self._checkpointer.delete_thread(thread_id)
        if self.run_history:
            self.run_history.delete_thread(thread_id)
