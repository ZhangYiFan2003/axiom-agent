from __future__ import annotations

import asyncio
from typing import Any

from axiom.agent import QueryEngine
from axiom.config import load_config
from axiom.tools import ToolRegistry, get_builtin_tools


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
        else:
            tool_messages = [message for message in messages if message.role == "tool"]
            assert tool_messages
            assert "1: hello" in tool_messages[-1].content
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}


class ToolReplayFakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0
        self.seen_messages: list[list[Any]] = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append(list(messages))
        assert any(tool["function"]["name"] == "read_file" for tool in tools)
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                },
            }
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "function": {"arguments": '"note.txt"}'},
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return

        tool_messages = [message for message in messages if message.role == "tool"]
        assert tool_messages
        assert "1: hello" in tool_messages[-1].content
        yield {"type": "text_delta", "text": "final: "}
        yield {"type": "text_delta", "text": "read hello"}
        yield {"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 5}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def test_query_engine_executes_tool_and_replays_result(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=FakeClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> Any:
        return await engine.ask_complete_async("read note")

    result = asyncio.run(run())
    assert result.text == "done"
    assert result.turns == 2


def test_react_loop_executes_tool_observes_result_and_finishes(tmp_path):
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    client = ToolReplayFakeClient()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> list[dict[str, Any]]:
        return [event async for event in engine.ask("read note")]

    events = asyncio.run(run())
    event_types = [event["type"] for event in events]
    done = events[-1]
    messages = done["messages"]

    assert client.calls == 2
    assert event_types == [
        "usage",
        "turn_complete",
        "tool_call",
        "tool_result",
        "text_delta",
        "text_delta",
        "usage",
        "turn_complete",
        "done",
    ]
    assert events[2]["name"] == "read_file"
    assert events[2]["input"] == {"path": "note.txt"}
    assert events[3]["name"] == "read_file"
    assert "1: hello" in events[3]["result"]
    assert not events[3]["is_error"]
    assert done["total_turns"] == 2
    assert done["total_tokens"] == 14
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[-1].content == "final: read hello"
