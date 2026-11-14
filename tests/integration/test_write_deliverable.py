"""The Worker must actually write deliverables, not just describe them.

Reported symptom: "I asked for an HTML file again and it still cannot read or
write files."  The model never called file_write: it pasted the whole document
into its answer and claimed it was saved. Two defects caused that:

  1. The system prompt said "use registered read-only tools", so the model
     believed it had no write capability.
  2. When the model answered without any tool call, the turn was accepted as
     a success and nothing verified the claimed artifact existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import (
    AgentOrchestrator,
    _looks_like_file_deliverable,
)
from super_agent.config import Settings
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import (
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
from super_agent.storage.models import TaskRecord
from super_agent.tools.loading import build_agent_tools

HTML = "<!doctype html><html><body><h1>delivered</h1></body></html>"


class RecordingRouter(MockModelGateway):
    """Router answers; the Worker script is supplied per test."""

    def __init__(self, worker_script) -> None:
        super().__init__([])
        self._worker_script = list(worker_script)
        self.system_prompts: list[str] = []
        self.worker_tool_names: list[list[str]] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = request.messages[0].content
        if system.startswith("Return exactly one route") or "backlogged triggers" in system:
            return ModelResponse(text="TASK: build the deliverable")
        self.system_prompts.append(system)
        self.worker_tool_names.append([tool.name for tool in request.tools])
        if not self._worker_script:
            return ModelResponse(text="fallback")
        return self._worker_script.pop(0)


def artifact(tmp_path, name: str):
    """Return the produced artifact wherever the task folder put it.

    Tasks write into their own folder under the cache root, so a test must not
    assume the workspace root itself. Searching keeps these assertions about
    "the file really exists" rather than about the folder layout.
    """

    matches = sorted(tmp_path.rglob(name))
    assert matches, f"{name} was never written under {tmp_path}"
    return matches[0]

def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url("sqlite:///" + (tmp_path / "orq.db").as_posix())
    create_schema(engine)
    return create_session_factory(engine)


def run_task(factory, gateway, message: str, tmp_path):
    registry, capabilities, _ = build_agent_tools(
        Settings(workspace_root=str(tmp_path)), workspace_root=tmp_path
    )
    trigger = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": message, "explicit": True},
        occurred_at=datetime.now(UTC),
        dedupe_key="deliverable-" + uuid4().hex,
    )
    with session_scope(factory) as session:
        return AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=6,
        ).submit(trigger)


def test_system_prompt_advertises_write_capability(tmp_path) -> None:
    """The prompt must not tell the model it only has read-only tools."""

    factory = make_factory(tmp_path)
    gateway = RecordingRouter([ModelResponse(text="done")])
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    prompt = gateway.system_prompts[0].lower()
    assert "read-only" not in prompt
    assert "file_write" in prompt
    # The prompt must tell the model to use the tool rather than describe it.
    assert "act with tools instead of describing actions" in prompt
    # It must forbid passing off an unwritten artifact as saved.
    assert "do not say a file was saved unless file_write returned success" in prompt
    assert "do not answer a file request by pasting" in prompt
    # The tool really is offered to the model.
    assert "file_write" in gateway.worker_tool_names[0]


def test_model_is_nudged_when_it_answers_without_writing(tmp_path) -> None:
    """Prose-only answer for a file request must trigger one corrective round."""

    factory = make_factory(tmp_path)
    gateway = RecordingRouter(
        [
            # First Worker turn: pastes the document, no tool call.
            ModelResponse(text="Here is the page: ```html\n" + HTML + "\n``` 已保存。"),
            # Second turn after the nudge: actually writes the file.
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="file_write",
                        arguments={"path": "page.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="已写入 page.html"),
        ]
    )
    result = run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "page.html").read_text(encoding="utf-8") == HTML
    assert "page.html" in result.text
    # The model was asked twice: the nudge really happened.
    assert len(gateway.system_prompts) >= 2


def test_no_nudge_when_the_task_is_just_a_question(tmp_path) -> None:
    """A plain question must finish in one round without a write demand."""

    factory = make_factory(tmp_path)
    gateway = RecordingRouter([ModelResponse(text="1+1 等于 2")])
    result = run_task(factory, gateway, "用一句话回答 1+1 等于几", tmp_path)

    assert result.text == "1+1 等于 2"
    assert len(gateway.system_prompts) == 1


def test_no_nudge_after_a_write_already_happened(tmp_path) -> None:
    """Once file_write succeeded, a prose answer is a valid completion."""

    factory = make_factory(tmp_path)
    gateway = RecordingRouter(
        [
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="file_write",
                        arguments={"path": "done.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="已写入 done.html"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "done.html").is_file()
    assert len(gateway.system_prompts) == 2


def test_deliverable_detection(tmp_path) -> None:
    for message in (
        "做一个 HTML 页面",
        "生成一份报告",
        "write a file with the results",
        "帮我写个脚本",
        "save this as markdown",
    ):
        assert _looks_like_file_deliverable(message), message
    for message in ("1+1 等于几", "你好", "summarize this in one line"):
        assert not _looks_like_file_deliverable(message), message


def test_write_tool_run_is_durable(tmp_path) -> None:
    """A real write must leave an auditable record, not just a chat line."""

    factory = make_factory(tmp_path)
    gateway = RecordingRouter(
        [
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="file_write",
                        arguments={"path": "audit.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="written"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "audit.html").is_file()
    with session_scope(factory) as session:
        tasks = session.query(TaskRecord).all()
    assert tasks, "a task should exist for the turn"
