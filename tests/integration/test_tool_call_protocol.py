"""The Worker must speak the standard tool-calling protocol.

Two harness defects made tool calls unreliable against real providers:

  1. Every round re-injected the whole durable context (including this run's
     own assistant/tool turns) as ONE user message, so the model saw each tool
     result twice in a shape no OpenAI-compatible endpoint expects.
  2. ``tool_choice`` was never sent, so a model was free to answer with prose
     and claim it had written the file.

These tests pin the request shape the harness must produce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import (
    MessageRole,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.tools.loading import build_agent_tools

HTML = "<!doctype html><html><body><h1>ok</h1></body></html>"


class CapturingGateway(MockModelGateway):
    """Router answers; Worker turns are scripted and fully recorded."""

    def __init__(self, worker_turns) -> None:
        super().__init__([])
        self._turns = list(worker_turns)
        self.worker_requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = request.messages[0].content
        if system.startswith("Return exactly one route") or "backlogged" in system:
            return ModelResponse(text="TASK: build it")
        self.worker_requests.append(request)
        if not self._turns:
            return ModelResponse(text="done")
        return self._turns.pop(0)


def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url("sqlite:///" + (tmp_path / "proto.db").as_posix())
    create_schema(engine)
    return create_session_factory(engine)


def run_task(factory, gateway, message: str, tmp_path):
    registry, capabilities, _ = build_agent_tools(
        Settings(workspace_root=str(tmp_path)), workspace_root=tmp_path
    )
    with session_scope(factory) as session:
        return AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=8,
        ).submit(
            TriggerInput(
                source="web",
                actor="user",
                thread_id=uuid4(),
                payload={"message": message, "explicit": True},
                occurred_at=datetime.now(UTC),
                dedupe_key="proto-" + uuid4().hex,
            )
        )


def test_worker_forces_a_tool_call_on_the_first_round(tmp_path) -> None:
    """A file request must be sent with tool_choice=required until it writes."""

    factory = make_factory(tmp_path)
    gateway = CapturingGateway(
        [
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="file_write",
                        arguments={"path": "a.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="done a.html"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    first = gateway.worker_requests[0]
    assert first.tool_choice == "required"
    # After the write succeeds the harness returns to normal behaviour.
    assert gateway.worker_requests[1].tool_choice == "auto"


def test_plain_question_is_not_forced_to_call_a_tool(tmp_path) -> None:
    factory = make_factory(tmp_path)
    gateway = CapturingGateway([ModelResponse(text="1+1 等于 2")])
    run_task(factory, gateway, "用一句话回答 1+1 等于几", tmp_path)

    assert gateway.worker_requests[0].tool_choice == "auto"


def test_tool_results_are_sent_once_as_tool_messages(tmp_path) -> None:
    """The durable context must not duplicate this run's tool results."""

    factory = make_factory(tmp_path)
    gateway = CapturingGateway(
        [
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="file_write",
                        arguments={"path": "once.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="written"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    second = gateway.worker_requests[1]
    roles = [message.role for message in second.messages]
    assert roles == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    # The tool output appears exactly once, in the tool message.
    tool_messages = [m for m in second.messages if m.role is MessageRole.TOOL]
    assert len(tool_messages) == 1
    assert "once.html" in tool_messages[0].content
    user_text = second.messages[1].content
    assert "once.html" not in user_text
    assert "做一个 HTML 页面" in user_text


def test_parallel_tool_calls_each_get_a_result(tmp_path, artifact) -> None:
    """Every call id in one assistant message must receive its own tool reply."""

    factory = make_factory(tmp_path)
    gateway = CapturingGateway(
        [
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="file_write",
                        arguments={"path": "one.html", "content": HTML},
                    ),
                    ToolCall(
                        id="c2",
                        name="file_write",
                        arguments={"path": "two.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="both written"),
        ]
    )
    run_task(factory, gateway, "做两个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "one.html").is_file()
    assert artifact(tmp_path, "two.html").is_file()
    tool_messages = [m for m in gateway.worker_requests[1].messages if m.role is MessageRole.TOOL]
    assert sorted(m.tool_call_id for m in tool_messages) == ["c1", "c2"]


def test_a_stubborn_model_cannot_spin_to_the_round_limit(tmp_path) -> None:
    """If the provider ignores tool_choice, nudge once and then stop."""

    factory = make_factory(tmp_path)
    # Every turn returns prose only, never a tool call.
    gateway = CapturingGateway([ModelResponse(text="已保存") for _ in range(10)])
    result = run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    # One forced round + one nudge + one final answer, not max_rounds.
    assert len(gateway.worker_requests) <= 3
    assert result.status == "succeeded"
