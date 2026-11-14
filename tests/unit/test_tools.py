import pytest

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext
from super_agent.tools.builtin import MockTool, ReadOnlyInputTool
from super_agent.tools.registry import ToolRegistry


def test_registry_rejects_unknown_tools_and_missing_capabilities() -> None:
    registry = ToolRegistry([ReadOnlyInputTool({"name": "Alice"})])

    with pytest.raises(PermissionDeniedError, match="not registered"):
        registry.describe("shell")
    with pytest.raises(PermissionDeniedError, match="capability denied"):
        registry.execute("input.read", {"key": "name"}, ExecutionContext())


def test_read_only_tool_requires_arguments_and_returns_bounded_output() -> None:
    registry = ToolRegistry([ReadOnlyInputTool({"name": "Alice"})])
    context = ExecutionContext(granted_capabilities=frozenset({"input.read"}))

    with pytest.raises(ValidationError, match="missing required"):
        registry.execute("input.read", {}, context)
    result = registry.execute("input.read", {"key": "name"}, context)
    assert result.output == '"Alice"'
    assert not result.truncated


def test_mock_tool_output_is_truncated_by_registry_policy() -> None:
    registry = ToolRegistry([MockTool(output="x" * 100, max_output_bytes=8)])

    result = registry.execute("mock", {}, ExecutionContext())
    assert result.output == "x" * 8
    assert result.truncated

