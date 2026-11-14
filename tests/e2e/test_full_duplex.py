"""End-to-end full-duplex ReAct loop: start → reply → completion re-injection."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.async_tools import (
    TOOL_COMPLETED_TOPIC,
    DurableToolRunner,
    tool_completed_trigger,
)
from super_agent.application.background import DurableOutboxWorker
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelResponse, ToolCall
from super_agent.ports.tool import ExecutionContext, ToolEffect, ToolResult, ToolSpec
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import (
    OutboxMessage,
    RunRecord,
    SubscriptionRecord,
    TaskRecord,
    ToolRunRecord,
    WorkerContextRecord,
    WorkerRecord,
)
from super_agent.tools.builtin import MockTool
from super_agent.tools.registry import ToolRegistry


class SlowTool:
    """A background tool slow enough that the first turn finishes before it."""

    def __init__(self, delay: float = 0.3, output: str = "background-result-42") -> None:
        self._delay = delay
        self._output = output

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="slow",
            description="Sleep then return a result.",
            effect=ToolEffect.MOCK,
            input_schema={},
        )

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        time.sleep(self._delay)
        return ToolResult(tool_name="slow", output=self._output)


def make_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'duplex.db'}")
    create_schema(engine)
    return create_session_factory(engine)


def make_trigger(thread_id: UUID, dedupe_key: str) -> TriggerInput:
    return TriggerInput(
        source="web",
        actor="user",
        thread_id=thread_id,
        payload={"message": "run the slow analysis"},
        occurred_at=datetime.now(UTC),
        dedupe_key=dedupe_key,
    )


def wait_for_tool_run(
    factory: sessionmaker[Session], run_id: UUID, *, timeout: float = 5.0
) -> ToolRunRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            record = session.get(ToolRunRecord, run_id)
            if record is not None and record.status != "running":
                return record
        time.sleep(0.02)
    raise AssertionError(f"tool run {run_id} did not finish in {timeout}s")


def started_run_id(text: str) -> UUID:
    assert "tool_run_id=" in text, text
    return UUID(text.split("tool_run_id=")[1].split(")")[0].strip())


def test_task_start_returns_started_and_worker_replies_without_waiting(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([SlowTool(delay=0.6)])
    runner = DurableToolRunner(factory, registry)
    thread_id = uuid4()
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: slow analysis"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-start",
                        name="task_start",
                        arguments={"tool_name": "slow", "arguments_json": "{}"},
                    ),
                )
            ),
            ModelResponse(text="Started the analysis in the background; I'll report back."),
        ]
    )
    try:
        began = time.monotonic()
        with session_scope(factory) as session:
            result = AgentOrchestrator(
                session, model, tools=registry, tool_runner=runner
            ).submit(make_trigger(thread_id, "duplex-start-1"))
        elapsed = time.monotonic() - began
        assert result.status == "succeeded"
        assert result.text == "Started the analysis in the background; I'll report back."
        assert elapsed < 0.6  # the turn did not wait for the slow tool

        with session_scope(factory) as session:
            contexts = session.query(WorkerContextRecord).all()
            started_entries = [
                entry
                for entry in contexts
                if entry.role == "tool" and entry.content.startswith("Started slow")
            ]
            assert len(started_entries) == 1
            run_id = started_run_id(started_entries[0].content)
            tool_run = session.get(ToolRunRecord, run_id)
            assert tool_run is not None
            assert tool_run.status in {"running", "succeeded"}

        finished = wait_for_tool_run(factory, run_id)
        assert finished.status == "succeeded"
        assert finished.result_text == "background-result-42"
        with session_scope(factory) as session:
            message = session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.dedupe_key == f"tool.completed:{run_id}"
                )
            )
            assert message is not None
            assert message.topic == TOOL_COMPLETED_TOPIC
            payload = json.loads(message.payload)
            assert payload["trigger"]["thread_id"] == str(thread_id)
            assert payload["result_summary"] == "background-result-42"
    finally:
        runner.shutdown()


def test_tool_completed_event_resumes_the_same_worker_with_the_result(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([SlowTool(delay=0.4)])
    runner = DurableToolRunner(factory, registry)
    thread_id = uuid4()
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: slow analysis"),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-start", name="task_start", arguments={"tool_name": "slow"}),
                )
            ),
            ModelResponse(text="Analysis started; I'll follow up."),
            ModelResponse(text="The analysis finished: background-result-42."),
        ]
    )
    worker = DurableOutboxWorker(factory, lambda _model: model, tools=registry, tool_runner=runner)
    try:
        with session_scope(factory) as session:
            first = AgentOrchestrator(
                session, model, tools=registry, tool_runner=runner
            ).submit(make_trigger(thread_id, "duplex-resume-1"))
        assert first.status == "succeeded"
        with session_scope(factory) as session:
            entry = (
                session.query(WorkerContextRecord)
                .filter(WorkerContextRecord.role == "tool")
                .first()
            )
            assert entry is not None
            run_id = started_run_id(entry.content)
        wait_for_tool_run(factory, run_id)

        # The durable worker consumes trigger.received (duplicate-safe) and then
        # the tool.completed event, resuming the subscribed Worker.
        assert worker.process_once()
        assert worker.process_once()
        assert not worker.process_once()

        with session_scope(factory) as session:
            tasks = session.query(TaskRecord).all()
            workers = session.query(WorkerRecord).all()
            runs = session.query(RunRecord).all()
            contexts = session.query(WorkerContextRecord).all()
            subscriptions = session.query(SubscriptionRecord).all()
        assert len(tasks) == 1
        assert len(workers) == 1
        assert len(subscriptions) == 1
        assert len(runs) == 2
        assert all(run.status == "succeeded" for run in runs)
        # The continuation injected the tool result into the same Worker context.
        assert any(
            entry.role == "trigger" and "background-result-42" in entry.content
            for entry in contexts
        )
        assert any(
            entry.role == "assistant"
            and entry.content == "The analysis finished: background-result-42."
            for entry in contexts
        )
        assert tasks[0].status == "succeeded"
    finally:
        runner.shutdown()


def test_continuation_dedupe_is_idempotent(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([MockTool(name="echo", output="echo-out")])
    runner = DurableToolRunner(factory, registry)
    thread_id = uuid4()
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: echo"),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-start", name="task_start", arguments={"tool_name": "echo"}),
                )
            ),
            ModelResponse(text="echo started"),
            ModelResponse(text="echo finished: echo-out"),
        ]
    )
    worker = DurableOutboxWorker(factory, lambda _model: model, tools=registry, tool_runner=runner)
    try:
        with session_scope(factory) as session:
            AgentOrchestrator(
                session, model, tools=registry, tool_runner=runner
            ).submit(make_trigger(thread_id, "duplex-idem-1"))
        with session_scope(factory) as session:
            entry = (
                session.query(WorkerContextRecord)
                .filter(WorkerContextRecord.role == "tool")
                .first()
            )
            assert entry is not None
            run_id = started_run_id(entry.content)
        wait_for_tool_run(factory, run_id)
        assert worker.process_once()  # trigger.received duplicate
        assert worker.process_once()  # tool.completed continuation
        assert not worker.process_once()
        with session_scope(factory) as session:
            assert len(session.query(RunRecord).all()) == 2
            triggers = session.execute(select(OutboxMessage.topic)).all()
        assert [row[0] for row in triggers].count(TOOL_COMPLETED_TOPIC) == 1
    finally:
        runner.shutdown()


def test_task_stop_cancels_a_background_run_through_the_model(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    registry = ToolRegistry([SlowTool(delay=0.8)])
    runner = DurableToolRunner(factory, registry)
    thread_id = uuid4()

    class StopAfterStartGateway:
        """Start the slow tool, then stop it using the returned run id."""

        def __init__(self) -> None:
            self.calls = 0
            self.run_id: UUID | None = None

        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text="TASK: stoppable work")
            if self.calls == 2:
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="call-start", name="task_start", arguments={"tool_name": "slow"}
                        ),
                    )
                )
            if self.calls == 3:
                tool_messages = [m.content for m in request.messages if m.role.value == "tool"]
                self.run_id = started_run_id(tool_messages[-1])
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="call-stop",
                            name="task_stop",
                            arguments={"tool_run_id": str(self.run_id)},
                        ),
                    )
                )
            return ModelResponse(text="Stopped the background work.")

    gateway = StopAfterStartGateway()
    try:
        with session_scope(factory) as session:
            result = AgentOrchestrator(
                session, gateway, tools=registry, tool_runner=runner
            ).submit(make_trigger(thread_id, "duplex-stop-1"))
        assert result.status == "succeeded"
        assert gateway.run_id is not None
        finished = wait_for_tool_run(factory, gateway.run_id)
        assert finished.status == "cancelled"
        assert finished.cancel_requested == 1
    finally:
        runner.shutdown()


def test_tool_completed_trigger_keeps_round_limits_bounded(tmp_path: Path) -> None:
    """A continuation that keeps starting tools still hits max_rounds."""

    factory = make_factory(tmp_path)
    registry = ToolRegistry([MockTool(name="echo", output="loop-out")])
    runner = DurableToolRunner(factory, registry)
    thread_id = uuid4()
    payload = {
        "tool_run_id": str(uuid4()),
        "tool_name": "echo",
        "status": "succeeded",
        "result_summary": "loop-out",
        "trigger": {
            "schema_version": "1",
            "source": "web",
            "tenant_id": "default",
            "thread_id": str(thread_id),
            "actor": "user",
        },
    }
    trigger_input = tool_completed_trigger(payload)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: loop forever"),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="loop", name="task_start", arguments={"tool_name": "echo"}),
                )
            ),
            # Final allowed round still requests a tool: the turn must fail
            # at the round limit instead of issuing another model call.
            ModelResponse(
                tool_calls=(
                    ToolCall(id="loop-b", name="task_start", arguments={"tool_name": "echo"}),
                )
            ),
        ]
    )
    try:
        with session_scope(factory) as session:
            first = AgentOrchestrator(
                session, model, tools=registry, tool_runner=runner, max_rounds=2
            ).submit(make_trigger(thread_id, "duplex-loop-1"))
        assert first.status == "failed"  # round limit reached on the first turn
        with session_scope(factory) as session:
            result = AgentOrchestrator(
                session,
                MockModelGateway(
                    [
                        # The continuation reaches the still-subscribed Worker
                        # directly (no Router call); its single allowed round
                        # requests a tool and must fail at the round limit.
                        ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    id="loop-2",
                                    name="task_start",
                                    arguments={"tool_name": "echo"},
                                ),
                            )
                        ),
                    ]
                ),
                tools=registry,
                tool_runner=runner,
                max_rounds=1,
            ).submit(trigger_input)
        assert result.status == "failed"
        assert "round limit" in result.text
    finally:
        runner.shutdown()