"""Phase 18 acceptance check: fetches a real issue from a real public repo
with no token (proving public repos work unauthenticated), confirms a
missing-token 404 fails with a clear message, and proves -- by capturing
everything written to stdout/stderr during a real request plus inspecting
every raised exception's text, not by reasoning about the code -- that a
GITHUB_TOKEN never appears in any log output, including on a real 401
failure from the live API.

Uses octocat/Hello-World, GitHub's own public sandbox repo that anyone can
file issues against: #10622 ("test issue") has a null body and one comment,
exercising both the empty-body edge case and the separate comments fetch in
one real request. Run from the repo root:  python -m app.fetch_issue_smoke_test
"""
from __future__ import annotations
import io
import sys
from contextlib import redirect_stderr, redirect_stdout

from adapters.github_issue import FetchIssueTool

PUBLIC_REPO = "octocat/Hello-World"
PUBLIC_ISSUE = 10622  # "test issue", body=null, 1 comment -- github.com/octocat/Hello-World/issues/10622
FAKE_TOKEN = "ghp_smoketestFAKEtoken0123456789abcd"

def main() -> int:
    captured = io.StringIO()

    # --- 1. public repo, no token: title/body/comments come back real ------
    tool = FetchIssueTool(repo=PUBLIC_REPO, token="")
    with redirect_stdout(captured), redirect_stderr(captured):
        issue = tool.run(number=PUBLIC_ISSUE)

    print(f"[ok] fetched {PUBLIC_REPO}#{PUBLIC_ISSUE} with no token")
    print(f"     title: {issue['title']!r}")
    print(f"     body: {issue['body']!r}")
    print(f"     labels: {issue['labels']!r}")
    print(f"     comments: {len(issue['comments'])}")

    if issue["number"] != PUBLIC_ISSUE:
        print(f"[!!] expected number={PUBLIC_ISSUE}, got {issue['number']!r}")
        return 1
    if not issue["title"].strip():
        print("[!!] expected a non-empty title")
        return 1
    if issue["body"] != "":
        print(f"[!!] expected body='' (this issue's body is null on GitHub), got {issue['body']!r}")
        return 1
    if not issue["comments"]:
        print("[!!] expected at least one comment on this issue")
        return 1
    if not issue["comments"][0]["body"].strip():
        print(f"[!!] expected a non-empty comment body, got {issue['comments'][0]!r}")
        return 1
    print(f"[ok] parsed title, empty body handled correctly, and {len(issue['comments'])} real comment(s) fetched")

    # --- 2. missing-token failure is clear, not a bare stack trace ---------
    try:
        FetchIssueTool(repo=PUBLIC_REPO, token="").run(number=999999999)
        print("[!!] expected a RuntimeError for a nonexistent issue number")
        return 1
    except RuntimeError as e:
        msg = str(e)
        if "GITHUB_TOKEN" not in msg and "private" not in msg:
            print(f"[!!] expected the 404 message to explain the token/private-repo ambiguity, got: {msg}")
            return 1
        print(f"[ok] nonexistent issue -> clear error: {msg}")

    # --- 3. token never appears in any log output, even on a real failure --
    bad_tool = FetchIssueTool(repo=PUBLIC_REPO, token=FAKE_TOKEN)
    exception_text: str | None = None
    with redirect_stdout(captured), redirect_stderr(captured):
        try:
            bad_tool.run(number=PUBLIC_ISSUE)  # a bogus token -> GitHub really returns 401
        except RuntimeError as e:
            exception_text = str(e)
        except Exception as e:  # noqa: BLE001 -- any other exception type still must not leak the token
            exception_text = repr(e)

    if exception_text is None:
        print("[!!] expected a RuntimeError for an invalid token, but the call succeeded")
        return 1

    all_output = captured.getvalue()
    if FAKE_TOKEN in all_output:
        print(f"[!!] token leaked into stdout/stderr: {all_output!r}")
        return 1
    if FAKE_TOKEN in exception_text:
        print(f"[!!] token leaked into an exception message: {exception_text!r}")
        return 1
    print(f"[ok] real 401 from an invalid token -> exception message clean: {exception_text!r}")
    print("[ok] token never appeared in stdout/stderr across every call in this test (verified, not assumed)")

    print("\nPhase 18 (fetch_issue) OK. Public issues readable with no token; "
          "missing-token/invalid-token failures are clear; the token is never logged.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
