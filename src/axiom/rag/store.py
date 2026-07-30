from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.analysis import FileAnalysis
from axiom.rag.embeddings import EmbeddingProfile
from axiom.rag.models import (
    CallEdge,
    CodeChunk,
    ImportBinding,
    IndexedFile,
    SymbolDefinition,
    SymbolReference,
)
from axiom.rag.tokenizer import build_lexical_text
from axiom.rag.vectors import VectorError, cosine_similarity, decode_vector

SCHEMA_VERSION = "6"


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
            for _ in range(8):
                state = self._schema_state(conn)
                if state == "v6":
                    return
                if state in {"empty", "legacy"}:
                    self._drop_schema(conn)
                    self._create_schema(conn, set_version=True)
                    return
                if state == "v2":
                    self._migrate_v2_to_v3(conn)
                    continue
                if state == "v3":
                    self._migrate_v3_to_v4(conn)
                    continue
                if state == "v4":
                    self._migrate_v4_to_v5(conn)
                    continue
                if state == "v5":
                    self._migrate_v5_to_v6(conn)
                    continue
                raise RuntimeError(f"unsupported code index schema state: {state}")
            raise RuntimeError("code index schema migration did not converge")

    def has_fts5(self) -> bool:
        with self.connect() as conn:
            return supports_fts5(conn) and self._table_exists(conn, "code_chunks_fts")

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
        analysis = FileAnalysis(list(chunks), [], [], [], indexed_file.parse_status)
        self.replace_file_analysis(indexed_file, analysis)

    def replace_file_analysis(self, indexed_file: IndexedFile, analysis: FileAnalysis) -> None:
        chunk_items = list(analysis.chunks)
        with self.connect() as conn:
            chunk_ids = [
                str(row[0])
                for row in conn.execute(
                    "select id from code_chunks where file_path = ?",
                    (indexed_file.path,),
                ).fetchall()
            ]
            if self._table_exists(conn, "code_chunks_fts") and chunk_ids:
                conn.executemany(
                    "delete from code_chunks_fts where chunk_id = ?",
                    [(chunk_id,) for chunk_id in chunk_ids],
                )
            if self._table_exists(conn, "chunk_embeddings") and chunk_ids:
                conn.executemany(
                    "delete from chunk_embeddings where chunk_id = ?",
                    [(chunk_id,) for chunk_id in chunk_ids],
                )
            self._delete_symbol_rows(conn, indexed_file.path)
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
                    for chunk in chunk_items
                ],
            )
            if self._table_exists(conn, "code_chunks_fts"):
                self._insert_fts_rows(conn, chunk_items)
            if self._table_exists(conn, "symbol_definitions"):
                self._insert_symbol_rows(conn, analysis)

    def delete_files(self, paths: Iterable[str]) -> int:
        items = list(paths)
        if not items:
            return 0
        with self.connect() as conn:
            if self._table_exists(conn, "code_chunks_fts"):
                conn.executemany(
                    "delete from code_chunks_fts where file_path = ?",
                    [(path,) for path in items],
                )
            if self._table_exists(conn, "chunk_embeddings"):
                conn.executemany(
                    """
                    delete from chunk_embeddings
                    where chunk_id in (select id from code_chunks where file_path = ?)
                    """,
                    [(path,) for path in items],
                )
            for path in items:
                self._delete_symbol_rows(conn, path)
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

    def search_fts(self, match_query: str, limit: int = 200) -> list[dict[str, object]]:
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            if not self._table_exists(conn, "code_chunks_fts"):
                return []
            rows = conn.execute(
                """
                select
                    c.id as chunk_id,
                    c.file_path,
                    c.start_line,
                    c.end_line,
                    c.content,
                    c.content_hash,
                    c.chunk_type,
                    c.symbol_name,
                    c.qualified_name,
                    c.parent_symbol,
                    c.is_fallback,
                    bm25(code_chunks_fts, 1.0, 1.5, 2.0, 2.5, 1.0) * -1 as bm25_score,
                    'fts5' as backend
                from code_chunks_fts
                join code_chunks c on c.id = code_chunks_fts.chunk_id
                where code_chunks_fts match ?
                order by bm25(code_chunks_fts, 1.0, 1.5, 2.0, 2.5, 1.0)
                limit ?
                """,
                (match_query, limit),
            ).fetchall()
            return [_dict(row) for row in rows]

    def search_like(self, tokens: list[str], limit: int = 500) -> list[dict[str, object]]:
        if not tokens:
            return []
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            like = f"%{tokens[0]}%"
            rows = conn.execute(
                """
                select
                    id as chunk_id,
                    file_path,
                    start_line,
                    end_line,
                    content,
                    content_hash,
                    chunk_type,
                    symbol_name,
                    qualified_name,
                    parent_symbol,
                    is_fallback,
                    0.0 as bm25_score,
                    'like-fallback' as backend
                from code_chunks
                where lower(content) like ?
                   or lower(coalesce(symbol_name, '')) like ?
                   or lower(coalesce(qualified_name, '')) like ?
                   or lower(file_path) like ?
                   or lower(chunk_type) like ?
                order by file_path, start_line, chunk_type
                limit ?
                """,
                (like, like, like, like, like, limit),
            ).fetchall()
            return [_dict(row) for row in rows]

    def list_chunks(self) -> list[CodeChunk]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, file_path, language, chunk_type, symbol_name, qualified_name,
                    parent_symbol, start_line, end_line, content, content_hash,
                    is_fallback, parse_status
                from code_chunks
                order by file_path, start_line, chunk_type, id
                """
            ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    def list_symbol_definitions(self) -> list[SymbolDefinition]:
        if not self._has_symbol_tables():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, file_path, language, symbol_kind, name, qualified_name,
                    container_symbol_id, container_qualified_name, signature,
                    start_line, end_line, chunk_id, exported, visibility, definition_hash
                from symbol_definitions
                order by file_path, start_line, qualified_name
                """
            ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def list_import_bindings(self) -> list[ImportBinding]:
        if not self._has_symbol_tables():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, file_path, language, module_name, imported_name, local_name,
                    import_kind, relative_level, start_line, end_line, resolved_file_path,
                    resolution_status
                from import_bindings
                order by file_path, start_line, local_name
                """
            ).fetchall()
        return [_import_from_row(row) for row in rows]

    def list_symbol_references(self) -> list[SymbolReference]:
        if not self._has_symbol_tables():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, file_path, language, reference_kind, name, qualifier,
                    enclosing_symbol_id, enclosing_qualified_name, argument_count,
                    start_line, end_line, resolved_symbol_id, resolution_status,
                    resolution_confidence, resolution_reason
                from symbol_references
                order by file_path, start_line, name
                """
            ).fetchall()
        return [_reference_from_row(row) for row in rows]

    def replace_workspace_symbols(
        self,
        imports: Iterable[ImportBinding],
        references: Iterable[SymbolReference],
    ) -> None:
        self.replace_workspace_graph(imports, references, [])

    def replace_workspace_graph(
        self,
        imports: Iterable[ImportBinding],
        references: Iterable[SymbolReference],
        call_edges: Iterable[CallEdge],
    ) -> None:
        import_items = list(imports)
        reference_items = list(references)
        edge_items = list(call_edges)
        with self.connect() as conn:
            if not self._has_symbol_tables(conn):
                self._create_symbol_tables(conn)
            if not self._has_call_graph_tables(conn):
                self._create_call_graph_tables(conn)
            conn.execute("delete from call_edges")
            conn.execute("delete from import_bindings")
            conn.execute("delete from symbol_references")
            self._insert_import_rows(conn, import_items)
            self._insert_reference_rows(conn, reference_items)
            self._insert_call_edge_rows(conn, edge_items)

    def find_definitions(
        self,
        name: str,
        *,
        file_path: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> list[SymbolDefinition]:
        if not self._has_symbol_tables():
            return []
        clauses = ["name = ?"]
        params: list[object] = [name]
        if file_path:
            clauses.append("file_path = ?")
            params.append(file_path)
        if language:
            clauses.append("language = ?")
            params.append(language)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                select
                    id, file_path, language, symbol_kind, name, qualified_name,
                    container_symbol_id, container_qualified_name, signature,
                    start_line, end_line, chunk_id, exported, visibility, definition_hash
                from symbol_definitions
                where {" and ".join(clauses)}
                order by file_path, start_line, qualified_name
                limit ?
                """,
                tuple(params),
            ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def find_references(self, symbol_id_or_name: str, *, limit: int = 100) -> list[SymbolReference]:
        if not self._has_symbol_tables():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, file_path, language, reference_kind, name, qualifier,
                    enclosing_symbol_id, enclosing_qualified_name, argument_count,
                    start_line, end_line, resolved_symbol_id, resolution_status,
                    resolution_confidence, resolution_reason
                from symbol_references
                where resolved_symbol_id = ? or name = ?
                order by file_path, start_line, name
                limit ?
                """,
                (symbol_id_or_name, symbol_id_or_name, limit),
            ).fetchall()
        return [_reference_from_row(row) for row in rows]

    def list_call_edges(self) -> list[CallEdge]:
        if not self._has_call_graph_tables():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    id, reference_id, caller_symbol_id, callee_symbol_id, file_path,
                    language, edge_kind, start_line, end_line, resolution_confidence,
                    resolution_reason
                from call_edges
                order by file_path, start_line, caller_symbol_id, callee_symbol_id, id
                """
            ).fetchall()
        return [_call_edge_from_row(row) for row in rows]

    def find_callers(self, symbol_id: str, *, limit: int = 100) -> list[CallEdge]:
        if not self._has_call_graph_tables():
            return []
        bounded_limit = min(max(limit, 1), 1000)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    e.id, e.reference_id, e.caller_symbol_id, e.callee_symbol_id,
                    e.file_path, e.language, e.edge_kind, e.start_line, e.end_line,
                    e.resolution_confidence, e.resolution_reason
                from call_edges e
                join symbol_definitions caller on caller.id = e.caller_symbol_id
                where e.callee_symbol_id = ?
                order by e.file_path, e.start_line, caller.qualified_name, e.id
                limit ?
                """,
                (symbol_id, bounded_limit),
            ).fetchall()
        return [_call_edge_from_row(row) for row in rows]

    def find_callees(self, symbol_id: str, *, limit: int = 100) -> list[CallEdge]:
        if not self._has_call_graph_tables():
            return []
        bounded_limit = min(max(limit, 1), 1000)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    e.id, e.reference_id, e.caller_symbol_id, e.callee_symbol_id,
                    e.file_path, e.language, e.edge_kind, e.start_line, e.end_line,
                    e.resolution_confidence, e.resolution_reason
                from call_edges e
                join symbol_definitions callee on callee.id = e.callee_symbol_id
                where e.caller_symbol_id = ?
                order by e.file_path, e.start_line, callee.qualified_name, e.id
                limit ?
                """,
                (symbol_id, bounded_limit),
            ).fetchall()
        return [_call_edge_from_row(row) for row in rows]

    def get_definition_by_id(self, symbol_id: str) -> SymbolDefinition | None:
        if not self._has_symbol_tables():
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                select
                    id, file_path, language, symbol_kind, name, qualified_name,
                    container_symbol_id, container_qualified_name, signature,
                    start_line, end_line, chunk_id, exported, visibility, definition_hash
                from symbol_definitions
                where id = ?
                """,
                (symbol_id,),
            ).fetchone()
        return _symbol_from_row(row) if row else None

    def get_embedding_hashes(self, profile_id: str) -> dict[str, str]:
        if not self._has_vector_tables():
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                select chunk_id, embedding_input_hash
                from chunk_embeddings
                where profile_id = ?
                """,
                (profile_id,),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def upsert_embedding_profile(self, profile: EmbeddingProfile) -> None:
        with self.connect() as conn:
            self._create_vector_tables(conn)
            self._insert_profile(conn, profile)

    def profile_exists(self, profile_id: str) -> bool:
        if not self._has_vector_tables():
            return False
        with self.connect() as conn:
            row = conn.execute(
                "select id from embedding_profiles where id = ?",
                (profile_id,),
            ).fetchone()
        return row is not None

    def count_embeddings(self, profile_id: str) -> int:
        if not self._has_vector_tables():
            return 0
        with self.connect() as conn:
            row = conn.execute(
                "select count(*) from chunk_embeddings where profile_id = ?",
                (profile_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def replace_embeddings(
        self,
        profile: EmbeddingProfile,
        rows: Iterable[tuple[str, str, bytes]],
    ) -> None:
        items = list(rows)
        with self.connect() as conn:
            self._create_vector_tables(conn)
            self._insert_profile(conn, profile)
            now = datetime.now(UTC).isoformat()
            conn.executemany(
                """
                insert or replace into chunk_embeddings(
                    chunk_id, profile_id, embedding_input_hash, vector, created_at
                )
                values (?, ?, ?, ?, ?)
                """,
                [
                    (chunk_id, profile.id, input_hash, vector, now)
                    for chunk_id, input_hash, vector in items
                ],
            )

    def delete_embeddings_for_chunks(self, chunk_ids: Iterable[str], profile_id: str) -> int:
        items = list(chunk_ids)
        if not items or not self._has_vector_tables():
            return 0
        with self.connect() as conn:
            conn.executemany(
                "delete from chunk_embeddings where chunk_id = ? and profile_id = ?",
                [(chunk_id, profile_id) for chunk_id in items],
            )
        return len(items)

    def search_vectors(
        self,
        query_vector: list[float],
        *,
        profile: EmbeddingProfile,
        limit: int,
    ) -> list[dict[str, object]]:
        if not self._has_vector_tables():
            return []
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select
                    c.id as chunk_id,
                    c.file_path,
                    c.start_line,
                    c.end_line,
                    c.content,
                    c.content_hash,
                    c.chunk_type,
                    c.symbol_name,
                    c.qualified_name,
                    c.parent_symbol,
                    c.is_fallback,
                    e.vector,
                    ? as embedding_profile,
                    'vector' as backend
                from chunk_embeddings e
                join code_chunks c on c.id = e.chunk_id
                where e.profile_id = ?
                """,
                (profile.id, profile.id),
            ).fetchall()
        scored: list[dict[str, object]] = []
        for row in rows:
            try:
                vector = decode_vector(bytes(row["vector"]), dimensions=profile.dimensions)
                score = cosine_similarity(query_vector, vector)
            except (TypeError, VectorError):
                continue
            if score <= 0:
                continue
            item = _dict(row)
            item.pop("vector", None)
            item["vector_score"] = score
            item["bm25_score"] = 0.0
            scored.append(item)
        scored.sort(
            key=lambda item: (
                -float(item["vector_score"]),
                str(item["file_path"]),
                int(item["start_line"]),
            )
        )
        return scored[:limit]

    def schema_version(self) -> str | None:
        with self.connect() as conn:
            if not self._table_exists(conn, "schema_metadata"):
                return None
            row = conn.execute(
                "select value from schema_metadata where key = 'schema_version'"
            ).fetchone()
        return str(row[0]) if row else None

    def _schema_state(self, conn: sqlite3.Connection) -> str:
        if not self._table_exists(conn, "code_chunks"):
            return "empty"
        if not self._table_exists(conn, "schema_metadata"):
            return "legacy"
        row = conn.execute(
            "select value from schema_metadata where key = 'schema_version'"
        ).fetchone()
        columns = {
            str(row[1])
            for row in conn.execute("pragma table_info(code_chunks)").fetchall()
        }
        if "file_path" not in columns or "chunk_type" not in columns:
            return "legacy"
        if row is None:
            return "legacy"
        version = str(row[0])
        if version == "2":
            return "v2"
        if version == "3":
            return "v3"
        if version == "4":
            return "v4"
        if version == "5":
            return "v5"
        if version != SCHEMA_VERSION:
            return "legacy"
        if supports_fts5(conn) and not self._table_exists(conn, "code_chunks_fts"):
            return "v2"
        if not self._has_vector_tables(conn):
            return "v3"
        if not self._has_symbol_tables(conn):
            return "v4"
        if not self._has_call_graph_tables(conn):
            return "v5"
        return "v6"

    def _drop_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("drop table if exists code_chunks_fts")
        conn.execute("drop table if exists call_edges")
        conn.execute("drop table if exists symbol_references")
        conn.execute("drop table if exists import_bindings")
        conn.execute("drop table if exists symbol_definitions")
        conn.execute("drop table if exists chunk_embeddings")
        conn.execute("drop table if exists embedding_profiles")
        conn.execute("drop table if exists code_chunks")
        conn.execute("drop table if exists indexed_files")
        conn.execute("drop table if exists schema_metadata")

    def _create_schema(self, conn: sqlite3.Connection, *, set_version: bool) -> None:
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
        if supports_fts5(conn):
            self._create_fts_table(conn)
        self._create_vector_tables(conn)
        self._create_symbol_tables(conn)
        self._create_call_graph_tables(conn)
        if set_version:
            conn.execute(
                """
                insert or replace into schema_metadata(key, value)
                values ('schema_version', ?)
                """,
                (SCHEMA_VERSION,),
            )

    def _create_fts_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create virtual table if not exists code_chunks_fts
            using fts5(
                chunk_id unindexed,
                file_path,
                chunk_type,
                symbol_name,
                qualified_name,
                lexical_text
            )
            """
        )

    def _create_vector_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create table if not exists embedding_profiles (
                id text primary key,
                provider text not null,
                model text not null,
                dimensions integer not null,
                input_version text not null,
                vector_format text not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists chunk_embeddings (
                chunk_id text not null references code_chunks(id) on delete cascade,
                profile_id text not null references embedding_profiles(id) on delete cascade,
                embedding_input_hash text not null,
                vector blob not null,
                created_at text not null,
                primary key (chunk_id, profile_id)
            )
            """
        )
        conn.execute(
            """
            create index if not exists idx_chunk_embeddings_profile
            on chunk_embeddings(profile_id)
            """
        )

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        self._create_schema(conn, set_version=False)
        if supports_fts5(conn):
            self._create_fts_table(conn)
            conn.execute("delete from code_chunks_fts")
            rows = conn.execute(
                """
                select
                    id, file_path, language, chunk_type, symbol_name, qualified_name,
                    parent_symbol, start_line, end_line, content, content_hash,
                    is_fallback, parse_status
                from code_chunks
                """
            ).fetchall()
            chunks = [_chunk_from_row(row) for row in rows]
            self._insert_fts_rows(conn, chunks)
        conn.execute(
            """
            insert or replace into schema_metadata(key, value)
            values ('schema_version', '3')
            """
        )

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        self._create_vector_tables(conn)
        conn.execute(
            """
            insert or replace into schema_metadata(key, value)
            values ('schema_version', '4')
            """,
        )

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        self._create_symbol_tables(conn)
        conn.execute(
            """
            insert or replace into schema_metadata(key, value)
            values ('schema_version', '5')
            """
        )

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        self._create_call_graph_tables(conn)
        conn.execute(
            """
            insert or replace into schema_metadata(key, value)
            values ('schema_version', ?)
            """,
            (SCHEMA_VERSION,),
        )

    def _insert_fts_rows(self, conn: sqlite3.Connection, chunks: Iterable[CodeChunk]) -> None:
        conn.executemany(
            """
            insert into code_chunks_fts(
                chunk_id, file_path, chunk_type, symbol_name, qualified_name, lexical_text
            )
            values (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chunk.id,
                    chunk.file_path,
                    chunk.chunk_type,
                    chunk.symbol_name or "",
                    chunk.qualified_name or "",
                    build_lexical_text(chunk),
                )
                for chunk in chunks
            ],
        )

    def _insert_profile(self, conn: sqlite3.Connection, profile: EmbeddingProfile) -> None:
        conn.execute(
            """
            insert or ignore into embedding_profiles(
                id, provider, model, dimensions, input_version, vector_format, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.id,
                profile.provider,
                profile.model,
                profile.dimensions,
                profile.input_version,
                profile.vector_format,
                datetime.now(UTC).isoformat(),
            ),
        )

    def _create_symbol_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create table if not exists symbol_definitions (
                id text primary key,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                symbol_kind text not null,
                name text not null,
                qualified_name text not null,
                container_symbol_id text,
                container_qualified_name text,
                signature text,
                start_line integer not null,
                end_line integer not null,
                chunk_id text,
                exported integer not null,
                visibility text,
                definition_hash text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists import_bindings (
                id text primary key,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                module_name text not null,
                imported_name text,
                local_name text,
                import_kind text not null,
                relative_level integer not null,
                start_line integer not null,
                end_line integer not null,
                resolved_file_path text,
                resolution_status text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists symbol_references (
                id text primary key,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                reference_kind text not null,
                name text not null,
                qualifier text,
                enclosing_symbol_id text,
                enclosing_qualified_name text,
                argument_count integer,
                start_line integer not null,
                end_line integer not null,
                resolved_symbol_id text,
                resolution_status text not null,
                resolution_confidence real not null,
                resolution_reason text
            )
            """
        )
        for statement in [
            "create index if not exists idx_symbol_def_file on symbol_definitions(file_path)",
            "create index if not exists idx_symbol_def_name on symbol_definitions(name)",
            (
                "create index if not exists idx_symbol_def_qualified "
                "on symbol_definitions(qualified_name)"
            ),
            "create index if not exists idx_symbol_def_kind on symbol_definitions(symbol_kind)",
            "create index if not exists idx_import_file on import_bindings(file_path)",
            "create index if not exists idx_import_module on import_bindings(module_name)",
            "create index if not exists idx_import_local on import_bindings(local_name)",
            "create index if not exists idx_ref_file on symbol_references(file_path)",
            "create index if not exists idx_ref_name on symbol_references(name)",
            (
                "create index if not exists idx_ref_enclosing "
                "on symbol_references(enclosing_symbol_id)"
            ),
            "create index if not exists idx_ref_resolved on symbol_references(resolved_symbol_id)",
            "create index if not exists idx_ref_status on symbol_references(resolution_status)",
        ]:
            conn.execute(statement)

    def _delete_symbol_rows(self, conn: sqlite3.Connection, file_path: str) -> None:
        if not self._has_symbol_tables(conn):
            return
        if self._has_call_graph_tables(conn):
            conn.execute("delete from call_edges where file_path = ?", (file_path,))
        conn.execute("delete from symbol_references where file_path = ?", (file_path,))
        conn.execute("delete from import_bindings where file_path = ?", (file_path,))
        conn.execute("delete from symbol_definitions where file_path = ?", (file_path,))

    def _insert_symbol_rows(self, conn: sqlite3.Connection, analysis: FileAnalysis) -> None:
        self._insert_definition_rows(conn, analysis.symbols)
        self._insert_import_rows(conn, analysis.imports)
        self._insert_reference_rows(conn, analysis.references)

    def _insert_definition_rows(
        self,
        conn: sqlite3.Connection,
        definitions: Iterable[SymbolDefinition],
    ) -> None:
        conn.executemany(
            """
            insert into symbol_definitions(
                id, file_path, language, symbol_kind, name, qualified_name,
                container_symbol_id, container_qualified_name, signature,
                start_line, end_line, chunk_id, exported, visibility, definition_hash
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.id,
                    item.file_path,
                    item.language,
                    item.symbol_kind,
                    item.name,
                    item.qualified_name,
                    item.container_symbol_id,
                    item.container_qualified_name,
                    item.signature,
                    item.start_line,
                    item.end_line,
                    item.chunk_id,
                    int(item.exported),
                    item.visibility,
                    item.definition_hash,
                )
                for item in definitions
            ],
        )

    def _insert_import_rows(
        self,
        conn: sqlite3.Connection,
        imports: Iterable[ImportBinding],
    ) -> None:
        conn.executemany(
            """
            insert into import_bindings(
                id, file_path, language, module_name, imported_name, local_name,
                import_kind, relative_level, start_line, end_line, resolved_file_path,
                resolution_status
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.id,
                    item.file_path,
                    item.language,
                    item.module_name,
                    item.imported_name,
                    item.local_name,
                    item.import_kind,
                    item.relative_level,
                    item.start_line,
                    item.end_line,
                    item.resolved_file_path,
                    item.resolution_status,
                )
                for item in imports
            ],
        )

    def _insert_reference_rows(
        self,
        conn: sqlite3.Connection,
        references: Iterable[SymbolReference],
    ) -> None:
        conn.executemany(
            """
            insert into symbol_references(
                id, file_path, language, reference_kind, name, qualifier,
                enclosing_symbol_id, enclosing_qualified_name, argument_count,
                start_line, end_line, resolved_symbol_id, resolution_status,
                resolution_confidence, resolution_reason
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.id,
                    item.file_path,
                    item.language,
                    item.reference_kind,
                    item.name,
                    item.qualifier,
                    item.enclosing_symbol_id,
                    item.enclosing_qualified_name,
                    item.argument_count,
                    item.start_line,
                    item.end_line,
                    item.resolved_symbol_id,
                    item.resolution_status,
                    item.resolution_confidence,
                    item.resolution_reason,
                )
                for item in references
            ],
        )

    def _create_call_graph_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            create table if not exists call_edges (
                id text primary key,
                reference_id text not null unique
                    references symbol_references(id) on delete cascade,
                caller_symbol_id text not null
                    references symbol_definitions(id) on delete cascade,
                callee_symbol_id text not null
                    references symbol_definitions(id) on delete cascade,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                edge_kind text not null,
                start_line integer not null,
                end_line integer not null,
                resolution_confidence real not null,
                resolution_reason text,
                check (edge_kind in ('call', 'constructor_call'))
            )
            """
        )
        for statement in [
            "create index if not exists idx_call_edges_reference on call_edges(reference_id)",
            "create index if not exists idx_call_edges_caller on call_edges(caller_symbol_id)",
            "create index if not exists idx_call_edges_callee on call_edges(callee_symbol_id)",
            "create index if not exists idx_call_edges_file on call_edges(file_path)",
            "create index if not exists idx_call_edges_kind on call_edges(edge_kind)",
            (
                "create index if not exists idx_call_edges_pair "
                "on call_edges(caller_symbol_id, callee_symbol_id)"
            ),
        ]:
            conn.execute(statement)

    def _insert_call_edge_rows(
        self,
        conn: sqlite3.Connection,
        edges: Iterable[CallEdge],
    ) -> None:
        conn.executemany(
            """
            insert into call_edges(
                id, reference_id, caller_symbol_id, callee_symbol_id, file_path,
                language, edge_kind, start_line, end_line, resolution_confidence,
                resolution_reason
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.id,
                    item.reference_id,
                    item.caller_symbol_id,
                    item.callee_symbol_id,
                    item.file_path,
                    item.language,
                    item.edge_kind,
                    item.start_line,
                    item.end_line,
                    item.resolution_confidence,
                    item.resolution_reason,
                )
                for item in edges
            ],
        )

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "select name from sqlite_master where type = 'table' and name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _has_vector_tables(self, conn: sqlite3.Connection | None = None) -> bool:
        if conn is not None:
            return self._table_exists(conn, "embedding_profiles") and self._table_exists(
                conn, "chunk_embeddings"
            )
        with self.connect() as owned:
            return self._has_vector_tables(owned)

    def _has_symbol_tables(self, conn: sqlite3.Connection | None = None) -> bool:
        if conn is not None:
            return (
                self._table_exists(conn, "symbol_definitions")
                and self._table_exists(conn, "import_bindings")
                and self._table_exists(conn, "symbol_references")
            )
        with self.connect() as owned:
            return self._has_symbol_tables(owned)

    def _has_call_graph_tables(self, conn: sqlite3.Connection | None = None) -> bool:
        if conn is not None:
            return self._table_exists(conn, "call_edges")
        with self.connect() as owned:
            return self._has_call_graph_tables(owned)


def supports_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("create virtual table temp.axiom_fts5_probe using fts5(content)")
        conn.execute("drop table temp.axiom_fts5_probe")
        return True
    except sqlite3.DatabaseError:
        return False


def _chunk_from_row(row: sqlite3.Row | tuple[object, ...]) -> CodeChunk:
    return CodeChunk(
        id=str(row[0]),
        file_path=str(row[1]),
        language=str(row[2]),
        chunk_type=str(row[3]),
        symbol_name=row[4],
        qualified_name=row[5],
        parent_symbol=row[6],
        start_line=int(row[7]),
        end_line=int(row[8]),
        content=str(row[9]),
        content_hash=str(row[10]),
        is_fallback=bool(row[11]),
        parse_status=str(row[12]),
    )


def _symbol_from_row(row: sqlite3.Row | tuple[object, ...]) -> SymbolDefinition:
    return SymbolDefinition(
        id=str(row[0]),
        file_path=str(row[1]),
        language=str(row[2]),
        symbol_kind=str(row[3]),
        name=str(row[4]),
        qualified_name=str(row[5]),
        container_symbol_id=row[6],
        container_qualified_name=row[7],
        signature=row[8],
        start_line=int(row[9]),
        end_line=int(row[10]),
        chunk_id=row[11],
        exported=bool(row[12]),
        visibility=row[13],
        definition_hash=str(row[14]),
    )


def _import_from_row(row: sqlite3.Row | tuple[object, ...]) -> ImportBinding:
    return ImportBinding(
        id=str(row[0]),
        file_path=str(row[1]),
        language=str(row[2]),
        module_name=str(row[3]),
        imported_name=row[4],
        local_name=row[5],
        import_kind=str(row[6]),
        relative_level=int(row[7]),
        start_line=int(row[8]),
        end_line=int(row[9]),
        resolved_file_path=row[10],
        resolution_status=str(row[11]),
    )


def _reference_from_row(row: sqlite3.Row | tuple[object, ...]) -> SymbolReference:
    return SymbolReference(
        id=str(row[0]),
        file_path=str(row[1]),
        language=str(row[2]),
        reference_kind=str(row[3]),
        name=str(row[4]),
        qualifier=row[5],
        enclosing_symbol_id=row[6],
        enclosing_qualified_name=row[7],
        argument_count=int(row[8]) if row[8] is not None else None,
        start_line=int(row[9]),
        end_line=int(row[10]),
        resolved_symbol_id=row[11],
        resolution_status=str(row[12]),
        resolution_confidence=float(row[13]),
        resolution_reason=row[14],
    )


def _call_edge_from_row(row: sqlite3.Row | tuple[object, ...]) -> CallEdge:
    return CallEdge(
        id=str(row[0]),
        reference_id=str(row[1]),
        caller_symbol_id=str(row[2]),
        callee_symbol_id=str(row[3]),
        file_path=str(row[4]),
        language=str(row[5]),
        edge_kind=str(row[6]),
        start_line=int(row[7]),
        end_line=int(row[8]),
        resolution_confidence=float(row[9]),
        resolution_reason=row[10],
    )


def _dict(row: sqlite3.Row) -> dict[str, object]:
    keys = row.keys()
    return {key: row[key] for key in keys}
