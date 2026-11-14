"""Capability-checked registry for Mock and read-only tools."""

from __future__ import annotations

from collections.abc import Iterable

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, Tool, ToolResult, ToolSpec
from super_agent.security.policy import ToolPolicy


def required_argument_names(input_schema: dict[str, object]) -> set[str]:
    """Resolve required argument names from compact or JSON Schema specs."""

    required: set[str] = set()
    declared = input_schema.get("required")
    if isinstance(declared, (list, tuple, set, frozenset)):
        required.update(str(item) for item in declared)
    properties = input_schema.get("properties")
    if isinstance(properties, dict):
        candidates = properties
        for name, value in candidates.items():
            if isinstance(value, dict) and value.get("required") in (
                True,
                "required",
                "true",
                1,
            ):
                required.add(str(name))
        return required
    for name, value in input_schema.items():
        if name in {"type", "properties", "required", "$schema"}:
            continue
        if value == "required" or (
            isinstance(value, dict)
            and value.get("required") in (True, "required", "true", 1)
        ):
            required.add(str(name))
    return required


class ToolRegistry:
    """Register tools and enforce their declared policy before execution."""

    def __init__(self, tools: Iterable[Tool] = (), *, policy: ToolPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy = policy or ToolPolicy()
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register one uniquely named tool."""

        spec = tool.describe()
        if not spec.name or spec.name in self._tools:
            raise ValidationError(f"tool name is missing or already registered: {spec.name!r}")
        if spec.max_output_bytes <= 0:
            raise ValidationError(f"tool {spec.name} must have a positive output limit")
        self._tools[spec.name] = tool

    def describe(self, name: str) -> ToolSpec:
        """Return a tool contract or reject unknown tools."""

        tool = self._tools.get(name)
        if tool is None:
            raise PermissionDeniedError(f"tool is not registered: {name}")
        return tool.describe()

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return a stable snapshot of all registered tool contracts."""

        return tuple(tool.describe() for tool in self._tools.values())

    def execute(
        self,
        name: str,
        arguments: dict[str, object],
        context: ExecutionContext,
        *,
        approved: bool = False,
    ) -> ToolResult:
        """Validate capability and required arguments before invoking a tool."""

        tool = self._tools.get(name)
        if tool is None:
            raise PermissionDeniedError(f"tool is not registered: {name}")
        spec = tool.describe()
        self._policy.authorize(spec, context, approved=approved)
        missing = {
            key for key in required_argument_names(spec.input_schema) if key not in arguments
        }
        if missing:
            raise ValidationError(f"missing required tool arguments: {sorted(missing)}")
        if not spec.capabilities.issubset(context.granted_capabilities):
            raise PermissionDeniedError(f"capability denied for tool {name}")
        try:
            result = tool.execute(arguments, context)
        except (PermissionDeniedError, ValidationError) as exc:
            self._policy.record_denial(spec.name, context, str(exc))
            raise
        encoded = result.output.encode("utf-8")
        if len(encoded) <= spec.max_output_bytes:
            return result
        bounded = encoded[: spec.max_output_bytes].decode("utf-8", errors="ignore")
        return ToolResult(
            run_id=result.run_id,
            tool_name=result.tool_name,
            output=bounded,
            output_ref=result.output_ref,
            truncated=True,
        )

