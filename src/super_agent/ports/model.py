"""Provider-neutral model gateway contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from super_agent.errors import AgentError


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelTool:
    name: str
    description: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ChatMessage, ...]
    model: str | None = None
    tools: tuple[ModelTool, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    request_id: str | None = None
    # OpenAI-compatible tool choice control. Providers that do not understand
    # it ignore the field; providers that do let the harness force a call.
    #   "auto"     - model decides (default)
    #   "none"     - model must not call a tool
    #   "required" - model must call at least one tool
    tool_choice: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    provider_request_id: str | None = None
    finish_reason: str = "stop"


class ModelGateway(Protocol):
    """Port used by Router and Worker code to call a language model."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one deterministic response or raise an AgentError."""


class ModelGatewayError(AgentError):
    """Base error for provider adapters after error normalization."""

