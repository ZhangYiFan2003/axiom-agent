from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from axiom.config import AxiomConfig
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.tools.executor import ToolExecutor
from axiom.tools.registry import ToolRegistry


@dataclass(slots=True)
class ActivityCounter:
    active: int = 0
    observed_max: int = 0


def _config(max_concurrent_read: int = 4) -> AxiomConfig:
    config = AxiomConfig()
    config.tools.max_concurrent_read = max_concurrent_read
    config.policy.hitl_mode = "never"
    config.features.audit_log = False
    return config


def _call(name: str, index: int = 1) -> dict[str, Any]:
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _context(tmp_path, max_concurrent_read: int = 4) -> ToolContext:
    return ToolContext(cwd=str(tmp_path), config=_config(max_concurrent_read))


def test_tool_executor_limits_read_concurrency(tmp_path):
    async def run(max_concurrent_read: int) -> ActivityCounter:
        registry = ToolRegistry()
        counter = ActivityCounter()
        lock = asyncio.Lock()

        for index in range(1, 5):

            async def handler(
                payload: dict[str, Any],
                context: ToolContext,
                *,
                tool_index: int = index,
            ) -> ToolResult:
                _ = payload, context, tool_index
                async with lock:
                    counter.active += 1
                    counter.observed_max = max(counter.observed_max, counter.active)
                await asyncio.sleep(0.02)
                async with lock:
                    counter.active -= 1
                return ToolResult("ok")

            registry.register(
                Tool(
                    name=f"read_{index}",
                    description="tracked read tool",
                    parameters=object_schema({}),
                    handler=handler,
                    is_read_only=True,
                    is_concurrency_safe=True,
                )
            )

        calls = [_call(f"read_{index}", index) for index in range(1, 5)]
        results = await ToolExecutor(registry).execute_all(
            calls,
            _context(tmp_path, max_concurrent_read),
        )
        assert [result.is_error for result in results] == [False, False, False, False]
        return counter

    one = asyncio.run(run(1))
    two = asyncio.run(run(2))
    four = asyncio.run(run(4))

    assert one.observed_max == 1
    assert 1 < two.observed_max <= 2
    assert 1 < four.observed_max <= 4


def test_write_tool_runs_after_concurrent_read_group(tmp_path):
    registry = ToolRegistry()
    state = {"active_reads": 0, "write_started_with_active_reads": -1}
    lock = asyncio.Lock()

    async def read_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = payload, context
        async with lock:
            state["active_reads"] += 1
        await asyncio.sleep(0.02)
        async with lock:
            state["active_reads"] -= 1
        return ToolResult("read ok")

    async def write_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = payload, context
        async with lock:
            state["write_started_with_active_reads"] = state["active_reads"]
        return ToolResult("write ok")

    registry.register(
        Tool(
            name="read_a",
            description="read",
            parameters=object_schema({}),
            handler=read_handler,
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        Tool(
            name="read_b",
            description="read",
            parameters=object_schema({}),
            handler=read_handler,
            is_read_only=True,
            is_concurrency_safe=True,
        )
    )
    registry.register(
        Tool(
            name="write_a",
            description="write",
            parameters=object_schema({}),
            handler=write_handler,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=False,
        )
    )

    async def run():
        return await ToolExecutor(registry).execute_all(
            [_call("read_a", 1), _call("write_a", 2), _call("read_b", 3)],
            _context(tmp_path, max_concurrent_read=2),
        )

    results = asyncio.run(run())

    assert [result.content for result in results] == ["read ok", "read ok", "write ok"]
    assert state["write_started_with_active_reads"] == 0


def test_read_only_non_concurrency_safe_tools_stay_serial(tmp_path):
    registry = ToolRegistry()
    counter = ActivityCounter()
    lock = asyncio.Lock()

    async def handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = payload, context
        async with lock:
            counter.active += 1
            counter.observed_max = max(counter.observed_max, counter.active)
        await asyncio.sleep(0.02)
        async with lock:
            counter.active -= 1
        return ToolResult("ok")

    for index in range(1, 3):
        registry.register(
            Tool(
                name=f"non_safe_{index}",
                description="read-only but not concurrency-safe",
                parameters=object_schema({}),
                handler=handler,
                is_read_only=True,
                is_concurrency_safe=False,
            )
        )

    async def run():
        return await ToolExecutor(registry).execute_all(
            [_call("non_safe_1", 1), _call("non_safe_2", 2)],
            _context(tmp_path, max_concurrent_read=4),
        )

    results = asyncio.run(run())

    assert [result.is_error for result in results] == [False, False]
    assert counter.observed_max == 1


def test_tool_timeout_is_reported_as_tool_error(tmp_path):
    registry = ToolRegistry()

    async def slow_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        _ = payload, context
        await asyncio.sleep(0.1)
        return ToolResult("too late")

    registry.register(
        Tool(
            name="slow_read",
            description="slow read",
            parameters=object_schema({}),
            handler=slow_handler,
            is_read_only=True,
            is_concurrency_safe=True,
            timeout=0.01,
        )
    )

    async def run():
        return await ToolExecutor(registry).execute_all(
            [_call("slow_read", 1)],
            _context(tmp_path, max_concurrent_read=4),
        )

    [result] = asyncio.run(run())

    assert result.is_error is True
    assert result.tool_use_id == "call_1"
    assert 'Tool "slow_read" execution error' in result.content
