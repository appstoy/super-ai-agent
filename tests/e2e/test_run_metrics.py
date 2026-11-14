"""Execution telemetry and continued-thread context behaviour.

Two questions this pins down:
1. Does a follow-up turn on the same thread actually resend prior context, and
   does the task report its cost (tokens, cache hits, rounds)?
2. Does a failed turn poison the thread - i.e. does the model's own "I cannot
   write files" excuse get replayed into the next attempt?
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.application.orchestrator import _is_self_defeating_answer
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse, ModelUsage


class MeteredGateway:
    """Router routes; the Worker reports token usage like a real provider."""

    def __init__(self) -> None:
        self.seen_prompts: list[str] = []
        # Flipped by a test to simulate a provider that now serves from cache.
        self.warm = False

    def complete(self, request: ModelRequest) -> ModelResponse:
        first = request.messages[0].content
        if first.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: metered turn")
        # Record what the Worker actually sent so the test can inspect context.
        self.seen_prompts.append("\n".join(m.content for m in request.messages))
        turn = len(self.seen_prompts)
        # A task may make several model calls; this gateway reports a cold cache
        # until the test flips the flag, so "cold" means what the test intends
        # rather than "however many calls happened to occur".
        usage = ModelUsage(
            prompt_tokens=1000,
            completion_tokens=20,
            total_tokens=1020,
            cached_tokens=800 if self.warm else 0,
        )
        return ModelResponse(text=f"turn {turn} done", usage=usage)


class GivingUpGateway(MeteredGateway):
    """First turn narrates its own limits instead of doing the work."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        first = request.messages[0].content
        if first.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: write something")
        self.seen_prompts.append("\n".join(m.content for m in request.messages))
        return ModelResponse(
            text="我没有写文件的能力，请把代码块内容保存为 out.html",
            usage=ModelUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        )


def _client(tmp_path, monkeypatch, gateway) -> TestClient:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *a, **k: gateway
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'metrics.db'}",
        model_name="test-model",
        workspace_root=str(tmp_path / "ws"),
        worker_max_rounds=2,
    )
    return TestClient(create_app(settings))


def test_result_reports_tokens_rounds_and_cache_hits(tmp_path, monkeypatch) -> None:
    gw = MeteredGateway()
    with _client(tmp_path, monkeypatch, gw) as client:
        r = client.post(
            "/api/tasks",
            json={"message": "第一轮", "model": "test-model", "thread_id": str(uuid4())},
        )
        task_id = r.json()["task_id"]
        body = client.get(f"/api/tasks/{task_id}/result").json()

        metrics = body["metrics"]
        assert metrics["model_rounds"] >= 1
        assert metrics["prompt_tokens"] > 0
        assert metrics["completion_tokens"] > 0
        # A cold run has no cache to hit.
        assert metrics["cached_tokens"] == 0
        assert metrics["cache_hit_rate"] == 0.0
        # Every field the console renders must be present.
        for key in ("duration_seconds", "tool_calls", "cache_hit_rate"):
            assert key in metrics


def test_continued_thread_resends_context_and_hits_cache(tmp_path, monkeypatch) -> None:
    """The whole point of continuing: prior context is sent, cache is reused."""

    gw = MeteredGateway()
    with _client(tmp_path, monkeypatch, gw) as client:
        thread = str(uuid4())
        first = client.post(
            "/api/tasks",
            json={"message": "我的名字叫小明", "model": "test-model", "thread_id": thread},
        ).json()["task_id"]

        # The provider now serves this thread's prefix from cache.
        gw.warm = True
        second = client.post(
            "/api/tasks",
            json={"message": "我叫什么名字", "model": "test-model", "thread_id": thread},
        ).json()["task_id"]

        first_body = client.get(f"/api/tasks/{first}/result").json()
        second_body = client.get(f"/api/tasks/{second}/result").json()

        assert first_body["thread_id"] == thread
        assert second_body["thread_id"] == thread

        # The second prompt must contain the first turn's words.
        assert len(gw.seen_prompts) >= 2
        second_prompt = gw.seen_prompts[-1]
        assert "小明" in second_prompt, "prior context was not resent"

        # And the follow-up reports its cache reuse.
        assert second_body["metrics"]["cached_tokens"] > 0
        assert second_body["metrics"]["cache_hit_rate"] > 0.5


def test_a_refusal_is_not_replayed_as_history() -> None:
    """A give-up turn must not teach the next turn to give up too."""

    assert _is_self_defeating_answer(
        "assistant", "我没有写文件的能力，请把代码块内容保存为 out.html"
    )
    assert _is_self_defeating_answer(
        "assistant", "I don't have the ability to write files, please save it."
    )
    # Real progress and other roles are never filtered.
    assert not _is_self_defeating_answer("assistant", "文件已写入 grid.html")
    assert not _is_self_defeating_answer("user", "我没有写文件的能力")
    assert not _is_self_defeating_answer("tool", "created out.html (100 bytes)")


def test_failed_task_still_continues_without_the_excuse(tmp_path, monkeypatch) -> None:
    gw = GivingUpGateway()
    with _client(tmp_path, monkeypatch, gw) as client:
        thread = str(uuid4())
        client.post(
            "/api/tasks",
            json={"message": "做个网页", "model": "test-model", "thread_id": thread},
        )
        client.post(
            "/api/tasks",
            json={"message": "再试一次", "model": "test-model", "thread_id": thread},
        )
        # The follow-up prompt must not contain the previous refusal text.
        assert len(gw.seen_prompts) >= 2
        follow_up = gw.seen_prompts[-1]
        assert "我没有写文件的能力" not in follow_up


def test_thread_totals_include_every_attempt(tmp_path, monkeypatch) -> None:
    """A retried task cost every attempt; reporting one would understate it.

    Measured on a real thread that failed twice and succeeded on the third try:
    the final attempt alone showed 4 rounds, while the thread had spent 29.
    """

    gw = MeteredGateway()
    with _client(tmp_path, monkeypatch, gw) as client:
        thread = str(uuid4())
        ids = []
        for turn in range(3):
            ids.append(
                client.post(
                    "/api/tasks",
                    json={
                        "message": f"turn {turn}",
                        "model": "test-model",
                        "thread_id": thread,
                    },
                ).json()["task_id"]
            )
        body = client.get(f"/api/tasks/{ids[-1]}/result").json()
        m = body["metrics"]

        # Thread totals must cover everything this thread spent.
        assert m["thread_attempts"] >= 1
        assert m["thread_model_rounds"] >= m["model_rounds"]
        assert m["thread_prompt_tokens"] >= m["prompt_tokens"]
        # And they must be reported, not left at zero.
        assert m["thread_prompt_tokens"] > 0


def test_thread_metrics_never_report_zero_for_work_that_happened(
    tmp_path, monkeypatch
) -> None:
    """The bug this guards: a live/empty newest run reported 0 for a real task."""

    gw = MeteredGateway()
    with _client(tmp_path, monkeypatch, gw) as client:
        task_id = client.post(
            "/api/tasks",
            json={
                "message": "work that costs tokens",
                "model": "test-model",
                "thread_id": str(uuid4()),
            },
        ).json()["task_id"]
        m = client.get(f"/api/tasks/{task_id}/result").json()["metrics"]
        assert m["thread_prompt_tokens"] > 0, "thread total must reflect real spend"
        assert m["thread_model_rounds"] > 0
