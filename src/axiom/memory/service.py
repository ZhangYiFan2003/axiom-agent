from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from axiom.memory.context import MemoryContextBuilder
from axiom.memory.models import MemoryContextResult, MemoryKind, MemoryRecord, MemoryScopeType
from axiom.memory.store import MemoryStore
from axiom.memory.summarizer import (
    ConversationSummarizer,
    DeterministicConversationSummarizer,
    SummaryPolicy,
    SummaryRunResult,
    segment_messages,
)
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
        summarizer: ConversationSummarizer | None = None,
        summary_policy: SummaryPolicy | None = None,
    ):
        self.store = MemoryStore(db_path)
        self.project_scope = project_scope
        self.user_scope = user_scope
        self.context_builder = MemoryContextBuilder(self.store)
        self.summarizer = summarizer or DeterministicConversationSummarizer()
        self.summary_policy = (summary_policy or SummaryPolicy()).normalized()

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
        active: bool = True,
        summary_stage: str = "reduce",
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        summary_metadata = {
            "active": active,
            "version": version,
            "replaces": replaces,
            "summary_stage": summary_stage,
            **dict(metadata or {}),
        }
        if active and summary_stage == "reduce":
            return self.store.save_active_summary(
                scope_id=thread_id,
                content=content,
                source_event_start_id=source_event_start_id,
                source_event_end_id=source_event_end_id,
                metadata=summary_metadata,
                replaces=replaces,
            )
        record = self.store.save_record(
            kind=MemoryKind.SUMMARY,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=content,
            source_event_start_id=source_event_start_id,
            source_event_end_id=source_event_end_id,
            metadata=summary_metadata,
        )
        return record

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

    async def summarize_thread(self, thread_id: str) -> SummaryRunResult:
        try:
            return await self._summarize_thread_unchecked(thread_id)
        except Exception as exc:  # noqa: BLE001 - summarization is derived state
            return SummaryRunResult(created=False, error=exc.__class__.__name__)

    async def _summarize_thread_unchecked(self, thread_id: str) -> SummaryRunResult:
        policy = self.summary_policy
        if not policy.enabled:
            return SummaryRunResult(created=False)
        active_summary = self.store.active_summary(thread_id=thread_id)
        covered_end = active_summary.source_event_end_id if active_summary else None
        conversation = self._conversation_records_for_summary(
            thread_id,
            after_event_id=covered_end,
            reserve=policy.recent_message_reserve,
        )
        total_conversation = self.store.list_records(
            kind=MemoryKind.CONVERSATION,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            limit=1000,
        )
        if len(total_conversation) < policy.threshold_messages:
            return SummaryRunResult(created=False)
        if len(conversation) < policy.minimum_unsummarized_messages:
            return SummaryRunResult(created=False)
        conversation = list(reversed(conversation))
        segments = segment_messages(
            conversation,
            max_estimated_tokens=policy.map_chunk_estimated_tokens,
        )
        if not segments:
            return SummaryRunResult(created=False)
        previous = active_summary.content if active_summary else None
        partials: list[MemoryRecord] = []
        for segment in segments:
            content = await self.summarizer.summarize_map(
                segment.messages,
                previous_summary=previous,
            )
            partials.append(
                self.save_summary(
                    thread_id,
                    _clip(content, policy.max_summary_chars),
                    source_event_start_id=segment.source_event_start_id,
                    source_event_end_id=segment.source_event_end_id,
                    version=_summary_version(active_summary) + 1,
                    active=False,
                    summary_stage="map",
                    metadata={
                        "message_count": len(segment.messages),
                        "summarizer_version": getattr(
                            self.summarizer,
                            "version",
                            self.summarizer.__class__.__name__,
                        ),
                    },
                )
            )
        reduce_input = _bounded_reduce_input(
            [record.content for record in partials],
            max_estimated_tokens=policy.reduce_input_estimated_tokens,
        )
        reduce_text = await self.summarizer.summarize_reduce(
            reduce_input,
            previous_summary=previous,
        )
        new_start_id = partials[0].source_event_start_id
        new_end_id = partials[-1].source_event_end_id
        start_id = (
            active_summary.source_event_start_id
            if active_summary is not None and active_summary.source_event_start_id is not None
            else new_start_id
        )
        end_id = new_end_id
        version = _summary_version(active_summary) + 1
        summarized_messages = [message for segment in segments for message in segment.messages]
        summarized_message_count = len(summarized_messages)
        estimated_before = sum(
            estimate_text_tokens(str(message.content)) for message in summarized_messages
        )
        reduced = _clip(reduce_text, policy.max_summary_chars)
        estimated_after = estimate_text_tokens(reduced)
        summary = self.save_summary(
            thread_id,
            reduced,
            source_event_start_id=start_id,
            source_event_end_id=end_id,
            version=version,
            replaces=active_summary.id if active_summary else None,
            active=True,
            summary_stage="reduce",
            metadata={
                "map_summary_ids": [record.id for record in partials],
                "message_count": summarized_message_count,
                "estimated_tokens_before": estimated_before,
                "estimated_tokens_after": estimated_after,
                "compression_ratio": _compression_ratio(estimated_before, estimated_after),
                "summarizer_version": getattr(
                    self.summarizer,
                    "version",
                    self.summarizer.__class__.__name__,
                ),
            },
        )
        return SummaryRunResult(
            created=True,
            summary_id=summary.id,
            source_event_start_id=start_id,
            source_event_end_id=end_id,
            version=version,
            map_count=len(partials),
            message_count=summarized_message_count,
            estimated_tokens_before=estimated_before,
            estimated_tokens_after=estimated_after,
            compression_ratio=_compression_ratio(estimated_before, estimated_after),
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

    def _conversation_records_for_summary(
        self,
        thread_id: str,
        *,
        after_event_id: int | None,
        reserve: int,
    ) -> list[MemoryRecord]:
        records = self.store.list_records(
            kind=MemoryKind.CONVERSATION,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            limit=1000,
        )
        records = [
            record
            for record in records
            if after_event_id is None
            or (
                record.source_event_start_id is not None
                and record.source_event_start_id > after_event_id
            )
        ]
        if reserve <= 0:
            return records
        ordered_oldest_first = list(reversed(records))
        eligible = ordered_oldest_first[:-reserve] if len(ordered_oldest_first) > reserve else []
        return list(reversed(eligible))


def _bounded_preview(text: str, max_chars: int) -> str:
    limit = max(0, min(max_chars, DEFAULT_TOOL_DIGEST_CHARS))
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _redact(text: str) -> str:
    redacted = SECRET_PATTERN.sub("[redacted]", text)
    redacted = WINDOWS_PATH_PATTERN.sub("[redacted-path]", redacted)
    return BASE64_BLOB_PATTERN.sub("[redacted-binary]", redacted)


def estimate_text_tokens(text: str) -> int:
    from axiom.memory.context import estimate_tokens

    return estimate_tokens(text)


def _summary_version(record: MemoryRecord | None) -> int:
    if record is None:
        return 0
    try:
        return int(record.metadata.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _compression_ratio(before: int, after: int) -> float | None:
    if before <= 0:
        return None
    return round(after / before, 4)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[truncated]"


def _bounded_reduce_input(items: list[str], *, max_estimated_tokens: int) -> list[str]:
    bounded: list[str] = []
    total = 0
    for item in items:
        tokens = estimate_text_tokens(item)
        if bounded and total + tokens > max_estimated_tokens:
            break
        if tokens > max_estimated_tokens:
            bounded.append(_clip(item, max_estimated_tokens * 4))
            break
        bounded.append(item)
        total += tokens
    return bounded
