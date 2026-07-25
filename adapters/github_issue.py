"""Adapter (Phase 18): implements core.tools.Tool as a read-only GitHub issue
fetcher via the REST API. Closes the gap WORKFLOW.md's task-verb heuristic
left open: classify_intent already recognizes "fix #42" as a task by its
issue reference, but Forge had no way to go read what #42 actually says --
the task text had to be pasted in by hand.

Same direct-HTTP-adapter shape as adapters/github_pr.py (requests, not an
SDK), but read-only: no diff to show, no approval gate, no git workspace
dependency -- repo owner/name come from config.yaml, not a git remote,
since fetching an issue doesn't require a local clone at all.
"""
from __future__ import annotations
from typing import Any

import requests

class FetchIssueTool:
    name = "fetch_issue"
    description = (
        "Fetch a GitHub issue's title, body, labels, and comments by number. "
        "Read-only -- never mutates anything, so it never requires approval."
    )
    requires_approval = False

    def __init__(self, repo: str, token: str, api_base: str = "https://api.github.com", timeout: float = 30):
        self._repo = repo
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue number, e.g. 42"},
            },
            "required": ["number"],
        }

    def run(self, number: int) -> dict:
        issue = self._get(f"/repos/{self._repo}/issues/{number}")
        comments: list[dict] = []
        if issue.get("comments", 0):
            comments = [
                {"author": c.get("user", {}).get("login", "?"), "body": c.get("body") or ""}
                for c in self._get(f"/repos/{self._repo}/issues/{number}/comments")
            ]
        return {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body") or "",
            "labels": [label.get("name") for label in issue.get("labels", [])],
            "comments": comments,
            "url": issue.get("html_url", ""),
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str) -> Any:
        try:
            response = requests.get(f"{self._api_base}{path}", headers=self._headers(), timeout=self._timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"could not reach the GitHub API at {self._api_base}: {e}") from e
        return self._parse(response, path)

    def _parse(self, response: "requests.Response", path: str) -> Any:
        if response.status_code == 200:
            return response.json()
        if response.status_code == 401:
            raise RuntimeError("GitHub rejected the request (401 Unauthorized) — GITHUB_TOKEN is invalid or expired.")
        if response.status_code == 404:
            # GitHub deliberately returns 404 (not 403) for a private repo/issue
            # the token can't see, to avoid confirming it exists at all -- so a
            # missing-token 404 on a real issue number usually means "private".
            hint = " (GITHUB_TOKEN is not set)" if not self._token else ""
            raise RuntimeError(
                f"{path} not found on {self._repo}{hint} — check the issue number, or if this "
                f"is a private repo, set GITHUB_TOKEN with access to it."
            )
        if response.status_code == 403:
            hint = "no GITHUB_TOKEN is set" if not self._token else "GITHUB_TOKEN may lack access or you're rate-limited"
            raise RuntimeError(f"GitHub denied access to {self._repo} (403 Forbidden) — {hint}.")
        try:
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise RuntimeError(f"GitHub API error {response.status_code}: {message}")
