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
import subprocess
from pathlib import Path
from typing import Iterable
from core.types import Document

_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".data", ".idea", ".claude",
    ".chainlit", "node_modules", ".pytest_cache", "dist", "build", "target",
}

_LANGUAGE_EXTENSIONS = {
    "python": (".py",),
    "javascript": (".js", ".jsx"),
    "typescript": (".ts", ".tsx"),
    "go": (".go",),
    "java": (".java",),
}

class FsIngestSource:
    def __init__(self, repo_path: str, languages: list[str]):
        self.repo_path = Path(repo_path).resolve()
        self.languages = languages
        self._extensions = {
            ext for lang in languages for ext in _LANGUAGE_EXTENSIONS.get(lang, ())
        }

    def documents(self, only: list[str] | None = None) -> Iterable[Document]:
        """only, if given, is a list of paths relative to repo_path (e.g. from
        changed_paths()) -- skips the full rglob walk and reads just those
        files, for reindexing what changed instead of the whole tree. Not
        part of the IngestSource port (index_repo never passes it); additive,
        used only by product.indexing.index_changed_files."""
        for path in self._walk(only):
            if path.suffix == ".py":
                yield from self._python_chunks(path)
            else:
                yield from self._whole_file_chunk(path)

    def _walk(self, only: list[str] | None = None) -> Iterable[Path]:
        if only is not None:
            for rel in only:
                path = self.repo_path / rel
                if path.is_file() and path.suffix in self._extensions:
                    yield path
            return
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

def changed_paths(repo_path: str) -> tuple[list[str], list[str]]:
    """Working-tree file paths (relative to repo_path, forward-slashed) that
    differ from HEAD, split into (changed, deleted) -- changed covers
    modified/added/untracked/renamed-to (anything FsIngestSource.documents(only=...)
    should re-embed), deleted covers anything gone from the working tree
    (anything Retriever.delete() should purge). Used to reindex only what's
    about to be committed instead of walking the whole repo.

    A plain `git diff --name-status HEAD` misses brand-new files that were
    never `git add`ed -- they're not in HEAD *or* the index, so diffing
    against HEAD alone shows nothing for them; `git ls-files --others` covers
    exactly that gap.
    """
    diff = subprocess.run(
        ["git", "diff", "--name-status", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed in {repo_path}: {diff.stderr.strip()}")
    changed: list[str] = []
    deleted: list[str] = []
    for line in diff.stdout.splitlines():
        if not line.strip():
            continue
        status, *paths = line.split("\t")
        if status == "D":
            deleted.append(paths[0])
        elif status.startswith("R"):  # "R100\told\tnew"
            deleted.append(paths[0])
            changed.append(paths[1])
        else:  # M, A, C, T, ...
            changed.append(paths[-1])

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    )
    if untracked.returncode != 0:
        raise RuntimeError(f"git ls-files failed in {repo_path}: {untracked.stderr.strip()}")
    changed.extend(p for p in untracked.stdout.splitlines() if p.strip())

    return changed, deleted
