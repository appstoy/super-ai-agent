from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from super_agent.application.run_leases import RunLeaseError, RunLeaseService
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RunRecord, TaskRecord, WorkerRecord


def test_run_lease_claim_heartbeat_cancel_and_recovery() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    run_id = uuid4()
    with session_scope(factory) as session:
        session.add(RunRecord(id=run_id, task_id=uuid4(), status="pending"))

    with session_scope(factory) as session:
        service = RunLeaseService(session)
        claimed = service.claim(run_id, "worker-a", lease_seconds=30)
        assert claimed.status == "running"
        service.heartbeat(run_id, "worker-a", lease_seconds=30)
        with pytest.raises(RunLeaseError, match="another owner"):
            service.claim(run_id, "worker-b", lease_seconds=30)
        service.request_cancel(run_id)

    with session_scope(factory) as session:
        service = RunLeaseService(session)
        run = session.get(RunRecord, run_id)
        assert run is not None
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        recovered = service.recover_expired()
        assert recovered[0].status == "cancelled"


def test_run_lease_recovery_marks_stale_running_run_timed_out() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    run_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            RunRecord(
                id=run_id,
                task_id=uuid4(),
                status="running",
                lease_owner="dead-worker",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

    with session_scope(factory) as session:
        recovered = RunLeaseService(session).recover_expired()
        assert recovered == (recovered[0],)
        assert recovered[0].status == "timed_out"


def test_run_lease_recovery_updates_task_and_worker_health() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = uuid4()
    worker_id = uuid4()
    run_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            TaskRecord(
                id=task_id,
                title="stale task",
                status="running",
                thread_ids="[]",
                worker_id=worker_id,
            )
        )
        session.add(
            WorkerRecord(
                id=worker_id,
                task_id=task_id,
                status="running",
                capabilities="[]",
            )
        )
        session.add(
            RunRecord(
                id=run_id,
                task_id=task_id,
                status="running",
                lease_owner="dead-worker",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

    with session_scope(factory) as session:
        RunLeaseService(session).recover_expired()
        task = session.get(TaskRecord, task_id)
        worker = session.get(WorkerRecord, worker_id)
        assert task is not None and task.status == "failed"
        assert worker is not None and worker.status == "failed"
