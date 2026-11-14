"""Provider-neutral tool contracts and execution context."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4


class ToolEffect(StrEnum):
    READ_ONLY = "read_only"
    MOCK = "mock"
    WRITE = "write"


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool's public capability and input contract."""

    name: str
    description: str
    capabilities: frozenset[str] = frozenset()
    effect: ToolEffect = ToolEffect.READ_ONLY
    input_schema: dict[str, object] = field(default_factory=dict)
    max_output_bytes: int = 16 * 1024
    risk: str = ToolRisk.LOW.value
    requires_approval: bool = False
    allowed_paths: tuple[str, ...] = ()
    network_access: bool = False
    credential_access: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Identity and limits supplied by the application before execution."""

    tenant_id: str = "default"
    task_id: UUID | None = None
    run_id: UUID | None = None
    granted_capabilities: frozenset[str] = frozenset()
    timeout_seconds: float = 30.0
    max_output_bytes: int = 16 * 1024
    # Per-task artifact directory. When set, file tools resolve relative paths
    # against it and refuse to leave it, so one task cannot scatter files across
    # the workspace. It can only NARROW the tool's configured roots, never widen
    # them: both checks must pass.
    workspace_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Bounded tool output stored by reference for later context injection."""

    run_id: UUID = field(default_factory=uuid4)
    tool_name: str = ""
    output: str = ""
    output_ref: str = ""
    truncated: bool = False


class Tool(Protocol):
    """Port implemented by safe, registered tools."""

    def describe(self) -> ToolSpec:
        """Return the stable public tool contract."""

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        """Execute with already-checked arguments and capabilities."""

