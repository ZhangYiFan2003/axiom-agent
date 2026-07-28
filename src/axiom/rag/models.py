from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IndexedFile:
    path: str
    language: str
    sha256: str
    size: int
    mtime_ns: int
    indexed_at: str
    parse_status: str


@dataclass(slots=True)
class CodeChunk:
    id: str
    file_path: str
    language: str
    chunk_type: str
    symbol_name: str | None
    qualified_name: str | None
    parent_symbol: str | None
    start_line: int
    end_line: int
    content: str
    content_hash: str
    is_fallback: bool = False
    parse_status: str = "parsed"


@dataclass(slots=True)
class CodeSearchResult:
    path: str
    line: int
    snippet: str
    chunk_type: str | None = None
    symbol_name: str | None = None
    qualified_name: str | None = None
    score: float | None = None
    backend: str | None = None
    matched_fields: tuple[str, ...] = ()


@dataclass(slots=True)
class IndexStats:
    scanned_files: int = 0
    indexed_files: int = 0
    unchanged_files: int = 0
    deleted_files: int = 0
    failed_files: int = 0
    chunk_count: int = 0
    duration_ms: float = 0.0
