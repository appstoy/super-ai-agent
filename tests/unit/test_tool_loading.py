"""User-loaded tool discovery, isolation and runtime wiring."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.cli import main
from super_agent.config import Settings
from super_agent.ports.tool import ExecutionContext
from super_agent.sdk import RESERVED_TOOL_NAMES, FunctionTool, build_agent_tools, load_user_tools
from super_agent.tools.registry import ToolRegistry

_PLUGIN = '''
from super_agent.ports.tool import ExecutionContext, ToolResult, ToolSpec
from uuid import uuid4

def greet(name: str, punctuation: str = "!") -> str:
    """Greet somebody."""
    return "hello " + name + punctuation

class FixedTool:
    def describe(self):
        return ToolSpec(name="fixed", description="fixed tool",
                        capabilities=frozenset({"custom.fixed"}),
                        input_schema={"value": "required"})
    def execute(self, arguments, context):
        return ToolResult(run_id=uuid4(), tool_name="fixed",
                          output=str(arguments["value"]), output_ref="fixed")

tools = (greet, FixedTool())
'''

_BROKEN = "raise RuntimeError('plugin exploded')\n"


def test_function_tool_wraps_a_plain_callable() -> None:
    def add(a: int, b: int = 2) -> int:
        """Add two integers."""

        return a + b

    tool = FunctionTool(add, capabilities=frozenset({"custom.math"}))
    spec = tool.describe()
    assert spec.name == "add"
    assert spec.description == "Add two integers."
    assert spec.input_schema == {"a": "required", "b": "optional"}
    assert tool.json_schema["properties"]["a"] == {"type": "integer"}
    result = tool.execute({"a": 3}, ExecutionContext())
    assert result.output == "5"
    assert result.tool_name == "add"


def test_load_user_tools_from_files_and_manifest(tmp_path: Path) -> None:
    plugin = tmp_path / "my_tools.py"
    plugin.write_text(_PLUGIN, encoding="utf-8")
    broken = tmp_path / "broken_tool.py"
    broken.write_text(_BROKEN, encoding="utf-8")
    manifest = tmp_path / "tools.json"
    manifest.write_text(
        json.dumps(
            {
                "tools": [
                    {"path": str(plugin), "capabilities": ["custom.greet"]},
                    {"path": str(broken), "name": "broken"},
                    {"path": str(plugin), "name": "dup", "enabled": False},
                    {"module": "no_such_module_xyz:tool", "name": "missing"},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(tool_manifest=str(manifest), workspace_root=str(tmp_path))
    registry, capabilities, report = build_agent_tools(settings)
    names = {spec.name for spec in registry.specs()}
    assert {"file.read", "file.write", "file.list", "greet", "fixed"} <= names
    assert "dup" not in names
    assert capabilities >= frozenset(
        {"file.read", "file.write", "recall.read", "custom.greet", "custom.fixed"}
    )
    assert any("broken" in label or "broken" in reason for label, reason in report.failed)
    assert any("no_such_module_xyz" in label for label, _ in report.failed)
    assert report.builtin == ("file.read", "file.write", "file.list")
    assert set(report.loaded) == {"greet", "fixed"}
    result = registry.execute(
        "greet", {"name": "Ada"}, ExecutionContext(granted_capabilities=capabilities)
    )
    assert result.output == "hello Ada!"


def test_module_attr_spec_and_reserved_name_rejection(tmp_path: Path) -> None:
    module_dir = tmp_path / "pkg"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "plugged.py").write_text(
        "from super_agent.sdk import FunctionTool\n"
        "\n"
        "def shout(text: str) -> str:\n"
        '    """Shout text."""\n'
        "    return text.upper()\n"
        "\n"
        "tool = FunctionTool(shout, capabilities=frozenset({'custom.shout'}))\n",
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        report = load_user_tools(
            [{"module": "pkg.plugged", "attr": "tool", "capabilities": ["custom.shout"]}],
            builtin_registry=ToolRegistry(),
        )
        assert report.loaded == ("shout",)
        assert report.capabilities == frozenset({"custom.shout"})

        reserved = load_user_tools(
            [{"path": _reserved_file(tmp_path)}],
            builtin_registry=ToolRegistry(),
        )
        assert reserved.loaded == ()
        assert any("reserved tool name" in reason for _, reason in reserved.failed)
    finally:
        sys.path.remove(str(tmp_path))


def _reserved_file(tmp_path: Path) -> str:
    path = tmp_path / "reserved_tool.py"
    path.write_text(
        "from super_agent.sdk import FunctionTool\n"
        "\n"
        "def noop() -> str:\n"
        '    """Noop."""\n'
        '    return ""\n'
        "\n"
        "tool = FunctionTool(noop, name='task.start')\n",
        encoding="utf-8",
    )
    return str(path)


def test_wildcard_capability_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wild.py"
    path.write_text(
        "def noop() -> str:\n"
        '    """Noop."""\n'
        '    return ""\n'
        "\n"
        "tools = (noop,)\n",
        encoding="utf-8",
    )
    report = load_user_tools(
        [{"path": str(path), "capabilities": ["*"]}],
        builtin_registry=ToolRegistry(),
    )
    assert report.loaded == ()
    assert any("wildcard" in reason for _, reason in report.failed)


def test_cli_tools_command_reports_json(tmp_path: Path, monkeypatch, capsys) -> None:
    plugin = tmp_path / "cli_tools.py"
    plugin.write_text(_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(
        "super_agent.cli.Settings.from_env",
        lambda: Settings(tool_paths=str(plugin), workspace_root=str(tmp_path)),
    )
    assert main(["tools"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["builtin"] == ["file.read", "file.write", "file.list"]
    assert "greet" in payload["loaded"]
    assert "custom.fixed" in payload["capabilities"]


def test_api_tools_endpoint_reports_loaded_tools(tmp_path: Path, monkeypatch) -> None:
    plugin = tmp_path / "api_tools.py"
    plugin.write_text(_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(
        "super_agent.api.Settings.from_env",
        lambda: Settings(tool_paths=str(plugin), workspace_root=str(tmp_path)),
    )
    client = TestClient(
        create_app(
            Settings(tool_paths=str(plugin), workspace_root=str(tmp_path), database_url="sqlite:///:memory:")
        )
    )
    response = client.get("/api/tools")
    assert response.status_code == 200
    payload = response.json()
    assert "greet" in payload["loaded"]
    assert "failed" in payload
    assert "custom.greet" in payload["capabilities"] or "custom.fixed" in payload["capabilities"]


def test_reserved_names_are_exported() -> None:
    assert "task.start" in RESERVED_TOOL_NAMES
    assert "task_status" in RESERVED_TOOL_NAMES
