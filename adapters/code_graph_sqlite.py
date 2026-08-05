"""Adapter (Prompt 27): implements core.code_graph.CodeGraphStore via its own
SQLite file (.data/code_graph.db by default) -- a separate file from
core.store.StateStore/RunHistory's forge.db, since this is populated by a
standalone CLI (app/index_graph.py), not the normal /index path, and clearing
it should never risk the state/audit-trail tables.

Bare-name matching: dst is frequently unqualified (see core/code_graph.py's
module docstring), so callers_of() matches on the target symbol's bare method
name, and callees_of() resolves an edge's (possibly bare) dst against nodes
whose symbol equals it exactly OR ends with ".<dst>" -- the same best-effort
convention throughout this adapter, never silently upgraded to something that
looks more precise than it is.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from core.code_graph import GraphEdge, GraphNode

def _bare_name(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1]

def _row_to_node(row: tuple) -> GraphNode:
    symbol, path, start_line, end_line, language = row
    return GraphNode(symbol=symbol, path=path, start_line=start_line, end_line=end_line, language=language)

class SqliteCodeGraphStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS code_graph_nodes (
                symbol TEXT NOT NULL,
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                language TEXT NOT NULL,
                PRIMARY KEY (symbol, path)
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS code_graph_edges (
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                kind TEXT NOT NULL,
                src_path TEXT NOT NULL,
                src_line INTEGER NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_edges_dst ON code_graph_edges (dst)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_edges_src ON code_graph_edges (src)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_path ON code_graph_nodes (path)")
        self._conn.commit()

    def add(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO code_graph_nodes (symbol, path, start_line, end_line, language) VALUES (?, ?, ?, ?, ?)",
                [(n.symbol, n.path, n.start_line, n.end_line, n.language) for n in nodes],
            )
            self._conn.executemany(
                "INSERT INTO code_graph_edges (src, dst, kind, src_path, src_line) VALUES (?, ?, ?, ?, ?)",
                [(e.src, e.dst, e.kind, e.src_path, e.src_line) for e in edges],
            )

    def callers_of(self, symbol: str) -> list[GraphNode]:
        bare = _bare_name(symbol)
        rows = self._conn.execute(
            """
            SELECT DISTINCT n.symbol, n.path, n.start_line, n.end_line, n.language
            FROM code_graph_edges e
            JOIN code_graph_nodes n ON n.symbol = e.src
            WHERE e.kind = 'calls' AND e.dst = ?
            """,
            (bare,),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def callees_of(self, symbol: str) -> list[GraphNode]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT n.symbol, n.path, n.start_line, n.end_line, n.language
            FROM code_graph_edges e
            JOIN code_graph_nodes n ON (n.symbol = e.dst OR n.symbol LIKE '%.' || e.dst)
            WHERE e.kind = 'calls' AND e.src = ?
            """,
            (symbol,),
        ).fetchall()
        return [_row_to_node(r) for r in rows]

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM code_graph_nodes")
            self._conn.execute("DELETE FROM code_graph_edges")

    def delete(self, paths: list[str]) -> None:
        if not paths:
            return
        placeholders = ",".join("?" * len(paths))
        with self._conn:
            self._conn.execute(f"DELETE FROM code_graph_nodes WHERE path IN ({placeholders})", paths)
            self._conn.execute(f"DELETE FROM code_graph_edges WHERE src_path IN ({placeholders})", paths)
