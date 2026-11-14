"""The Worker must be able to produce files, not just describe them.

Reported symptom: "I asked it to build an HTML page, it could not create the
file, and the write tool was never called."  Root cause: the built-in registry
only contained file.read and offered no write tool at all, so a model that
tried to write got "unknown tool" back and the request could never succeed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_agent.config import Settings
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext
from super_agent.tools.loading import build_agent_tools
from super_agent.tools.restricted import ListFilesTool, WriteFileTool


def workspace(tmp_path: Path):
    registry, capabilities, report = build_agent_tools(
        Settings(workspace_root=str(tmp_path))
    )
    assert "file.write" in capabilities
    assert "file.write" in report.builtin
    return registry, capabilities


def context(capabilities: frozenset[str]) -> ExecutionContext:
    return ExecutionContext(granted_capabilities=capabilities)


def test_worker_can_create_an_html_file_end_to_end(tmp_path) -> None:
    """The exact user scenario: create an HTML page and read it back."""

    registry, capabilities = workspace(tmp_path)
    html = "<!doctype html><html><body><h1>hello</h1></body></html>"

    result = registry.execute(
        "file.write",
        {"path": "site/index.html", "content": html},
        context(capabilities),
    )
    assert "created" in result.output

    written = tmp_path / "site" / "index.html"
    assert written.read_text(encoding="utf-8") == html

    # The same agent turn can verify its own output.
    read_back = registry.execute(
        "file.read", {"path": "site/index.html"}, context(capabilities)
    )
    assert read_back.output == html

    listing = registry.execute("file.list", {"path": "site"}, context(capabilities))
    assert "index.html" in listing.output


def test_write_requires_overwrite_for_an_existing_file(tmp_path) -> None:
    tool = WriteFileTool((tmp_path,))
    tool.execute({"path": "a.txt", "content": "first"}, ExecutionContext())

    with pytest.raises(ValidationError, match="overwrite"):
        tool.execute({"path": "a.txt", "content": "second"}, ExecutionContext())

    tool.execute(
        {"path": "a.txt", "content": "second", "overwrite": True}, ExecutionContext()
    )
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "second"


def test_write_refuses_to_escape_the_workspace(tmp_path) -> None:
    tool = WriteFileTool((tmp_path,))

    with pytest.raises(PermissionDeniedError):
        tool.execute(
            {"path": "../escaped.txt", "content": "nope"}, ExecutionContext()
        )

    assert not (tmp_path.parent / "escaped.txt").exists()


def test_write_is_capped_to_a_bounded_size(tmp_path) -> None:
    tool = WriteFileTool((tmp_path,), max_bytes=32)

    with pytest.raises(ValidationError, match="limit"):
        tool.execute(
            {"path": "big.txt", "content": "x" * 64}, ExecutionContext()
        )


def test_absolute_path_inside_the_workspace_is_allowed(tmp_path) -> None:
    tool = WriteFileTool((tmp_path,))
    target = tmp_path / "nested" / "deep" / "file.txt"

    tool.execute({"path": str(target), "content": "ok"}, ExecutionContext())
    assert target.read_text(encoding="utf-8") == "ok"


def test_file_tools_deny_a_call_without_the_capability(tmp_path) -> None:
    registry, _capabilities = workspace(tmp_path)

    with pytest.raises(PermissionDeniedError):
        registry.execute(
            "file.write",
            {"path": "blocked.txt", "content": "x"},
            ExecutionContext(granted_capabilities=frozenset({"file.read"})),
        )


def test_write_is_offered_to_the_model(tmp_path) -> None:
    """The model can only call a tool that was actually offered to it."""

    registry, _capabilities = workspace(tmp_path)
    specs = {spec.name: spec for spec in registry.specs()}

    assert "file.write" in specs
    write_spec = specs["file.write"]
    assert write_spec.effect.value == "write"
    assert set(write_spec.input_schema["required"]) == {"path", "content"}


def test_speculation_never_gets_write_capability(tmp_path) -> None:
    """Speculative drafts have no side effects, so they must not offer writes."""

    from super_agent.policies.speculative_runner import SpeculativeRunner

    registry, capabilities = workspace(tmp_path)

    runner = SpeculativeRunner(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        tools=registry,
    )
    granted = runner._granted_capabilities
    assert "file.read" in granted
    assert "file.write" not in granted
    # The shared registry still knows the tool; only the grant excludes it.
    assert "file.write" in capabilities


def test_list_files_reports_an_empty_directory(tmp_path) -> None:
    tool = ListFilesTool((tmp_path,))
    (tmp_path / "empty").mkdir()

    result = tool.execute({"path": "empty"}, ExecutionContext())
    assert "empty (0 entries)" in result.output
