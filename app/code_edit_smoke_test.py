"""Phase 11 smoke test: proves the code-edit tool actually generates a real
patch with the dedicated code_model and applies it only through the existing
approval-gated write_file tool.

Checks: (1) a denied edit never touches disk, (2) an approved edit writes
real, instruction-following content, (3) editing an existing file changes it
per a second instruction rather than starting over. Run from the repo root:
  python -m app.code_edit_smoke_test
"""
from __future__ import annotations
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_code_model, build_tools
from product.approval import ApprovalDenied
from product.code_edit import edit_file

TARGET = "code_edit_demo.py"

def main() -> int:
    cfg = load_config()

    leftover = Path(cfg["tools"]["workspace"]) / TARGET
    leftover.unlink(missing_ok=True)

    code_model = build_code_model(cfg)
    print(f"[ok] wired code_model (model={code_model.model})")

    tools = {t.name: t for t in build_tools(cfg)}
    read_tool, write_tool = tools["read_file"], tools["write_file"]

    instruction_1 = "Create a function greet(name) that returns f'Hello, {name}!'"
    try:
        edit_file(code_model, read_tool, write_tool, TARGET, instruction_1, approve=lambda t, a: False)
        print("[!!] expected ApprovalDenied when the gate denies")
        return 1
    except ApprovalDenied:
        print("[ok] denied approval -> edit_file did not write anything")

    try:
        read_tool.run(path=TARGET)
        print("[!!] file should not exist after a denied edit")
        return 1
    except RuntimeError:
        print("[ok] confirmed: the file was never created")

    result = edit_file(code_model, read_tool, write_tool, TARGET, instruction_1, approve=lambda t, a: True)
    print(f"[ok] approved -> {result}")

    content = read_tool.run(path=TARGET)
    if "def greet" not in content:
        print(f"[!!] expected 'def greet' in generated content, got:\n{content}")
        return 1
    print(f"[ok] generated content contains 'def greet':\n{'-'*40}\n{content.strip()}\n{'-'*40}")

    instruction_2 = "Add a one-line docstring to the greet function."
    edit_file(code_model, read_tool, write_tool, TARGET, instruction_2, approve=lambda t, a: True)
    updated = read_tool.run(path=TARGET)
    if "def greet" not in updated or '"""' not in updated and "'''" not in updated:
        print(f"[!!] expected greet() to survive the second edit with a docstring added, got:\n{updated}")
        return 1
    print(f"[ok] second edit updated the existing file (kept greet, added a docstring):\n{'-'*40}\n{updated.strip()}\n{'-'*40}")

    print("\nPhase 11 OK. Code-edit tool wired, gated by the existing approval flow.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
