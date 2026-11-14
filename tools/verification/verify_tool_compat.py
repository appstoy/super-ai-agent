"""Runnable acceptance evidence for tool-call compatibility + user-loaded tools.

Run from the repository root:

    python tools/verification/verify_tool_compat.py

Asserts, with no network access or credentials:
  1. Provider-specific tool-call shapes normalize to ToolCall.
  2. Tool names are consistent in request and result messages.
  3. A user-loaded Python function is discovered, granted and executed.
  4. Unknown tools / missing args are returned as tool results so the Worker
     self-corrects instead of failing the task.
  5. A real HTTP round trip exercises both a custom and a built-in tool.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

# ruff: noqa: E402

# Keep the documented ``python tools/verification/...`` invocation working
# from a source-layout checkout without requiring an editable install.
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sqlalchemy import create_engine, select

from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ChatMessage, MessageRole, ModelRequest, ModelTool, ToolCall
from super_agent.ports.tool import ExecutionContext
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import TaskRecord, WorkerContextRecord
from super_agent.tools.loading import build_agent_tools, load_user_tools
from super_agent.tools.registry import ToolRegistry, required_argument_names

CHECKS: list[str] = []


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    CHECKS.append(label)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _one(message: dict[str, object]) -> dict[str, object]:
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


def verify_adapter_shapes() -> None:
    """Patch urlopen locally; no real HTTP for shape normalization checks."""

    import super_agent.adapters.models.openai_compatible as mod

    original = mod.request.urlopen
    try:
        gateway = OpenAICompatibleGateway("http://localhost/v1", "k", "m")
        request = ModelRequest(
            messages=(ChatMessage(MessageRole.USER, "go"),),
            tools=(ModelTool("file.read", "read", {"type": "object"}),),
        )
        mod.request.urlopen = lambda *a, **k: _FakeResponse(  # type: ignore[assignment]
            _one(
                {
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "functions.file_read",
                                "arguments": {"path": "a.txt"},
                            },
                        }
                    ]
                }
            )
        )
        response = gateway.complete(request)
        check(
            "standard tool_calls + prefixed name + dict args",
            response.tool_calls
            == (ToolCall(id="c1", name="file_read", arguments={"path": "a.txt"}),),
        )

        mod.request.urlopen = lambda *a, **k: _FakeResponse(  # type: ignore[assignment]
            _one({"function_call": {"name": "file.read", "arguments": "{'path':'b',}"}})
        )
        response = gateway.complete(request)
        check(
            "legacy function_call + lenient arguments",
            response.tool_calls[0].name == "file_read"
            and response.tool_calls[0].arguments == {"path": "b"},
        )

        fence = chr(96) * 3
        embedded = (
            "calling\n" + fence
            + 'json\n{"tool_calls":[{"name":"mock","arguments":{}}]}\n' + fence
        )
        mod.request.urlopen = lambda *a, **k: _FakeResponse(  # type: ignore[assignment]
            _one({"content": embedded})
        )
        response = gateway.complete(request)
        check("fenced JSON tool call in content", response.tool_calls[0].name == "mock")

        mod.request.urlopen = lambda *a, **k: _FakeResponse(  # type: ignore[assignment]
            _one(
                {
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "tool_use",
                            "id": "tu-1",
                            "name": "input.read",
                            "input": {"key": "n"},
                        },
                    ]
                }
            )
        )
        response = gateway.complete(request)
        check(
            "Anthropic tool_use part",
            response.tool_calls
            == (ToolCall(id="tu-1", name="input_read", arguments={"key": "n"}),),
        )

        mod.request.urlopen = lambda *a, **k: _FakeResponse(  # type: ignore[assignment]
            _one({"content": "config: {name: x} [1, 2]"})
        )
        response = gateway.complete(request)
        check("plain prose stays plain text", response.tool_calls == ())

        tool_message = gateway._message(
            ChatMessage(MessageRole.TOOL, "data", name="file.read", tool_call_id="c1")
        )
        assistant_message = gateway._message(
            ChatMessage(
                MessageRole.ASSISTANT,
                "",
                tool_calls=(ToolCall(id="c1", name="file.read", arguments={}),),
            )
        )
        check(
            "tool result name + assistant call name normalized consistently",
            tool_message["name"] == "file_read"
            and assistant_message["content"] is None
            and assistant_message["tool_calls"][0]["function"]["name"] == "file_read",
        )
    finally:
        mod.request.urlopen = original  # type: ignore[assignment]


def verify_loading(tmp: Path) -> tuple[ToolRegistry, frozenset[str], Path]:
    plugin = tmp / "user_tools.py"
    plugin.write_text(
        "def shout(text: str) -> str:\n"
        '    """Shout text."""\n'
        "    return text.upper() + '!'\n"
        "\n"
        "tools = (shout,)\n",
        encoding="utf-8",
    )
    manifest = tmp / "tools.json"
    manifest.write_text(
        json.dumps({"tools": [{"path": str(plugin), "capabilities": ["custom.shout"]}]}),
        encoding="utf-8",
    )
    settings = Settings(tool_manifest=str(manifest), workspace_root=str(tmp))
    registry, capabilities, report = build_agent_tools(settings)
    check("user tool discovered from manifest", "shout" in report.loaded)
    check("custom capability granted", "custom.shout" in capabilities)
    result = registry.execute(
        "shout", {"text": "hi"}, ExecutionContext(granted_capabilities=capabilities)
    )
    check("user tool actually executes", result.output == "HI!")

    broken = tmp / "broken.py"
    broken.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    isolated = load_user_tools(
        [{"path": str(broken)}, {"path": str(plugin)}],
        builtin_registry=ToolRegistry(),
    )
    check(
        "one bad plugin does not block the good one",
        "shout" in isolated.loaded and bool(isolated.failed),
    )
    check(
        "JSON Schema required args",
        required_argument_names(
            {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}
        )
        == {"p"},
    )
    return registry, capabilities, tmp


def verify_worker_self_correction(registry: ToolRegistry, capabilities: frozenset[str]) -> None:
    from super_agent.adapters.models.mock import MockModelGateway
    from super_agent.ports.model import ModelResponse

    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: recover"),
            ModelResponse(tool_calls=(ToolCall(id="bad", name="no_such_tool", arguments={}),)),
            ModelResponse(text="Recovered."),
        ]
    )
    event = TriggerInput(
        source="cli", actor="u", thread_id=uuid4(),
        payload={"message": "go"}, occurred_at=datetime.now(UTC),
        dedupe_key="verify-self-correction",
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session, model, tools=registry,
            granted_capabilities=capabilities, max_rounds=6,
        ).submit(event)
    with session_scope(factory) as session:
        task = session.execute(select(TaskRecord)).scalar_one()
        entries = [
            entry.content
            for entry in session.query(WorkerContextRecord)
            .filter_by(task_id=task.id, role="tool").all()
        ]
    check("unknown tool does not fail the task", result.status == "succeeded")
    check("error returned as tool result", any("Error" in item for item in entries))
    engine.dispose()


def verify_http_end_to_end(
    tmp: Path, registry: ToolRegistry, capabilities: frozenset[str]
) -> None:
    """A real HTTP round trip through the OpenAI-compatible gateway."""

    (tmp / "demo.txt").write_text("REAL FILE OUTPUT", encoding="utf-8")
    fence = chr(96) * 3
    fenced = json.dumps(
        {"tool_calls": [{"name": "shout", "arguments": "{'text': 'hi',}"}]}
    )
    script: list[dict[str, object]] = [
        {"choices": [{"message": {"content": "TASK: use tools"}}]},
        {"choices": [{"message": {
            "content": "calling\n" + fence + "json\n" + fenced + "\n" + fence
        }}]},
        {"choices": [{"message": {"tool_calls": [
            {"id": "c2", "function": {
                "name": "functions.file_read",
                "arguments": {"path": str(tmp / "demo.txt")},
            }}
        ]}}]},
        {"choices": [{"message": {"content": "All tools used."}}]},
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            body = script.pop(0)
            raw = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = OpenAICompatibleGateway(f"http://127.0.0.1:{server.server_port}/v1", "k", "m")
        engine = create_engine("sqlite:///:memory:")
        create_schema(engine)
        factory = create_session_factory(engine)
        event = TriggerInput(
            source="cli", actor="u", thread_id=uuid4(),
            payload={"message": "go"}, occurred_at=datetime.now(UTC),
            dedupe_key="verify-http-e2e",
        )
        with session_scope(factory) as session:
            result = AgentOrchestrator(
                session, gateway, tools=registry,
                granted_capabilities=capabilities, max_rounds=8,
            ).submit(event)
        with session_scope(factory) as session:
            task = session.execute(select(TaskRecord)).scalar_one()
            entries = [
                entry.content
                for entry in session.query(WorkerContextRecord)
                .filter_by(task_id=task.id, role="tool").all()
            ]
        check("HTTP end-to-end task succeeded", result.status == "succeeded")
        check("HTTP fenced legacy call executed the user tool", "HI!" in entries)
        check("HTTP prefixed call executed the builtin tool", "REAL FILE OUTPUT" in entries)
        engine.dispose()
    finally:
        server.shutdown()
        thread.join(timeout=2)



def verify_streaming_tool_calls() -> None:
    """SSE deltas carrying a fragmented tool call are assembled, not lost."""

    import super_agent.adapters.models.openai_compatible as mod

    class _Stream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = lines

        def __enter__(self) -> _Stream:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def __iter__(self):
            return iter(self._lines)

    def sse(items: list[dict[str, object]]) -> list[bytes]:
        newline = chr(10) * 2
        return [("data: " + json.dumps(item) + newline).encode("utf-8") for item in items]

    captured: dict[str, object] = {}

    def fake_urlopen(http_request: object, timeout: float = 0) -> _Stream:
        captured.update(json.loads(http_request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return _Stream(
            sse(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "cs1",
                                            "function": {
                                                "name": "file.read",
                                                "arguments": '{"path":',
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {"delta": {"tool_calls": [
                                {"index": 0, "function": {"arguments": '"a.txt"}'}}
                            ]}}
                        ]
                    },
                    {"choices": [{"delta": {"content": "ok"}, "finish_reason": "tool_calls"}]},
                ]
            )
        )

    original = mod.request.urlopen
    mod.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        gateway = OpenAICompatibleGateway("http://localhost/v1", "k", "m")
        result = gateway.stream(
            ModelRequest(
                messages=(ChatMessage(MessageRole.USER, "go"),),
                tools=(ModelTool("file.read", "read", {"type": "object"}),),
            )
        )
        check(
            "streaming fragmented tool_calls assembled into ToolCall",
            result.tool_calls
            == (ToolCall(id="cs1", name="file_read", arguments={"path": "a.txt"}),),
        )
        check("streaming request carries normalized tools", captured.get("stream") is True
              and [t["function"]["name"] for t in captured["tools"]] == ["file_read"])
        check("streaming text still delivered", result.text == "ok")
    finally:
        mod.request.urlopen = original  # type: ignore[assignment]


def verify_windows_path_attr() -> None:
    from super_agent.tools.loading import _normalize_spec

    sep = chr(92)
    win_path = "C:" + sep + "tools" + sep + "my.py"
    parsed = _normalize_spec(win_path + ":make_tool")
    check(
        "Windows path:attr parsed without splitting on drive letter",
        parsed.get("path") == win_path and parsed.get("attr") == "make_tool",
    )
    win_plain = "D:" + sep + "a" + sep + "b.py"
    plain = _normalize_spec(win_plain)
    check(
        "plain Windows .py path has no bogus attr",
        plain.get("path") == win_plain and "attr" not in plain,
    )


def main() -> int:
    verify_adapter_shapes()
    verify_streaming_tool_calls()
    verify_windows_path_attr()
    with TemporaryDirectory() as tmp_name:
        registry, capabilities, tmp = verify_loading(Path(tmp_name))
        verify_worker_self_correction(registry, capabilities)
        verify_http_end_to_end(tmp, registry, capabilities)
    print(json.dumps({"passed": len(CHECKS), "checks": CHECKS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
