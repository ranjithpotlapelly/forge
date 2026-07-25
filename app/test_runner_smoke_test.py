"""Acceptance check for the run_tests tool (Phase 13): creates a temp pytest
project in .data/workspace/, first with a passing test, then a failing one,
and runs the tool against both.

Requires pytest installed in this venv (`pip install pytest`) — it's the
target project's own tooling, not a Forge dependency, so it's not in
requirements.txt. Run from the repo root:
  python -m app.test_runner_smoke_test
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_tools

_PASSING_TEST = "def test_addition():\n    assert 1 + 1 == 2\n"
_FAILING_TEST = "def test_addition():\n    assert 1 + 1 == 3  # deliberately wrong\n"

def main() -> int:
    cfg = load_config()
    workspace = Path(cfg["tools"]["workspace"])
    marker = workspace / "pyproject.toml"
    test_file = workspace / "test_sample.py"

    # Idempotency: leftovers from a previous run.
    marker.unlink(missing_ok=True)
    test_file.unlink(missing_ok=True)

    tools = {t.name: t for t in build_tools(cfg)}
    run_tests = tools["run_tests"]
    print(f"[ok] loaded tool: name={run_tests.name!r} requires_approval={run_tests.requires_approval}")
    if run_tests.requires_approval:
        print("[!!] expected run_tests.requires_approval=False")
        return 1

    marker.write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    test_file.write_text(_PASSING_TEST, encoding="utf-8")
    result = json.loads(run_tests.run())
    print(f"\n--- passing test ---\n{json.dumps(result, indent=2)}")
    if not result["passed"] or result["exit_code"] != 0:
        print("[!!] expected passed=True, exit_code=0")
        return 1
    print("[ok] passing test correctly reported as passed")

    test_file.write_text(_FAILING_TEST, encoding="utf-8")
    result = json.loads(run_tests.run())
    print(f"\n--- failing test ---\n{json.dumps(result, indent=2)}")
    if result["passed"] or result["exit_code"] == 0:
        print("[!!] expected passed=False, non-zero exit_code")
        return 1
    print("[ok] failing test correctly reported as failed")

    test_file.unlink()
    marker.unlink()

    print("\nrun_tests tool OK: auto-detects pytest, reports pass/fail correctly, requires_approval=False.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
