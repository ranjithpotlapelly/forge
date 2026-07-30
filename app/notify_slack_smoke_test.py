"""Acceptance check for the optional Slack notifier (adapters/tool_slack.py):
proves no-webhook is a true no-op (zero network calls) and a configured
webhook posts exactly one short status line, with the webhook URL never
appearing in the tool's return value.

Uses a throwaway local HTTP server, never real Slack.
Run from the repo root:  python -m app.notify_slack_smoke_test
"""
from __future__ import annotations
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from adapters.tool_slack import SlackNotifyTool

class _FakeSlack(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append(json.loads(body) if body else None)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # silence default request logging
        pass

def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), _FakeSlack)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webhook_url = f"http://127.0.0.1:{server.server_port}/services/fake"

    try:
        # --- 1. no webhook configured -> silent no-op, zero network calls ---
        off_tool = SlackNotifyTool(webhook_url="")
        result = off_tool.run(message="task complete")
        if server.requests:
            print(f"[!!] no-webhook run() made a network call: {server.requests}")
            return 1
        print(f"[ok] no webhook configured -> no-op, zero requests ({result!r})")

        # --- 2. webhook configured -> exactly one POST with the status line ---
        tool = SlackNotifyTool(webhook_url=webhook_url)
        result = tool.run(message="task complete")
        if len(server.requests) != 1:
            print(f"[!!] expected exactly 1 request, got {len(server.requests)}")
            return 1
        if server.requests[0] != {"text": "task complete"}:
            print(f"[!!] unexpected request body: {server.requests[0]}")
            return 1
        if webhook_url in result:
            print(f"[!!] webhook URL leaked into the tool's return value: {result!r}")
            return 1
        print(f"[ok] webhook configured -> 1 POST with the status line, URL not echoed back ({result!r})")

        # --- 3. tool identity matches what app/wiring.py registers ---
        if tool.name != "notify_slack" or tool.requires_approval is not False:
            print(f"[!!] expected name='notify_slack', requires_approval=False, got name={tool.name!r} requires_approval={tool.requires_approval!r}")
            return 1
        print("[ok] name='notify_slack', requires_approval=False")

        print("\nnotify_slack OK. Default (no webhook) is a true no-op; a configured "
              "webhook posts exactly one short status line; the URL is never echoed back.")
        return 0
    finally:
        server.shutdown()

if __name__ == "__main__":
    sys.exit(main())
