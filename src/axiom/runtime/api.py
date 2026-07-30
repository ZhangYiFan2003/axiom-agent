from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from axiom.agent import QueryEngine
from axiom.bootstrap import build_tool_registry
from axiom.config import AxiomConfig
from axiom.llm import create_llm_client
from axiom.memory import MemoryService, SummaryPolicy
from axiom.runtime.tasks import DurableTaskManager
from axiom.types import Message


@dataclass(slots=True)
class RuntimeEvent:
    id: int
    thread_id: str
    type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class RuntimeTurnContext:
    thread_id: str | None
    message: str
    history: list[Message]
    cwd: str
    config: AxiomConfig


EngineFactory = Callable[[RuntimeTurnContext], Any]
ToolRegistryFactory = Callable[[AxiomConfig, str], Any]


class ThreadEventRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create_thread(self) -> str:
        thread_id = f"thread_{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "insert into threads(id, created_at) values (?, ?)",
                (thread_id, now),
            )
        self.append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def thread_exists(self, thread_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from threads where id = ?", (thread_id,)).fetchone()
        return row is not None

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int:
        if not self.thread_exists(thread_id):
            raise ValueError("thread not found")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into events(thread_id, type, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    thread_id,
                    event_type,
                    json.dumps(_jsonable(payload), ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, thread_id: str, after_id: int | None = None) -> list[RuntimeEvent]:
        if not self.thread_exists(thread_id):
            return []
        clause = "thread_id = ?"
        params: list[object] = [thread_id]
        if after_id is not None:
            clause += " and id > ?"
            params.append(after_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, thread_id, type, payload, created_at
                from events
                where {clause}
                order by id
                """,
                tuple(params),
            ).fetchall()
        return [
            RuntimeEvent(
                id=int(row[0]),
                thread_id=str(row[1]),
                type=str(row[2]),
                payload=_decode_payload(str(row[3])),
                created_at=str(row[4]),
            )
            for row in rows
        ]

    def database_ok(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("select 1").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists threads (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null references threads(id) on delete cascade,
                    type text not null,
                    payload text not null,
                    created_at text not null
                )
                """
            )
            conn.execute("create index if not exists idx_events_thread_id on events(thread_id, id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma foreign_keys = on")
        return conn


class RuntimeApiServer:
    def __init__(
        self,
        *,
        cwd: str,
        config: AxiomConfig,
        api_key: str,
        port: int = 8080,
        workers: int = 2,
        data_dir: str | Path | None = None,
        task_manager: DurableTaskManager | None = None,
        engine_factory: EngineFactory | None = None,
        tool_registry_factory: ToolRegistryFactory | None = None,
        memory_service: MemoryService | None = None,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self.api_key = api_key
        self.port = port
        self.workers = workers
        self.data_dir = (
            Path(data_dir).expanduser() if data_dir else Path.home() / ".axiom" / "runtime"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repository = ThreadEventRepository(self.data_dir / "runtime.db")
        self.task_manager = task_manager or DurableTaskManager(self.data_dir / "tasks.db")
        self.memory_service = memory_service or MemoryService(
            self.data_dir / "memory.db",
            project_scope=self.cwd,
            summary_policy=_summary_policy_from_config(config),
        )
        self.engine_factory = engine_factory
        self.tool_registry_factory = tool_registry_factory or build_tool_registry
        self._stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._worker_threads: list[threading.Thread] = []
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_locks_guard = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return ("127.0.0.1", self.port)
        host, port = self._httpd.server_address[:2]
        return (str(host), int(port))

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._stop.clear()
        self._start_workers()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="axiom-runtime-api",
            daemon=False,
        )
        self._server_thread.start()
        self.port = self.address[1]

    def serve_forever(self) -> None:
        self._stop.clear()
        self._start_workers()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self.port = self.address[1]
        print(f"Axiom Runtime API listening on http://127.0.0.1:{self.port}", flush=True)
        try:
            self._httpd.serve_forever()
        finally:
            self._stop.set()
            if self._httpd is not None:
                self._httpd.server_close()
            for worker in self._worker_threads:
                if worker.is_alive():
                    worker.join(timeout=5)
            self._httpd = None
            self._worker_threads = []

    def shutdown(self) -> None:
        self._stop.set()
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        for worker in self._worker_threads:
            if worker.is_alive():
                worker.join(timeout=5)
        self._httpd = None
        self._server_thread = None
        self._worker_threads = []

    def running(self):
        return _RunningRuntimeServer(self)

    def _start_workers(self) -> None:
        if self._worker_threads:
            return
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"axiom-task-{index}",
                daemon=False,
            )
            thread.start()
            self._worker_threads.append(thread)

    def _handler_class(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        return Handler

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(request.path)
        method = request.command
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/health":
            _send_json(
                request,
                200,
                {
                    "status": "ok",
                    "workers": self.workers,
                    "database": "ok" if self.repository.database_ok() else "error",
                },
            )
            return
        if not self._authorized(request):
            _send_json(request, 401, {"error": "unauthorized"})
            return
        try:
            body = _read_json(request)
            if method == "POST" and path == "/v1/threads":
                thread_id = self.repository.create_thread()
                _send_json(request, 200, {"id": thread_id})
            elif method == "POST" and path.startswith("/v1/threads/") and path.endswith("/turns"):
                thread_id = path.split("/")[3]
                if not self.repository.thread_exists(thread_id):
                    _send_json(request, 404, {"error": "thread not found"})
                    return
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                lock = self._thread_lock(thread_id)
                if not lock.acquire(blocking=False):
                    _send_json(request, 409, {"error": "thread turn already running"})
                    return
                try:
                    result = asyncio.run(self._run_turn(thread_id, message))
                    _send_json(request, 200, result)
                finally:
                    lock.release()
            elif method == "GET" and path.startswith("/v1/threads/") and path.endswith("/events"):
                thread_id = path.split("/")[3]
                after_id = _first_int(query.get("after_id"))
                if not self.repository.thread_exists(thread_id):
                    _send_json(request, 404, {"error": "thread not found"})
                    return
                self._send_events(request, thread_id, after_id=after_id)
            elif method == "POST" and path == "/v1/tasks":
                prompt = str(body.get("message") or body.get("prompt") or "")
                if not prompt:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                task_id = self.task_manager.add(prompt)
                _send_json(request, 200, {"id": task_id, "status": "queued"})
            elif method == "GET" and path == "/v1/tasks":
                _send_json(request, 200, {"tasks": [asdict(t) for t in self.task_manager.list()]})
            elif method == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload = asdict(task) if task else {"error": "not found"}
                _send_json(request, 200 if task else 404, payload)
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]
                _send_json(request, 200, {"canceled": self.task_manager.cancel(task_id)})
            else:
                _send_json(request, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": _safe_error(exc)})

    async def _run_turn(self, thread_id: str, message: str) -> dict[str, Any]:
        history_events = self.repository.list_events(thread_id)
        history = self.memory_service.history_from_runtime_events(history_events)
        context = RuntimeTurnContext(
            thread_id=thread_id,
            message=message,
            history=history,
            cwd=self.cwd,
            config=self.config,
        )
        self.repository.append_event(thread_id, "turn.started", {"message_chars": len(message)})
        user_event_id = self.repository.append_event(thread_id, "user.message", {"text": message})
        self._derive_conversation_memory(
            thread_id,
            role="user",
            content=message,
            event_id=user_event_id,
        )
        engine = await self._engine(context)
        text = ""
        done_payload: dict[str, Any] = {}
        try:
            async for event in _ask_events(engine, message, history):
                event_type = str(event.get("type"))
                if event_type == "text_delta":
                    delta = str(event.get("text") or "")
                    text += delta
                elif event_type == "tool_call":
                    self.repository.append_event(thread_id, "tool_call", _jsonable(event))
                elif event_type == "tool_result":
                    event_id = self.repository.append_event(
                        thread_id,
                        "tool_result",
                        _jsonable(event),
                    )
                    self._derive_tool_result_memory(
                        thread_id,
                        tool_name=str(event.get("name") or "unknown"),
                        success=not bool(event.get("error")),
                        content=_tool_result_content(event),
                        source_event_id=event_id,
                    )
                elif event_type == "error":
                    self.repository.append_event(thread_id, "error", _jsonable(event))
                elif event_type == "done":
                    done_payload = _jsonable(event)
        except Exception as exc:
            self.repository.append_event(thread_id, "error", {"error": _safe_error(exc)})
            raise
        assistant_event_id = self.repository.append_event(
            thread_id,
            "assistant.message",
            {"text": text},
        )
        self._derive_conversation_memory(
            thread_id,
            role="assistant",
            content=text,
            event_id=assistant_event_id,
        )
        self.repository.append_event(thread_id, "turn.completed", done_payload)
        await self._summarize_thread_best_effort(thread_id)
        return {"thread_id": thread_id, "text": text}

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.task_manager.claim_next()
            if not task:
                self._stop.wait(0.05)
                continue
            try:
                result = asyncio.run(self._run_task(task.prompt))
                self.task_manager.complete(task.id, result)
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(task.id, _safe_error(exc))

    async def _run_task(self, prompt: str) -> str:
        context = RuntimeTurnContext(
            thread_id=None,
            message=prompt,
            history=[],
            cwd=self.cwd,
            config=self.config,
        )
        engine = await self._engine(context)
        return (await engine.ask_complete_async(prompt)).text

    async def _engine(self, context: RuntimeTurnContext) -> Any:
        if self.engine_factory is not None:
            engine = self.engine_factory(context)
            if inspect.isawaitable(engine):
                return await engine
            return engine
        self._ensure_llm_key()
        registry_result = self.tool_registry_factory(config=self.config, cwd=self.cwd)
        if inspect.isawaitable(registry_result):
            registry, _manager = await registry_result
        else:
            registry, _manager = registry_result
        return QueryEngine(
            llm_client=create_llm_client(self.config.llm),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
        )

    def _ensure_llm_key(self) -> None:
        if not self.config.llm.api_key:
            raise ValueError(
                "AXIOM_API_KEY is not configured. Runtime turns/tasks need a working LLM key."
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key", "")
        return auth == f"Bearer {self.api_key}" or token == self.api_key

    def _create_thread(self) -> str:
        return self.repository.create_thread()

    def _append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.repository.append_event(thread_id, event_type, payload)

    def _thread_lock(self, thread_id: str) -> threading.Lock:
        with self._thread_locks_guard:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[thread_id] = lock
            return lock

    def _derive_conversation_memory(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        event_id: int,
    ) -> None:
        try:
            self.memory_service.save_conversation(
                thread_id,
                role=role,
                content=content,
                event_id=event_id,
            )
        except Exception:
            return

    def _derive_tool_result_memory(
        self,
        thread_id: str,
        *,
        tool_name: str,
        success: bool,
        content: str,
        source_event_id: int,
    ) -> None:
        try:
            self.memory_service.save_tool_result(
                thread_id,
                tool_name=tool_name,
                success=success,
                content=content,
                source_event_id=source_event_id,
            )
        except Exception:
            return

    async def _summarize_thread_best_effort(self, thread_id: str) -> None:
        summarize = getattr(self.memory_service, "summarize_thread", None)
        if summarize is None:
            return
        try:
            result = summarize(thread_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

    def _send_events(
        self,
        request: BaseHTTPRequestHandler,
        thread_id: str,
        *,
        after_id: int | None = None,
    ) -> None:
        rows = self.repository.list_events(thread_id, after_id=after_id)
        body = "".join(
            f"id: {event.id}\nevent: {event.type}\ndata: "
            f"{json.dumps(event.payload, ensure_ascii=False)}\n\n"
            for event in rows
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _ensure_schema(self) -> None:
        self.repository._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return self.repository._connect()


class _RunningRuntimeServer:
    def __init__(self, server: RuntimeApiServer):
        self.server = server

    def __enter__(self) -> RuntimeApiServer:
        self.server.start()
        return self.server

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.server.shutdown()


def _read_json(request: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(request.headers.get("content-length") or 0)
    if length == 0:
        return {}
    try:
        value = json.loads(request.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _send_json(request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.send_response(status)
    request.send_header("content-type", "application/json")
    request.send_header("content-length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _jsonable(event: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in event.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def _decode_payload(payload: str) -> dict[str, Any]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _first_int(values: list[str] | None) -> int | None:
    if not values:
        return None
    try:
        value = int(values[0])
    except ValueError:
        return None
    return value if value >= 0 else None


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for secret in ["AXIOM_RUNTIME_API_KEY", "Authorization", "Bearer"]:
        text = text.replace(secret, "[redacted]")
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _ask_events(engine: Any, message: str, history: list[Message]):
    method = engine.ask
    signature = inspect.signature(method)
    accepts_history = "history" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_history:
        async for event in method(message, history=history):
            yield event
        return
    async for event in method(message):
        yield event


def _tool_result_content(event: dict[str, Any]) -> str:
    for key in ["content", "result", "output", "text"]:
        value = event.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(_jsonable(event), ensure_ascii=False)


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("AXIOM_RUNTIME_API_KEY")
    if not key:
        raise ValueError("AXIOM_RUNTIME_API_KEY is required for Runtime API")
    return key


def _summary_policy_from_config(config: AxiomConfig) -> SummaryPolicy:
    return SummaryPolicy(
        enabled=config.features.context_compression,
        threshold_messages=config.memory.summary_threshold_messages,
        map_chunk_estimated_tokens=config.memory.summary_map_chunk_estimated_tokens,
        reduce_input_estimated_tokens=config.memory.summary_reduce_input_estimated_tokens,
        minimum_unsummarized_messages=config.memory.summary_minimum_unsummarized_messages,
        recent_message_reserve=config.memory.summary_recent_message_reserve,
        max_summary_chars=config.memory.summary_max_chars,
        max_attempts=config.memory.summary_max_attempts,
    )
