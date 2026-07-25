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
"""
from __future__ import annotations
import json
import re
import sqlite3
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, TypedDict
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END
from langgraph.types import RunnableConfig
from opentelemetry import trace
from core.llm import LLMClient
from core.retriever import Retriever
from core.tools import Tool
from core.types import Answer, Chunk, Message

_tracer = trace.get_tracer(__name__)

MIN_RELEVANCE = 0.2  # below this, treat retrieval as "no relevant context"

def _emit(config: RunnableConfig, text: str) -> None:
    """Best-effort progress notification -- on_progress is optional in
    configurable (CLI/test callers rarely supply one), so this never raises
    just because nobody's listening."""
    config["configurable"].get("on_progress", lambda *_: None)(text)

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

def build_answer_messages(question: str, chunks: list[Chunk]) -> list[Message]:
    """The Q&A prompt: shared by the `answer` node (llm.generate, below) and
    the Chainlit UI's streaming path (llm.stream) so both ask the model the
    exact same question the exact same way -- one place defines it."""
    context = "\n\n".join(
        f"[{c.metadata.get('path', '?')}:{c.metadata.get('start_line', '?')}]\n{c.content}"
        for c in chunks
    )
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

def approval_gate_node(state: GraphState, config: RunnableConfig) -> GraphState:
    plan = state["plan"]
    print("\n=== Proposed plan ===")
    print(f"Title:  {plan.title}")
    print(f"Branch: {plan.branch}")
    print("Steps:")
    for i, step in enumerate(plan.steps, 1):
        print(f"  {i}. [{step.kind}] {step.target_path} - {step.description}")
    print()
    approve = config["configurable"]["approve"]
    decision = approve(plan)  # duck-typed PlanDecision: .decision in {"approve","edit","reject"}, .feedback
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

def pr_approval_gate_node(state: GraphState, config: RunnableConfig) -> GraphState:
    plan = state["plan"]
    diff = state.get("edit_diff", "")
    print("\n=== Ready to open a pull request ===")
    print(f"Title:  {plan.title}")
    print(f"Branch: {state.get('workspace_branch')}")
    print(f"Diff:\n{diff}\n")
    approve_pr = config["configurable"]["approve_pr"]
    if not approve_pr(diff, plan.title, plan.body):
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
        g.add_node("classify_intent", partial(classify_intent, llm=self.llm))
        g.add_node("retrieve", partial(retrieve, retriever=self.retriever, k=self.k))
        g.add_node("answer", partial(answer, llm=self.llm))
        g.add_node("decline", decline)
        g.add_node("prepare_workspace", partial(
            prepare_workspace_node, retriever=self.retriever,
            repo_path=self.repo_path, prepare_tool=self.prepare_tool,
        ))
        g.add_node("task_retrieve", partial(
            task_retrieve, retriever=self.retriever, workspace=self.workspace, k=self.k,
        ))
        g.add_node("plan", partial(plan_node, plan_fn=self.plan_fn, llm=self.llm))
        g.add_node("approval_gate", approval_gate_node)
        g.add_node("rejected", rejected_node)
        g.add_node("edit", partial(
            edit_node, apply_plan_fn=self.apply_plan_fn, code_model=self.code_model,
            retriever=self.retriever, read_tool=self.read_tool, write_tool=self.write_tool,
        ))
        g.add_node("test", partial(test_node, run_tests_tool=self.run_tests_tool))
        g.add_node("bump_attempt", bump_attempt_node)
        g.add_node("commit", partial(commit_node, commit_fn=self.commit_fn, commit_tool=self.commit_tool))
        g.add_node("push", partial(push_node, push_fn=self.push_fn, push_tool=self.push_tool))
        g.add_node("push_failed", push_failed_node)
        g.add_node("pr_gate", pr_approval_gate_node)
        g.add_node("pr_rejected", pr_rejected_node)
        g.add_node("open_pr", partial(open_pr_node, open_pr_fn=self.open_pr_fn, open_pr_tool=self.open_pr_tool))
        g.add_node("give_up", partial(give_up_node, rollback_fn=self.rollback_fn, write_tool=self.write_tool))

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

    def ask(self, question: str, thread_id: str = "default") -> Answer:
        with _tracer.start_as_current_span("engine.ask") as span:
            span.set_attribute("engine.thread_id", thread_id)
            span.set_attribute("engine.question", question)
            config = {"configurable": {
                "thread_id": thread_id,
                # A plain question should never reach the task path's approval
                # gates, but the fallback is duck-typed like a real
                # PlanDecision (not a bare bool) so it wouldn't crash
                # approval_gate_node's decision.decision read if it somehow did.
                "approve": lambda *_: SimpleNamespace(decision="reject", feedback=None),
                "approve_pr": lambda *_: False,
            }}
            result = self._graph.invoke({"question": question}, config=config)
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
        """approve receives the ProposedPR and returns a duck-typed
        PlanDecision (product.approval.PlanDecision, or anything with the
        same .decision/.feedback shape). approve_pr receives (diff, title,
        body) for the second, PR-specific gate and returns a plain bool.
        Both default to a safe "never proceed without an explicit human
        decision" rather than silently running unattended."""
        with _tracer.start_as_current_span("engine.run_task") as span:
            span.set_attribute("engine.thread_id", thread_id)
            span.set_attribute("engine.task", task)
            config = {"configurable": {
                "thread_id": thread_id,
                "approve": approve or (lambda *_: SimpleNamespace(decision="reject", feedback=None)),
                "approve_pr": approve_pr or (lambda *_: False),
                "on_progress": on_progress or (lambda *_: None),
            }}
            result = self._graph.invoke({"question": task, "attempt": 1}, config=config)
            span.set_attribute("engine.approved", bool(result.get("approved")))
            span.set_attribute("engine.attempts", result.get("attempt", 1))
            return result
