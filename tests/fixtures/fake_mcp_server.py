from __future__ import annotations

import argparse
import atexit
import socket
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def _touch(path: str | None, text: str) -> None:
    if not path:
        return
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _signal_when_listening(host: str, port: int, ready_file: str | None) -> None:
    if not ready_file:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                _touch(ready_file, "ready")
                return
        except OSError:
            time.sleep(0.05)
    _touch(ready_file, "timeout")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ready-file")
    parser.add_argument("--exit-file")
    parser.add_argument("--noisy", action="store_true")
    args = parser.parse_args()

    if args.noisy:
        sys.stderr.write("NOISY_MCP_STARTUP\n")
        sys.stderr.flush()

    atexit.register(_touch, args.exit_file, "exit")

    mcp = FastMCP(
        "fake",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        log_level="ERROR",
    )

    @mcp.tool()
    def echo(text: str) -> str:
        return "echo:" + text

    if args.transport == "streamable-http":
        threading.Thread(
            target=_signal_when_listening,
            args=(args.host, args.port, args.ready_file),
            daemon=True,
        ).start()

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
