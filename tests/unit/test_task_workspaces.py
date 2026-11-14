"""Per-task output folders: layout, slugging, and the write-only sandbox.

The reported problem was that generated artifacts piled up in the repository
root, so every task now writes into its own folder under one cache directory.
These tests pin the two things that matter: the folder is derived consistently
from the Task, and the sandbox narrows WRITES without blocking READS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_agent.errors import PermissionDeniedError
from super_agent.ports.tool import ExecutionContext
from super_agent.tools.restricted import ListFilesTool, ReadFileTool, WriteFileTool
from super_agent.tools.workspace import (
    DEFAULT_CACHE_DIR_NAME,
    cache_root,
    find_task_workspace,
    task_folder_name,
    task_slug,
    task_workspace,
)

TASK_ID = "005540d9-dae8-47d0-91f1-fea84c0811dc"


def test_slug_keeps_readable_text_and_drops_path_syntax() -> None:
    assert task_slug("做一个 HTML 页面") == "做一个-HTML-页面"
    assert task_slug("Build a Landing Page!!!") == "Build-a-Landing-Page"
    # Path traversal in a title must not survive into a folder name.
    assert "/" not in task_slug("../../etc/passwd")
    assert ".." not in task_slug("../../etc/passwd")


def test_slug_never_empty_and_bounded() -> None:
    assert task_slug("") == "task"
    assert task_slug("///") == "task"
    long = task_slug("a" * 200)
    assert 0 < len(long) <= 40


def test_folder_name_is_stable_and_unique_per_task() -> None:
    name = task_folder_name(TASK_ID, "做一个 HTML 页面")
    assert name == task_folder_name(TASK_ID, "做一个 HTML 页面")
    assert name.startswith("005540d9-")
    other = task_folder_name("11111111-2222-3333-4444-555555555555", "做一个 HTML 页面")
    assert other != name


def test_task_workspace_lives_under_the_cache_root(tmp_path: Path) -> None:
    folder = task_workspace(tmp_path, TASK_ID, "做一个 HTML 页面", create=True)
    assert folder.is_dir()
    assert folder.parent == cache_root(tmp_path)
    assert folder.parent.name == DEFAULT_CACHE_DIR_NAME
    # The artifact folder is never the workspace root itself.
    assert folder != tmp_path


def test_resume_finds_the_folder_created_by_the_first_attempt(tmp_path: Path) -> None:
    created = task_workspace(tmp_path, TASK_ID, "做一个 HTML 页面", create=True)
    # A resumed Worker knows only the id, and may even carry a different title.
    assert find_task_workspace(tmp_path, TASK_ID) == created
    assert find_task_workspace(tmp_path, "ffffffff-0000-0000-0000-000000000000") is None


def test_cache_name_cannot_escape_the_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cache_root(tmp_path, "../outside")
    with pytest.raises(ValueError):
        cache_root(tmp_path, "..")


def test_relative_write_lands_inside_the_task_folder(tmp_path: Path) -> None:
    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    tool = WriteFileTool((tmp_path,))
    result = tool.execute(
        {"path": "report.html", "content": "<h1>ok</h1>"},
        ExecutionContext(workspace_dir=task_dir),
    )
    assert "report.html" in result.output
    assert (task_dir / "report.html").is_file()
    # Nothing appeared in the workspace root.
    assert not (tmp_path / "report.html").exists()


def test_write_cannot_climb_out_of_the_task_folder(tmp_path: Path) -> None:
    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    tool = WriteFileTool((tmp_path,))
    with pytest.raises(PermissionDeniedError):
        tool.execute(
            {"path": "../escaped.html", "content": "x"},
            ExecutionContext(workspace_dir=task_dir),
        )
    assert not (tmp_path / "escaped.html").exists()


def test_write_rejects_an_absolute_path_into_the_root(tmp_path: Path) -> None:
    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    tool = WriteFileTool((tmp_path,))
    with pytest.raises(PermissionDeniedError):
        tool.execute(
            {"path": str(tmp_path / "absolute.html"), "content": "x"},
            ExecutionContext(workspace_dir=task_dir),
        )


def test_write_still_explains_what_went_wrong(tmp_path: Path) -> None:
    """A refusal must be actionable, otherwise the model retries blindly."""

    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    tool = WriteFileTool((tmp_path,))
    with pytest.raises(PermissionDeniedError) as excinfo:
        tool.execute(
            {"path": "../x.html", "content": "x"},
            ExecutionContext(workspace_dir=task_dir),
        )
    message = str(excinfo.value)
    assert "task" in message
    assert "relative" in message


def test_reads_are_not_trapped_inside_the_task_folder(tmp_path: Path) -> None:
    """Reading must still reach the project, or the agent cannot inspect it."""

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("print('hi')\n", encoding="utf-8")

    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    reader = ReadFileTool((tmp_path,))
    result = reader.execute(
        {"path": "src/app.py"}, ExecutionContext(workspace_dir=task_dir)
    )
    assert result.output == "print('hi')\n"


def test_listing_defaults_to_the_task_folder(tmp_path: Path) -> None:
    (tmp_path / "root-only.txt").write_text("x", encoding="utf-8")
    task_dir = task_workspace(tmp_path, TASK_ID, "task", create=True)
    (task_dir / "mine.html").write_text("x", encoding="utf-8")

    lister = ListFilesTool((tmp_path,))
    default = lister.execute({}, ExecutionContext(workspace_dir=task_dir))
    assert "mine.html" in default.output
    assert "root-only.txt" not in default.output

    # An explicit path still reaches the workspace root.
    explicit = lister.execute(
        {"path": str(tmp_path)}, ExecutionContext(workspace_dir=task_dir)
    )
    assert "root-only.txt" in explicit.output


def test_without_a_task_folder_writes_keep_legacy_behaviour(tmp_path: Path) -> None:
    """Callers that pass no folder (CLI, tests, tools used directly) still work."""

    tool = WriteFileTool((tmp_path,))
    tool.execute({"path": "plain.html", "content": "x"}, ExecutionContext())
    assert (tmp_path / "plain.html").is_file()
