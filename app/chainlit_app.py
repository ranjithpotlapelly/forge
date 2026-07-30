"""Chainlit chat interface (Phase 8/16/17/18/21): the user-facing surface for
Forge's Q&A engine, its plan/edit/test/commit/push/open_pr task flow --
previously reachable only by calling LangGraphEngine.run_task() from a
terminal with a hardcoded approve lambda -- fetch_issue (Phase 18): "fix
#42" used to require pasting the issue text in by hand -- and conversation
history (Phase 21): thread_id used to be a per-browser-session UUID with no
listing or resume UI, even though the checkpointer underneath already
supported it. /history lists past threads (core.run_history.RunHistory --
the same audit trail every graph node already writes to, not a second
store) with Resume/Delete buttons; resuming a Q&A thread replays its
transcript and rebinds this session to it, resuming a task thread continues
it from its checkpoint via LangGraphEngine.resume_task() -- including one
still paused mid-approval, exactly like `python -m app.resume` but from the
browser.

Streams answers token-by-token (LLMClient.stream(), Phase 16) instead of
Phase 8's blocking cl.Message(answer.text).send() -- on CPU a full answer
takes 20-60s, long enough that a non-streamed reply reads as a frozen app.
Q&A bypasses core.engine.Engine on purpose: Engine.ask() only returns a
finished Answer and always runs classify_intent (routing to the task-path
graph). This calls directly into the same building blocks Engine.ask()'s Q&A
branch already uses (Retriever.search, adapters.engine_langgraph.has_context/
decline/build_answer_messages, LLMClient.stream) instead of duplicating them.

The task flow (Phase 17) *does* go through the real engine
(LangGraphEngine.run_task(), via _engine below) -- unlike Q&A, that graph's
plan/edit/test/retry/commit/push/PR machinery is too substantial to
reimplement here, and the whole point is to reuse it unchanged. What's new is
*who answers the graph's approval callbacks*: product/approval.py's
PlanDecision/PlanApprove contract is unchanged, and product/approval.py's
run_tool/Approve/ApprovalDenied (the tool-level gate under write_file/commit/
push/open_pr) are untouched too -- this file only supplies a concrete
PlanApprove (and its PR-approval counterpart) backed by Chainlit action
buttons instead of the terminal-print + lambda every other caller still uses.

Run with:  chainlit run app/chainlit_app.py -w
"""
from __future__ import annotations
import asyncio
import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Iterable

import chainlit as cl

from adapters.engine_langgraph import build_answer_messages, classify_intent, decline, has_context
from app.config_loader import load_config
from app.index import run_incremental_index, run_index
from app.wiring import (
    build_answer_model, build_engine, build_fetch_issue_tool, build_llm,
    build_retriever, build_run_history, build_tracing,
)
from core.types import Chunk
from product.approval import PlanDecision
from product.citations import read_lines

_cfg = load_config()
_tracing = build_tracing(_cfg)
_tracing.start()
_llm = build_llm(_cfg)
# Phase 23 (perf tuning): the Q&A answer path below uses a separate, faster
# model than _llm (which stays on config.yaml's llm.model -- qwen3:8b here --
# for classify_intent and the task-flow graph). See config.yaml's
# answer_model section and adapters/llm_ollama.py's num_ctx/keep_alive
# handling for why this alone cuts answer latency independent of context size.
_answer_llm = build_answer_model(_cfg)
_retriever = build_retriever(_cfg)
# The single source of truth for conversation history (Phase 21) -- not
# core.store.StateStore, which Phase 16 originally used for a parallel
# "chat:{thread_id}:{ts}" log. That log had no reader anywhere and would
# have become a second, divergent copy of the same data once run_history
# grew rich enough to reconstruct a transcript on its own; removed rather
# than kept in sync by hand.
_run_history = build_run_history(_cfg)
_engine = build_engine(_cfg, llm=_llm, retriever=_retriever)
_fetch_issue_tool = build_fetch_issue_tool(_cfg)
_repo_path = _cfg["forge"]["repo_path"]
# Phase 23 (perf tuning): the Q&A path retrieves fewer, higher-ranked chunks
# than the general top_k (faster prefill) and expands only the best few to
# full source in the prompt -- see build_answer_messages's max_expanded param.
_answer_top_k = _cfg["retriever"].get("answer_top_k", 5)
_answer_max_expanded = _cfg["retriever"].get("answer_max_expanded", 4)
_ISSUE_REF_RE = re.compile(r"#(\d+)")

_LANGUAGE_BY_EXTENSION = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go",
}

_HELP_TEXT = (
    "**Available commands**\n"
    "- `/index <path>` — index a repo (structure-aware chunking + embedding) so it becomes askable\n"
    "- `/index <path> --changed` — reindex only files changed since HEAD (fast, before a commit)\n"
    "- `/index --clear` — wipe the entire index (asks for confirmation first)\n"
    "- `/history` — list past conversations, with buttons to resume or delete each one\n"
    "- `/help` — show this message\n\n"
    "Anything else is classified as either a question about the indexed codebase "
    "(answered directly) or a task (\"fix ...\", \"add ...\", \"refactor ...\") that "
    "proposes a plan for your approval before touching any files.\n\n"
    "Mention `#<number>` (e.g. \"fix #42\") and I'll offer to fetch that GitHub "
    "issue's title, body, labels, and comments and use them as context."
)

def _language_for(path: str) -> str | None:
    for ext, language in _LANGUAGE_BY_EXTENSION.items():
        if path.endswith(ext):
            return language
    return None

# --- Q&A (Phase 16) ----------------------------------------------------------

def _citation_elements(chunks: list[Chunk]) -> list[cl.Text]:
    """One expandable element per unique cited location: collapsed shows
    path:start-end (the element's name -- Chainlit renders it as a small
    reference chip); expanded shows the real source, read fresh off disk
    (product.citations.read_lines) rather than the indexed chunk copy."""
    elements: list[cl.Text] = []
    seen: set[tuple[str, int, int]] = set()
    for c in chunks:
        path = c.metadata.get("path", "?")
        start = c.metadata.get("start_line", 1)
        end = c.metadata.get("end_line", start)
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        try:
            source = read_lines(_repo_path, path, start, end)
        except (OSError, ValueError) as e:
            source = f"(could not read {path}:{start}-{end}: {e})"
        # cl.Text's Element base rejects empty content (a blank chunk is a
        # valid-but-unusual read result, not an error worth failing over).
        source = source or "(empty)"
        elements.append(cl.Text(name=f"{path}:{start}-{end}", content=source, language=_language_for(path), display="side"))
    return elements

async def _stream_tokens(
    tokens: Iterable[str], msg: cl.Message, on_first_token: Callable[[], None] | None = None,
) -> str:
    """Bridges LLMClient.stream()'s blocking generator (it calls Ollama
    synchronously) onto Chainlit's async stream_token, one token at a time --
    asyncio.to_thread(list, tokens) would also get it off the event loop, but
    only returns once the whole generator is exhausted, which defeats the
    point of streaming to the UI as tokens actually arrive.

    on_first_token (Phase 23, perf tuning), if given, fires once when the
    first real token is dequeued -- lets the caller measure time-to-first-
    token without threading its own clock through the producer thread."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _produce() -> None:
        try:
            for token in tokens:
                loop.call_soon_threadsafe(queue.put_nowait, token)
        except Exception as e:  # noqa: BLE001 -- forwarded to the consumer below, not swallowed
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_produce, daemon=True).start()

    parts: list[str] = []
    first = True
    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, BaseException):
            raise item
        if first:
            first = False
            if on_first_token is not None:
                on_first_token()
        parts.append(item)
        await msg.stream_token(item)
    return "".join(parts)

async def _handle_question(question: str, thread_id: str) -> None:
    # This path bypasses LangGraphEngine.ask() by design (Phase 16 -- see
    # module docstring), so it never gets the automatic per-node run_history
    # recording adapters/engine_langgraph.py's _instrumented() gives every
    # graph node. Recorded explicitly here instead, using the same node
    # names (retrieve/decline/answer) the graph would use for the same steps.
    run_id = _run_history.start_run(thread_id, "qa", question)
    t0 = time.monotonic()
    async with cl.Step(name="Retrieving...", type="retrieval") as step:
        step.input = question
        chunks = await asyncio.to_thread(_retriever.search, question, _answer_top_k)
        step.output = f"{len(chunks)} chunk(s) found"
    retrieve_s = time.monotonic() - t0
    _run_history.record_step(run_id, "retrieve", "ok", json.dumps({"chunks": len(chunks)}), retrieve_s * 1000)

    if has_context({"chunks": chunks}) != "answer":
        canned = decline({})["answer"]
        # detail carries the actual text, not just an outcome flag -- this is
        # what lets /history's "resume" replay a full Q&A transcript straight
        # from run_history, with no second, parallel copy of the same data.
        _run_history.record_step(run_id, "decline", "ok", json.dumps({"answer": canned.text}), None)
        _run_history.finish_run(run_id, "completed")
        await cl.Message(content=canned.text).send()
        return

    messages = build_answer_messages(question, chunks, max_expanded=_answer_max_expanded)
    # Rough chars/4 heuristic, not a real tokenizer call -- good enough for a
    # one-line perf-tuning signal, not for anything that needs to be exact.
    prompt_tokens_est = max(1, sum(len(m.content) for m in messages) // 4)
    msg = cl.Message(content="", elements=_citation_elements(chunks))
    t1 = time.monotonic()
    ttft_holder: list[float] = []

    def _mark_first_token() -> None:
        ttft_holder.append(time.monotonic() - t1)

    try:
        full_text = await _stream_tokens(
            _answer_llm.stream(
                messages,
                num_ctx=_cfg["llm"]["answer_num_ctx"],
                keep_alive=_cfg["llm"]["keep_alive"],
            ),
            msg,
            on_first_token=_mark_first_token,
        )
    except Exception as e:  # noqa: BLE001 -- surfaced to the user, not a silent truncation
        _run_history.record_step(run_id, "answer", "error", str(e), (time.monotonic() - t1) * 1000)
        _run_history.finish_run(run_id, "failed", str(e))
        await cl.Message(content=f"Something went wrong while generating the answer: {e}").send()
        return
    total_s = time.monotonic() - t1
    await msg.send()
    _run_history.record_step(
        run_id, "answer", "ok",
        json.dumps({"answer": full_text, "citations": len(chunks)}), total_s * 1000,
    )
    _run_history.finish_run(run_id, "completed")

    if _cfg["llm"].get("show_timing", True):
        ttft = ttft_holder[0] if ttft_holder else total_s
        completion_tokens_est = max(1, len(full_text) // 4)
        tok_s = completion_tokens_est / total_s if total_s > 0 else 0.0
        print(
            f"[timing] retrieve {retrieve_s:.1f}s | prompt ~{prompt_tokens_est} tok | "
            f"first-token {ttft:.1f}s | total {total_s:.1f}s | {tok_s:.1f} tok/s"
        )

# --- /index (Phase 16) --------------------------------------------------------

async def _handle_index(path: str, changed_only: bool = False) -> None:
    verb = "Reindexing changed files in" if changed_only else "Indexing"
    progress = cl.Message(content=f"{verb} `{path}`…")
    await progress.send()
    loop = asyncio.get_running_loop()

    def on_progress(symbols: int, files: int) -> None:
        progress.content = f"{verb} `{path}`… {symbols} symbol(s) across {files} file(s) so far"
        asyncio.run_coroutine_threadsafe(progress.update(), loop)

    fn = run_incremental_index if changed_only else run_index
    try:
        stats = await asyncio.to_thread(fn, path, _cfg, _retriever, on_progress)
    except Exception as e:  # noqa: BLE001 -- e.g. path doesn't exist; shown to the user, not swallowed
        progress.content = f"{verb} `{path}` failed: {e}"
        await progress.update()
        return

    if changed_only:
        progress.content = (
            f"Reindexed **{stats['symbols']}** symbol(s) across **{stats['files']}** changed file(s) "
            f"(**{stats['deleted']}** deleted) from `{path}`."
        )
    else:
        progress.content = f"Indexed **{stats['symbols']}** symbol(s) across **{stats['files']}** file(s) from `{path}`."
    await progress.update()

# --- fetch_issue (Phase 18) ---------------------------------------------------
# Forge can't read GitHub issues on its own -- "fix #42" only works today if
# the issue text is pasted in by hand. This closes that gap: detect a "#N"
# reference and offer (never silently) to fetch it and fold it into the
# message before classify_intent/task handling ever sees it.

async def _maybe_enrich_with_issue(text: str) -> str:
    match = _ISSUE_REF_RE.search(text)
    if not match:
        return text
    number = int(match.group(1))
    repo = _cfg["github"]["repo"]
    res = await cl.AskActionMessage(
        content=f"This mentions #{number} — fetch that issue from `{repo}` and use it as context?",
        actions=[
            cl.Action(name="fetch", payload={}, label="✅ Fetch issue"),
            cl.Action(name="skip", payload={}, label="Skip"),
        ],
        timeout=120,
    ).send()
    if not res or res["name"] != "fetch":
        return text

    try:
        issue = await asyncio.to_thread(_fetch_issue_tool.run, number=number)
    except Exception as e:  # noqa: BLE001 -- e.g. private repo with no token; shown, not swallowed
        await cl.Message(content=f"Could not fetch #{number}: {e}").send()
        return text

    await cl.Message(content=f"Fetched #{number}: **{issue['title']}**").send()
    enriched = f"{text}\n\n--- Issue #{issue['number']}: {issue['title']} ---\n{issue['body']}"
    if issue["labels"]:
        enriched += f"\nLabels: {', '.join(issue['labels'])}"
    for comment in issue["comments"]:
        enriched += f"\n\n{comment['author']} commented:\n{comment['body']}"
    return enriched

# --- task flow (Phase 17) -----------------------------------------------------

def _plan_card(plan: Any) -> str:
    steps = "\n".join(
        f"{i}. **[{step.kind}]** `{step.target_path}` — {step.description}"
        for i, step in enumerate(plan.steps, 1)
    )
    return (
        f"### Proposed change\n"
        f"**Title:** {plan.title}\n\n"
        f"**Branch:** `{plan.branch}`\n\n"
        f"**Steps:**\n{steps}\n\n"
        f"**Description:**\n{plan.body}"
    )

def _plan_actions() -> list[cl.Action]:
    # Fresh Action objects per prompt: reusing objects across AskActionMessage
    # calls would reuse their ids too, which is asking for trouble once two
    # of these have been shown in the same session.
    return [
        cl.Action(name="approve", payload={}, label="✅ Approve"),
        cl.Action(name="edit", payload={}, label="✏️ Edit"),
        cl.Action(name="reject", payload={}, label="❌ Reject"),
    ]

async def _ask_plan_decision(plan: Any) -> PlanDecision:
    res = await cl.AskActionMessage(content=_plan_card(plan), actions=_plan_actions(), timeout=1800).send()
    if res is None or res["name"] == "reject":
        return PlanDecision("reject")
    if res["name"] == "approve":
        return PlanDecision("approve")
    reply = await cl.AskUserMessage(content="What should change? Describe the correction.", timeout=600).send()
    feedback = ((reply or {}).get("output") or "").strip()
    return PlanDecision("edit", feedback=feedback or "(no feedback given — use your best judgement)")

async def _ask_pr_decision(diff: str, title: str, body: str) -> bool:
    content = (
        f"### Ready to open a pull request\n"
        f"**Title:** {title}\n\n**Body:**\n{body or '(none)'}\n\n"
        f"```diff\n{diff or '(no changes)'}\n```"
    )
    actions = [
        cl.Action(name="approve", payload={}, label="✅ Open PR"),
        cl.Action(name="reject", payload={}, label="❌ Don't open"),
    ]
    res = await cl.AskActionMessage(content=content, actions=actions, timeout=1800).send()
    return bool(res and res["name"] == "approve")

def _make_plan_approver(loop: asyncio.AbstractEventLoop) -> Callable[[Any], PlanDecision]:
    # Called from run_task()'s background thread (asyncio.to_thread, below) --
    # this is the sync/async bridge: schedule the real UI interaction onto
    # the session's own event loop and block this thread for the result,
    # exactly like index's on_progress bridge but returning a real answer
    # instead of firing and forgetting.
    def approve(plan: Any) -> PlanDecision:
        return asyncio.run_coroutine_threadsafe(_ask_plan_decision(plan), loop).result()
    return approve

def _make_pr_approver(loop: asyncio.AbstractEventLoop) -> Callable[[str, str, str], bool]:
    def approve_pr(diff: str, title: str, body: str) -> bool:
        return asyncio.run_coroutine_threadsafe(_ask_pr_decision(diff, title, body), loop).result()
    return approve_pr

async def _report_task_result(result: dict) -> None:
    if result.get("plan_decision") == "reject":
        await cl.Message(content=f"Plan rejected — nothing was written. ({result.get('rejection_reason', '')})").send()
        return

    diff = result.get("edit_diff")
    if diff:
        await cl.Message(content=f"### Diff\n```diff\n{diff}\n```").send()

    if result.get("gave_up"):
        rolled_back = result.get("rolled_back_paths", [])
        await cl.Message(content=(
            f"Gave up after {result.get('attempt', 1)} attempt(s) — tests never passed. "
            f"Rolled back: {', '.join(rolled_back) or '(none)'}"
        )).send()
        return

    lines = []
    commit_result = result.get("commit_result")
    if commit_result:
        lines.append(f"**Commit:** {commit_result.get('status')} — {commit_result.get('detail')}")
    push_result = result.get("push_result")
    if push_result:
        lines.append(f"**Push:** {push_result.get('status')} — {push_result.get('detail')}")
    if result.get("pr_approved") is False:
        lines.append(f"**PR:** not opened ({result.get('pr_rejection_reason', 'rejected')})")
    pr_result = result.get("pr_result")
    if pr_result:
        if pr_result.get("status") == "ok":
            lines.append(f"**PR opened:** {pr_result.get('url')}")
        else:
            lines.append(f"**PR failed:** {pr_result.get('detail')}")
    await cl.Message(content="\n\n".join(lines) or "Task finished.").send()

async def _handle_task(task: str, session_thread_id: str) -> None:
    loop = asyncio.get_running_loop()
    # A fresh thread_id per task run, not the chat session's: the graph is
    # checkpointed by thread_id, and reusing the session's id across multiple
    # unrelated tasks in one chat would resume/merge with earlier task state
    # instead of starting clean.
    task_thread_id = f"{session_thread_id}-task-{uuid.uuid4()}"

    progress = cl.Message(content=f"**Task:** {task}")
    await progress.send()

    def on_progress(text: str) -> None:
        progress.content += f"\n\n{text}"
        asyncio.run_coroutine_threadsafe(progress.update(), loop)

    try:
        result = await asyncio.to_thread(
            _engine.run_task, task, task_thread_id,
            _make_plan_approver(loop), _make_pr_approver(loop), on_progress,
        )
    except Exception as e:  # noqa: BLE001 -- surfaced to the user, not a silent stop
        await cl.Message(content=f"Task failed: {e}").send()
        return

    await _report_task_result(result)

# --- conversation history (Phase 21) ------------------------------------------
# Everything here reads core.run_history.RunHistory -- the same table every
# graph node and the Q&A path above already write to -- and nothing else;
# there is no separate "thread list" store to keep in sync.

def _truncate(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"

def _short_ts(ts: str | None) -> str:
    return (ts or "")[:19].replace("T", " ")

def _thread_actions(thread_id: str) -> list[cl.Action]:
    return [
        cl.Action(name="resume_thread", payload={"thread_id": thread_id}, label="▶ Resume"),
        cl.Action(name="delete_thread", payload={"thread_id": thread_id}, label="🗑 Delete"),
    ]

async def _handle_history() -> None:
    threads = _run_history.list_threads(limit=20)
    if not threads:
        await cl.Message(content="No past conversations yet.").send()
        return
    await cl.Message(content=f"**{len(threads)} past conversation(s)** (most recently active first):").send()
    for t in threads:
        icon = {"qa": "💬", "task": "🛠"}.get(t["kind"], "•")
        content = (
            f"{icon} **{_truncate(t['title'])}**\n"
            f"`{t['thread_id']}` — {t['kind']}, {t['run_count']} run(s), "
            f"status={t['status']}, last active {_short_ts(t['last_active_at'])}"
        )
        await cl.Message(content=content, actions=_thread_actions(t["thread_id"])).send()

async def _replay_qa_transcript(thread_id: str) -> None:
    """Reconstructs a Q&A conversation purely from run_history: each run's
    task_text is the question; its "answer"/"decline" step's detail (stored
    as JSON including the real text, not just an outcome flag) is the reply."""
    runs = _run_history.list_runs_for_thread(thread_id)
    for run in runs:
        await cl.Message(content=run["task_text"], author="You", type="user_message").send()
        steps = _run_history.list_steps(run["id"])
        reply_step = next((s for s in steps if s["node"] in ("answer", "decline")), None)
        text = "(no answer recorded for this turn)"
        if reply_step and reply_step["detail"]:
            try:
                text = json.loads(reply_step["detail"]).get("answer", text)
            except (TypeError, ValueError):
                pass
        await cl.Message(content=text).send()

async def _show_task_summary(run: dict) -> None:
    """A finished (completed/failed) task thread can't be resumed -- its
    checkpoint has already reached END -- so this reconstructs what happened
    from run_history's steps instead, the same audit trail `python -m
    app.history <id>` reads."""
    lines = [f"**Task:** {run['task_text']}", f"**Status:** {run['status']}"]
    if run.get("error"):
        lines.append(f"**Error:** {run['error']}")
    for s in _run_history.list_steps(run["id"]):
        if s["node"] in ("approval_gate", "pr_gate") and s["detail"]:
            try:
                decision = json.loads(s["detail"]).get("decision")
                lines.append(f"- {s['node']}: {decision}")
            except (TypeError, ValueError):
                pass
        elif s["node"] in ("commit", "push", "open_pr", "give_up"):
            lines.append(f"- {s['node']}: {s['status']}")
    await cl.Message(content="\n".join(lines)).send()

async def _resume_thread(thread_id: str) -> None:
    threads = {t["thread_id"]: t for t in _run_history.list_threads(limit=200)}
    thread = threads.get(thread_id)
    if thread is None:
        await cl.Message(content=f"No conversation found for `{thread_id}`.").send()
        return

    if thread["kind"] == "qa":
        await _replay_qa_transcript(thread_id)
        # Rebind this session to the resumed thread so new questions keep
        # appending to the same conversation, not a fresh one.
        cl.user_session.set("thread_id", thread_id)
        await cl.Message(content="_(resumed -- continue asking questions in this conversation)_").send()
        return

    # kind == "task"
    run = _run_history.get_run(thread["latest_run_id"])
    if thread["status"] != "running":
        await _show_task_summary(run)
        return

    loop = asyncio.get_running_loop()
    progress = cl.Message(content=f"**Resuming task:** {run['task_text']}")
    await progress.send()

    def on_progress(text: str) -> None:
        progress.content += f"\n\n{text}"
        asyncio.run_coroutine_threadsafe(progress.update(), loop)

    try:
        result = await asyncio.to_thread(
            _engine.resume_task, thread_id,
            _make_plan_approver(loop), _make_pr_approver(loop), on_progress,
        )
    except Exception as e:  # noqa: BLE001 -- e.g. the checkpoint is gone; shown, not swallowed
        await cl.Message(content=f"Could not resume: {e}").send()
        return
    await _report_task_result(result)

@cl.action_callback("resume_thread")
async def on_resume_thread(action: cl.Action) -> None:
    await _resume_thread(action.payload["thread_id"])

@cl.action_callback("delete_thread")
async def on_delete_thread(action: cl.Action) -> None:
    thread_id = action.payload["thread_id"]
    res = await cl.AskActionMessage(
        content=f"Delete conversation `{thread_id}`? This removes its checkpoints and history permanently.",
        actions=[
            cl.Action(name="confirm", payload={}, label="🗑 Yes, delete"),
            cl.Action(name="cancel", payload={}, label="Cancel"),
        ],
        timeout=60,
    ).send()
    if not res or res["name"] != "confirm":
        return
    _engine.delete_thread(thread_id)
    await cl.Message(content=f"Deleted `{thread_id}`.").send()

@cl.on_chat_start
async def on_chat_start():
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    await cl.Message(
        content="Ask me anything about the indexed codebase, or describe a change "
        "(\"fix ...\", \"add ...\") and I'll propose a plan for your approval. "
        "Type `/help` for commands, or `/history` to resume a past conversation."
    ).send()

@cl.on_message
async def on_message(message: cl.Message):
    thread_id = cl.user_session.get("thread_id")
    text = message.content.strip()
    lowered = text.lower()

    if lowered in ("/help", "/?"):
        await cl.Message(content=_HELP_TEXT).send()
        return

    if lowered.startswith("/index"):
        arg = text[len("/index"):].strip()
        if arg == "--clear":
            res = await cl.AskActionMessage(
                content="Clear the **entire** code index? Q&A will have no context until you `/index` again.",
                actions=[
                    cl.Action(name="confirm", payload={}, label="🗑 Yes, clear index"),
                    cl.Action(name="cancel", payload={}, label="Cancel"),
                ],
                timeout=60,
            ).send()
            if res and res["name"] == "confirm":
                await asyncio.to_thread(_retriever.clear)
                await cl.Message(content="Index cleared.").send()
            return
        changed_only = arg.endswith("--changed")
        if changed_only:
            arg = arg[: -len("--changed")].strip()
        if not arg:
            await cl.Message(content="Usage: `/index C:\\path\\to\\repo` (add `--changed` to only reindex files changed since HEAD)").send()
            return
        await _handle_index(arg, changed_only=changed_only)
        return

    if lowered == "/history":
        await _handle_history()
        return

    text = await _maybe_enrich_with_issue(text)

    intent = classify_intent({"question": text}, llm=_llm).get("intent", "question")
    if intent == "task":
        await _handle_task(text, thread_id)
        return

    await _handle_question(text, thread_id)
