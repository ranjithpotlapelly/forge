"""Phase 7 smoke test: proves MCP tools actually round-trip through a real
subprocess, and the approval gate actually blocks/allows correctly.

Checks: (1) tools load from the MCP server with the right requires_approval
flags, (2) a gated tool call is blocked when denied and the file is NOT
written, (3) it succeeds once approved, (4) an ungated tool runs without ever
invoking the approval callback, (5) the workspace sandbox rejects path
escapes. Run from the repo root:  python -m app.tools_smoke_test
"""
from __future__ import annotations
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_tools
from product.approval import ApprovalDenied, run_tool

def main() -> int:
    cfg = load_config()

    # Idempotency: previous runs leave smoke_test.txt in the workspace, which
    # would make the "denied write never happened" check below a false pass.
    leftover = Path(cfg["tools"]["workspace"]) / "smoke_test.txt"
    leftover.unlink(missing_ok=True)

    tools = build_tools(cfg)
    by_name = {t.name: t for t in tools}
    print(f"[ok] loaded {len(tools)} MCP tool(s): {sorted(by_name)}")

    write_file = by_name.get("write_file")
    read_file = by_name.get("read_file")
    if write_file is None or read_file is None:
        print("[!!] expected both write_file and read_file tools from the server")
        return 1

    if not write_file.requires_approval:
        print("[!!] write_file should require approval (config.yaml forge.require_approval_for)")
        return 1
    if read_file.requires_approval:
        print("[!!] read_file should NOT require approval")
        return 1
    print("[ok] write_file.requires_approval=True, read_file.requires_approval=False")

    args = {"path": "smoke_test.txt", "content": "hello from the approval gate"}

    try:
        run_tool(write_file, args, approve=lambda tool, kwargs: False)
        print("[!!] expected ApprovalDenied when the gate denies")
        return 1
    except ApprovalDenied:
        print("[ok] denied approval -> write_file did not run (ApprovalDenied raised)")

    try:
        read_tool_result = read_file.run(path=args["path"])
        print(f"[!!] file should not exist after a denied write, but read_file returned {read_tool_result!r}")
        return 1
    except RuntimeError:
        print("[ok] confirmed: the file was never written")

    approve_calls = []
    run_tool(write_file, args, approve=lambda tool, kwargs: (approve_calls.append((tool.name, kwargs)), True)[1])
    if approve_calls != [("write_file", args)]:
        print(f"[!!] approve() should have been called once with the tool call, got {approve_calls!r}")
        return 1
    print("[ok] approved -> write_file ran, approve() saw the right tool+args")

    def _fail_if_called(tool, kwargs):
        raise AssertionError("approve() must not be called for an ungated tool")

    content = run_tool(read_file, {"path": args["path"]}, approve=_fail_if_called)
    if content != args["content"]:
        print(f"[!!] round-trip mismatch: wrote {args['content']!r}, read {content!r}")
        return 1
    print(f"[ok] read_file (ungated) -> {content!r}, approve() was never invoked")

    try:
        write_file.run(path="../escape.txt", content="nope")
        print("[!!] expected the sandbox to reject a path that escapes the workspace")
        return 1
    except RuntimeError as e:
        print(f"[ok] sandbox rejected a path escape: {e}")

    print("\nPhase 7 (tools + approval gate) OK. PR flow (commit/push/open_pr) not yet wired.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
