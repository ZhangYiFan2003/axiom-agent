from __future__ import annotations

import asyncio
from contextlib import suppress

from axiom.memory import MemoryKind, MemoryScopeType, MemoryService, SummaryPolicy


def _conversation(service: MemoryService, thread_id: str, start_event_id: int, pairs: int) -> None:
    event_id = start_event_id
    for index in range(pairs):
        service.save_conversation(
            thread_id,
            role="user",
            content=f"user goal {index}",
            event_id=event_id,
        )
        event_id += 1
        service.save_conversation(
            thread_id,
            role="assistant",
            content=f"assistant action {index}",
            event_id=event_id,
        )
        event_id += 1


def test_summary_below_threshold_does_not_create_record(tmp_path):
    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summary_policy=SummaryPolicy(threshold_messages=8),
    )
    _conversation(service, "thread-a", 1, pairs=2)

    result = asyncio.run(service.summarize_thread("thread-a"))

    assert result.created is False
    assert service.list_records(kind=MemoryKind.SUMMARY, scope_id="thread-a") == []


def test_summary_map_reduce_activation_and_provenance(tmp_path):
    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=6,
            minimum_unsummarized_messages=4,
            recent_message_reserve=2,
            map_chunk_estimated_tokens=50,
        ),
    )
    _conversation(service, "thread-a", 1, pairs=4)

    result = asyncio.run(service.summarize_thread("thread-a"))

    assert result.created is True
    assert result.version == 1
    assert result.source_event_start_id == 1
    assert result.source_event_end_id == 6
    assert result.message_count == 6
    active = service.store.active_summary(thread_id="thread-a")
    assert active is not None
    assert active.id == result.summary_id
    assert active.metadata["summary_stage"] == "reduce"
    maps = service.list_records(
        kind=MemoryKind.SUMMARY,
        scope_type=MemoryScopeType.THREAD,
        scope_id="thread-a",
        include_inactive=True,
    )
    assert any(record.metadata.get("summary_stage") == "map" for record in maps)


def test_summary_incremental_range_and_single_active_invariant(tmp_path):
    db_path = tmp_path / "memory.db"
    service = MemoryService(
        db_path,
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=4,
            minimum_unsummarized_messages=2,
            recent_message_reserve=0,
        ),
    )
    _conversation(service, "thread-a", 1, pairs=2)
    first = asyncio.run(service.summarize_thread("thread-a"))
    reloaded = MemoryService(
        db_path,
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=4,
            minimum_unsummarized_messages=2,
            recent_message_reserve=0,
        ),
    )
    _conversation(reloaded, "thread-a", 5, pairs=2)

    second = asyncio.run(reloaded.summarize_thread("thread-a"))

    summaries = reloaded.list_records(
        kind=MemoryKind.SUMMARY,
        scope_type=MemoryScopeType.THREAD,
        scope_id="thread-a",
        include_inactive=True,
        limit=20,
    )
    active = [record for record in summaries if record.metadata.get("active", True)]

    assert first.created is True
    assert second.created is True
    assert second.version == 2
    assert second.source_event_start_id == 1
    assert second.source_event_end_id == 8
    assert len([record for record in active if record.metadata["summary_stage"] == "reduce"]) == 1
    current = reloaded.store.active_summary(thread_id="thread-a")
    assert current is not None
    assert current.id == second.summary_id
    assert "Previous active summary" in current.content
    second_maps = [
        record
        for record in summaries
        if record.metadata["summary_stage"] == "map" and record.metadata["version"] == 2
    ]
    second_map_ranges = [
        (record.source_event_start_id, record.source_event_end_id) for record in second_maps
    ]
    assert second_map_ranges == [(5, 8)]


def test_active_summary_insert_failure_keeps_old_summary_active(tmp_path):
    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
    )
    old = service.save_summary(
        "thread-a",
        "old active",
        source_event_start_id=1,
        source_event_end_id=2,
        active=True,
    )

    with suppress(Exception):
        service.store.save_active_summary(
            scope_id="thread-a",
            content="new active",
            source_event_start_id=1,
            source_event_end_id=4,
            metadata={"version": 2},
            replaces=old.id,
            record_id=old.id,
        )

    active = service.store.active_summary(thread_id="thread-a")
    assert active is not None
    assert active.id == old.id


def test_summary_excludes_incomplete_and_tool_only_records(tmp_path):
    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=2,
            minimum_unsummarized_messages=2,
            recent_message_reserve=0,
        ),
    )
    service.save_conversation("thread-a", role="user", content="complete user", event_id=1)
    service.save_conversation("thread-a", role="assistant", content="complete answer", event_id=2)
    service.save_tool_result(
        "thread-a",
        tool_name="shell",
        success=True,
        content="tool only",
        source_event_id=3,
    )
    service.save_conversation("thread-a", role="user", content="pending user", event_id=4)
    service.save_summary(
        "thread-a",
        "malformed inactive noise",
        source_event_start_id=5,
        source_event_end_id=5,
        active=False,
        summary_stage="map",
    )

    result = asyncio.run(service.summarize_thread("thread-a"))
    active = service.store.active_summary(thread_id="thread-a")

    assert result.created is True
    assert result.source_event_start_id == 1
    assert result.source_event_end_id == 2
    assert result.message_count == 2
    assert active is not None
    assert "pending user" not in active.content
    assert "tool only" not in active.content


def test_summary_failure_keeps_existing_active_summary(tmp_path):
    class BrokenSummarizer:
        async def summarize_map(self, _messages, *, previous_summary=None):
            raise RuntimeError("summary failed")

        async def summarize_reduce(self, _partial_summaries, *, previous_summary=None):
            raise RuntimeError("summary failed")

    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=4,
            minimum_unsummarized_messages=2,
            recent_message_reserve=0,
        ),
    )
    _conversation(service, "thread-a", 1, pairs=2)
    first = asyncio.run(service.summarize_thread("thread-a"))
    broken = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summarizer=BrokenSummarizer(),
        summary_policy=SummaryPolicy(
            threshold_messages=4,
            minimum_unsummarized_messages=2,
            recent_message_reserve=0,
        ),
    )
    _conversation(broken, "thread-a", 5, pairs=2)

    failed = asyncio.run(broken.summarize_thread("thread-a"))

    active = broken.store.active_summary(thread_id="thread-a")
    assert first.summary_id is not None
    assert failed.created is False
    assert failed.error == "RuntimeError"
    assert active is not None
    assert active.id == first.summary_id


def test_context_uses_summary_and_skips_covered_raw_conversation(tmp_path):
    service = MemoryService(
        tmp_path / "memory.db",
        project_scope="project-a",
        summary_policy=SummaryPolicy(
            threshold_messages=6,
            minimum_unsummarized_messages=4,
            recent_message_reserve=2,
        ),
    )
    _conversation(service, "thread-a", 1, pairs=4)
    asyncio.run(service.summarize_thread("thread-a"))

    result = service.build_context(thread_id="thread-a", max_context_chars=2_000)
    serialized = "".join(section.content for section in result.sections)
    raw_conversation = next(
        section.content for section in result.sections if section.name == "recent conversation"
    )

    assert result.summary_used is True
    assert result.summary_source_end_id == 6
    assert result.raw_messages_skipped_by_summary == 6
    assert result.raw_messages_included == 2
    assert "user goal 0" not in raw_conversation
    assert "assistant action 0" not in raw_conversation
    assert "user goal 3" in serialized
    assert "assistant action 3" in serialized
    assert result.compression_ratio is not None
