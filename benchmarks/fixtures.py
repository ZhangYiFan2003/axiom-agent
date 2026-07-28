from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from axiom.config import AxiomConfig
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.tools.executor import ToolExecutor
from axiom.tools.registry import ToolRegistry

TOOL_NAMES = [f"io_task_{index}" for index in range(1, 5)]


@dataclass(slots=True)
class ToolRunFixture:
    registry: ToolRegistry
    executor: ToolExecutor
    calls: list[dict[str, Any]]
    context: ToolContext


def create_io_registry(delay_seconds: float) -> ToolRegistry:
    registry = ToolRegistry()

    for name in TOOL_NAMES:

        async def handler(
            payload: dict[str, Any],
            context: ToolContext,
            *,
            tool_name: str = name,
        ) -> ToolResult:
            _ = payload, context
            await asyncio.sleep(delay_seconds)
            return ToolResult(f"{tool_name}:ok")

        registry.register(
            Tool(
                name=name,
                description=f"Deterministic synthetic I/O task {name}.",
                parameters=object_schema({}),
                handler=handler,
                is_read_only=True,
                is_concurrency_safe=True,
            )
        )

    return registry


def create_tool_calls() -> list[dict[str, Any]]:
    return [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for index, name in enumerate(TOOL_NAMES, start=1)
    ]


def create_fixture(
    *,
    delay_seconds: float,
    max_concurrent_read: int,
    cwd: str,
) -> ToolRunFixture:
    config = AxiomConfig()
    config.tools.max_concurrent_read = max_concurrent_read
    config.policy.hitl_mode = "never"
    config.features.audit_log = False
    registry = create_io_registry(delay_seconds)
    return ToolRunFixture(
        registry=registry,
        executor=ToolExecutor(registry),
        calls=create_tool_calls(),
        context=ToolContext(cwd=cwd, config=config),
    )
