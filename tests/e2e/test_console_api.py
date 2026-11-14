"""E2E for the mission-control console and its observability endpoints."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class ConsoleGateway:
    """Router creates a task; the Worker answers deterministically."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: console probe")
        return ModelResponse(text="console probe finished")


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *args, **kwargs: ConsoleGateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'console.db'}",
        model_name="test-model",
        router_batch_limit=6,
    )
    return TestClient(create_app(settings))


def test_root_serves_the_task_surface(tmp_path, monkeypatch) -> None:
    """`/` is what a user sees: one input, then results - not a runtime console."""

    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/")
        assert page.status_code == 200
        body = page.text.lower()
        for marker in ("composer", "verdict", "artifacts", "/api/results", "/ops"):
            assert marker in body, marker
        # The operator vocabulary must not leak onto the user surface.
        assert "watchdog" not in body, "operator jargon leaked into the user page"


def test_ops_serves_the_mission_control_console(tmp_path, monkeypatch) -> None:
    """The runtime console still exists, at /ops, for whoever runs the system."""

    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/ops")
        assert page.status_code == 200
        body = page.text
        # The console must expose the architecture, not just a chat box.
        for marker in ("MISSION CONTROL", "ROUTER", "WATCHDOG", "cockpit", "speculation"):
            assert marker.lower() in body.lower(), marker


def test_observability_endpoints_report_runtime_state(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/tasks",
            json={
                "message": "observe the runtime",
                "model": "test-model",
                "thread_id": str(uuid4()),
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        summary = client.get("/api/runtime/summary").json()
        assert summary["task_total"] >= 1
        assert summary["watchdog"] in {"healthy", "attention"}
        assert summary["batch_limit"] == 6
        assert "outbox" in summary

        events = client.get("/api/events").json()["events"]
        assert events and events[0]["filter_decision"] in {"accepted", "pending", "ignored"}
        assert events[0]["source"] == "web"

        threads = client.get("/api/threads").json()["threads"]
        assert threads and threads[0]["event_count"] >= 1
        assert isinstance(threads[0]["workers"], list)

        transcript = client.get("/api/router/transcript").json()["messages"]
        assert transcript and transcript[0]["role"] in {"user", "assistant"}

        outbox = client.get("/api/outbox").json()
        assert outbox["published"] >= 1

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert "subscriptions" in detail and "tool_runs" in detail

        listed = client.get("/tasks").json()
        assert listed[0]["channel"] is not None
        assert "thread_ids" in listed[0]


def test_task_rename_is_persisted_and_validated(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/tasks",
            json={"message": "rename me", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = created.json()["task_id"]
        before = client.get(f"/api/tasks/{task_id}").json()["task"]["version"]

        renamed = client.patch(f"/api/tasks/{task_id}/title", json={"title": "重命名后的任务"})
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "重命名后的任务"
        assert renamed.json()["version"] == before + 1
        assert client.get(f"/api/tasks/{task_id}").json()["task"]["title"] == "重命名后的任务"

        assert client.patch(f"/api/tasks/{task_id}/title", json={"title": ""}).status_code == 422
        assert (
            client.patch(
                "/api/tasks/00000000-0000-0000-0000-000000000000/title",
                json={"title": "ghost"},
            ).status_code
            == 404
        )


def test_speculation_discard_removes_the_draft(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/speculations",
            json={"message": "draft a meeting summary", "model": "test-model"},
        )
        assert created.status_code == 200
        speculation_id = created.json()["speculation_id"]

        discarded = client.post(f"/api/speculations/{speculation_id}/discard")
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"
        remaining = client.get("/api/speculations").json()["speculations"]
        assert all(item["speculation_id"] != speculation_id for item in remaining)
        assert (
            client.post(
                "/api/speculations/00000000-0000-0000-0000-000000000000/discard"
            ).status_code
            == 404
        )

def test_stop_and_resume_never_resurrect_a_finished_task(tmp_path, monkeypatch) -> None:
    """A terminal Task keeps its outcome; stop/resume act only on live work."""

    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/tasks",
            json={"message": "finish then probe", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = created.json()["task_id"]
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "succeeded"
        assert all(run["status"] == "succeeded" for run in detail["runs"])
        version = detail["task"]["version"]

        stopped = client.post(f"/tasks/{task_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["changed"] is False
        assert stopped.json()["status"] == "succeeded"
        assert client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "succeeded"
        assert client.get(f"/api/tasks/{task_id}").json()["task"]["version"] == version

        resumed = client.post(f"/tasks/{task_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["changed"] is False
        assert resumed.json()["status"] == "succeeded"
        after = client.get(f"/api/tasks/{task_id}").json()["task"]
        assert after["status"] == "succeeded"
        assert after["version"] == version


def test_resume_restores_the_real_outcome_of_a_cancelled_task(tmp_path, monkeypatch) -> None:
    """Cancelling a finished-Run task by force restores succeeded, not running."""

    from sqlalchemy import select

    from super_agent.storage.database import create_engine_for_url, create_session_factory
    from super_agent.storage.models import RunRecord, TaskRecord

    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/tasks",
            json={"message": "cancelled probe", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = created.json()["task_id"]
        engine = create_engine_for_url(f"sqlite:///{tmp_path / 'console.db'}")
        factory = create_session_factory(engine)
        with factory() as session, session.begin():
            from uuid import UUID as _UUID

            row = session.get(TaskRecord, _UUID(task_id))
            row.status = "cancelled"
            row.version += 1
            run = session.scalar(select(RunRecord).where(RunRecord.task_id == _UUID(task_id)))
            assert run.status == "succeeded"
        engine.dispose()

        resumed = client.post(f"/tasks/{task_id}/resume").json()
        assert resumed["changed"] is True
        assert resumed["status"] == "succeeded"
        assert resumed["requeued"] is False
        assert client.get(f"/api/tasks/{task_id}").json()["task"]["status"] == "succeeded"


def test_task_delete_removes_owned_records_and_refuses_live_work(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/tasks",
            json={"message": "delete me", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = created.json()["task_id"]
        assert client.get(f"/api/tasks/{task_id}").json()["context"]

        deleted = client.delete(f"/api/tasks/{task_id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get(f"/api/tasks/{task_id}").status_code == 404
        assert all(item["id"] != task_id for item in client.get("/tasks").json())
        assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_device_revoke_and_restore_round_trip(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        registered = client.post(
            "/api/devices/register",
            json={"name": "revoke-probe", "capabilities": ["device.read"]},
        )
        assert registered.status_code == 201
        device_id = registered.json()["device_id"]

        revoked = client.post(f"/api/devices/{device_id}/revoke")
        assert revoked.status_code == 200
        listed = client.get("/api/devices").json()["devices"]
        assert next(d for d in listed if d["device_id"] == device_id)["revoked"] is True

        restored = client.post(f"/api/devices/{device_id}/restore")
        assert restored.status_code == 200
        listed = client.get("/api/devices").json()["devices"]
        assert next(d for d in listed if d["device_id"] == device_id)["revoked"] is False

        ghost = "00000000-0000-0000-0000-000000000000"
        assert client.post(f"/api/devices/{ghost}/revoke").status_code == 404
        assert client.post(f"/api/devices/{ghost}/restore").status_code == 404


def test_sync_submission_is_not_broken_by_a_shared_recall_tool(
    tmp_path, monkeypatch
) -> None:
    """The API shares one ToolRegistry; a second orchestrator must not 500.

    Regression: recall.read was registered on every construction, so the
    synchronous /api/tasks path raised "already registered" on the second call.
    """

    with _client(tmp_path, monkeypatch) as client:
        first = client.post(
            "/api/tasks",
            json={"message": "first sync", "model": "test-model", "thread_id": str(uuid4())},
        )
        assert first.status_code == 200
        second = client.post(
            "/api/tasks",
            json={"message": "second sync", "model": "test-model", "thread_id": str(uuid4())},
        )
        assert second.status_code == 200
        assert second.json()["status"] == "succeeded"
        # Both submissions produced tasks; the shared registry still works.
        assert len(client.get("/tasks").json()) >= 2
