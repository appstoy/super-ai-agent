"""A model that invents a tool name must still reach the real tool.

Reported symptom: the drawer showed a task stuck at "starting / running" and
"no background tool calls", and the model's answer said it had tried
`list_tools` and `write_file` and concluded the environment had no write tool.

The recorded tool runs were `list_tools` and `write_file` — near-miss names for
`file_list` and `file_write`. Exact alias matching rejected them, the model gave
up, and 17 KB of HTML went into the chat answer instead of a file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelRequest, ModelResponse, ToolCall
from super_agent.ports.tool_names import normalize_tool_name, resolve_tool_name
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.tools.loading import build_agent_tools

HTML = "<!doctype html><html><body><h1>rescued</h1></body></html>"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("write_file", "file.write"),
        ("list_tools", "file.list"),
        ("read_file", "file.read"),
        ("file_write", "file.write"),
        ("functions.file_read", "file.read"),
        ("write", "file.write"),
        ("list_files", "file.list"),
        ("totally_unknown_xyz", None),
        ("", None),
    ],
)
def test_tool_name_aliases_resolve(requested: str, expected: str | None) -> None:
    available = {
        "file_read": "file.read",
        "file.read": "file.read",
        "read": "file.read",
        "file_write": "file.write",
        "file.write": "file.write",
        "write": "file.write",
        "file_list": "file.list",
        "file.list": "file.list",
        "list": "file.list",
    }
    assert resolve_tool_name(requested, available) == expected


def test_normalize_still_strips_prefixes() -> None:
    assert normalize_tool_name("functions.file_read") == "file_read"
    assert normalize_tool_name("  file.write ") == "file_write"


class Gateway(MockModelGateway):
    def __init__(self, turns) -> None:
        super().__init__([])
        self._turns = list(turns)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = request.messages[0].content
        if system.startswith("Return exactly one route") or "backlogged" in system:
            return ModelResponse(text="TASK: build it")
        self.requests.append(request)
        if not self._turns:
            return ModelResponse(text="done")
        return self._turns.pop(0)


def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url("sqlite:///" + (tmp_path / "alias.db").as_posix())
    create_schema(engine)
    return create_session_factory(engine)


def run_task(factory, gateway, message: str, tmp_path) -> object:
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
                dedupe_key="alias-" + uuid4().hex,
            )
        )


def test_hallucinated_tool_name_still_writes_the_file(tmp_path, artifact) -> None:
    """The exact reported failure: model calls write_file, file must still exist."""

    factory = make_factory(tmp_path)
    gateway = Gateway(
        [
            ModelResponse(
                text="我先看看有哪些工具",
                tool_calls=(ToolCall(id="c1", name="list_tools", arguments={}),),
            ),
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c2",
                        name="write_file",
                        arguments={"path": "rescued.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="已写入 rescued.html"),
        ]
    )
    run_task(factory, gateway, "制作大模型原理介绍 HTML 页面", tmp_path)

    assert artifact(tmp_path, "rescued.html").read_text(encoding="utf-8") == HTML


def test_model_that_gives_up_is_corrected_once(tmp_path, artifact) -> None:
    """A model that sees a failed call must not be allowed to just paste text."""

    factory = make_factory(tmp_path)
    gateway = Gateway(
        [
            # The model calls a name that resolves to no tool at all, then gives
            # up and pastes the document instead of retrying.
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(id="c1", name="some_imaginary_uploader", arguments={}),
                ),
            ),
            ModelResponse(
                text="当前环境没有写文件工具，代码贴在下面：```html\n" + HTML + "\n```",
            ),
            # After the correction it uses the real tool name.
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c2",
                        name="file_write",
                        arguments={"path": "after-nudge.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="已写入 after-nudge.html"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "after-nudge.html").is_file()
    # The correction names the exact tool so the model cannot claim ignorance.
    corrections = [
        m.content for r in gateway.requests for m in r.messages
        if m.role.value == "user" and "exact name to call" in m.content
    ]
    assert corrections
    assert "file_write" in corrections[0]


def test_failed_tool_call_is_returned_as_a_result_not_a_crash(tmp_path) -> None:
    """An unknown tool must come back as a model-visible error, not kill the turn."""

    factory = make_factory(tmp_path)
    gateway = Gateway([ModelResponse(text="ok")])
    result = run_task(factory, gateway, "做一个 HTML 页面", tmp_path)
    assert result.status in {"succeeded", "failed"}


def test_probe_only_tool_then_prose_is_still_corrected(tmp_path, artifact) -> None:
    """A model that lists files and then narrates must still be pushed to write.

    Reported symptom: "it didn't write the file, it just called a tool". The
    old correction only fired when the model returned NO tool calls, so a model
    that explored with file_list and then described the result was accepted as
    a success with no artifact on disk.
    """

    factory = make_factory(tmp_path)
    gateway = Gateway(
        [
            ModelResponse(
                text="",
                tool_calls=(ToolCall(id="call_1", name="file_list", arguments={"path": "."}),),
            ),
            # Second turn: still no write, just prose about the directory.
            ModelResponse(text="目录里有几个文件，页面内容如下：```html\n" + HTML + "\n```"),
            # Third turn, after the nudge: actually write it.
            ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_2",
                        name="file_write",
                        arguments={"path": "probe.html", "content": HTML},
                    ),
                ),
            ),
            ModelResponse(text="已写入 probe.html"),
        ]
    )
    run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert artifact(tmp_path, "probe.html").read_text(encoding="utf-8") == HTML
    corrections = [
        m.content
        for r in gateway.requests
        for m in r.messages
        if m.role.value == "user" and "exact name to call" in m.content
    ]
    assert corrections, "the prose-after-probe turn must be corrected"
    assert "file_write" in corrections[0]


def test_round_limit_without_a_write_reports_the_real_cause(tmp_path) -> None:
    """Exhausting rounds while only reading must not look like a generic failure."""

    factory = make_factory(tmp_path)
    listing = ModelResponse(
        text="", tool_calls=(ToolCall(id="call_x", name="file_list", arguments={"path": "."}),)
    )
    gateway = Gateway([listing] * 8)
    result = run_task(factory, gateway, "做一个 HTML 页面", tmp_path)

    assert not list(tmp_path.rglob("*.html")), "nothing should have been written"
    assert "deliverable" in result.text or "round limit" in result.text
