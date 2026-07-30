from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from axiom.memory.models import MemoryKind, MemoryRecord, MemoryScopeType

SCHEMA_VERSION = "2"


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def save_record(
        self,
        *,
        kind: MemoryKind,
        scope_type: MemoryScopeType,
        scope_id: str,
        content: str,
        source_event_start_id: int | None = None,
        source_event_end_id: int | None = None,
        metadata: dict[str, object] | None = None,
        record_id: str | None = None,
    ) -> MemoryRecord:
        now = _now()
        memory_id = record_id or f"mem_{uuid4().hex}"
        record = MemoryRecord(
            id=memory_id,
            kind=kind,
            scope_type=scope_type,
            scope_id=scope_id,
            content=content.strip(),
            created_at=now,
            updated_at=now,
            source_event_start_id=source_event_start_id,
            source_event_end_id=source_event_end_id,
            metadata=dict(metadata or {}),
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into memory_records(
                    id, kind, scope_type, scope_id, content, created_at, updated_at,
                    source_event_start_id, source_event_end_id, metadata
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _record_row(record),
            )
        return record

    def save_active_summary(
        self,
        *,
        scope_id: str,
        content: str,
        source_event_start_id: int | None,
        source_event_end_id: int | None,
        metadata: dict[str, object],
        replaces: str | None = None,
        record_id: str | None = None,
    ) -> MemoryRecord:
        now = _now()
        memory_id = record_id or f"mem_{uuid4().hex}"
        metadata = dict(metadata)
        metadata["active"] = True
        metadata["summary_stage"] = "reduce"
        metadata["replaces"] = replaces
        record = MemoryRecord(
            id=memory_id,
            kind=MemoryKind.SUMMARY,
            scope_type=MemoryScopeType.THREAD,
            scope_id=scope_id,
            content=content.strip(),
            created_at=now,
            updated_at=now,
            source_event_start_id=source_event_start_id,
            source_event_end_id=source_event_end_id,
            metadata=metadata,
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into memory_records(
                    id, kind, scope_type, scope_id, content, created_at, updated_at,
                    source_event_start_id, source_event_end_id, metadata
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _record_row(record),
            )
            rows = conn.execute(
                """
                select id, metadata
                from memory_records
                where kind = 'summary'
                  and scope_type = ?
                  and scope_id = ?
                  and id != ?
                """,
                (record.scope_type.value, record.scope_id, record.id),
            ).fetchall()
            for row in rows:
                old_metadata = _metadata(str(row[1]))
                if old_metadata.get("summary_stage") != "reduce":
                    continue
                if not old_metadata.get("active", True):
                    continue
                old_metadata["active"] = False
                old_metadata["superseded_by"] = record.id
                conn.execute(
                    "update memory_records set metadata = ?, updated_at = ? where id = ?",
                    (json.dumps(old_metadata, ensure_ascii=False), _now(), str(row[0])),
                )
        return record

    def active_summary(self, *, thread_id: str) -> MemoryRecord | None:
        records = self.list_records(
            kind=MemoryKind.SUMMARY,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            include_inactive=False,
            limit=100,
        )
        reduce_summaries = [
            record
            for record in records
            if record.metadata.get("summary_stage", "reduce") == "reduce"
        ]
        reduce_summaries.sort(
            key=lambda record: (
                int(record.metadata.get("version") or 0),
                record.source_event_end_id or 0,
                record.updated_at,
                record.id,
            ),
            reverse=True,
        )
        return reduce_summaries[0] if reduce_summaries else None

    def list_records(
        self,
        *,
        kind: MemoryKind | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if scope_type is not None:
            clauses.append("scope_type = ?")
            params.append(scope_type.value)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        where = " where " + " and ".join(clauses) if clauses else ""
        params.append(min(max(limit, 1), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, kind, scope_type, scope_id, content, created_at, updated_at,
                       source_event_start_id, source_event_end_id, metadata
                from memory_records
                {where}
                order by created_at desc, id desc
                limit ?
                """,
                tuple(params),
            ).fetchall()
        records = [_record_from_row(row) for row in rows]
        if not include_inactive:
            records = [record for record in records if record.metadata.get("active", True)]
        return records

    def search_records(
        self,
        query: str,
        *,
        kinds: Iterable[MemoryKind] | None = None,
        scopes: Iterable[tuple[MemoryScopeType, str]] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        candidates: list[MemoryRecord] = []
        for kind in kinds or list(MemoryKind):
            candidates.extend(self.list_records(kind=kind, include_inactive=False, limit=500))
        scope_set = set(scopes or [])
        filtered = []
        seen: set[str] = set()
        for record in candidates:
            if record.id in seen:
                continue
            seen.add(record.id)
            if scope_set and (record.scope_type, record.scope_id) not in scope_set:
                continue
            searchable = " ".join([record.content, json.dumps(record.metadata)]).casefold()
            if not terms or all(term in searchable for term in terms):
                filtered.append(record)
        filtered.sort(key=_search_sort_key, reverse=True)
        return filtered[:limit]

    def supersede_fact(
        self,
        *,
        scope_type: MemoryScopeType,
        scope_id: str,
        key: str,
        superseded_by: str,
    ) -> int:
        count = 0
        records = self.list_records(
            kind=MemoryKind.FACT,
            scope_type=scope_type,
            scope_id=scope_id,
            include_inactive=False,
            limit=1000,
        )
        with self._connect() as conn:
            for record in records:
                if record.metadata.get("key") != key or record.id == superseded_by:
                    continue
                metadata = dict(record.metadata)
                metadata["active"] = False
                metadata["superseded_by"] = superseded_by
                conn.execute(
                    """
                    update memory_records
                    set metadata = ?, updated_at = ?
                    where id = ?
                    """,
                    (json.dumps(metadata, ensure_ascii=False), _now(), record.id),
                )
                count += 1
        return count

    def clear_scope(self, scope_type: MemoryScopeType, scope_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "delete from memory_records where scope_type = ? and scope_id = ?",
                (scope_type.value, scope_id),
            )
            conn.execute("delete from memories where scope = ?", (scope_id,))
            return int(cursor.rowcount)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists memories (
                    id integer primary key autoincrement,
                    scope text not null,
                    content text not null,
                    created_at text not null
                )
                """
            )
            conn.execute("create index if not exists idx_memories_scope on memories(scope, id)")
            conn.execute(
                """
                create table if not exists memory_schema_meta (
                    key text primary key,
                    value text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists memory_records (
                    id text primary key,
                    kind text not null,
                    scope_type text not null,
                    scope_id text not null,
                    content text not null,
                    created_at text not null,
                    updated_at text not null,
                    source_event_start_id integer,
                    source_event_end_id integer,
                    metadata text not null
                )
                """
            )
            for statement in [
                "create index if not exists idx_memory_records_kind on memory_records(kind)",
                (
                    "create index if not exists idx_memory_records_scope "
                    "on memory_records(scope_type, scope_id, kind, created_at)"
                ),
                (
                    "create index if not exists idx_memory_records_source "
                    "on memory_records(source_event_start_id, source_event_end_id)"
                ),
            ]:
                conn.execute(statement)
            self._migrate_old_rows(conn)
            conn.execute(
                """
                insert or replace into memory_schema_meta(key, value)
                values ('schema_version', ?)
                """,
                (SCHEMA_VERSION,),
            )

    def _migrate_old_rows(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select id, scope, content, created_at
            from memories
            order by id
            """
        ).fetchall()
        for row in rows:
            record_id = f"legacy_{int(row[0])}"
            exists = conn.execute(
                "select 1 from memory_records where id = ?",
                (record_id,),
            ).fetchone()
            if exists:
                continue
            metadata = {
                "legacy_id": int(row[0]),
                "active": True,
                "source": "legacy_memory_manager",
            }
            conn.execute(
                """
                insert into memory_records(
                    id, kind, scope_type, scope_id, content, created_at, updated_at,
                    source_event_start_id, source_event_end_id, metadata
                )
                values (?, 'fact', 'project', ?, ?, ?, ?, null, null, ?)
                """,
                (
                    record_id,
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[3]),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

    def schema_version(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select value from memory_schema_meta where key = 'schema_version'"
            ).fetchone()
        return str(row[0]) if row else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _record_row(record: MemoryRecord) -> tuple[object, ...]:
    return (
        record.id,
        record.kind.value,
        record.scope_type.value,
        record.scope_id,
        record.content,
        record.created_at,
        record.updated_at,
        record.source_event_start_id,
        record.source_event_end_id,
        json.dumps(record.metadata, ensure_ascii=False),
    )


def _record_from_row(row: tuple[object, ...]) -> MemoryRecord:
    return MemoryRecord(
        id=str(row[0]),
        kind=MemoryKind(str(row[1])),
        scope_type=MemoryScopeType(str(row[2])),
        scope_id=str(row[3]),
        content=str(row[4]),
        created_at=str(row[5]),
        updated_at=str(row[6]),
        source_event_start_id=int(row[7]) if row[7] is not None else None,
        source_event_end_id=int(row[8]) if row[8] is not None else None,
        metadata=_metadata(str(row[9])),
    )


def _metadata(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _search_sort_key(record: MemoryRecord) -> tuple[float, str, str]:
    confidence = record.metadata.get("confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    return (confidence_value, record.updated_at, record.id)
