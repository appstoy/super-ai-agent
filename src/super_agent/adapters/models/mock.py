"""Scripted, provider-free ModelGateway for tests and replay."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from super_agent.errors import AgentError
from super_agent.ports.model import ModelGateway, ModelRequest, ModelResponse


@dataclass(frozen=True, slots=True)
class MockCall:
    """Recorded request metadata without retaining unbounded provider state."""

    request_id: str | None
    message_count: int
    tool_count: int
    prompt_chars: int


class MockModelGateway(ModelGateway):
    """Return scripted responses in order and record each request."""

    def __init__(self, script: Iterable[ModelResponse | AgentError]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[MockCall] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Consume the next scripted item or fail with a clear exhaustion error."""

        self.calls.append(
            MockCall(
                request_id=request.request_id,
                message_count=len(request.messages),
                tool_count=len(request.tools),
                prompt_chars=sum(len(message.content) for message in request.messages),
            )
        )
        if self._index >= len(self._script):
            raise AgentError("mock model script exhausted")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, AgentError):
            raise item
        return item

