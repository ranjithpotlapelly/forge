"""Code-edit workflow (Phase 11): asks the dedicated code_model for a full
replacement file, then applies it via the sandboxed, approval-gated
write_file MCP tool. No new approval mechanism — write_file already gates on
forge.require_approval_for (Phase 7); this just calls it.
"""
from __future__ import annotations
import re
from core.llm import LLMClient
from core.tools import Tool
from core.types import Message
from product.approval import Approve, run_tool

_SYSTEM_PROMPT = (
    "You rewrite a single source file per an instruction. Output ONLY the "
    "complete new file content - no explanation, no markdown code fences, "
    "nothing else. If the file doesn't exist yet, write it from scratch."
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
