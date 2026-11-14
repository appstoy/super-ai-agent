"""Restricted local file and command tools for trusted development folders."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, Tool, ToolEffect, ToolResult, ToolSpec


class _RootScopedTool:
    """Share the allowed-root containment check across file tools."""

    def __init__(self, allowed_roots: tuple[Path, ...]) -> None:
        self._roots = tuple(root.resolve() for root in allowed_roots)

    def _rooted_spec(self, spec: ToolSpec) -> ToolSpec:
        """Publish the configured roots on the spec.

        Callers that place per-task folders need to know which directory the
        tools are pinned to; ``allowed_paths`` is the existing field for exactly
        that, and it stays internal because it is not part of the input schema.
        """

        if spec.allowed_paths:
            return spec
        return replace(spec, allowed_paths=tuple(str(root) for root in self._roots))

    def _resolve_inside(
        self,
        raw_path: object,
        tool_name: str,
        context: ExecutionContext | None = None,
        *,
        confine_to_task: bool = False,
    ) -> Path:
        """Resolve a user-supplied path and reject anything outside the roots.

        A task may carry its own ``workspace_dir``. That folder confines
        **writes** only: its whole point is to keep generated artifacts out of
        the workspace root, while reading must still reach the project itself
        (sources, earlier artifacts) or the agent becomes useless. When
        ``confine_to_task`` is set, a relative path resolves inside the task
        folder and cannot leave it; absolute paths are rejected outright rather
        than silently retargeted, because the model should not be guessing
        absolute paths in the first place.
        """

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValidationError(f"{tool_name} path must be a non-empty string")
        task_root = self._task_root(context)
        path = Path(raw_path).expanduser()
        confine = confine_to_task and task_root is not None
        if not path.is_absolute():
            base = task_root if confine and task_root is not None else self._roots[0]
            path = base / path
        path = path.resolve()
        outside_task = (
            confine
            and task_root is not None
            and path != task_root
            and task_root not in path.parents
        )
        if outside_task:
            raise PermissionDeniedError(
                f"{tool_name} path is outside this task's folder: {path}. "
                f"Write inside {task_root} using a path relative to it."
            )
        if not any(path == root or root in path.parents for root in self._roots):
            raise PermissionDeniedError(
                f"{tool_name} path is outside allowed roots: {path}"
            )
        return path

    @staticmethod
    def _task_root(context: ExecutionContext | None) -> Path | None:
        """Return the task's artifact folder, if the caller supplied a usable one."""

        if context is None:
            return None
        candidate = getattr(context, "workspace_dir", None)
        if candidate is None:
            return None
        try:
            return Path(candidate).resolve()
        except (OSError, ValueError):
            return None


class ReadFileTool(_RootScopedTool, Tool):
    """Read UTF-8 files only below explicitly allowed directories."""

    def __init__(self, allowed_roots: tuple[Path, ...], *, max_bytes: int = 16 * 1024) -> None:
        super().__init__(allowed_roots)
        self._spec = ToolSpec(
            name="file.read",
            description="Read a UTF-8 file in an allowed directory.",
            capabilities=frozenset({"file.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={"path": "required"},
            max_output_bytes=max_bytes,
            risk="low",
        )

    def describe(self) -> ToolSpec:
        return self._rooted_spec(self._spec)

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        path = self._resolve_inside(arguments.get("path"), "file.read", context)
        if not path.is_file():
            raise ValidationError("file.read path is not a regular file")
        output = path.read_text(encoding="utf-8")
        return ToolResult(tool_name=self._spec.name, output=output, output_ref=f"file:{path}")


class WriteFileTool(_RootScopedTool, Tool):
    """Write a UTF-8 file below an allowed directory, creating parents as needed.

    This is the tool the Worker needs to actually produce artifacts (an HTML
    page, a script, a report). It stays inside the workspace root, refuses to
    overwrite a file unless ``overwrite`` is true, and caps the written size so
    a runaway model cannot fill the disk.
    """

    def __init__(
        self,
        allowed_roots: tuple[Path, ...],
        *,
        max_bytes: int = 1_000_000,
        max_output_bytes: int = 4 * 1024,
    ) -> None:
        super().__init__(allowed_roots)
        self._max_bytes = max_bytes
        self._spec = ToolSpec(
            name="file.write",
            description=(
                "Write a UTF-8 text file inside the workspace. Use this to create or "
                "update HTML, code, or any text artifact. Parent directories are "
                "created automatically. Set overwrite=true to replace an existing file."
            ),
            capabilities=frozenset({"file.write"}),
            effect=ToolEffect.WRITE,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path relative to the workspace root, or an absolute "
                            "path inside it."
                        ),
                    },
                    "content": {"type": "string", "description": "Full UTF-8 file content."},
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace the file if it already exists.",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
            max_output_bytes=max_output_bytes,
            risk="medium",
        )

    def describe(self) -> ToolSpec:
        return self._rooted_spec(self._spec)

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        path = self._resolve_inside(
            arguments.get("path"), "file.write", context, confine_to_task=True
        )
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValidationError("file.write content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise ValidationError(
                f"file.write content is {len(encoded)} bytes; limit is {self._max_bytes}"
            )
        overwrite = arguments.get("overwrite") is True
        existed = path.exists()
        if existed and not overwrite:
            raise ValidationError(
                f"file already exists: {path}. Pass overwrite=true to replace it."
            )
        if existed and not path.is_file():
            raise ValidationError(f"file.write target is not a regular file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "updated" if existed else "created"
        return ToolResult(
            tool_name=self._spec.name,
            output=f"{verb} {path} ({len(encoded)} bytes, {content.count(chr(10)) + 1} lines)",
            output_ref=f"file:{path}",
        )


class ListFilesTool(_RootScopedTool, Tool):
    """List entries of one directory inside an allowed root."""

    def __init__(self, allowed_roots: tuple[Path, ...], *, max_entries: int = 500) -> None:
        super().__init__(allowed_roots)
        self._max_entries = max_entries
        self._spec = ToolSpec(
            name="file.list",
            description="List files and directories under a workspace directory.",
            capabilities=frozenset({"file.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory relative to the workspace root. "
                            "Use '.' for the root."
                        ),
                        "default": ".",
                    }
                },
            },
            max_output_bytes=64 * 1024,
            risk="low",
        )

    def describe(self) -> ToolSpec:
        return self._rooted_spec(self._spec)

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        # "." means "where my output goes": the task's own folder when it has
        # one, so the model sees what it just produced instead of the
        # repository root. Any explicit path is resolved normally.
        raw_list_path = arguments.get("path")
        asks_for_default = not (
            isinstance(raw_list_path, str)
            and raw_list_path.strip()
            and raw_list_path.strip() not in {".", "./"}
        )
        task_root = self._task_root(context)
        if asks_for_default and task_root is not None:
            path = self._resolve_inside(
                str(task_root), "file.list", context
            )
        else:
            path = self._resolve_inside(
                raw_list_path if isinstance(raw_list_path, str) else ".",
                "file.list",
                context,
            )
        if not path.is_dir():
            raise ValidationError(f"file.list path is not a directory: {path}")
        rows: list[str] = []
        for child in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name)):
            if len(rows) >= self._max_entries:
                rows.append(f"... (truncated at {self._max_entries} entries)")
                break
            suffix = "/" if child.is_dir() else ""
            try:
                size = "" if child.is_dir() else f"  {child.stat().st_size}B"
            except OSError:
                size = ""
            rows.append(f"{child.name}{suffix}{size}")
        root = self._task_root(context) or self._roots[0]
        try:
            shown = path.relative_to(root)
        except ValueError:
            shown = path
        header = f"{shown if str(shown) != '.' else '.'} ({len(rows)} entries)"
        return ToolResult(
            tool_name=self._spec.name,
            output="\n".join([header, *rows]) if rows else f"{header}\n(empty)",
            output_ref=f"dir:{path}",
        )


class AllowlistedCommandTool(Tool):
    """Run one executable from an allowlist without shell interpretation."""

    def __init__(
        self,
        allowed_commands: frozenset[str],
        allowed_roots: tuple[Path, ...],
        *,
        max_output_bytes: int = 16 * 1024,
    ) -> None:
        self._commands = allowed_commands
        self._roots = tuple(root.resolve() for root in allowed_roots)
        self._spec = ToolSpec(
            name="command.run",
            description="Run an allowlisted command in an allowed directory.",
            capabilities=frozenset({"command.run"}),
            effect=ToolEffect.MOCK,
            input_schema={"command": "required", "args": "optional", "cwd": "required"},
            max_output_bytes=max_output_bytes,
            risk="high",
            requires_approval=True,
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        command = arguments.get("command")
        args = arguments.get("args", [])
        cwd_value = arguments.get("cwd")
        if not isinstance(command, str) or command not in self._commands:
            raise PermissionDeniedError("command is not allowlisted")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValidationError("command args must be a list of strings")
        if not isinstance(cwd_value, str):
            raise ValidationError("command cwd must be a string")
        cwd = Path(cwd_value).resolve()
        if not any(cwd == root or root in cwd.parents for root in self._roots):
            raise PermissionDeniedError("command cwd is outside allowed roots")
        completed = subprocess.run(
            [command, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            shell=False,
            timeout=context.timeout_seconds,
            check=False,
        )
        output = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        return ToolResult(
            tool_name=self._spec.name,
            output=output,
            output_ref=f"command:{command}:{completed.returncode}",
        )

