"""E2E for the §2.1 batch drain, §3.4 auto-speculation hook, and runtime metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.channels.memory import CliChannelAdapter
from super_agent.api import create_app
from super_agent.application.background import DurableOutboxWorker
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.delivery.service import DeliveryService
from super_agent.ingress.schemas import TriggerInput
from super_agent.policies.speculative_runner import SpeculativeRunner
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import OutboxMessage, TaskRecord


class BatchGateway:
    """Router answers batched and single routing contracts; Worker finishes."""

    def __init__(self) -> None:
        self.router_calls = 0
        self.batch_sizes: list[int] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = request.messages[0].content
        user = request.messages[-1].content
        if system.startswith("Return exactly one route") or "backlogged triggers" in system:
            self.router_calls += 1
            if "backlogged triggers" in user:
                lines = [
                    line
                    for line in user.splitlines()
                    if line.partition(": ")[0].strip().isdigit() and line.partition(": ")[2]
                ]
                self.batch_sizes.append(len(lines))
                return ModelResponse(
                    text="\n".join(
                        f"{line.partition(': ')[0].strip()}: TASK: batched work"
                        for line in lines
                    )
                )
            self.batch_sizes.append(1)
            return ModelResponse(text="TASK: single work")
        return ModelResponse(text="batched work done")


def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'batch.db'}")
    create_schema(engine)
    return create_session_factory(engine)


def make_trigger(message: str, dedupe: str) -> TriggerInput:
    return TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": message},
        occurred_at=datetime.now(UTC),
        dedupe_key=dedupe,
    )


def test_process_batch_merges_cold_start_router_calls(tmp_path) -> None:
    factory = make_factory(tmp_path)
    gateway = BatchGateway()
    with session_scope(factory) as session:
        for index in range(4):
            AgentOrchestrator(session, gateway).submit_via_outbox(
                make_trigger(f"cold request {index}", f"batch-cold-{index}")
            )
    # Four cold tasks already ran; now queue four more triggers without running
    # them, then drain the backlog through the batch path.
    with session_scope(factory) as session:
        from super_agent.ingress.service import TriggerIngressService

        for index in range(4):
            TriggerIngressService(session).ingest(
                make_trigger(f"backlog request {index}", f"batch-backlog-{index}")
            )
    worker = DurableOutboxWorker(factory, lambda _model: gateway, batch_limit=8)
    assert worker.process_batch() is True
    # submit_via_outbox consumed the first four events; the batch drain
    # merges exactly the four backlogged cold-start triggers into one call.
    assert gateway.batch_sizes[-1] == 4, gateway.batch_sizes
    assert worker.router_metrics["cold_start_batch"] == 4
    with session_scope(factory) as session:
        tasks = session.query(TaskRecord).all()
        pending = session.query(OutboxMessage).filter(OutboxMessage.status == "PENDING").all()
    assert len(tasks) == 8
    assert pending == []


def test_speculation_hook_fires_on_signal_keywords(tmp_path) -> None:
    factory = make_factory(tmp_path)
    gateway = BatchGateway()
    deferred: list = []
    runner = SpeculativeRunner(
        factory,
        gateway,
        DeliveryService([CliChannelAdapter()]),
        spawn=deferred.append,  # run after the drain transaction commits
    )
    worker = DurableOutboxWorker(
        factory, lambda _model: gateway, speculative_runner=runner, batch_limit=4
    )
    with session_scope(factory) as session:
        from super_agent.ingress.service import TriggerIngressService

        TriggerIngressService(session).ingest(
            make_trigger("there is a bug in the login flow", "spec-signal-1")
        )
        TriggerIngressService(session).ingest(
            make_trigger("lunch?", "spec-noise-1")
        )
    assert worker.process_batch() is True
    for work in deferred:
        work()
    outcomes = runner.pending()
    assert len(outcomes) == 1
    assert outcomes[0].status in {"running", "completed"}


def test_runtime_router_endpoint_reports_batching(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *args, **kwargs: BatchGateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        model_name="test-model",
        router_batch_limit=6,
        watchdog_interval_seconds=0.0,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/runtime/router")
        assert response.status_code == 200
        body = response.json()
        assert body["batch_limit"] == 6
        assert body["transcript_entries"] == 0
        assert body["speculative_enabled"] is False
        created = client.post(
            "/api/tasks/async",
            json={"message": "router metrics probe", "thread_id": str(uuid4())},
        )
        assert created.status_code == 202
        for _ in range(40):
            status = client.get(
                "/api/tasks/status", params={"dedupe_key": created.json()["dedupe_key"]}
            ).json()
            if status["status"] == "succeeded":
                break
        after = client.get("/api/runtime/router").json()
        assert after["transcript_entries"] >= 2