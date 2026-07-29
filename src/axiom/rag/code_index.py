from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.chunker import chunk_source
from axiom.rag.languages import SKIP_DIRS, detect_language, is_indexable
from axiom.rag.models import CodeSearchResult, IndexedFile, IndexStats
from axiom.rag.ranking import rank_rows
from axiom.rag.store import CodeIndexStore
from axiom.rag.tokenizer import fts_match_query, tokenize_code_text, tokenize_query


class CodeIndex:
    def __init__(self, root: str | Path, db_path: str | Path | None = None):
        self.root = Path(root).resolve()
        self.db_path = (
            Path(db_path).expanduser() if db_path else self.root / ".axiom" / "code_index.sqlite3"
        )
        self.store = CodeIndexStore(self.db_path)
        self.last_stats = IndexStats()

    def rebuild(self, path: str | Path | None = None) -> int:
        self.last_stats = self.update(path, force=True)
        return self.last_stats.chunk_count

    def update(self, path: str | Path | None = None, *, force: bool = False) -> IndexStats:
        started = time.perf_counter()
        base = self._resolve(path or self.root)
        files = [base] if base.is_file() else list(self._iter_files(base))
        scanned_paths = {self._relative(file_path) for file_path in files}
        stats = IndexStats(scanned_files=len(files))

        for file_path in files:
            rel = self._relative(file_path)
            try:
                file_hash = self._sha256(file_path)
                stat = file_path.stat()
                language = detect_language(file_path)
                indexed = self.store.get_file(rel)
                if not force and indexed and indexed.sha256 == file_hash:
                    stats.unchanged_files += 1
                    continue

                source = file_path.read_text(encoding="utf-8", errors="ignore")
                chunks, parse_status = chunk_source(rel, language, source)
                indexed_file = IndexedFile(
                    path=rel,
                    language=language,
                    sha256=file_hash,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    indexed_at=datetime.now(UTC).isoformat(),
                    parse_status=parse_status,
                )
                self.store.replace_file(indexed_file, chunks)
                stats.indexed_files += 1
            except OSError:
                stats.failed_files += 1

        stats.deleted_files = self.remove_missing_files(base, scanned_paths)
        stats.chunk_count = self.store.count_chunks()
        stats.duration_ms = (time.perf_counter() - started) * 1000
        self.last_stats = stats
        return stats

    def remove_missing_files(
        self,
        path: str | Path | None = None,
        scanned_paths: set[str] | None = None,
    ) -> int:
        base = self._resolve(path or self.root)
        existing = scanned_paths
        if existing is None:
            files = [base] if base.is_file() else list(self._iter_files(base))
            existing = {self._relative(file_path) for file_path in files}

        missing: list[str] = []
        for stored_path in self.store.list_file_paths():
            absolute = (self.root / stored_path).resolve()
            if base.is_file() and absolute != base:
                continue
            if not base.is_file() and not _is_relative_to(absolute, base):
                continue
            if stored_path not in existing:
                missing.append(stored_path)
        return self.store.delete_files(missing)

    def search(self, query: str, limit: int = 20) -> list[CodeSearchResult]:
        tokens = tokenize_query(query)
        if not tokens:
            return []

        rows: list[dict[str, object]]
        if self.store.has_fts5():
            try:
                rows = self.store.search_fts(fts_match_query(tokens), limit=200)
            except sqlite3.DatabaseError:
                rows = self._fallback_rows(tokens)
        else:
            rows = self._fallback_rows(tokens)
        return rank_rows(rows, query, limit)

    def _iter_files(self, base: Path):
        for path in base.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and is_indexable(path):
                yield path

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        resolved.relative_to(self.root)
        return resolved

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _fallback_rows(self, tokens: list[str]) -> list[dict[str, object]]:
        rows = self.store.search_like(tokens, limit=500)
        filtered: list[dict[str, object]] = []
        for row in rows:
            searchable_tokens = tokenize_code_text(
                " ".join(
                    [
                        str(row["content"]),
                        str(row["symbol_name"] or ""),
                        str(row["qualified_name"] or ""),
                        str(row["file_path"]),
                        str(row["chunk_type"]),
                    ]
                )
            )
            searchable = set(searchable_tokens)
            if all(token in searchable for token in tokens):
                filtered.append(row)
        return filtered


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
