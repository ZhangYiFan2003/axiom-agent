from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from axiom.rag.models import CodeChunk, IndexedFile

SCHEMA_VERSION = "2"


class CodeIndexStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma foreign_keys = on")
        return conn

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            if self._needs_rebuild(conn):
                self._drop_schema(conn)
            self._create_schema(conn)

    def get_file(self, path: str) -> IndexedFile | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select path, language, sha256, size, mtime_ns, indexed_at, parse_status
                from indexed_files
                where path = ?
                """,
                (path,),
            ).fetchone()
        if row is None:
            return None
        return IndexedFile(
            path=str(row[0]),
            language=str(row[1]),
            sha256=str(row[2]),
            size=int(row[3]),
            mtime_ns=int(row[4]),
            indexed_at=str(row[5]),
            parse_status=str(row[6]),
        )

    def replace_file(self, indexed_file: IndexedFile, chunks: Iterable[CodeChunk]) -> None:
        with self.connect() as conn:
            conn.execute("delete from code_chunks where file_path = ?", (indexed_file.path,))
            conn.execute(
                """
                insert or replace into indexed_files(
                    path, language, sha256, size, mtime_ns, indexed_at, parse_status
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    indexed_file.path,
                    indexed_file.language,
                    indexed_file.sha256,
                    indexed_file.size,
                    indexed_file.mtime_ns,
                    indexed_file.indexed_at,
                    indexed_file.parse_status,
                ),
            )
            conn.executemany(
                """
                insert into code_chunks(
                    id, file_path, language, chunk_type, symbol_name, qualified_name,
                    parent_symbol, start_line, end_line, content, content_hash,
                    is_fallback, parse_status
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.id,
                        chunk.file_path,
                        chunk.language,
                        chunk.chunk_type,
                        chunk.symbol_name,
                        chunk.qualified_name,
                        chunk.parent_symbol,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.content,
                        chunk.content_hash,
                        int(chunk.is_fallback),
                        chunk.parse_status,
                    )
                    for chunk in chunks
                ],
            )

    def delete_files(self, paths: Iterable[str]) -> int:
        items = list(paths)
        if not items:
            return 0
        with self.connect() as conn:
            conn.executemany(
                "delete from indexed_files where path = ?",
                [(path,) for path in items],
            )
        return len(items)

    def list_file_paths(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("select path from indexed_files").fetchall()
            return [str(row[0]) for row in rows]

    def count_chunks(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select count(*) from code_chunks").fetchone()
        return int(row[0] if row else 0)

    def search_rows(self, term: str, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            like = f"%{term}%"
            return conn.execute(
                """
                select file_path, start_line, content, chunk_type, symbol_name, qualified_name
                from code_chunks
                where lower(content) like ?
                   or lower(coalesce(symbol_name, '')) like ?
                   or lower(coalesce(qualified_name, '')) like ?
                order by file_path, start_line, chunk_type
                limit ?
                """,
                (like, like, like, limit),
            ).fetchall()

    def schema_version(self) -> str | None:
        with self.connect() as conn:
            if not self._table_exists(conn, "schema_metadata"):
                return None
            row = conn.execute(
                "select value from schema_metadata where key = 'schema_version'"
            ).fetchone()
        return str(row[0]) if row else None

    def _needs_rebuild(self, conn: sqlite3.Connection) -> bool:
        if not self._table_exists(conn, "code_chunks"):
            return False
        if not self._table_exists(conn, "schema_metadata"):
            return True
        row = conn.execute(
            "select value from schema_metadata where key = 'schema_version'"
        ).fetchone()
        if row is None or str(row[0]) != SCHEMA_VERSION:
            return True
        columns = {
            str(row[1])
            for row in conn.execute("pragma table_info(code_chunks)").fetchall()
        }
        return "file_path" not in columns or "chunk_type" not in columns

    def _drop_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("drop table if exists code_chunks")
        conn.execute("drop table if exists indexed_files")
        conn.execute("drop table if exists schema_metadata")

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create table if not exists schema_metadata (
                key text primary key,
                value text not null
            )
            """
        )
        conn.execute(
            """
            insert or replace into schema_metadata(key, value)
            values ('schema_version', ?)
            """,
            (SCHEMA_VERSION,),
        )
        conn.execute(
            """
            create table if not exists indexed_files (
                path text primary key,
                language text not null,
                sha256 text not null,
                size integer not null,
                mtime_ns integer not null,
                indexed_at text not null,
                parse_status text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists code_chunks (
                id text primary key,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                chunk_type text not null,
                symbol_name text,
                qualified_name text,
                parent_symbol text,
                start_line integer not null,
                end_line integer not null,
                content text not null,
                content_hash text not null,
                is_fallback integer not null default 0,
                parse_status text not null
            )
            """
        )
        conn.execute("create index if not exists idx_indexed_files_path on indexed_files(path)")
        conn.execute("create index if not exists idx_code_chunks_file on code_chunks(file_path)")
        conn.execute(
            "create index if not exists idx_code_chunks_symbol on code_chunks(symbol_name)"
        )
        conn.execute("create index if not exists idx_code_chunks_type on code_chunks(chunk_type)")

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "select name from sqlite_master where type = 'table' and name = ?",
            (name,),
        ).fetchone()
        return row is not None
