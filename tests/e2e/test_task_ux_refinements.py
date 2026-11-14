"""Acceptance tests for user-facing UX refinements.

1. Category tabs & artifact filter
2. Detailed step traces with concrete evidence (inputs, outputs, decisions)
3. Context continuation on existing threads
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class StatefulGateway:
    """A mock gateway that responds and remembers conversation length."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        content = request.messages[0].content
        if content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: execute turn")
        self.calls.append(content)
        turn_num = len(self.calls)
        return ModelResponse(text=f"这是第 {turn_num} 轮回答。已保存上下文。")


def _client(tmp_path, monkeypatch, gateway=None) -> TestClient:
    gw = gateway or StatefulGateway()
    monkeypatch.setattr("super_agent.api.MagpieModelGateway", lambda *args, **kwargs: gw)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'ux.db'}",
        model_name="test-model",
        workspace_root=str(tmp_path / "ws"),
    )
    return TestClient(create_app(settings))


def test_page_exposes_categories_and_artifact_filter(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        # Category tabs
        assert 'data-cat="all"' in page
        assert 'data-cat="running"' in page
        assert 'data-cat="succeeded"' in page
        assert 'data-cat="failed"' in page
        assert 'data-cat="cancelled"' in page
        # Artifact filter button
        assert 'id="f-artifacts"' in page
        assert "只看产出文件" in page


def test_page_exposes_context_continuation_elements(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        # Continuation banner and cancel button
        assert 'id="thread-bar"' in page
        assert 'id="thread-cancel"' in page
        # Action button inside cards
        assert "data-continue" in page
        assert "基于此任务继续" in page


def test_steps_carry_concrete_evidence_details(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.post(
            "/api/tasks",
            json={"message": "第一轮提问", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = r.json()["task_id"]

        result = client.get(f"/api/tasks/{task_id}/result").json()
        assert result["thread_id"] is not None
        steps = result["steps"]
        assert len(steps) >= 2
        # Verify first step has summary and deep details
        step0 = steps[0]
        assert "summary" in step0 and len(step0["summary"]) > 0
        assert "details" in step0 and len(step0["details"]) >= 2
        labels = [d["label"] for d in step0["details"]]
        assert "用户原始输入" in labels
        assert "快速通道过滤" in labels


def test_thread_continuation_reuses_worker_context(tmp_path, monkeypatch) -> None:
    """Submitting with the same thread_id must continue the conversation."""

    gw = StatefulGateway()
    with _client(tmp_path, monkeypatch, gateway=gw) as client:
        thread = str(uuid4())
        # Turn 1
        r1 = client.post(
            "/api/tasks",
            json={"message": "我的名字叫小明", "model": "test-model", "thread_id": thread},
        )
        task1_id = r1.json()["task_id"]

        # Turn 2 on the SAME thread
        r2 = client.post(
            "/api/tasks",
            json={"message": "请问我叫什么名字？", "model": "test-model", "thread_id": thread},
        )
        task2_id = r2.json()["task_id"]

        res1 = client.get(f"/api/tasks/{task1_id}/result").json()
        res2 = client.get(f"/api/tasks/{task2_id}/result").json()

        assert res1["thread_id"] == thread
        assert res2["thread_id"] == thread
        # Both tasks successfully recorded
        assert res1["status"] == "succeeded"
        assert res2["status"] == "succeeded"
