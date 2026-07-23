"""Approval gate (Phase 7): decides whether a tool call must pause for human
sign-off before it runs, per forge.require_approval_for in config.yaml.

How approval is actually collected (CLI prompt, UI button, auto-approve in
tests) is the caller's business — this only enforces the gate.
"""
from __future__ import annotations
from typing import Any, Callable
from core.tools import Tool

Approve = Callable[[Tool, dict[str, Any]], bool]

class ApprovalDenied(Exception):
    """Raised when a human explicitly rejects a gated tool call."""

def run_tool(tool: Tool, kwargs: dict[str, Any], approve: Approve) -> Any:
    if tool.requires_approval and not approve(tool, kwargs):
        raise ApprovalDenied(f"{tool.name} was not approved to run with {kwargs!r}")
    return tool.run(**kwargs)
