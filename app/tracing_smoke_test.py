"""Phase 6 smoke test: proves spans actually reach the Phoenix collector.

Checks: (1) tracing starts and registers the global tracer provider, (2) an
engine.ask() call emits real spans (llm.generate, retriever.search, ...) that
land in Phoenix, verified via Phoenix's own REST API — not just "no exception
was raised". Requires `docker compose up -d phoenix` to be running first.
Run from the repo root:  python -m app.tracing_smoke_test
"""
from __future__ import annotations
import sys
import time
import urllib.error
import urllib.request
import json
from app.config_loader import load_config
from app.wiring import build_engine, build_tracing

def _fetch_span_names(base_url: str, project_name: str) -> list[str]:
    url = f"{base_url}/v1/projects/{project_name}/spans?limit=100"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())
    return [span["name"] for span in data["data"]]

def main() -> int:
    cfg = load_config()
    obs = cfg["observability"]

    try:
        urllib.request.urlopen("http://localhost:6006/v1/projects", timeout=3)
    except urllib.error.URLError as e:
        print(f"[!!] Phoenix not reachable at localhost:6006: {e}")
        print("     -> run: docker compose up -d phoenix")
        return 1
    print("[ok] Phoenix collector reachable at localhost:6006")

    tracing = build_tracing(cfg)
    tracing.start()
    print(f"[ok] tracing started (project={obs['project_name']}, endpoint={obs['endpoint']})")

    engine = build_engine(cfg)
    result = engine.ask("What function adds two numbers together?", thread_id="tracing-smoke")
    if not result.text.strip():
        print("[!!] ask() returned an empty answer")
        return 1
    print(f"[ok] ask() -> {result.text.strip()!r}")

    # Simple exporter (batch=False) sends synchronously, but Phoenix still needs
    # a moment to ingest and index before it's queryable.
    expected = {"engine.ask", "retriever.search", "retriever.embed", "llm.generate"}
    seen: set[str] = set()
    for attempt in range(10):
        seen = set(_fetch_span_names("http://localhost:6006", obs["project_name"]))
        if expected <= seen:
            break
        time.sleep(1)

    missing = expected - seen
    if missing:
        print(f"[!!] Phoenix never received spans named {missing!r}; saw {seen!r}")
        return 1
    print(f"[ok] Phoenix project {obs['project_name']!r} has all expected spans: {sorted(expected)}")

    print("\nPhase 6 OK. Next: Phase 7 - Action (MCP tools + approval gates + PR flow).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
