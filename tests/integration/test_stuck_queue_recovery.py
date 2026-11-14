"""Regression tests for a queue that could stall forever.

The reported symptom: "10 pending, 1 running, and nothing ever moves."  Root
causes were (a) the whole batch ran in one transaction, so any failure rolled
back the attempts increment too and the queue head was re-picked forever, (b)
instantiating the model gateway happened outside any per-message guard, (c) a
corrupt payload escaped as a batch-level exception, and (d) loop-level
exceptions were swallowed with no status surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockCall, MockModelGateway
from super_agent.application.background import DurableOutboxWorker, outbox_backoff
from super_agent.errors import ExternalDependencyError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import OutboxMessage
from super_agent.storage.outbox import OutboxRepository

# Small enough that a test drives several attempts without sleeping.
TEST_BACKOFF = 0.001


def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url("sqlite:///" + (tmp_path / "queue.db").as_posix())
    create_schema(engine)
    return create_session_factory(engine)


def queue_triggers(factory: sessionmaker[Session], count: int) -> None:
    with session_scope(factory) as session:
        for index in range(count):
            TriggerIngressService(session).ingest(
                TriggerInput(
                    source="web",
                    actor="user",
                    thread_id=uuid4(),
                    payload={"message": "queued request " + str(index)},
                    occurred_at=datetime.now(UTC),
                    dedupe_key="queued-" + str(index),
                )
            )


class BatchAwareGateway(MockModelGateway):
    """Router/Worker gateway that answers any number of triggers correctly.

    A fixed script cannot work here: the Router merges backlogged triggers into
    one call and expects exactly one ``<n>: decision`` line per trigger, while
    the Worker then finishes each task separately.
    """

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(
            MockCall(
                request_id=request.request_id,
                message_count=len(request.messages),
                tool_count=len(request.tools),
                prompt_chars=sum(len(message.content) for message in request.messages),
            )
        )
        system = request.messages[0].content
        user = request.messages[-1].content
        if system.startswith("Return exactly one route") or "backlogged triggers" in system:
            numbered = [
                line.partition(": ")[0].strip()
                for line in user.splitlines()
                if line.partition(": ")[0].strip().isdigit() and line.partition(": ")[2]
            ]
            if numbered:
                return ModelResponse(
                    text="\n".join(f"{index}: TASK: recovered work" for index in numbered)
                )
            return ModelResponse(text="TASK: recovered work")
        return ModelResponse(text="done")


def recover_gateway() -> BatchAwareGateway:
    return BatchAwareGateway([])


def test_gateway_construction_failure_records_attempts_and_recovers(tmp_path) -> None:
    """A missing model credential must not make the first message immortal."""

    factory = make_factory(tmp_path)
    queue_triggers(factory, 3)

    def broken_gateway(_model: str | None) -> object:
        raise ExternalDependencyError("set SUPER_AGENT_MODEL before running work")

    worker = DurableOutboxWorker(
        factory,
        broken_gateway,
        batch_limit=4,
        retry_backoff_seconds=TEST_BACKOFF,
    )
    assert worker.process_batch() is False
    assert worker.last_error is not None

    with session_scope(factory) as session:
        messages = session.query(OutboxMessage).order_by(OutboxMessage.created_at).all()
        # Every claimed message recorded an attempt, so the queue advances
        # instead of re-picking one immortal head message forever.
        assert [message.attempts for message in messages] == [1, 1, 1]
        assert all(message.status == "PENDING" for message in messages)
        assert all(message.last_error for message in messages)

    # Once the gateway recovers, the same queue drains normally.
    healthy = DurableOutboxWorker(
        factory,
        lambda _model: recover_gateway(),
        batch_limit=4,
        retry_backoff_seconds=TEST_BACKOFF,
    )
    for _ in range(10):
        healthy.process_batch()
        with session_scope(factory) as session:
            outstanding = (
                session.query(OutboxMessage)
                .filter(OutboxMessage.status != "PUBLISHED")
                .count()
            )
        if not outstanding:
            break

    with session_scope(factory) as session:
        statuses = {message.status for message in session.query(OutboxMessage).all()}
    assert statuses == {"PUBLISHED"}


def test_poison_message_is_parked_instead_of_retried_forever(tmp_path) -> None:
    """One permanently failing event must not block every message behind it."""

    factory = make_factory(tmp_path)
    with session_scope(factory) as session:
        session.add(
            OutboxMessage(
                id=uuid4(),
                topic="trigger.received",
                dedupe_key="poison-1",
                payload="not-json-at-all",
                status="PENDING",
                attempts=0,
                available_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
    queue_triggers(factory, 2)

    worker = DurableOutboxWorker(
        factory,
        lambda _model: recover_gateway(),
        max_attempts=2,
        retry_backoff_seconds=TEST_BACKOFF,
    )
    for _ in range(20):
        worker.process_once()

    with session_scope(factory) as session:
        poison = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.dedupe_key == "poison-1")
            .one()
        )
        others = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.dedupe_key != "poison-1")
            .all()
        )
    assert poison.status == "FAILED"
    assert poison.last_error
    # The messages queued behind the poison pill still completed.
    assert all(message.status == "PUBLISHED" for message in others)


def test_backoff_grows_and_is_capped() -> None:
    assert outbox_backoff(1) == 1.0
    assert outbox_backoff(2) == 2.0
    assert outbox_backoff(3) == 4.0
    assert outbox_backoff(99) == 60.0
    assert outbox_backoff(2, base_seconds=0.5) == 1.0


def test_failed_message_is_not_reclaimed_before_its_backoff_elapses(tmp_path) -> None:
    factory = make_factory(tmp_path)
    queue_triggers(factory, 1)

    with session_scope(factory) as session:
        repository = OutboxRepository(session)
        claimed = repository.claim_next()
        assert claimed is not None
        repository.mark_failed(claimed, "temporary", retry_in_seconds=30)

    with session_scope(factory) as session:
        # Not yet due, so it must not be handed out again immediately.
        assert OutboxRepository(session).claim_next() is None


def test_exhausted_messages_are_retired_so_depth_reflects_reality(tmp_path) -> None:
    """A spent retry budget must show up as FAILED, not as eternal PENDING."""

    factory = make_factory(tmp_path)
    queue_triggers(factory, 2)

    with session_scope(factory) as session:
        repository = OutboxRepository(session)
        # Burn both attempts of the head message: it is re-picked each time.
        for attempt in (1, 2):
            claimed = repository.claim_next(max_attempts=5)
            assert claimed is not None
            assert claimed.attempts == attempt
            repository.mark_failed(claimed, "still broken", retry_in_seconds=0)
        # retire_exhausted parks only the message that spent its budget.
        retired = repository.retire_exhausted(max_attempts=2)
        assert len(retired) == 1
        assert retired[0].attempts == 2

    with session_scope(factory) as session:
        statuses = sorted(message.status for message in session.query(OutboxMessage).all())
    # One parked, one still waiting for its next attempt.
    assert statuses == ["FAILED", "PENDING"]


def test_runtime_summary_lists_the_actual_live_runs(tmp_path, monkeypatch) -> None:
    """Counting live runs is not enough; the console needs to name them."""

    from fastapi.testclient import TestClient

    from super_agent.api import create_app
    from super_agent.config import Settings
    from super_agent.storage.models import RunRecord, TaskRecord, WorkerRecord

    database_url = "sqlite:///" + (tmp_path / "api.db").as_posix()
    factory = create_session_factory(create_engine_for_url(database_url))
    create_schema(create_engine_for_url(database_url))

    # A live Run that no Worker will ever finish: exactly what the operator
    # could not see behind the "1 running" counter.
    task_id, worker_id, run_id = uuid4(), uuid4(), uuid4()
    with session_scope(factory) as session:
        session.add(
            TaskRecord(
                id=task_id,
                title="make me an html page",
                status="running",
                thread_ids="[]",
                worker_id=worker_id,
                version=1,
            )
        )
        session.add(
            WorkerRecord(
                id=worker_id,
                task_id=task_id,
                status="running",
                capabilities="[]",
                version=1,
            )
        )
        session.add(
            RunRecord(
                id=run_id,
                task_id=task_id,
                trigger_id=None,
                status="running",
                attempt=1,
                started_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
            )
        )

    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *a, **k: recover_gateway()
    )
    settings = Settings(database_url=database_url, model_name=None)
    with TestClient(create_app(settings)) as client:
        summary = client.get("/api/runtime/summary").json()

    assert summary["active_runs"] == 1
    details = summary["active_run_details"]
    assert len(details) == 1
    assert details[0]["task_id"] == str(task_id)
    assert details[0]["task_title"] == "make me an html page"
    assert details[0]["run_id"] == str(run_id)
    assert details[0]["run_status"] == "running"
    assert details[0]["heartbeat_age_seconds"] >= 0


def test_outbox_endpoint_exposes_queued_message_details(tmp_path, monkeypatch) -> None:
    """ "10 pending" must resolve to which 10 events they actually are. """

    from fastapi.testclient import TestClient

    from super_agent.api import create_app
    from super_agent.config import Settings

    database_url = "sqlite:///" + (tmp_path / "api2.db").as_posix()
    factory = create_session_factory(create_engine_for_url(database_url))
    create_schema(create_engine_for_url(database_url))
    with session_scope(factory) as session:
        TriggerIngressService(session).ingest(
            TriggerInput(
                source="web",
                actor="user",
                thread_id=uuid4(),
                payload={"message": "做一个 HTML 页面"},
                occurred_at=datetime.now(UTC),
                dedupe_key="detail-1",
            )
        )

    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *a, **k: recover_gateway()
    )
    settings = Settings(database_url=database_url, model_name="test-model")
    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/outbox").json()

    assert payload["pending"] == 1
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["topic"] == "trigger.received"
    assert item["text"] == "做一个 HTML 页面"
    assert item["attempts"] == 0
    assert item["status"] == "PENDING"
