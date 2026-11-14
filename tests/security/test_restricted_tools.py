from pathlib import Path

import pytest

from super_agent.errors import PermissionDeniedError
from super_agent.ports.tool import ExecutionContext
from super_agent.security.policy import ToolPolicy
from super_agent.tools.registry import ToolRegistry
from super_agent.tools.restricted import AllowlistedCommandTool, ReadFileTool


def test_read_file_tool_stays_inside_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    file_path = allowed / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool((allowed,))])
    context = ExecutionContext(granted_capabilities=frozenset({"file.read"}))

    result = registry.execute("file.read", {"path": str(file_path)}, context)
    assert result.output == "hello"
    with pytest.raises(PermissionDeniedError, match="outside"):
        registry.execute("file.read", {"path": str(tmp_path / "no.txt")}, context)


def test_command_tool_requires_approval_and_uses_allowlist(tmp_path: Path) -> None:
    tool = AllowlistedCommandTool(frozenset({"cmd.exe"}), (tmp_path,))
    policy = ToolPolicy()
    registry = ToolRegistry([tool], policy=policy)
    context = ExecutionContext(
        granted_capabilities=frozenset({"command.run"}), timeout_seconds=2.0
    )
    with pytest.raises(PermissionDeniedError, match="approval"):
        registry.execute(
            "command.run",
            {"command": "cmd.exe", "args": ["/c", "echo ok"], "cwd": str(tmp_path)},
            context,
        )
    result = registry.execute(
        "command.run",
        {"command": "cmd.exe", "args": ["/c", "echo ok"], "cwd": str(tmp_path)},
        context,
        approved=True,
    )
    assert "ok" in result.output

