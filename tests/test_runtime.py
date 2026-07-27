from __future__ import annotations

from axiom.runtime import DurableTaskManager


def test_durable_task_lifecycle(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    task = manager.claim_next()
    assert task is not None
    assert task.id == task_id
    assert task.status == "running"

    manager.complete(task_id, "done")
    completed = manager.get(task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]

import json
from io import BytesIO
from typing import Any

from axiom.config import load_config
from axiom.runtime.api import RuntimeApiServer


class FakeHttpRequest:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        api_key: str | None = "test-api-key",
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
    ):
        body = raw_body if raw_body is not None else json.dumps(payload or {}).encode("utf-8")
        self.command = method
        self.path = path
        self.headers = {"content-length": str(len(body))}
        if api_key is not None:
            self.headers["x-api-key"] = api_key
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        return

    def json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _runtime_server(tmp_path) -> RuntimeApiServer:
    config = load_config(project_root=tmp_path)
    return RuntimeApiServer(cwd=str(tmp_path), config=config, api_key="test-api-key", port=0)


def _handle(server: RuntimeApiServer, request: FakeHttpRequest) -> tuple[int, dict[str, Any]]:
    server._handle(request)
    assert request.status is not None
    return request.status, request.json()


def test_runtime_api_rejects_unauthorized_request(tmp_path):
    server = _runtime_server(tmp_path)
    status, payload = _handle(
        server,
        FakeHttpRequest(method="GET", path="/v1/tasks", api_key=None),
    )

    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_runtime_api_task_create_get_complete_and_cancel(tmp_path):
    server = _runtime_server(tmp_path)

    status, created = _handle(
        server,
        FakeHttpRequest(method="POST", path="/v1/tasks", payload={"message": "do work"}),
    )
    assert status == 200
    assert created["status"] == "queued"
    task_id = created["id"]

    status, queued = _handle(server, FakeHttpRequest(method="GET", path=f"/v1/tasks/{task_id}"))
    assert status == 200
    assert queued["id"] == task_id
    assert queued["status"] == "queued"

    server.task_manager.complete(task_id, "done")
    status, completed = _handle(
        server,
        FakeHttpRequest(method="GET", path=f"/v1/tasks/{task_id}"),
    )
    assert status == 200
    assert completed["status"] == "completed"
    assert completed["result"] == "done"

    cancel_id = server.task_manager.add("cancel me")
    status, canceled = _handle(
        server,
        FakeHttpRequest(method="POST", path=f"/v1/tasks/{cancel_id}/cancel"),
    )
    assert status == 200
    assert canceled == {"canceled": True}
    assert server.task_manager.get(cancel_id).status == "canceled"  # type: ignore[union-attr]


def test_runtime_api_handles_invalid_and_malformed_requests_without_internal_leaks(tmp_path):
    server = _runtime_server(tmp_path)

    cases = [
        FakeHttpRequest(method="GET", path="/v1/tasks/missing"),
        FakeHttpRequest(method="POST", path="/v1/tasks", payload={}),
        FakeHttpRequest(method="POST", path="/v1/tasks", raw_body=b"{"),
        FakeHttpRequest(method="DELETE", path="/v1/tasks"),
    ]
    responses = [_handle(server, request) for request in cases]

    assert responses[0][0] == 404
    assert responses[0][1] == {"error": "not found"}
    assert responses[1][0] == 400
    assert responses[1][1] == {"error": "message is required"}
    assert responses[2][0] == 400
    assert responses[2][1] == {"error": "message is required"}
    assert responses[3][0] == 404
    assert responses[3][1] == {"error": "not found"}

    for _status, payload in responses:
        body = json.dumps(payload)
        assert "Traceback" not in body
        assert ".py" not in body
        assert "test-api-key" not in body
