"""Acceptance tests for the user-facing task result contract.

A user asks for one thing and wants one answer back: did it work, what came out
of it, and where can I see it. These tests pin that contract so the console can
render a result without ever learning about Runs, Workers or events.
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class PlainGateway:
    """Router creates a task; the Worker answers with a conclusion plus a table."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: write the page")
        return ModelResponse(
            text="做好了，页面已经写完。\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        )


class FailingGateway:
    """Router routes, then the Worker hits a provider error."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: break on purpose")
        raise RuntimeError("provider exploded")


def _client(tmp_path, monkeypatch, gateway=PlainGateway) -> TestClient:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *args, **kwargs: gateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'results.db'}",
        model_name="test-model",
        router_batch_limit=4,
        workspace_root=str(tmp_path / "workspace"),
    )
    return TestClient(create_app(settings))


def _submit(client: TestClient, message: str) -> str:
    created = client.post(
        "/api/tasks",
        json={"message": message, "model": "test-model", "thread_id": str(uuid4())},
    )
    assert created.status_code == 200, created.text
    task_id = created.json().get("task_id")
    assert task_id, created.text
    return task_id


def test_result_reports_the_answer_and_not_the_machinery(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        task_id = _submit(client, "写个页面")

        res = client.get(f"/api/tasks/{task_id}/result")
        assert res.status_code == 200
        body = res.json()

        # The verdict is the model's leading sentence, not the table below it.
        assert body["status"] == "succeeded"
        assert body["state"]["tone"] == "ok"
        assert body["conclusion"] == "做好了，页面已经写完。"
        assert "|" not in body["conclusion"]
        # The user must recognise their own words, not the Router's title.
        assert body["ask"] == "写个页面"
        assert body["error"] is None
        # A successful run must be explainable in plain steps.
        assert len(body["steps"]) >= 2
        assert "初筛" in body["steps"][0]["text"] or "收到任务" in body["steps"][0]["text"]
        assert body["steps"][0]["summary"]
        assert body.get("thread_id") is not None
        # Nothing internal leaks through the projection.
        flat = json.dumps(body)
        for internal in ("worker_id", "run_id", "output_ref", "trigger"):
            assert internal not in flat


def test_result_exposes_artifacts_with_a_preview_url(tmp_path, monkeypatch) -> None:
    """A file the task wrote must be reachable from the result alone."""

    with _client(tmp_path, monkeypatch) as client:
        task_id = _submit(client, "写个文件")

        # Stand in for the Worker's write tool: put a file in the task folder.
        short = task_id.replace("-", "")[:8]
        folder = tmp_path / "workspace" / "项目缓存文件" / f"{short}-probe"
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / "index.html"
        page.write_text("<h1>hi</h1>", encoding="utf-8")

        body = client.get(f"/api/tasks/{task_id}/result").json()
        names = [a["name"] for a in body["artifacts"]]
        assert "index.html" in names
        artifact = next(a for a in body["artifacts"] if a["name"] == "index.html")
        assert artifact["previewable"] is True
        assert artifact["size"] == len("<h1>hi</h1>")
        assert body["open_url"] and body["open_url"].startswith("/files/")
        # The URL the result advertises must actually serve the file.
        served = client.get(body["open_url"])
        assert served.status_code == 200
        assert "hi" in served.text


def test_result_reports_failure_in_plain_words(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, FailingGateway) as client:
        created = client.post(
            "/api/tasks",
            json={
                "message": "故意失败",
                "model": "test-model",
                "thread_id": str(uuid4()),
            },
        )
        # A provider error is an outcome, not a 500.
        assert created.status_code in {200, 502}
        if created.status_code != 200:
            return
        body = client.get(f"/api/tasks/{created.json()['task_id']}/result").json()
        assert body["status"] in {"failed", "timed_out"}
        assert body["state"]["tone"] == "error"
        assert body["error"], "a failure must say why"


def test_result_is_404_for_an_unknown_task(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.get(f"/api/tasks/{uuid4()}/result").status_code == 404


def test_results_feed_returns_newest_first(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        ids = [_submit(client, f"任务 {index}") for index in range(3)]

        feed = client.get("/api/results?limit=10")
        assert feed.status_code == 200
        data = feed.json()
        assert data["count"] == len(data["items"])
        assert len(data["items"]) >= len(ids)
        for item in data["items"]:
            assert set(item) >= {"task_id", "state", "conclusion", "artifacts", "steps"}
            assert isinstance(item["steps"], list)
            assert isinstance(item["conclusion"], str)
        seen = [item["task_id"] for item in data["items"] if item["task_id"] in ids]
        assert seen == list(reversed(ids)), "the feed must be newest-first"


def test_all_artifacts_endpoint_aggregates_files(tmp_path, monkeypatch) -> None:
    """GET /api/artifacts must group generated files for the shelf."""

    with _client(tmp_path, monkeypatch) as client:
        task_id = _submit(client, "生成用于产物架的文件")

        # Fake an artifact in the task folder
        short = task_id.replace("-", "")[:8]
        folder = tmp_path / "workspace" / "项目缓存文件" / f"{short}-probe"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "shelf_item.html").write_text("<h2>shelf</h2>", encoding="utf-8")

        res = client.get("/api/artifacts")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] >= 1
        names = [a["name"] for a in data["artifacts"]]
        assert "shelf_item.html" in names
        item = next(a for a in data["artifacts"] if a["name"] == "shelf_item.html")
        assert item["previewable"] is True
        assert item["open_url"] is not None


def test_result_projection_survives_a_stopped_task(tmp_path, monkeypatch) -> None:
    """A stopped task still has to explain itself without a delivery row."""

    with _client(tmp_path, monkeypatch) as client:
        task_id = _submit(client, "会被停掉")
        client.post(f"/tasks/{task_id}/stop")
        body = client.get(f"/api/tasks/{task_id}/result").json()
        assert body["status"] in {"succeeded", "cancelled", "failed", "timed_out"}
        assert body["conclusion"]
        json.dumps(body)
