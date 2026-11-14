from uuid import uuid4

import pytest

from super_agent.application.worker import WorkerEventType, WorkerRuntime
from super_agent.context import ContextEntry, WorkerContext
from super_agent.domain import Worker, WorkerStatus
from super_agent.errors import StateTransitionError


def test_worker_context_isolated_and_copy_on_write() -> None:
    first_worker = Worker(task_id=uuid4())
    second_worker = Worker(task_id=uuid4())
    first = WorkerRuntime(first_worker)
    second = WorkerRuntime(second_worker)

    first.start()
    first.append_context(ContextEntry("user", "first task"))
    second.start()
    second.append_context(ContextEntry("user", "second task"))

    assert [item.content for item in first.context.entries] == ["first task"]
    assert [item.content for item in second.context.entries] == ["second task"]
    assert first.context.revision == 1
    assert second.context.revision == 1


def test_worker_events_are_ordered_and_replayable() -> None:
    worker = Worker(task_id=uuid4())
    emitted = []
    runtime = WorkerRuntime(worker, event_sink=emitted.append)

    runtime.start()
    runtime.wait("waiting for input")
    runtime.resume()
    result = runtime.succeed("artifact:1", "done")

    assert result.status is WorkerStatus.SUCCEEDED
    assert [event.sequence for event in runtime.events] == [1, 2, 3, 4, 5]
    assert emitted == list(runtime.events)
    assert runtime.events[-1].event_type is WorkerEventType.RESULT_REPORTED


def test_worker_rejects_foreign_context_and_terminal_transition() -> None:
    worker = Worker(task_id=uuid4())
    foreign = WorkerContext(worker_id=uuid4(), task_id=worker.task_id)
    with pytest.raises(ValueError, match="does not belong"):
        WorkerRuntime(worker, context=foreign)

    runtime = WorkerRuntime(worker)
    runtime.start()
    runtime.succeed("artifact:1")
    with pytest.raises(StateTransitionError):
        runtime.start()

