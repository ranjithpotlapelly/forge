"""Port (Prompt 27, additive): a dependency graph over the indexed codebase --
who calls whom, so retrieval and Q&A can follow edges instead of only
similarity. Adapter: SQLite (adapters/code_graph_sqlite.py), populated by a
separate tree-sitter analysis pass (adapters/code_graph_extract.py) that does
not touch the existing chunker (adapters/ingest_fs.py).

Symbol names follow the same "ClassName.methodName" convention
adapters/ingest_fs.py already uses for chunk metadata, but edges are resolved
by BEST-EFFORT NAME MATCHING, not real type resolution -- there is no
type-checker here. A call site's callee is usually only known by its bare
method name (e.g. `login(...)`, not `AuthService.login(...)`), so an edge's
`dst` is frequently unqualified. This means a common method name shared by
two unrelated classes can be conflated; callers/callees are a best-effort
signal to expand context with, not a guarantee.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

@dataclass
class GraphNode:
    """A declaration: a class, interface, or method -- one row per symbol the
    extractor found, independent of whether anything calls it."""
    symbol: str        # e.g. "AuthService" or "AuthService.login"
    path: str
    start_line: int
    end_line: int
    language: str

@dataclass
class GraphEdge:
    """kind is one of 'calls', 'implements', 'imports'. src is always a
    qualified symbol (the declaration the edge originates from); dst is
    best-effort -- a bare name for 'calls' (no receiver-type resolution), a
    type/interface name for 'implements', a module specifier for 'imports'."""
    src: str
    dst: str
    kind: str
    src_path: str
    src_line: int

@runtime_checkable
class CodeGraphStore(Protocol):
    def add(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        """Insert/replace nodes (keyed by symbol+path) and append edges."""
        ...

    def callers_of(self, symbol: str) -> list[GraphNode]:
        """Declarations whose body contains a call matching symbol's bare
        (unqualified) name -- "what calls X". Best-effort: a shared method
        name across unrelated classes can produce a false positive."""
        ...

    def callees_of(self, symbol: str) -> list[GraphNode]:
        """Declarations that symbol's own body calls, for every callee name
        that resolves to a known declaration elsewhere in the graph. Callees
        with no matching declaration (external libraries, unindexed code)
        are silently omitted -- there is nothing to cite for them."""
        ...

    def clear(self) -> None:
        """Remove every node and edge -- a full wipe, like Retriever.clear()."""
        ...

    def delete(self, paths: list[str]) -> None:
        """Remove every node and edge whose source file is in `paths` -- a
        no-op for an empty list, mirroring Retriever.delete(paths)."""
        ...
