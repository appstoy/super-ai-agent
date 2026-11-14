"""Card options (pin / archive / colour), file deletion, and Markdown rendering.

These are user-owned presentation choices, so they must persist independently of
the task lifecycle and must never break the runtime records they decorate.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class EchoGateway:
    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: echo")
        return ModelResponse(text="**加粗** 和 `代码`")


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *a, **k: EchoGateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'flags.db'}",
        model_name="test-model",
        workspace_root=str(tmp_path / "ws"),
    )
    return TestClient(create_app(settings))


def _task(client: TestClient, message: str = "测试卡片选项") -> str:
    r = client.post(
        "/api/tasks",
        json={"message": message, "model": "test-model", "thread_id": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def test_flags_default_to_unset(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        task_id = _task(client)
        body = client.get(f"/api/tasks/{task_id}/result").json()
        assert body["pinned"] is False
        assert body["archived"] is False
        assert body["color"] == ""


def test_pin_and_unpin_persist(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        task_id = _task(client)

        r = client.patch(f"/api/tasks/{task_id}/flags", json={"pinned": True})
        assert r.status_code == 200
        assert r.json()["pinned"] is True
        assert client.get(f"/api/tasks/{task_id}/result").json()["pinned"] is True

        r = client.patch(f"/api/tasks/{task_id}/flags", json={"pinned": False})
        assert r.json()["pinned"] is False
        assert client.get(f"/api/tasks/{task_id}/result").json()["pinned"] is False


def test_archive_is_separate_from_task_status(tmp_path, monkeypatch) -> None:
    """Archiving is a view choice; the task itself is still succeeded."""

    with _client(tmp_path, monkeypatch) as client:
        task_id = _task(client)
        client.patch(f"/api/tasks/{task_id}/flags", json={"archived": True})

        body = client.get(f"/api/tasks/{task_id}/result").json()
        assert body["archived"] is True
        # The runtime outcome must be untouched.
        assert body["status"] == "succeeded"
        assert body["conclusion"]


def test_colour_accepts_only_known_values(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        task_id = _task(client)
        ok = client.patch(f"/api/tasks/{task_id}/flags", json={"color": "red"})
        assert ok.status_code == 200
        assert ok.json()["color"] == "red"

        bad = client.patch(f"/api/tasks/{task_id}/flags", json={"color": "chartreuse"})
        assert bad.status_code == 422

        cleared = client.patch(f"/api/tasks/{task_id}/flags", json={"color": ""})
        assert cleared.json()["color"] == ""


def test_flags_are_independent_per_task(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        a = _task(client, "任务 A")
        b = _task(client, "任务 B")
        client.patch(f"/api/tasks/{a}/flags", json={"pinned": True, "color": "blue"})

        ra = client.get(f"/api/tasks/{a}/result").json()
        rb = client.get(f"/api/tasks/{b}/result").json()
        assert ra["pinned"] is True and ra["color"] == "blue"
        assert rb["pinned"] is False and rb["color"] == ""


def test_flags_404_for_unknown_task(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.patch(f"/api/tasks/{uuid4()}/flags", json={"pinned": True})
        assert r.status_code == 404


def test_file_delete_removes_only_that_file(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        task_id = _task(client)
        short = task_id.replace("-", "")[:8]
        folder = tmp_path / "ws" / "项目缓存文件" / f"{short}-probe"
        folder.mkdir(parents=True, exist_ok=True)
        keep = folder / "keep.txt"
        gone = folder / "gone.txt"
        keep.write_text("keep", encoding="utf-8")
        gone.write_text("gone", encoding="utf-8")

        rel = f"项目缓存文件/{short}-probe/gone.txt"
        r = client.delete("/api/files", params={"path": rel})
        assert r.status_code == 200
        assert not gone.exists(), "the requested file must be gone"
        assert keep.exists(), "other files must survive"


def test_file_delete_refuses_paths_outside_workspace(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.delete("/api/files", params={"path": "../../etc/passwd"})
        assert r.status_code in {403, 404}


def test_page_ships_a_markdown_renderer_and_fold_controls(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        # Markdown support
        assert "function md(" in page
        assert "md-table" in page
        assert "md-code" in page
        # Card options
        assert "buildCardMenu" in page
        assert "buildFileMenu" in page
        assert "置顶" in page and "归档" in page and "删除" in page
        # Folding
        assert "FOLD_LIMIT" in page
        assert "fold-more" in page


def test_menu_button_does_not_overlap_the_timestamp(tmp_path, monkeypatch) -> None:
    """The floating ⋯ sits top-right, where the timestamp also wants to be.

    Measured on the real page: the button overlapped the "N 分钟前" text, making
    both unreadable. The meta row must reserve room for it.
    """

    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        assert "padding-right:30px" in page, "meta row must reserve room for the menu"
        # and the button must be anchored to that reserved corner
        assert "position:absolute;top:16px;right:12px" in page


def test_metrics_are_split_into_this_run_and_whole_thread(tmp_path, monkeypatch) -> None:
    """Two numbers with no labels were indistinguishable; both now carry one."""

    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        assert "这一次" in page
        assert "整个对话" in page
        assert "metric-label" in page
        # A user must be able to learn what a number means without docs.
        assert "命中了大模型的提示缓存" in page
        assert "每轮都会重发历史上下文" in page


def test_metric_tooltips_are_present(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/").text
        assert 'title="' in page
        # every metric span is rendered with a title attribute
        assert "esc(tip)" in page
