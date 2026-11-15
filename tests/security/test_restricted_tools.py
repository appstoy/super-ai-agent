from pathlib import Path
import sys

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
    # The tool spawns [command, *args] with shell=False, so `command` has to be a
    # real executable. Using the running interpreter keeps this identical on
    # Linux and Windows -- the old version hardcoded cmd.exe and only ran on Windows.
    script = tmp_path / "hello.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    command = sys.executable
    tool = AllowlistedCommandTool(frozenset({command}), (tmp_path,))
    policy = ToolPolicy()
    registry = ToolRegistry([tool], policy=policy)
    context = ExecutionContext(
        granted_capabilities=frozenset({"command.run"}), timeout_seconds=15.0
    )
    payload = {"command": command, "args": [str(script)], "cwd": str(tmp_path)}

    with pytest.raises(PermissionDeniedError, match="approval"):
        registry.execute("command.run", payload, context)

    result = registry.execute("command.run", payload, context, approved=True)
    assert "ok" in result.output
