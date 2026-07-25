"""Adapter (Phase 19): implements core.run_history.RunHistory via the same
local SQLite file core.store.StateStore already uses (.data/forge.db by
default) -- two new tables (runs, run_steps) alongside that store's own `kv`
table, not a new database file. One process opening two separate sqlite3
connections to the same file is fine (SQLite serializes writes itself);
that's exactly what app/wiring.py does, since StateStore and RunHistory are
deliberately two ports, not one widened one.
"""
from __future__ import annotations
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class SqliteRunHistory:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                task_text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                error TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS run_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                node TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                duration_ms REAL,
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps (run_id)")
        self._conn.commit()

    def start_run(self, thread_id: str, kind: str, task_text: str) -> str:
        run_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO runs (id, thread_id, kind, task_text, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
            (run_id, thread_id, kind, task_text, _now()),
        )
        self._conn.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, ended_at = ?, error = ? WHERE id = ?",
            (status, _now(), error, run_id),
        )
        self._conn.commit()

    def record_step(
        self, run_id: str, node: str, status: str,
        detail: str | None = None, duration_ms: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO run_steps (run_id, node, status, detail, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, node, status, detail, duration_ms, _now()),
        )
        self._conn.commit()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, thread_id, kind, task_text, status, started_at, ended_at, error "
            "FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._run_row(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        resolved = self._resolve_run_id(run_id)
        if resolved is None:
            return None
        row = self._conn.execute(
            "SELECT id, thread_id, kind, task_text, status, started_at, ended_at, error FROM runs WHERE id = ?",
            (resolved,),
        ).fetchone()
        return self._run_row(row) if row else None

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        resolved = self._resolve_run_id(run_id)
        if resolved is None:
            return []
        rows = self._conn.execute(
            "SELECT id, run_id, node, status, detail, duration_ms, created_at "
            "FROM run_steps WHERE run_id = ? ORDER BY id",
            (resolved,),
        ).fetchall()
        cols = ("id", "run_id", "node", "status", "detail", "duration_ms", "created_at")
        return [dict(zip(cols, row)) for row in rows]

    def _resolve_run_id(self, run_id: str) -> str | None:
        """Exact id, or -- for a friendlier CLI -- a unique prefix of one."""
        row = self._conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row:
            return row[0]
        row = self._conn.execute(
            "SELECT id FROM runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1", (run_id + "%",),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _run_row(row: tuple) -> dict[str, Any]:
        cols = ("id", "thread_id", "kind", "task_text", "status", "started_at", "ended_at", "error")
        return dict(zip(cols, row))
