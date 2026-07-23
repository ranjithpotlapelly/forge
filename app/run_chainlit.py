"""Launcher for the Chainlit UI (Phase 8). Run with:  python -m app.run_chainlit

Chainlit's CLI unconditionally calls nest_asyncio.apply() at import time
(chainlit/cli/__init__.py), which breaks anyio's event-loop detection on this
Python version — static assets (JS/CSS/favicon) come back 503/500 because
anyio.to_thread.run_sync can no longer find the running loop. This neutralizes
that patch before Chainlit's CLI module is imported; `chainlit run` itself
can't be used directly until upstream fixes the nest_asyncio/anyio interaction.
"""
from __future__ import annotations
import sys
import nest_asyncio

nest_asyncio.apply = lambda *a, **k: None

from chainlit.cli import cli  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "chainlit"
    cli()
