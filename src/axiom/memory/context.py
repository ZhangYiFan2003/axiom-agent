from __future__ import annotations

from axiom.memory.models import (
    MemoryContextResult,
    MemoryContextSection,
    MemoryKind,
    MemoryRecord,
    MemoryScopeType,
)
from axiom.memory.store import MemoryStore

DEFAULT_MAX_CONTEXT_CHARS = 8_000
DEFAULT_MAX_ESTIMATED_TOKENS = 2_000
DEFAULT_MAX_RECORDS = 40
HARD_MAX_CONTEXT_CHARS = 24_000
HARD_MAX_ESTIMATED_TOKENS = 6_000
HARD_MAX_RECORDS = 100


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4, len(text.splitlines()))


class MemoryContextBuilder:
    def __init__(self, store: MemoryStore):
        self.store = store

    def build(
        self,
        *,
        thread_id: str | None = None,
        project_scope: str | None = None,
        user_scope: str | None = None,
        query: str = "",
        recent_limit: int = 8,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_estimated_tokens: int = DEFAULT_MAX_ESTIMATED_TOKENS,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> MemoryContextResult:
        max_context_chars = min(max(max_context_chars, 1), HARD_MAX_CONTEXT_CHARS)
        max_estimated_tokens = min(max(max_estimated_tokens, 1), HARD_MAX_ESTIMATED_TOKENS)
        max_records = min(max(max_records, 1), HARD_MAX_RECORDS)
        candidates = self._candidate_records(
            thread_id=thread_id,
            project_scope=project_scope,
            user_scope=user_scope,
            query=query,
            recent_limit=recent_limit,
        )
        sections: list[MemoryContextSection] = []
        chars = 0
        tokens = 0
        records = 0
        evicted = 0
        truncated = False
        for name, candidate_records in candidates:
            records_for_section = list(candidate_records)
            if not records_for_section:
                continue
            while records_for_section:
                section = _section(name, records_for_section)
                next_chars = chars + len(section.content)
                next_tokens = tokens + section.estimated_tokens
                next_records = records + len(section.records)
                if (
                    next_chars <= max_context_chars
                    and next_tokens <= max_estimated_tokens
                    and next_records <= max_records
                ):
                    sections.append(section)
                    chars = next_chars
                    tokens = next_tokens
                    records = next_records
                    break
                truncated = True
                if name == "recent conversation":
                    next_records = _drop_oldest_conversation_turn(records_for_section)
                    evicted += len(records_for_section) - len(next_records)
                    records_for_section = next_records
                else:
                    evicted += 1
                    records_for_section = records_for_section[:-1]
        return MemoryContextResult(
            sections=sections,
            estimated_chars=chars,
            estimated_tokens=tokens,
            truncated=truncated,
            evicted_count=evicted,
            included_count=records,
        )

    def _candidate_records(
        self,
        *,
        thread_id: str | None,
        project_scope: str | None,
        user_scope: str | None,
        query: str,
        recent_limit: int,
    ) -> list[tuple[str, list[MemoryRecord]]]:
        sections: list[tuple[str, list[MemoryRecord]]] = []
        if thread_id:
            summaries = self.store.list_records(
                kind=MemoryKind.SUMMARY,
                scope_type=MemoryScopeType.THREAD,
                scope_id=thread_id,
                limit=1,
            )
            sections.append(("latest summary", summaries))
            recent = list(
                reversed(
                    self.store.list_records(
                        kind=MemoryKind.CONVERSATION,
                        scope_type=MemoryScopeType.THREAD,
                        scope_id=thread_id,
                        limit=recent_limit,
                    )
                )
            )
            sections.append(("recent conversation", recent))
            tools = self.store.list_records(
                kind=MemoryKind.TOOL_RESULT,
                scope_type=MemoryScopeType.THREAD,
                scope_id=thread_id,
                limit=recent_limit,
            )
            sections.append(("tool result digests", tools))
        facts: list[MemoryRecord] = []
        scopes = []
        if project_scope:
            scopes.append((MemoryScopeType.PROJECT, project_scope))
        if user_scope:
            scopes.append((MemoryScopeType.USER, user_scope))
        if scopes:
            facts = self.store.search_records(
                query,
                kinds=[MemoryKind.FACT],
                scopes=scopes,
                limit=20,
            )
        sections.insert(2, ("facts and preferences", facts))
        return sections


def _section(name: str, records: list[MemoryRecord]) -> MemoryContextSection:
    content = _serialize_section(name, records)
    return MemoryContextSection(
        name=name,
        records=records,
        content=content,
        estimated_tokens=estimate_tokens(content),
    )


def _serialize_section(name: str, records: list[MemoryRecord]) -> str:
    if not records:
        return ""
    rows = [f"[{name}]"]
    for record in records:
        prefix = f"- {record.kind.value}:{record.scope_type.value}:{record.scope_id}: "
        rows.append(f"{prefix}{record.content}")
    return "\n".join(rows) + "\n"


def _drop_oldest_conversation_turn(records: list[MemoryRecord]) -> list[MemoryRecord]:
    if len(records) >= 2:
        first_role = records[0].metadata.get("role")
        second_role = records[1].metadata.get("role")
        if first_role == "user" and second_role == "assistant":
            return records[2:]
    return records[1:]
