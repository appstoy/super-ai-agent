import hashlib
import hmac
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class FakeGateway:
    """Small deterministic gateway that exercises the Router and Worker turns."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: async control task")
        return ModelResponse(text="async control task completed")


class ClarifyGateway:
    def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(text="CLARIFY: 请补充范围")


def test_async_control_api_reports_durable_completion(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("super_agent.api.MagpieModelGateway", lambda *args, **kwargs: FakeGateway())
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'async-control.db'}",
        model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        watchdog = client.get("/api/runtime/watchdog")
        assert watchdog.status_code == 200
        assert watchdog.json()["status"] == "healthy"
        response = client.post(
            "/api/tasks/async",
            json={"message": "run asynchronously", "thread_id": str(uuid4())},
        )
        assert response.status_code == 202
        dedupe_key = response.json()["dedupe_key"]

        for _ in range(40):
            status = client.get("/api/tasks/status", params={"dedupe_key": dedupe_key}).json()
            if status["status"] == "succeeded":
                break
        else:
            raise AssertionError(f"background task did not complete: {status}")

        assert status["text"] == "async control task completed"
        assert status["task_status"] == "succeeded"


def test_async_control_api_reports_router_clarification(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *args, **kwargs: ClarifyGateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'async-clarify.db'}",
        model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/tasks/async", json={"message": "ambiguous"})
        dedupe_key = response.json()["dedupe_key"]
        for _ in range(40):
            status = client.get("/api/tasks/status", params={"dedupe_key": dedupe_key}).json()
            if status["status"] == "completed":
                break
        else:
            raise AssertionError(f"clarification did not complete: {status}")
        assert status["text"] == "请补充范围"


def test_task_stream_emits_progress_and_done_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("super_agent.api.MagpieModelGateway", lambda *args, **kwargs: FakeGateway())
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'stream.db'}",
        model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post("/api/tasks", json={"message": "stream me"}).json()
        response = client.get(f"/api/tasks/{created['task_id']}/stream")
        assert response.status_code == 200
        assert "event: progress" in response.text
        assert "event: done" in response.text


def test_signed_webhook_enters_the_same_background_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("super_agent.api.MagpieModelGateway", lambda *args, **kwargs: FakeGateway())
    secret = "webhook-secret"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'webhook.db'}",
        model_name="test-model",
        webhook_secret=secret,
    )
    canonical = {
        "message": "webhook task",
        "model": "test-model",
        "thread_id": None,
        "actor": "webhook",
        "dedupe_key": None,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    with TestClient(create_app(settings)) as client:
        assert client.post(
            "/api/webhooks/github",
            json={"message": "webhook task", "model": "test-model"},
            headers={"X-Super-Agent-Signature": "sha256=bad"},
        ).status_code == 401
        response = client.post(
            "/api/webhooks/github",
            json={"message": "webhook task", "model": "test-model"},
            headers={"X-Super-Agent-Signature": f"sha256={signature}"},
        )
        assert response.status_code == 202
        assert response.json()["source"] == "github"
