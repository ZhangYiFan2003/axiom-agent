from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from axiom.memory.context import MemoryContextBuilder
from axiom.memory.models import MemoryContextResult, MemoryKind, MemoryRecord, MemoryScopeType
from axiom.memory.store import MemoryStore
from axiom.types import Message

SECRET_PATTERN = re.compile(
    r"(authorization|bearer\s+[a-z0-9._-]+|sk-[a-z0-9]+|api[_-]?key|\.env)",
    re.IGNORECASE,
)
WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:\\[^\s]+")
BASE64_BLOB_PATTERN = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")
DEFAULT_TOOL_DIGEST_CHARS = 500


class MemoryService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        project_scope: str,
        user_scope: str = "local",
    ):
        self.store = MemoryStore(db_path)
        self.project_scope = project_scope
        self.user_scope = user_scope
        self.context_builder = MemoryContextBuilder(self.store)

    def save_fact(
        self,
        content: str,
        *,
        key: str | None = None,
        category: str = "fact",
        confidence: float = 1.0,
        source: str = "explicit",
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        target_scope = scope_id or self._default_scope(scope_type)
        record = self.store.save_record(
            kind=MemoryKind.FACT,
            scope_type=scope_type,
            scope_id=target_scope,
            content=content,
            metadata={
                "active": True,
                "category": category,
                "confidence": confidence,
                "source": source,
                **({"key": key} if key else {}),
                **dict(metadata or {}),
            },
        )
        if key:
            self.store.supersede_fact(
                scope_type=scope_type,
                scope_id=target_scope,
                key=key,
                superseded_by=record.id,
            )
        return record

    def save_summary(
        self,
        thread_id: str,
        content: str,
        *,
        source_event_start_id: int | None = None,
        source_event_end_id: int | None = None,
        version: int = 1,
        replaces: str | None = None,
    ) -> MemoryRecord:
        return self.store.save_record(
            kind=MemoryKind.SUMMARY,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=content,
            source_event_start_id=source_event_start_id,
            source_event_end_id=source_event_end_id,
            metadata={"active": True, "version": version, "replaces": replaces},
        )

    def save_conversation(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        event_id: int,
        turn_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        return self.store.save_record(
            kind=MemoryKind.CONVERSATION,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=content,
            source_event_start_id=event_id,
            source_event_end_id=event_id,
            metadata={
                "active": True,
                "role": role,
                "turn_id": turn_id,
                **dict(metadata or {}),
            },
            record_id=f"event_{event_id}",
        )

    def save_tool_result(
        self,
        thread_id: str,
        *,
        tool_name: str,
        success: bool,
        content: str,
        source_event_id: int,
        max_preview_chars: int = DEFAULT_TOOL_DIGEST_CHARS,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        raw = str(content or "")
        preview = _bounded_preview(_redact(raw), max_preview_chars)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self.store.save_record(
            kind=MemoryKind.TOOL_RESULT,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=preview,
            source_event_start_id=source_event_id,
            source_event_end_id=source_event_id,
            metadata={
                "active": True,
                "tool_name": tool_name,
                "success": success,
                "result_chars": len(raw),
                "sha256": digest,
                "truncated": len(raw) > max_preview_chars,
                **dict(metadata or {}),
            },
            record_id=f"tool_event_{source_event_id}",
        )

    def list_records(
        self,
        *,
        kind: MemoryKind | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return self.store.list_records(
            kind=kind,
            scope_type=scope_type,
            scope_id=scope_id,
            include_inactive=include_inactive,
            limit=limit,
        )

    def search_records(
        self,
        query: str,
        *,
        kinds: Iterable[MemoryKind] | None = None,
        scopes: Iterable[tuple[MemoryScopeType, str]] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        return self.store.search_records(query, kinds=kinds, scopes=scopes, limit=limit)

    def build_context(
        self,
        *,
        thread_id: str | None = None,
        query: str = "",
        max_context_chars: int = 8_000,
        max_estimated_tokens: int = 2_000,
        max_records: int = 40,
    ) -> MemoryContextResult:
        return self.context_builder.build(
            thread_id=thread_id,
            project_scope=self.project_scope,
            user_scope=self.user_scope,
            query=query,
            max_context_chars=max_context_chars,
            max_estimated_tokens=max_estimated_tokens,
            max_records=max_records,
        )

    def history_from_runtime_events(self, events: Iterable[Any]) -> list[Message]:
        messages: list[Message] = []
        seen_event_ids: set[int] = set()
        pending_user: str | None = None
        for event in events:
            event_id = getattr(event, "id", None)
            if isinstance(event_id, int):
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
            event_type = str(getattr(event, "type", ""))
            payload = getattr(event, "payload", {})
            if not isinstance(payload, dict):
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            if event_type == "user.message":
                pending_user = text
            elif event_type == "assistant.message":
                if pending_user is None:
                    continue
                messages.append(Message(role="user", content=pending_user))
                messages.append(Message(role="assistant", content=text))
                pending_user = None
        return messages

    def _default_scope(self, scope_type: MemoryScopeType) -> str:
        if scope_type == MemoryScopeType.USER:
            return self.user_scope
        if scope_type == MemoryScopeType.PROJECT:
            return self.project_scope
        return self.project_scope


def _bounded_preview(text: str, max_chars: int) -> str:
    limit = max(0, min(max_chars, DEFAULT_TOOL_DIGEST_CHARS))
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _redact(text: str) -> str:
    redacted = SECRET_PATTERN.sub("[redacted]", text)
    redacted = WINDOWS_PATH_PATTERN.sub("[redacted-path]", redacted)
    return BASE64_BLOB_PATTERN.sub("[redacted-binary]", redacted)
