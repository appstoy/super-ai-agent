"""Compatibility coverage for provider-specific tool call encodings."""

from __future__ import annotations

import json
from typing import Any

import pytest

from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.ports.model import ChatMessage, MessageRole, ModelRequest, ToolCall


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _complete(message: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> Any:
    payload = {
        "id": "resp-compat",
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    monkeypatch.setattr(
        "super_agent.adapters.models.openai_compatible.request.urlopen",
        lambda *args, **kwargs: _FakeResponse(payload),
    )
    gateway = OpenAICompatibleGateway("http://localhost/v1", "key", "model")
    return gateway.complete(ModelRequest(messages=(ChatMessage(MessageRole.USER, "go"),)))


def test_standard_tool_calls_with_dict_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _complete(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "functions.file_read", "arguments": {"path": "a.txt"}},
                }
            ],
        },
        monkeypatch,
    )
    assert response.tool_calls == (
        ToolCall(id="call-1", name="file_read", arguments={"path": "a.txt"}),
    )


def test_legacy_function_call_with_python_literal_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _complete(
        {
            "role": "assistant",
            "content": "",
            "function_call": {"name": "file.read", "arguments": "{'path': 'a.txt',}"},
        },
        monkeypatch,
    )
    assert response.tool_calls[0].name == "file_read"
    assert response.tool_calls[0].arguments == {"path": "a.txt"}
    assert response.tool_calls[0].id.startswith("call-")


def test_fenced_json_tool_calls_in_content(monkeypatch: pytest.MonkeyPatch) -> None:
    fence = chr(96) * 3
    content = (
        "Let me read it.\n"
        + fence
        + 'json\n{"tool_calls": [{"name": "file_read", "arguments": {"path": "b.txt"}}]}\n'
        + fence
    )
    response = _complete({"role": "assistant", "content": content}, monkeypatch)
    assert response.text == content
    assert response.tool_calls[0].name == "file_read"
    assert response.tool_calls[0].arguments == {"path": "b.txt"}


def test_anthropic_style_tool_use_part(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _complete(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "input.read",
                    "input": {"key": "name"},
                },
            ],
        },
        monkeypatch,
    )
    assert response.text == "checking"
    assert response.tool_calls == (
        ToolCall(id="toolu-1", name="input_read", arguments={"key": "name"}),
    )


def test_array_arguments_are_wrapped_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _complete(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-arr",
                    "function": {"name": "mock", "arguments": "[1, 2, 3]"},
                }
            ],
        },
        monkeypatch,
    )
    assert response.tool_calls[0].arguments == {"input": [1, 2, 3]}


def test_json_boolean_and_unquoted_keys_are_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _complete(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-repair",
                    "function": {"name": "mock", "arguments": "{verbose: true, retries: 2,}"},
                }
            ],
        },
        monkeypatch,
    )
    assert response.tool_calls[0].arguments == {"verbose": True, "retries": 2}


def test_plain_prose_is_not_misread_as_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "Here is a config: {name: example} and a list [1, 2]."
    response = _complete({"role": "assistant", "content": text}, monkeypatch)
    assert response.text == text
    assert response.tool_calls == ()


def test_tool_message_names_are_normalized_and_empty_assistant_content_is_null() -> None:
    gateway = OpenAICompatibleGateway("http://localhost/v1", "key", "model")
    tool_message = gateway._message(
        ChatMessage(
            MessageRole.TOOL,
            "content of file",
            name="file.read",
            tool_call_id="call-1",
        )
    )
    assert tool_message["name"] == "file_read"
    assert tool_message["tool_call_id"] == "call-1"

    assistant_message = gateway._message(
        ChatMessage(
            MessageRole.ASSISTANT,
            "",
            tool_calls=(ToolCall(id="call-1", name="file.read", arguments={"path": "a"}),),
        )
    )
    assert assistant_message["content"] is None
    assert assistant_message["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "file_read", "arguments": '{"path": "a"}'},
        }
    ]


def test_tool_names_in_request_payload_are_api_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    from super_agent.ports.model import ModelTool

    captured: dict[str, object] = {}

    class _Capture(_FakeResponse):
        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {},
                }
            ).encode("utf-8")

    def fake_urlopen(http_request: Any, timeout: float = 0) -> _Capture:
        captured.update(json.loads(http_request.data.decode("utf-8")))
        return _Capture({})

    monkeypatch.setattr(
        "super_agent.adapters.models.openai_compatible.request.urlopen", fake_urlopen
    )
    gateway = OpenAICompatibleGateway("http://localhost/v1", "key", "model")
    gateway.complete(
        ModelRequest(
            messages=(ChatMessage(MessageRole.USER, "go"),),
            tools=(ModelTool("file.read", "read a file", {"type": "object"}),),
        )
    )
    names = [tool["function"]["name"] for tool in captured["tools"]]  # type: ignore[index,union-attr]
    assert names == ["file_read"]


class _FakeStream:
    """Minimal file-like SSE response for streaming tests."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


def _sse(payloads: list[dict[str, object]]) -> list[bytes]:
    return [
        ("data: " + json.dumps(item) + "\n\n").encode("utf-8") for item in payloads
    ] + [b"data: [DONE]\n\n"]


def test_stream_assembles_fragmented_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from super_agent.ports.model import ModelTool

    captured: dict[str, object] = {}

    def fake_urlopen(http_request: Any, timeout: float = 0) -> _FakeStream:
        captured.update(json.loads(http_request.data.decode("utf-8")))
        return _FakeStream(
            _sse(
                [
                    {
                        "id": "stream-1",
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-s1",
                                            "type": "function",
                                            "function": {
                                                "name": "file.read",
                                                "arguments": '{"path":',
                                            },
                                        }
                                    ]
                                }
                            }
                        ],
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": '"a.txt"}'}}
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {"delta": {"content": "thinking"}, "finish_reason": "tool_calls"}
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                    },
                ]
            )
        )

    monkeypatch.setattr(
        "super_agent.adapters.models.openai_compatible.request.urlopen", fake_urlopen
    )
    gateway = OpenAICompatibleGateway("http://localhost/v1", "key", "model")
    result = gateway.stream(
        ModelRequest(
            messages=(ChatMessage(MessageRole.USER, "go"),),
            tools=(ModelTool("file.read", "read a file", {"type": "object"}),),
        )
    )
    assert result.text == "thinking"
    assert result.tool_calls == (
        ToolCall(id="call-s1", name="file_read", arguments={"path": "a.txt"}),
    )
    assert result.usage.total_tokens == 5
    assert result.finish_reason == "tool_calls"
    assert result.provider_request_id == "stream-1"
    assert captured["stream"] is True
    names = [tool["function"]["name"] for tool in captured["tools"]]
    assert names == ["file_read"]


def test_stream_text_ignores_tool_call_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "super_agent.adapters.models.openai_compatible.request.urlopen",
        lambda *a, **k: _FakeStream(
            _sse(
                [
                    {"choices": [{"delta": {"content": "hel"}}]},
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": "lo",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c",
                                            "function": {"name": "mock", "arguments": "{}"},
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                ]
            )
        ),
    )
    gateway = OpenAICompatibleGateway("http://localhost/v1", "key", "model")
    chunks = list(
        gateway.stream_text(ModelRequest(messages=(ChatMessage(MessageRole.USER, "go"),)))
    )
    assert chunks == ["hel", "lo"]


def test_stream_over_real_http_assembles_tool_call() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    chunks = [
        'data: {"id":"s1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"h1",'
        '"function":{"name":"file.read","arguments":"{\\"path\\":"}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"x.txt\\"}"}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"tool_calls"}]}\n\n',
        "data: [DONE]\n\n",
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = OpenAICompatibleGateway(
            f"http://127.0.0.1:{server.server_port}/v1", "key", "model"
        )
        result = gateway.stream(
            ModelRequest(messages=(ChatMessage(MessageRole.USER, "go"),))
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert result.text == "done"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == (
        ToolCall(id="h1", name="file_read", arguments={"path": "x.txt"}),
    )
