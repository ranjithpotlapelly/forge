"""Adapter (Phase 13): implements core.tools.Tool directly against the GitHub
REST API, not as an MCP tool like write_file/commit/push.

Two reasons it stays out of the MCP server (adapters/mcp_servers/workspace_server.py):
1. It must show the diff and PR title/body to a human before the network
   call. Printing inside that server would write straight to the process's
   stdout, which doubles as the MCP JSON-RPC transport — that would corrupt
   the protocol stream, the same class of bug the server's own `_run_git`
   avoids by setting stdin=DEVNULL on its git subprocesses.
2. Opening a PR is a call to a third-party API, not a git/filesystem op
   scoped to the sandboxed workspace the MCP server owns.
"""
from __future__ import annotations
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import requests

_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$")

class OpenPrTool:
    name = "open_pr"
    description = (
        "Open a GitHub pull request from the workspace's current branch. "
        "Mutates a shared remote (creates a visible PR), so Forge gates this "
        "behind human approval."
    )

    def __init__(
        self,
        workspace: str,
        token: str,
        requires_approval: bool = True,
        api_base: str = "https://api.github.com",
        display: Callable[[str], None] = print,
        timeout: float = 30,
    ):
        self.requires_approval = requires_approval
        self._workspace = Path(workspace).resolve()
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._display = display
        self._timeout = timeout

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Pull request title"},
                "body": {"type": "string", "description": "Pull request body/description"},
                "base": {"type": "string", "description": "Base branch to merge into", "default": "main"},
                "head": {
                    "type": "string",
                    "description": "Branch with the changes (defaults to the workspace's current branch)",
                },
            },
            "required": ["title", "body"],
        }

    def run(self, title: str, body: str, base: str = "main", head: str | None = None) -> str:
        if not self._token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set — open_pr requires a token with pull-request "
                "write access to the target repo (see .env.template)."
            )

        head = head or self._current_branch()
        owner, repo = self._parse_origin()
        diff = self._diff(base, head)

        self._display(
            f"--- open_pr: {owner}/{repo}  {head} -> {base} ---\n"
            f"Title: {title}\n\n"
            f"Body:\n{body}\n\n"
            f"Diff:\n{diff or '(no changes)'}\n"
            f"--- end of diff ---"
        )

        try:
            response = requests.post(
                f"{self._api_base}/repos/{owner}/{repo}/pulls",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body, "head": head, "base": base},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"could not reach the GitHub API at {self._api_base}: {e}") from e

        return self._handle_response(response, owner, repo)

    def _handle_response(self, response: "requests.Response", owner: str, repo: str) -> str:
        if response.status_code == 201:
            return response.json()["html_url"]
        if response.status_code == 401:
            raise RuntimeError(
                "GitHub rejected the request (401 Unauthorized) — GITHUB_TOKEN is missing, "
                "invalid, or expired."
            )
        if response.status_code == 403:
            raise RuntimeError(
                f"GitHub denied permission (403 Forbidden) — GITHUB_TOKEN lacks access to "
                f"open a pull request on {owner}/{repo}."
            )
        try:
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise RuntimeError(f"GitHub API error {response.status_code}: {message}")

    def _run_git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self._workspace, capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _current_branch(self) -> str:
        return self._run_git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def _diff(self, base: str, head: str) -> str:
        try:
            return self._run_git("diff", f"{base}...{head}")
        except RuntimeError:
            # base may only exist on the remote (e.g. right after clone).
            return self._run_git("diff", f"origin/{base}...{head}")

    def _parse_origin(self) -> tuple[str, str]:
        url = self._run_git("remote", "get-url", "origin").strip()
        match = _GITHUB_REMOTE_RE.search(url)
        if not match:
            raise RuntimeError(f"origin remote {url!r} is not a github.com URL — open_pr only supports GitHub")
        return match.group(1), match.group(2)