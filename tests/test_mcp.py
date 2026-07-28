from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from axiom.config import load_config
from axiom.mcp import McpClientManager
from axiom.mcp.server import _handle_request
from axiom.tools.base import ToolContext

ROOT = Path(__file__).resolve().parents[1]
FAKE_MCP_SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def _test_env() -> dict[str, str]:
    pythonpath = os.pathsep.join(
        part for part in [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")] if part
    )
    return {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": pythonpath,
    }


def _write_mcp_config(tmp_path: Path, server_name: str, spec: dict[str, object]) -> None:
    config_dir = tmp_path / ".axiom"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {server_name: spec}}),
        encoding="utf-8",
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_lines(path: Path, count: int = 1, timeout: float = 10.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) >= count:
                return lines
        time.sleep(0.05)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    raise AssertionError(f"Timed out waiting for {count} lines in {path}; got {lines!r}")


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
    exit_file = tmp_path / "stdio-exit.log"
    _write_mcp_config(
        tmp_path,
        "fake",
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-u",
                str(FAKE_MCP_SERVER),
                "--transport",
                "stdio",
                "--exit-file",
                str(exit_file),
            ],
            "cwd": str(tmp_path),
            "env": _test_env(),
            "timeout": 10,
        },
    )

    async def run():
        manager = McpClientManager(tmp_path)
        tools = await asyncio.wait_for(manager.load_tools(), timeout=10)
        names = [tool.name for tool in tools]
        tool = next(item for item in tools if item.name == "mcp__fake__echo")
        config = load_config(project_root=tmp_path)
        config.policy.hitl_mode = "never"
        result = await asyncio.wait_for(
            tool.execute({"text": "ok"}, ToolContext(cwd=str(tmp_path), config=config)),
            timeout=10,
        )
        return names, result

    names, result = asyncio.run(run())
    assert "mcp__fake__echo" in names
    assert result.content == "echo:ok"
    _wait_for_lines(exit_file, count=2)


def test_mcp_client_suppresses_stdio_server_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write_mcp_config(
        tmp_path,
        "noisy",
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-u",
                str(FAKE_MCP_SERVER),
                "--transport",
                "stdio",
                "--noisy",
            ],
            "cwd": str(tmp_path),
            "env": _test_env(),
            "timeout": 10,
        },
    )

    async def run():
        manager = McpClientManager(tmp_path)
        return await asyncio.wait_for(manager.load_tools(), timeout=10)

    tools = asyncio.run(run())

    assert any(tool.name == "mcp__noisy__echo" for tool in tools)
    captured = capsys.readouterr()
    assert "NOISY_MCP_STARTUP" not in captured.err


def test_mcp_client_streamable_http_tool_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    port = _free_local_port()
    ready_file = tmp_path / "http-ready.log"
    exit_file = tmp_path / "http-exit.log"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(FAKE_MCP_SERVER),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ready-file",
            str(ready_file),
            "--exit-file",
            str(exit_file),
        ],
        cwd=str(tmp_path),
        env={**os.environ, **_test_env()},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        ready = _wait_for_lines(ready_file)
        assert ready[-1] == "ready"
        _write_mcp_config(
            tmp_path,
            "fake_http",
            {
                "type": "streamable_http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "timeout": 10,
            },
        )

        async def run():
            manager = McpClientManager(tmp_path)
            tools = await asyncio.wait_for(manager.load_tools(), timeout=10)
            names = [tool.name for tool in tools]
            tool = next(item for item in tools if item.name == "mcp__fake_http__echo")
            config = load_config(project_root=tmp_path)
            config.policy.hitl_mode = "never"
            result = await asyncio.wait_for(
                tool.execute({"text": "ok"}, ToolContext(cwd=str(tmp_path), config=config)),
                timeout=10,
            )
            return names, result

        names, result = asyncio.run(run())
        assert "mcp__fake_http__echo" in names
        assert result.content == "echo:ok"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    assert process.poll() is not None


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

    assert unknown_tool["error"]["message"] == 'Tool "missing_tool" not found'
    assert unknown_method["error"]["message"] == "Unknown method: missing/method"
    assert missing_method["error"]["message"] == "Unknown method: None"
