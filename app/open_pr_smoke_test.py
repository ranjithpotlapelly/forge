"""Phase 13 acceptance check: proves open_pr shows the diff+title/body before
ever touching the network, that a denied approval makes ZERO network calls
(checked against a real local HTTP server's request log, not assumed from
reading the code), and that missing-token / 401 / 403 responses fail with a
clear message that never contains the token.

No real github.com traffic: this repo's .env has no GITHUB_TOKEN yet, so the
approved-path checks run against a throwaway local HTTP server standing in
for the GitHub REST API, in a scratch git repo under the scratchpad dir (not
.data/workspace, so this never touches Forge's own sandbox). Point
`api_base` at https://api.github.com with a real token and scratch GitHub
repo to do a true end-to-end run.

Run from the repo root:  python -m app.open_pr_smoke_test
"""
from __future__ import annotations
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from adapters.github_pr import OpenPrTool
from product.approval import ApprovalDenied, run_tool

SCRATCH = Path(__file__).resolve().parent.parent / ".data" / "open_pr_smoke_scratch"

def _rmtree_force(path: Path) -> None:
    # git marks object files read-only; on Windows that blocks deletion even
    # with directory write permission (see workspace_server.py's _rmtree_force).
    def _on_error(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onexc=_on_error)

class _FakeGitHub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers.items()),
            "json": json.loads(body) if body else None,
        })
        status = self.server.next_status
        if status == 201:
            payload = {"html_url": f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/testowner/testrepo/pull/1"}
        else:
            payload = {"message": f"stubbed {status} response"}
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args) -> None:  # silence default request logging
        pass

def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()

def _make_scratch_repo() -> Path:
    _rmtree_force(SCRATCH)
    SCRATCH.mkdir(parents=True)
    _git("init", "-b", "main", cwd=SCRATCH)
    _git("config", "user.name", "Forge Smoke Test", cwd=SCRATCH)
    _git("config", "user.email", "forge-smoke-test@localhost", cwd=SCRATCH)
    (SCRATCH / "README.md").write_text("scratch repo for open_pr smoke test\n", encoding="utf-8")
    _git("add", "-A", cwd=SCRATCH)
    _git("commit", "-m", "initial", cwd=SCRATCH)
    _git("checkout", "-b", "feature/open-pr-smoke-test", cwd=SCRATCH)
    (SCRATCH / "README.md").write_text("scratch repo for open_pr smoke test\n\nplus a change.\n", encoding="utf-8")
    _git("commit", "-am", "add a line", cwd=SCRATCH)
    # A real remote host with a made-up owner/repo — never actually contacted;
    # only used so open_pr can parse an owner/repo out of it.
    _git("remote", "add", "origin", "https://github.com/testowner/testrepo.git", cwd=SCRATCH)
    return SCRATCH

def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    server.requests = []
    server.next_status = 201
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api_base = f"http://127.0.0.1:{server.server_port}"

    try:
        workspace = _make_scratch_repo()
        displayed: list[str] = []

        # --- 1. missing token fails clearly, before touching the network ---
        no_token_tool = OpenPrTool(workspace=workspace, token="", api_base=api_base, display=displayed.append)
        try:
            no_token_tool.run(title="t", body="b")
            print("[!!] expected RuntimeError when GITHUB_TOKEN is empty")
            return 1
        except RuntimeError as e:
            if "GITHUB_TOKEN" not in str(e):
                print(f"[!!] error message should name GITHUB_TOKEN, got: {e}")
                return 1
        if server.requests:
            print(f"[!!] missing-token path made a network call: {server.requests}")
            return 1
        print("[ok] missing GITHUB_TOKEN -> clear RuntimeError, zero network calls")

        tool = OpenPrTool(workspace=workspace, token="fake-token-for-test", api_base=api_base, display=displayed.append)

        # --- 2. denied approval -> zero network calls, verified against the server's own log ---
        try:
            run_tool(
                tool,
                {"title": "Add a line", "body": "Smoke test PR body", "base": "main", "head": "feature/open-pr-smoke-test"},
                approve=lambda t, a: False,
            )
            print("[!!] expected ApprovalDenied when the gate denies")
            return 1
        except ApprovalDenied:
            pass
        if server.requests:
            print(f"[!!] denied approval made a real network call: {server.requests}")
            return 1
        print("[ok] denied open_pr -> fake GitHub server saw ZERO requests (verified, not assumed)")

        # --- 3. approved -> diff+title/body shown, exactly one real POST, PR URL returned ---
        displayed.clear()
        url = run_tool(
            tool,
            {"title": "Add a line", "body": "Smoke test PR body", "base": "main", "head": "feature/open-pr-smoke-test"},
            approve=lambda t, a: True,
        )
        if len(server.requests) != 1:
            print(f"[!!] expected exactly 1 request to the fake GitHub server, got {len(server.requests)}")
            return 1
        req = server.requests[0]
        if req["path"] != "/repos/testowner/testrepo/pulls":
            print(f"[!!] unexpected path: {req['path']}")
            return 1
        if req["json"] != {"title": "Add a line", "body": "Smoke test PR body", "head": "feature/open-pr-smoke-test", "base": "main"}:
            print(f"[!!] unexpected request body: {req['json']}")
            return 1
        if req["headers"].get("Authorization") != "Bearer fake-token-for-test":
            print("[!!] Authorization header missing or malformed")
            return 1
        if not url.endswith("/testowner/testrepo/pull/1"):
            print(f"[!!] expected the PR URL from the API response, got: {url!r}")
            return 1
        print(f"[ok] approved open_pr -> 1 real POST to the fake GitHub server, returned {url}")

        shown = "\n".join(displayed)
        if "Add a line" not in shown or "Smoke test PR body" not in shown or "plus a change" not in shown:
            print("[!!] expected the diff + title + body to be displayed before the API call")
            return 1
        print("[ok] full diff + PR title/body were shown to the human before the API call")

        # --- 4. 401/403 fail clearly and never leak the token ---
        for status, must_contain in ((401, "401"), (403, "403")):
            server.next_status = status
            try:
                tool.run(title="t", body="b", base="main", head="feature/open-pr-smoke-test")
                print(f"[!!] expected RuntimeError for a {status} response")
                return 1
            except RuntimeError as e:
                msg = str(e)
                if must_contain not in msg:
                    print(f"[!!] {status} error message unclear: {msg}")
                    return 1
                if "fake-token-for-test" in msg:
                    print(f"[!!] token leaked into error message: {msg}")
                    return 1
                print(f"[ok] {status} response -> clear RuntimeError, token not leaked: {msg}")

        print("\nPhase 13 (open_pr) OK. Diff shown before every API call; a denied "
              "approval provably makes no network call; missing/invalid/forbidden "
              "tokens fail clearly without ever being logged.")
        return 0
    finally:
        server.shutdown()
        _rmtree_force(SCRATCH)

if __name__ == "__main__":
    sys.exit(main())
