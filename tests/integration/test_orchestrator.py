from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select

from super_agent.adapters.channels.memory import CliChannelAdapter
from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.delivery.service import DeliveryService
from super_agent.domain import TaskStatus
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelResponse, ToolCall
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RunRecord, TaskRecord, WorkerContextRecord, WorkerRecord
from super_agent.tools.builtin import MockTool
from super_agent.tools.registry import ToolRegistry, required_argument_names
from super_agent.tools.restricted import ReadFileTool


class CancellingGateway:
    def __init__(self, session) -> None:
        self._session = session
        self._calls = 0

    def complete(self, request):
        del request
        self._calls += 1
        if self._calls == 1:
            return ModelResponse(text="TASK: cancel me")
        run = self._session.scalar(select(RunRecord))
        assert run is not None
        run.status = "cancel_requested"
        self._session.flush()
        return ModelResponse(text="should not be delivered")


def test_orchestrator_runs_trigger_router_worker_and_persists_state() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: inspect repository"),
            ModelResponse(text="Repository inspected successfully."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "inspect repository"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-1",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, model).submit(event)
        assert result.status == TaskStatus.SUCCEEDED.value
        assert result.text == "Repository inspected successfully."
        assert result.task_id is not None

    with session_scope(factory) as session:
        task = session.scalar(select(TaskRecord))
        worker = session.scalar(select(WorkerRecord))
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert worker is not None and worker.status == "succeeded"


def test_orchestrator_executes_model_tool_call_and_persists_context() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: use a tool"),
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="mock", arguments={}),)
            ),
            ModelResponse(text="Tool result incorporated."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "use a tool"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-tool-1",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([MockTool(output="mock-tool-output")]),
        ).submit(event)
        assert result.status == TaskStatus.SUCCEEDED.value
        assert result.text == "Tool result incorporated."

    with session_scope(factory) as session:
        task = session.execute(
            select(TaskRecord).where(TaskRecord.title == "use a tool")
        ).scalar_one()
        context = session.query(WorkerContextRecord).filter_by(task_id=task.id).all()
        assert any(
            entry.role == "tool" and "mock-tool-output" in entry.content for entry in context
        )


def test_orchestrator_maps_safe_model_tool_names_to_restricted_tools(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("bounded file content", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: read a file"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-file",
                        name="file_read",
                        arguments={"path": str(target)},
                    ),
                )
            ),
            ModelResponse(text="File content incorporated."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "read note"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-file-tool-1",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([ReadFileTool((tmp_path,))]),
            granted_capabilities=frozenset({"file.read"}),
        ).submit(event)
        assert result.text == "File content incorporated."


def test_orchestrator_persists_failed_worker_when_tool_round_limit_is_reached() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: loop"),
            ModelResponse(tool_calls=(ToolCall(id="loop-1", name="mock", arguments={}),)),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "loop"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-loop-limit-1",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([MockTool()]),
            max_rounds=1,
        ).submit(event)
        assert result.status == "failed"

    with session_scope(factory) as session:
        assert session.scalar(select(TaskRecord)).status == "failed"


def test_orchestrator_resumes_the_same_worker_for_a_thread_follow_up() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    thread_id = uuid4()
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: ongoing thread task"),
            ModelResponse(text="first answer"),
            ModelResponse(text="second answer"),
        ]
    )
    first = TriggerInput(
        source="web",
        actor="user",
        thread_id=thread_id,
        payload={"message": "start thread"},
        occurred_at=datetime.now(UTC),
        dedupe_key="thread-follow-up-1",
    )
    second = TriggerInput(
        source="web",
        actor="user",
        thread_id=thread_id,
        payload={"message": "continue thread"},
        occurred_at=datetime.now(UTC),
        dedupe_key="thread-follow-up-2",
    )

    with session_scope(factory) as session:
        first_result = AgentOrchestrator(session, model).submit(first)
        second_result = AgentOrchestrator(session, model).submit(second)
        assert first_result.task_id == second_result.task_id
        assert second_result.text == "second answer"

    with session_scope(factory) as session:
        assert len(session.query(RunRecord).all()) == 2
        context = session.query(WorkerContextRecord).all()
        assert any(
            entry.role == "assistant" and entry.content == "first answer" for entry in context
        )


def test_orchestrator_stops_at_a_cooperative_cancel_checkpoint() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "cancel me"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-cancel-1",
    )

    with session_scope(factory) as session:
        result = AgentOrchestrator(session, CancellingGateway(session)).submit(event)
        assert result.status == "cancelled"
        assert result.text == "Worker stopped by cancellation or lease policy."

    with session_scope(factory) as session:
        run = session.scalar(select(RunRecord))
        task = session.scalar(select(TaskRecord))
        assert run is not None and run.status == "cancelled"
        assert task is not None and task.status == "cancelled"


def test_orchestrator_delivers_final_result_through_delivery_service() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    cli = CliChannelAdapter()
    model = MockModelGateway(
        [ModelResponse(text="TASK: deliver result"), ModelResponse(text="delivered")]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "deliver result"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-delivery-1",
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(session, model, delivery=DeliveryService([cli])).submit(event)
        assert result.status == TaskStatus.SUCCEEDED.value
    assert cli.messages == ["delivered"]


def _tool_error_context(factory, message: str) -> list[str]:
    with session_scope(factory) as session:
        task = session.execute(select(TaskRecord).where(TaskRecord.title == message)).scalar_one()
        return [
            entry.content
            for entry in session.query(WorkerContextRecord).filter_by(task_id=task.id).all()
            if entry.role == "tool"
        ]


def test_orchestrator_returns_unknown_tool_errors_for_model_self_correction() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: fix a bad tool call"),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="bad", name="does_not_exist", arguments={"path": "x"}),
                )
            ),
            ModelResponse(text="Recovered after the tool error."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "fix a bad tool call"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-unknown-tool-1",
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([MockTool()]),
            granted_capabilities=frozenset({"file.read"}),
        ).submit(event)
    assert result.text == "Recovered after the tool error."
    entries = _tool_error_context(factory, "fix a bad tool call")
    assert len(entries) == 1
    assert "Error" in entries[0]
    assert "unknown tool" in entries[0]
    assert "mock" in entries[0]


@pytest.mark.parametrize("tool_name", ["file.read", "functions.file_read", " file_read "])
def test_orchestrator_accepts_tool_name_variants(tmp_path, tool_name: str) -> None:
    target = tmp_path / "note.txt"
    target.write_text("bounded file content", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: read a variant file"),
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-variant", name=tool_name, arguments={"path": str(target)}),
                )
            ),
            ModelResponse(text="Variant file read."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "read variant file"},
        occurred_at=datetime.now(UTC),
        dedupe_key=f"orchestrator-variant-{tool_name.strip()}",
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([ReadFileTool((tmp_path,))]),
            granted_capabilities=frozenset({"file.read"}),
        ).submit(event)
    assert result.text == "Variant file read."
    entries = _tool_error_context(factory, "read a variant file")
    assert entries == ["bounded file content"]


def test_orchestrator_returns_missing_argument_error_without_failing(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: recover missing args"),
            ModelResponse(
                tool_calls=(ToolCall(id="call-missing", name="file_read", arguments={}),)
            ),
            ModelResponse(text="Retried with a path."),
        ]
    )
    event = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "recover missing args"},
        occurred_at=datetime.now(UTC),
        dedupe_key="orchestrator-missing-args-1",
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session,
            model,
            tools=ToolRegistry([ReadFileTool((tmp_path,))]),
            granted_capabilities=frozenset({"file.read"}),
        ).submit(event)
    assert result.text == "Retried with a path."
    entries = _tool_error_context(factory, "recover missing args")
    assert len(entries) == 1
    assert "missing required" in entries[0]



def test_required_argument_names_supports_compact_and_json_schema() -> None:
    assert required_argument_names({"path": "required", "cwd": "optional"}) == {"path"}
    assert required_argument_names(
        {"path": {"type": "string", "required": True}, "cwd": {"type": "string"}}
    ) == {"path"}
    assert required_argument_names(
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "cwd": {"type": "string"}},
            "required": ["path"],
        }
    ) == {"path"}
