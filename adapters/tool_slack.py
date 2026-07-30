"""Adapter: optional Slack Incoming Webhook notifier.

Posts a short status line (e.g. "task complete", "PR opened: <url>") so a
CPU-bound task that takes minutes can ping when it's done. Default off: with
no SLACK_WEBHOOK_URL configured (app/wiring.py passes "" for webhook_url),
run() is a silent no-op -- nothing about existing behaviour changes for
anyone who hasn't opted in.

Never posts source code or diffs -- callers pass a short status string only.
The webhook URL itself is never logged, including on failure (only the
exception's class name is printed, not str(e), which could echo the URL
back in a requests error message).
"""
from __future__ import annotations
from typing import Any
import requests

class SlackNotifyTool:
    name = "notify_slack"
    description = (
        "Post a short status line to Slack (e.g. 'task complete', 'PR opened: <url>'). "
        "Silently does nothing if no webhook is configured. Never send source code or diffs through this tool."
    )
    requires_approval = False

    def __init__(self, webhook_url: str = "", timeout: float = 10):
        self._webhook_url = webhook_url
        self._timeout = timeout

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Short status line to post (no source code)"},
            },
            "required": ["message"],
        }

    def run(self, message: str) -> str:
        if not self._webhook_url:
            return "notify_slack: no webhook configured, skipped"
        try:
            response = requests.post(self._webhook_url, json={"text": message}, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"notify_slack: failed to post ({e.__class__.__name__})")
            return "notify_slack: failed"
        return "notify_slack: sent"
