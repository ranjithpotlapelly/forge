"""Phase 5 smoke test: proves the SQLite store actually persists to disk.

Checks: (1) put/get round-trips a value, (2) list filters by prefix, (3) a
fresh connection to the same file still sees the data (real durability, not
just an in-memory dict). Run from the repo root:  python -m app.store_smoke_test
"""
from __future__ import annotations
import sys
from app.config_loader import load_config
from app.wiring import build_store

def main() -> int:
    cfg = load_config()
    store = build_store(cfg)
    print(f"[ok] wired {type(store).__name__} (path={store.path})")

    if store.get("smoke:missing") is not None:
        print("[!!] get() on a missing key should return None")
        return 1
    print("[ok] get() on a missing key -> None")

    value = {"status": "ran", "citations": 3}
    store.put("smoke:run:1", value)
    store.put("smoke:run:2", {"status": "pending"})
    store.put("other:thing", {"unrelated": True})

    fetched = store.get("smoke:run:1")
    if fetched != value:
        print(f"[!!] round-trip mismatch: put {value!r}, got {fetched!r}")
        return 1
    print(f"[ok] put()/get() round-trip -> {fetched!r}")

    keys = store.list(prefix="smoke:")
    if keys != ["smoke:run:1", "smoke:run:2"]:
        print(f"[!!] list(prefix='smoke:') -> {keys!r}, expected the two smoke: keys only")
        return 1
    print(f"[ok] list(prefix='smoke:') -> {keys!r}")

    # A second, independent connection to the same file must see the same row —
    # proves this is durable state, not just process-local memory.
    reopened = build_store(cfg)
    if reopened.get("smoke:run:1") != value:
        print("[!!] a fresh connection to the same path did not see the persisted value")
        return 1
    print("[ok] value survives a fresh connection to the same SQLite file")

    print("\nPhase 5 OK (store). See app.checkpointer_smoke_test for the checkpointer half.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
