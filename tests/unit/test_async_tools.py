"""Unit tests for the durable thread-pool tool runner and its controls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.application.async_tools import (
    TOOL_COMPLETED_TOPIC,
    AsyncToolDispatcher,
    DurableToolRunner,
    ToolOrigin,
    tool_completed_trigger,
)
from super_agent.domain import Trigger
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, ToolEffect, ToolResult, ToolSpec
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
)
from super_agent.storage.models import OutboxMessage, ToolRunRecord
from super_agent.tools.builtin import MockTool, ReadOnlyInputTool
from super_agent.tools.registry import ToolRegistry


class SlowTool:
    """Sleep before returning so cancellation and timeout can race it."""

    def __init__(self, delay: float = 0.4, output: str = "slow result") -> None:
        self._delay = delay
        self._output = output

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="slow",
            description="Sleep then return.",
            effect=ToolEffect.MOCK,
            input_schema={},
        )

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        time.sleep(self._delay)
        return ToolResult(tool_name="slow", output=self._output)


class ExplodingTool:
    def describe(self) -> ToolSpec:
        return ToolSpec(name="boom", description="Always fails.", effect=ToolEffect.MOCK)

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        raise RuntimeError("tool exploded")


def make_factory(tmp_path: Path) -> sessionmaker[Session]:
    # A file-backed database gives each thread its own connection, matching the
    # production WAL setup; StaticPool would share one connection across threads.
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'tool-runs.db'}")
    create_schema(engine)
    return create_session_factory(engine)


def make_origin(thread_id: UUID | None = None) -> ToolOrigin:
    return ToolOrigin(
        source="github",
        tenant_id="default",
        thread_id=thread_id or uuid4(),
        actor="alice",
    )


def make_context(task_id: UUID | None = None) -> ExecutionContext:
    return ExecutionContext(task_id=task_id or uuid4(), granted_capabilities=frozenset())


def completion_messages(factory: sessionmaker[Session]) -> list[OutboxMessage]:
    with factory() as session:
        return list(
            session.scalars(
                select(OutboxMessage).where(OutboxMessage.topic == TOOL_COMPLETED_TOPIC)
            )
        )


def test_start_returns_immediately_and_persists_succeeded_with_outbox_event(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([MockTool(name="echo", output="echo-output")])
    runner = DurableToolRunner(factory, registry)
    task_id = uuid4()
    worker_id = uuid4()
    origin = make_origin()
    try:
        started = time.monotonic()
        run_id = runner.start(
            "echo",
            {"key": "value"},
            make_context(task_id),
            worker_id=worker_id,
            origin=origin,
        )
        assert time.monotonic() - started < 0.5
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "succeeded"
        assert snapshot.result_text == "echo-output"
        with factory() as session:
            record = session.get(ToolRunRecord, run_id)
            assert record is not None
            assert record.status == "succeeded"
            assert record.task_id == task_id
            assert record.worker_id == worker_id
            assert record.finished_at is not None
            assert json.loads(record.arguments_json) == {"key": "value"}
        messages = completion_messages(factory)
        assert len(messages) == 1
        assert messages[0].dedupe_key == f"tool.completed:{run_id}"
        payload = json.loads(messages[0].payload)
        assert payload["tool_run_id"] == str(run_id)
        assert payload["status"] == "succeeded"
        assert payload["result_summary"] == "echo-output"
        assert payload["trigger"] == {
            "schema_version": "1",
            "source": "github",
            "tenant_id": "default",
            "thread_id": str(origin.thread_id),
            "actor": "alice",
        }
    finally:
        runner.shutdown()


def test_failed_tool_lands_failed_and_still_emits_completion_event(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([ExplodingTool()]))
    try:
        run_id = runner.start(
            "boom", {}, make_context(), worker_id=uuid4(), origin=make_origin()
        )
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "failed"
        assert snapshot.error == "tool exploded"
        messages = completion_messages(factory)
        assert len(messages) == 1
        payload = json.loads(messages[0].payload)
        assert payload["status"] == "failed"
        assert payload["error"] == "tool exploded"
    finally:
        runner.shutdown()


def test_stop_sets_cancel_flag_and_completion_lands_cancelled(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([SlowTool(delay=0.5)]))
    try:
        run_id = runner.start(
            "slow", {}, make_context(), worker_id=uuid4(), origin=make_origin()
        )
        stopped = runner.stop(run_id)
        assert stopped.cancel_requested
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "cancelled"
        with factory() as session:
            record = session.get(ToolRunRecord, run_id)
            assert record is not None and record.cancel_requested == 1
        payload = json.loads(completion_messages(factory)[0].payload)
        assert payload["status"] == "cancelled"
    finally:
        runner.shutdown()


def test_timeout_lands_timed_out_with_completion_event(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([SlowTool(delay=1.0)]))
    try:
        run_id = runner.start(
            "slow",
            {},
            make_context(),
            worker_id=uuid4(),
            origin=make_origin(),
            timeout_seconds=0.05,
        )
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "timed_out"
        assert snapshot.error is not None and "timed out" in snapshot.error
        payload = json.loads(completion_messages(factory)[0].payload)
        assert payload["status"] == "timed_out"
    finally:
        runner.shutdown()


def test_capability_denial_lands_failed_without_executing(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([ReadOnlyInputTool({"k": "v"})])
    runner = DurableToolRunner(factory, registry)
    try:
        run_id = runner.start(
            "input.read",
            {"key": "k"},
            ExecutionContext(task_id=uuid4(), granted_capabilities=frozenset()),
            worker_id=uuid4(),
            origin=make_origin(),
        )
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "failed"
        assert snapshot.error is not None and "capability" in snapshot.error
    finally:
        runner.shutdown()


def make_dispatcher(
    runner: DurableToolRunner,
    *,
    worker_id: UUID | None = None,
    task_id: UUID | None = None,
    granted: frozenset[str] = frozenset(),
) -> tuple[AsyncToolDispatcher, UUID, UUID]:
    worker = worker_id or uuid4()
    task = task_id or uuid4()
    trigger = Trigger(
        source="github",
        tenant_id="default",
        thread_id=uuid4(),
        actor="alice",
        payload_ref="do work",
        dedupe_key="dispatch-1",
    )
    dispatcher = AsyncToolDispatcher(
        runner,
        worker_id=worker,
        task_id=task,
        origin_run_id=uuid4(),
        trigger=trigger,
        granted_capabilities=granted,
    )
    return dispatcher, worker, task


def test_dispatcher_start_returns_started_with_run_id(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([MockTool(name="echo", output="out")]))
    try:
        dispatcher, worker, task = make_dispatcher(runner)
        result = dispatcher.execute(
            "task.start", {"tool_name": "echo", "arguments_json": "{}"}
        )
        assert result.output.startswith("Started echo")
        assert "tool_run_id=" in result.output
        run_id = UUID(result.output.split("tool_run_id=")[1].split(")")[0])
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "succeeded"
        assert snapshot.worker_id == worker
        assert snapshot.task_id == task
    finally:
        runner.shutdown()


def test_dispatcher_status_reports_truncated_result(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    long_output = "x" * 5_000
    runner = DurableToolRunner(factory, ToolRegistry([MockTool(name="echo", output=long_output)]))
    try:
        dispatcher, _, _ = make_dispatcher(runner)
        started = dispatcher.execute("task.start", {"tool_name": "echo"})
        run_id = UUID(started.output.split("tool_run_id=")[1].split(")")[0])
        runner.wait(run_id, timeout=5.0)
        status = dispatcher.execute("task.status", {"tool_run_id": str(run_id)})
        assert "status=succeeded" in status.output
        assert "(truncated)" in status.output
        assert len(status.output) < 1_000
    finally:
        runner.shutdown()


def test_dispatcher_stop_marks_cancel_requested(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([SlowTool(delay=0.5)]))
    try:
        dispatcher, _, _ = make_dispatcher(runner)
        started = dispatcher.execute("task.start", {"tool_name": "slow"})
        run_id = UUID(started.output.split("tool_run_id=")[1].split(")")[0])
        stopped = dispatcher.execute("task.stop", {"tool_run_id": str(run_id)})
        assert "Stop requested" in stopped.output
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "cancelled"
    finally:
        runner.shutdown()


def test_dispatcher_rejects_foreign_worker_runs_and_bad_arguments(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry([MockTool()]))
    try:
        dispatcher, _, _ = make_dispatcher(runner)
        foreign = runner.start(
            "mock", {}, make_context(), worker_id=uuid4(), origin=make_origin()
        )
        runner.wait(foreign, timeout=5.0)
        denied = dispatcher.execute("task.status", {"tool_run_id": str(foreign)})
        assert denied.output.startswith("Error:")
        missing = dispatcher.execute("task.start", {})
        assert missing.output.startswith("Error:")
        recursive = dispatcher.execute("task.start", {"tool_name": "task.start"})
        assert recursive.output.startswith("Error:")
        bad_json = dispatcher.execute(
            "task.start", {"tool_name": "mock", "arguments_json": "not-json"}
        )
        assert bad_json.output.startswith("Error:")
        unknown = dispatcher.execute("task.stop", {"tool_run_id": str(uuid4())})
        assert unknown.output.startswith("Error:")
    finally:
        runner.shutdown()


def test_dispatcher_specs_are_model_visible_controls(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    runner = DurableToolRunner(factory, ToolRegistry())
    try:
        dispatcher, _, _ = make_dispatcher(runner)
        names = {spec.name for spec in dispatcher.specs()}
        assert names == {"task.start", "task.stop", "task.status"}
        assert all(dispatcher.handles(name) for name in names)
        assert not dispatcher.handles("file.read")
    finally:
        runner.shutdown()


def test_tool_completed_trigger_rebuilds_thread_and_unique_dedupe(tmp_path: Path) -> None:
    thread_id = uuid4()
    run_id = uuid4()
    payload = {
        "tool_run_id": str(run_id),
        "tool_name": "echo",
        "status": "succeeded",
        "result_summary": "echo-output",
        "error": None,
        "trigger": {
            "schema_version": "1",
            "source": "github",
            "tenant_id": "tenant-a",
            "thread_id": str(thread_id),
            "actor": "alice",
        },
    }
    trigger = tool_completed_trigger(payload)
    assert trigger.thread_id == thread_id
    assert trigger.source == "github"
    assert trigger.tenant_id == "tenant-a"
    assert trigger.actor == "alice"
    assert trigger.dedupe_key == f"tool.completed:{run_id}"
    assert trigger.occurred_at.tzinfo is not None
    message = trigger.payload["message"]
    assert isinstance(message, str)
    assert "echo-output" in message and "status=succeeded" in message
    with pytest.raises(ValidationError):
        tool_completed_trigger({"tool_run_id": "x"})


def test_runner_rejects_invalid_limits_and_unknown_runs(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    with pytest.raises(ValueError):
        DurableToolRunner(factory, ToolRegistry(), max_workers=0)
    runner = DurableToolRunner(factory, ToolRegistry())
    try:
        with pytest.raises(ValidationError):
            runner.status(uuid4())
        with pytest.raises(ValidationError):
            runner.stop(uuid4())
        with pytest.raises(ValidationError):
            runner.start(
                "mock",
                {},
                ExecutionContext(task_id=None),
                worker_id=uuid4(),
                origin=make_origin(),
            )
        with pytest.raises(ValidationError):
            runner.start(
                "mock",
                {},
                make_context(),
                worker_id=uuid4(),
                origin=make_origin(),
                timeout_seconds=-1,
            )
    finally:
        runner.shutdown()


def test_permission_denied_error_from_registry_is_recorded_as_failed(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)

    class DeniedTool:
        def describe(self) -> ToolSpec:
            return ToolSpec(
                name="denied",
                description="Raises permission denied.",
                capabilities=frozenset({"denied.read"}),
            )

        def execute(
            self, arguments: dict[str, object], context: ExecutionContext
        ) -> ToolResult:
            del arguments, context
            raise PermissionDeniedError("nope")

    runner = DurableToolRunner(factory, ToolRegistry([DeniedTool()]))
    try:
        run_id = runner.start(
            "denied",
            {},
            ExecutionContext(task_id=uuid4(), granted_capabilities=frozenset({"denied.read"})),
            worker_id=uuid4(),
            origin=make_origin(),
        )
        snapshot = runner.wait(run_id, timeout=5.0)
        assert snapshot.status == "failed"
        assert snapshot.error == "nope"
    finally:
        runner.shutdown()