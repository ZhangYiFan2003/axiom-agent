from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MemoryKind(StrEnum):
    CONVERSATION = "conversation"
    SUMMARY = "summary"
    FACT = "fact"
    TOOL_RESULT = "tool_result"


class MemoryScopeType(StrEnum):
    THREAD = "thread"
    PROJECT = "project"
    USER = "user"
    TASK = "task"


@dataclass(slots=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    scope_type: MemoryScopeType
    scope_id: str
    content: str
    created_at: str
    updated_at: str
    source_event_start_id: int | None = None
    source_event_end_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryContextSection:
    name: str
    records: list[MemoryRecord]
    content: str
    estimated_tokens: int


@dataclass(slots=True)
class MemoryContextResult:
    sections: list[MemoryContextSection]
    estimated_chars: int
    estimated_tokens: int
    truncated: bool
    evicted_count: int
    included_count: int
    summary_used: bool = False
    summary_source_start_id: int | None = None
    summary_source_end_id: int | None = None
    raw_messages_included: int = 0
    raw_messages_skipped_by_summary: int = 0
    estimated_tokens_before_compression: int = 0
    estimated_tokens_after_compression: int = 0
    compression_ratio: float | None = None
