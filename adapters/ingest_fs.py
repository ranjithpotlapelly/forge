"""Adapter (Phase 10): implements core.ingest.IngestSource by walking a local
repo checkout and yielding structure-aware chunks.

Python files are chunked per top-level function/class via the stdlib `ast`
module (no tree-sitter: Phase 9 dropped it as an unused dependency, and ast
already gives real function/class-level chunks for the language this repo is
actually written in). Other configured languages fall back to whole-file
chunks, since there's no parser for them here.
"""
from __future__ import annotations
import ast
from pathlib import Path
from typing import Iterable
from core.types import Document

_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".data", ".idea", ".claude",
    ".chainlit", "node_modules", ".pytest_cache", "dist", "build",
}

_LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "javascript": (".js", ".jsx"),
    "typescript": (".ts", ".tsx"),
    "go": (".go",),
}

class FsIngestSource:
    def __init__(self, repo_path: str, languages: list[str]):
        self.repo_path = Path(repo_path).resolve()
        self.languages = languages
        self._extensions = {
            ext for lang in languages for ext in _LANGUAGE_EXTENSIONS.get(lang, ())
        }

    def documents(self) -> Iterable[Document]:
        for path in self._walk():
            if path.suffix == ".py":
                yield from self._python_chunks(path)
            else:
                yield from self._whole_file_chunk(path)

    def _walk(self) -> Iterable[Path]:
        for path in self.repo_path.rglob("*"):
            if not path.is_file() or path.suffix not in self._extensions:
                continue
            if _EXCLUDED_DIRS & set(path.relative_to(self.repo_path).parts[:-1]):
                continue
            yield path

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.repo_path)).replace("\\", "/")

    def _python_chunks(self, path: Path) -> Iterable[Document]:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        if not source.strip():
            return
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return

        lines = source.splitlines()
        rel_path = self._rel(path)
        chunked = False
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
                yield Document(
                    content="\n".join(lines[start - 1:end]),
                    metadata={
                        "path": rel_path, "symbol": node.name,
                        "start_line": start, "end_line": end, "language": "python",
                    },
                )
                chunked = True
        if not chunked:
            yield Document(
                content=source,
                metadata={
                    "path": rel_path, "symbol": "<module>",
                    "start_line": 1, "end_line": len(lines), "language": "python",
                },
            )

    def _whole_file_chunk(self, path: Path) -> Iterable[Document]:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
        if not source.strip():
            return
        yield Document(
            content=source,
            metadata={
                "path": self._rel(path), "symbol": "<file>",
                "start_line": 1, "end_line": len(source.splitlines()),
            },
        )
