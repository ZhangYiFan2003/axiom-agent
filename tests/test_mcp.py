from __future__ import annotations

import asyncio
import json

from axiom.config import load_config
from axiom.mcp import McpClientManager
from axiom.mcp.server import _handle_request
from axiom.tools.base import ToolContext


def test_mcp_tools_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    async def run():
        return await _handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            str(tmp_path),
        )

    response = asyncio.run(run())
    tools = response["result"]["tools"]
    assert any(tool["name"] == "read_file" for tool in tools)
    assert any(tool["name"] == "execute_command" for tool in tools)


def test_mcp_client_registers_and_calls_stdio_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")

@mcp.tool()
def echo(text: str) -> str:
    return "echo:" + text

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".axiom").mkdir()
    (tmp_path / ".axiom" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "type": "stdio",
                        "command": "python",
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def run():
        manager = McpClientManager(tmp_path)
        tools = await manager.load_tools()
        names = [tool.name for tool in tools]
        tool = next(item for item in tools if item.name == "mcp__fake__echo")
        config = load_config(project_root=tmp_path)
        config.policy.hitl_mode = "never"
        result = await tool.execute({"text": "ok"}, ToolContext(cwd=str(tmp_path), config=config))
        return names, result

    names, result = asyncio.run(run())
    assert "mcp__fake__echo" in names
    assert result.content == "echo:ok"


def test_mcp_client_suppresses_stdio_server_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    server = tmp_path / "noisy_mcp_server.py"
    server.write_text(
        """
import sys
from mcp.server.fastmcp import FastMCP

sys.stderr.write("NOISY_MCP_STARTUP\\n")
sys.stderr.flush()

mcp = FastMCP("noisy")

@mcp.tool()
def echo(text: str) -> str:
    return text

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".axiom").mkdir()
    (tmp_path / ".axiom" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "noisy": {
                        "type": "stdio",
                        "command": "python",
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def run():
        manager = McpClientManager(tmp_path)
        return await manager.load_tools()

    tools = asyncio.run(run())

    assert any(tool.name == "mcp__noisy__echo" for tool in tools)
    captured = capsys.readouterr()
    assert "NOISY_MCP_STARTUP" not in captured.err


def test_mcp_server_initialize_and_call_safe_builtin_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    note = tmp_path / "note.txt"
    note.write_text("hello\n", encoding="utf-8")

    async def run():
        initialized = await _handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            str(tmp_path),
        )
        listed = await _handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            str(tmp_path),
        )
        called = await _handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "note.txt"}},
            },
            str(tmp_path),
        )
        return initialized, listed, called

    initialized, listed, called = asyncio.run(run())

    assert initialized["result"]["serverInfo"]["name"] == "axiom"
    assert any(tool["name"] == "read_file" for tool in listed["result"]["tools"])
    assert called["id"] == 3
    assert called["result"]["isError"] is False
    assert "1: hello" in called["result"]["content"][0]["text"]


def test_mcp_server_reports_unknown_tool_and_method_with_json_rpc_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    async def run():
        unknown_tool = await _handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "missing_tool", "arguments": {}},
            },
            str(tmp_path),
        )
        unknown_method = await _handle_request(
            {"jsonrpc": "2.0", "id": 5, "method": "missing/method", "params": {}},
            str(tmp_path),
        )
        missing_method = await _handle_request({"jsonrpc": "2.0", "id": 6}, str(tmp_path))
        return unknown_tool, unknown_method, missing_method

    unknown_tool, unknown_method, missing_method = asyncio.run(run())

    for response in [unknown_tool, unknown_method, missing_method]:
        assert response["jsonrpc"] == "2.0"
        assert "error" in response
        assert isinstance(response["error"]["message"], str)

    assert 'Tool "missing_tool" not found' == unknown_tool["error"]["message"]
    assert "Unknown method: missing/method" == unknown_method["error"]["message"]
    assert "Unknown method: None" == missing_method["error"]["message"]
