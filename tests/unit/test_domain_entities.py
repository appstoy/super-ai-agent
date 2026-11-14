from uuid import UUID

import pytest

from super_agent.domain import (
    Delivery,
    DeliveryStatus,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    Thread,
    ThreadStatus,
    Trigger,
    Worker,
    WorkerStatus,
)
from super_agent.errors import StateTransitionError, VersionConflictError


@pytest.mark.parametrize(
    "entity",
    [Trigger(), Thread(), Task(), Worker(), Run(), Delivery()],
)
def test_entities_have_stable_ids_and_times(entity) -> None:
    payload = entity.to_dict()

    assert isinstance(entity.id, UUID)
    assert payload["id"] == str(entity.id)
    if hasattr(entity, "version"):
        assert payload["version"] == 0


def test_task_allows_expected_transition_and_increments_version() -> None:
    task = Task(title="demo")

    running = task.transition_to(TaskStatus.RUNNING, expected_version=0)

    assert running.status is TaskStatus.RUNNING
    assert running.version == 1
    with pytest.raises(StateTransitionError):
        running.transition_to(TaskStatus.PENDING)


def test_version_conflict_is_rejected() -> None:
    with pytest.raises(VersionConflictError):
        Task(title="demo").transition_to(TaskStatus.RUNNING, expected_version=3)


def test_worker_and_thread_terminal_states_are_enforced() -> None:
    worker = Worker().transition_to(WorkerStatus.RUNNING)
    stopped = worker.transition_to(WorkerStatus.STOPPED)
    assert stopped.status is WorkerStatus.STOPPED
    with pytest.raises(StateTransitionError):
        stopped.transition_to(WorkerStatus.RUNNING)

    closed = Thread().transition_to(ThreadStatus.CLOSED)
    with pytest.raises(StateTransitionError):
        closed.transition_to(ThreadStatus.OPEN)


def test_run_retry_increments_attempt_after_failure() -> None:
    run = Run().transition_to(RunStatus.RUNNING).transition_to(RunStatus.FAILED)
    retried = run.transition_to(RunStatus.PENDING)

    assert retried.attempt == 2
    assert retried.version == 3


def test_delivery_failure_can_be_requeued_but_sent_cannot() -> None:
    failed = Delivery().transition_to(DeliveryStatus.FAILED)
    pending = failed.transition_to(DeliveryStatus.PENDING)
    assert pending.status is DeliveryStatus.PENDING

    sent = pending.transition_to(DeliveryStatus.SENT)
    with pytest.raises(StateTransitionError):
        sent.transition_to(DeliveryStatus.PENDING)

