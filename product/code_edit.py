"""Code-edit workflow (Phase 11/14): asks the dedicated code_model for a full
replacement file, then applies it via the sandboxed, approval-gated
write_file MCP tool. No new approval mechanism — write_file already gates on
forge.require_approval_for (Phase 7); this just calls it.

apply_plan() (Phase 14) is the more precise path for an approved
ProposedPR's steps: it never asks the model for a diff (small local models
produce diffs with wrong line offsets constantly) or a whole file rewrite for
existing files. Instead it asks for just the replacement body of the target
symbol and splices it in at the start_line/end_line the index already
recorded, backing up and rolling back on failure, and produces the shown
diff itself via difflib.
"""
from __future__ import annotations
import ast
import difflib
import re
from typing import Any, Callable
from core.llm import LLMClient
from core.retriever import Retriever
from core.tools import Tool
from core.types import Message
from product.approval import Approve, run_tool

_SYSTEM_PROMPT = (
    "You rewrite a single source file per an instruction. Output ONLY the "
    "complete new file content - no explanation, no markdown code fences, "
    "nothing else. If the file doesn't exist yet, write it from scratch."
)

_SYMBOL_SYSTEM_PROMPT = (
    "You rewrite a single function/method/class body per an instruction. "
    "You are given the full file for context and the current code of just "
    "the symbol to rewrite. Output ONLY the complete replacement code for "
    "that symbol - same signature/declaration through its closing, matching "
    "the original indentation - no markdown fences, no explanation, nothing else."
)

# Small local models don't reliably follow "no fences, no commentary" — they
# sometimes prepend an explanation before a fenced block. Extract the fenced
# block's contents wherever it appears rather than only stripping fences at
# the exact string boundaries.
_FENCE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)\n```", re.DOTALL)

def _clean(text: str) -> str:
    text = text.strip()
    match = _FENCE_BLOCK_RE.search(text)
    return match.group(1) if match else text

def propose_edit(code_model: LLMClient, path: str, current_content: str, instruction: str) -> str:
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=(
            f"File: {path}\n\nCurrent content:\n{current_content or '(new file, currently empty)'}"
            f"\n\nInstruction: {instruction}\n\nNew file content:"
        )),
    ]
    return _clean(code_model.generate(messages))

def edit_file(
    code_model: LLMClient, read_tool: Tool, write_tool: Tool,
    path: str, instruction: str, approve: Approve,
) -> str:
    try:
        current = read_tool.run(path=path)
    except RuntimeError:
        current = ""
    new_content = propose_edit(code_model, path, current, instruction)
    return run_tool(write_tool, {"path": path, "content": new_content}, approve)

# --- precise, splice-based editing for an approved plan (Phase 14) ---------

class EditFailure(Exception):
    """Raised when applying a plan step fails. Callers should treat any
    already-backed-up files as needing rollback (apply_plan does this itself)."""

def _propose_symbol_rewrite(
    code_model: LLMClient, path: str, full_file: str, symbol: str, current_body: str, instruction: str,
) -> str:
    messages = [
        Message(role="system", content=_SYMBOL_SYSTEM_PROMPT),
        Message(role="user", content=(
            f"File: {path}\n\nFull file (context only, do not repeat it):\n{full_file}\n\n"
            f"Current code of {symbol!r} to rewrite:\n{current_body}\n\n"
            f"Instruction: {instruction}\n\nNew code for {symbol!r}:"
        )),
    ]
    return _clean(code_model.generate(messages))

def _locate_symbol(retriever: Retriever, target_path: str, description: str):
    """Find the indexed chunk (with start_line/end_line) this step most
    likely targets, scoped to the step's file via metadata filtering — same
    Retriever port as everywhere else, just a filtered call."""
    hits = retriever.search(description, k=1, path=target_path)
    if not hits:
        return None
    meta = hits[0].metadata
    if "start_line" not in meta or "end_line" not in meta:
        return None
    return hits[0]

def _splice(original: str, start_line: int, end_line: int, new_body: str) -> str:
    lines = original.splitlines(keepends=True)
    new_lines = new_body.splitlines(keepends=True)
    if new_lines and not new_lines[-1].endswith("\n") and end_line <= len(lines):
        new_lines[-1] += "\n"
    return "".join(lines[:start_line - 1] + new_lines + lines[end_line:])

def _validate_python(path: str, content: str) -> None:
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as e:
            raise EditFailure(f"generated code for {path} does not parse: {e}") from e

def apply_plan(
    code_model: LLMClient, retriever: Retriever, read_tool: Tool, write_tool: Tool, plan: Any,
    backups: dict[str, str] | None = None,
    attempt: int = 1,
    test_feedback: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Apply an already-approved ProposedPR to the workspace, one step at a
    time.

    On the first attempt, "edit" steps rewrite only the target symbol's body
    (located via the index's own start_line/end_line) and splice it into the
    original file; "create" steps get a normal whole-file write. On a retry
    (attempt > 1, after a failed test run), every step falls back to a
    whole-file rewrite with the test failure appended to its instruction
    instead: the index's start_line/end_line reflects the *original* file,
    which the first attempt already changed, so those line numbers are stale
    and re-splicing against them would be wrong.

    `backups` should be threaded through across retries by the caller (e.g.
    graph state) rather than left as None each time, so it always reflects
    the *pre-any-edit* content — re-passing None on a retry would silently
    back up the first attempt's already-modified file instead of the
    original, and a later rollback would only undo the last attempt rather
    than all of them. Every touched file is backed up before its first
    write across the whole retry sequence; any failure inside this call
    rolls back everything backed up so far *within this call* and raises
    EditFailure — the caller is responsible for rolling back across a whole
    exhausted retry sequence using the returned `backups`.

    Returns {"results": [...], "diff": "<unified diff>", "backups": {...}}
    — the diff is generated here with difflib, never by the model.
    """
    backups = {} if backups is None else backups
    results: list[dict] = []
    diff_parts: list[str] = []
    this_call_backups: set[str] = set()
    notify = on_progress or (lambda *_: None)
    total_steps = len(plan.steps)

    def _rollback_this_call() -> None:
        for path in this_call_backups:
            run_tool(write_tool, {"path": path, "content": backups[path]}, approve=lambda *_: True)

    try:
        for i, step in enumerate(plan.steps, start=1):
            notify(f"Editing file {i}/{total_steps} (attempt {attempt}/3): {step.target_path}")
            if step.kind == "delete":
                results.append({"target_path": step.target_path, "kind": step.kind, "status": "unsupported", "detail": "no delete tool yet"})
                continue
            if step.kind == "run_tests":
                results.append({"target_path": step.target_path, "kind": step.kind, "status": "skipped", "detail": "handled by the graph's own test node"})
                continue

            try:
                original = read_tool.run(path=step.target_path)
            except RuntimeError:
                original = ""
            if step.target_path not in backups:
                backups[step.target_path] = original
                this_call_backups.add(step.target_path)

            instruction = step.description
            if test_feedback:
                instruction += (
                    f"\n\nA previous attempt at this failed its test suite. Fix the "
                    f"code so the tests pass. Test output:\n{test_feedback}"
                )

            if attempt > 1:
                # Line numbers from the index are stale after the first
                # attempt's edit; rewrite the whole file instead of re-splicing.
                new_content = propose_edit(code_model, step.target_path, original, instruction)
            elif step.kind == "create" or not original:
                new_content = propose_edit(code_model, step.target_path, original, instruction)
            else:
                located = _locate_symbol(retriever, step.target_path, step.description)
                if located is not None:
                    start_line, end_line = located.metadata["start_line"], located.metadata["end_line"]
                    symbol = located.metadata.get("symbol", "?")
                    current_body = "\n".join(original.splitlines()[start_line - 1:end_line])
                    new_body = _propose_symbol_rewrite(code_model, step.target_path, original, symbol, current_body, instruction)
                    new_content = _splice(original, start_line, end_line, new_body)
                else:
                    # No indexed symbol found for this file — fall back to a
                    # whole-file rewrite rather than guessing at line numbers.
                    new_content = propose_edit(code_model, step.target_path, original, instruction)

            _validate_python(step.target_path, new_content)
            run_tool(write_tool, {"path": step.target_path, "content": new_content}, approve=lambda *_: True)

            diff_parts.append("".join(difflib.unified_diff(
                backups[step.target_path].splitlines(keepends=True), new_content.splitlines(keepends=True),
                fromfile=f"a/{step.target_path}", tofile=f"b/{step.target_path}",
            )))
            results.append({"target_path": step.target_path, "kind": step.kind, "status": "ok"})
    except Exception as e:
        _rollback_this_call()
        raise EditFailure(f"applying plan (attempt {attempt}) failed, rolled back {len(this_call_backups)} file(s): {e}") from e

    return {"results": results, "diff": "".join(diff_parts), "backups": backups}

def commit_changes(commit_tool: Tool, title: str, body: str) -> str:
    """Commit the workspace after a successful edit+test cycle. Still goes
    through run_tool (so the approval mechanism structurally still applies
    to every write) with an always-approve callback — the plan as a whole
    was already approved before the edit node ran; this isn't a second,
    separate human decision point."""
    message = f"{title}\n\n{body}" if body else title
    return run_tool(commit_tool, {"message": message}, approve=lambda *_: True)

def push_changes(push_tool: Tool, branch: str) -> str:
    """Push the workspace branch after a successful commit. Same rationale as
    commit_changes: the plan (which this push is part of executing) was
    already approved; not a second decision point of its own."""
    return run_tool(push_tool, {"remote": "origin", "branch": branch}, approve=lambda *_: True)

def open_pr_changes(open_pr_tool: Tool, title: str, body: str, base: str, head: str) -> str:
    """Open the PR. Unlike commit/push above, opening a PR *does* get its own
    separate human decision (Phase 17: pr_approval_gate_node, gated by a
    PlanApprove-style callback the graph calls before this function is ever
    reached) — by the time this runs that decision is already made, so this
    goes through run_tool the same always-approve way, for the same reason."""
    return run_tool(open_pr_tool, {"title": title, "body": body, "base": base, "head": head}, approve=lambda *_: True)

def rollback_files(write_tool: Tool, backups: dict[str, str]) -> list[str]:
    """Restore every backed-up file to its pre-edit content. Used when the
    edit/test retry budget is exhausted."""
    for path, original in backups.items():
        run_tool(write_tool, {"path": path, "content": original}, approve=lambda *_: True)
    return sorted(backups)
