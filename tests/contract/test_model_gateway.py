import pytest

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.errors import ExternalDependencyError
from super_agent.ports.model import (
    ChatMessage,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelTool,
    ModelUsage,
    ToolCall,
)


def test_mock_gateway_returns_scripted_response_and_records_request() -> None:
    response = ModelResponse(
        text="done",
        tool_calls=(ToolCall(id="call-1", name="read", arguments={"path": "README.md"}),),
        usage=ModelUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        provider_request_id="mock-1",
    )
    gateway = MockModelGateway([response])
    request = ModelRequest(
        messages=(ChatMessage(MessageRole.USER, "inspect"),),
        tools=(ModelTool("read", "read a file"),),
        request_id="request-1",
    )

    assert gateway.complete(request) == response
    assert gateway.calls[0].message_count == 1
    assert gateway.calls[0].tool_count == 1


def test_mock_gateway_replays_normalized_error() -> None:
    gateway = MockModelGateway([ExternalDependencyError("provider unavailable")])

    with pytest.raises(ExternalDependencyError, match="provider unavailable"):
        gateway.complete(ModelRequest(messages=()))


def test_mock_gateway_fails_when_script_is_exhausted() -> None:
    gateway = MockModelGateway([])

    with pytest.raises(Exception, match="script exhausted"):
        gateway.complete(ModelRequest(messages=()))

