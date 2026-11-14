"""User-loaded tools: from plain functions, modules, files or entry points.

All loaded tools still go through ToolRegistry and ToolPolicy; this module only
discovers them. One broken tool never blocks startup: every failure is captured
in ToolLoadReport.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from super_agent.config import Settings
from super_agent.errors import ConfigurationError, ValidationError
from super_agent.ports.tool import ExecutionContext, Tool, ToolEffect, ToolResult, ToolSpec
from super_agent.security.policy import ToolPolicy, ToolRisk
from super_agent.skills.loader import (
    SkillLoadReport,
    build_skill_registry,
)
from super_agent.skills.tools import SkillListTool, SkillReadTool
from super_agent.tools.registry import ToolRegistry
from super_agent.tools.restricted import ListFilesTool, ReadFileTool, WriteFileTool

RESERVED_TOOL_NAMES = frozenset(
    {"task.start", "task.stop", "task.status", "task_start", "task_stop", "task_status"}
)
_EXPORT_NAMES = ("tool", "TOOL", "tools", "TOOLS", "make_tool", "create_tool", "build_tool")
_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
}


def _json_type(annotation: object) -> str:
    name = str(annotation).replace("| None", "").strip().lower()
    for key, value in _TYPE_MAP.items():
        if name == key or name.endswith("." + key):
            return value
    return "string"


@dataclass(frozen=True, slots=True)
class ToolLoadReport:
    """Structured, credential-free view of one tool discovery pass."""

    builtin: tuple[str, ...] = ()
    loaded: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    capabilities: frozenset[str] = frozenset()
    registry: ToolRegistry | None = field(default=None, compare=False, repr=False)
    skills: SkillLoadReport = field(default_factory=SkillLoadReport, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "builtin": list(self.builtin),
            "loaded": list(self.loaded),
            "failed": [list(item) for item in self.failed],
            "capabilities": sorted(self.capabilities),
            "skills": self.skills.to_dict(),
        }


class FunctionTool(Tool):
    """Adapt a plain Python callable into the capability-checked Tool port."""

    def __init__(
        self,
        func: Callable[..., object],
        *,
        name: str | None = None,
        description: str | None = None,
        capabilities: frozenset[str] = frozenset(),
        max_output_bytes: int = 16 * 1024,
    ) -> None:
        if not callable(func):
            raise ValidationError("FunctionTool requires a callable")
        self._func = func
        raw_name = name or getattr(func, "__name__", "custom_tool") or "custom_tool"
        self._name = str(raw_name).strip()
        if not self._name:
            raise ValidationError("custom tool name must not be empty")
        doc = inspect.getdoc(func) or ""
        first_line = doc.splitlines()[0] if doc else ""
        self._description = description or first_line or f"User-provided tool {self._name}."
        self._parameters: dict[str, object] = {}
        self._accepted: set[str] = set()
        input_schema: dict[str, object] = {}
        self._signature = inspect.signature(func)
        for parameter in self._signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            ):
                continue
            self._accepted.add(parameter.name)
            required = parameter.default is inspect.Parameter.empty
            schema: dict[str, object] = {"type": _json_type(parameter.annotation)}
            if not required:
                schema["default"] = parameter.default
            self._parameters[parameter.name] = schema
            input_schema[parameter.name] = "required" if required else "optional"
        self._json_schema: dict[str, object] = {
            "type": "object",
            "properties": self._parameters,
            "required": [key for key, value in input_schema.items() if value == "required"],
        }
        self._spec = ToolSpec(
            name=self._name,
            description=self._description,
            capabilities=frozenset(capabilities),
            effect=ToolEffect.READ_ONLY,
            input_schema=input_schema,
            max_output_bytes=max_output_bytes,
        )

    @property
    def json_schema(self) -> dict[str, object]:
        """Standard JSON Schema view for adapters that prefer it."""

        return dict(self._json_schema)

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del context
        selected = {key: value for key, value in arguments.items() if key in self._accepted}
        try:
            value = self._func(**selected)
        except Exception as exc:  # noqa: BLE001 - isolate user code
            raise ValidationError(
                f"custom tool {self._name} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(value, str):
            output = value
        else:
            try:
                output = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"custom tool {self._name} result is not serializable"
                ) from exc
        encoded = output.encode("utf-8")
        truncated = len(encoded) > self._spec.max_output_bytes
        if truncated:
            output = encoded[: self._spec.max_output_bytes].decode("utf-8", errors="ignore")
        return ToolResult(
            run_id=uuid4(),
            tool_name=self._name,
            output=output,
            output_ref=f"custom-tool:{self._name}",
            truncated=truncated,
        )


def _is_tool(value: object) -> bool:
    return callable(getattr(value, "describe", None)) and callable(
        getattr(value, "execute", None)
    )


def _coerce(value: object, spec: dict[str, Any]) -> Tool | tuple[Tool, ...]:
    """Turn one discovered object into one or more Tool instances."""

    if isinstance(value, (list, tuple, set, frozenset)) and not _is_tool(value):
        collected: list[Tool] = []
        for item in value:
            one = _coerce(item, spec)
            collected.extend(one if isinstance(one, tuple) else (one,))
        return tuple(collected)
    options = spec.get("args")
    kwargs: dict[str, object] = options if isinstance(options, dict) else {}
    name = str(spec.get("name") or "")
    description = str(spec.get("description") or "")
    raw_caps = spec.get("capabilities", ())
    capabilities = (
        frozenset(str(item) for item in raw_caps)
        if isinstance(raw_caps, (list, tuple, set, frozenset))
        else frozenset()
    )
    kind = str(spec.get("kind") or "auto").lower()

    def from_function(func: Callable[..., object]) -> Tool:
        return FunctionTool(
            func,
            name=name or None,
            description=description or None,
            capabilities=capabilities,
        )

    if kind == "function":
        if not callable(value):
            raise ValidationError("kind=function requires a callable")
        return from_function(value)
    if kind == "factory":
        if not callable(value):
            raise ValidationError("kind=factory requires a callable")
        value = value(**kwargs)
    if _is_tool(value):
        return cast(Tool, value)
    if isinstance(value, type):
        instance = value(**kwargs)
        if _is_tool(instance):
            return cast(Tool, instance)
        raise ValidationError("tool class did not produce a Tool instance")
    if callable(value):
        if kind == "class":
            raise ValidationError("kind=class requires a Tool class")
        if kind == "auto" and kwargs:
            produced = value(**kwargs)
            if _is_tool(produced):
                return cast(Tool, produced)
            raise ValidationError("factory did not produce a Tool instance")
        return from_function(value)
    raise ValidationError("unsupported tool object")


def _load_module(spec: dict[str, Any]) -> tuple[object, str]:
    """Import a ``module:attr`` or file-path spec and return (object, label)."""

    path_value = spec.get("path")
    module_name = str(spec.get("module") or "")
    attr = str(spec.get("attr") or "")
    if path_value:
        path = Path(str(path_value)).expanduser().resolve()
        if not path.is_file():
            raise ValidationError(f"tool file does not exist: {path}")
        module_id = f"super_agent_user_tool_{abs(hash(str(path)))}"
        module_spec = importlib.util.spec_from_file_location(module_id, path)
        if module_spec is None or module_spec.loader is None:
            raise ValidationError(f"cannot load tool file: {path}")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_id] = module
        try:
            module_spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - isolate user code
            raise ValidationError(f"tool file import failed: {type(exc).__name__}: {exc}") from exc
        label = str(path)
        if attr:
            value = getattr(module, attr, None)
            if value is None:
                raise ValidationError(f"tool file {path} has no attribute {attr!r}")
            return value, label
        for export in _EXPORT_NAMES:
            value = getattr(module, export, None)
            if value is not None:
                return value, label
        raise ValidationError(f"tool file {path} must export one of {', '.join(_EXPORT_NAMES)}")
    if not module_name:
        raise ValidationError("tool spec needs module:attr or a path")
    module = importlib.import_module(module_name)
    if not attr:
        for export in _EXPORT_NAMES:
            value = getattr(module, export, None)
            if value is not None:
                return value, module_name
        raise ValidationError(f"module {module_name} exports no tool")
    value = getattr(module, attr, None)
    if value is None:
        raise ValidationError(f"module {module_name} has no attribute {attr!r}")
    return value, f"{module_name}:{attr}"


def _normalize_spec(raw: object) -> dict[str, Any]:
    """Normalize string, dict and entry-point specs into one shape."""

    if isinstance(raw, dict):
        spec: dict[str, Any] = dict(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            spec = {"enabled": False}
        elif text.lower().endswith(".py") or "/" in text or "\\" in text:
            # A trailing ":attr" may follow a Windows drive letter (C:\...),
            # so split on the LAST colon after any path separator instead of
            # the first one.
            tail = ""
            separators = [text.rfind("/"), text.rfind("\\")]
            last_sep = max(separators)
            last_colon = text.rfind(":")
            if last_colon > last_sep and last_colon > 1:
                tail = text[last_colon + 1 :].strip()
                text = text[:last_colon]
            spec = {"path": text}
            if tail:
                spec["attr"] = tail
        elif ":" in text:
            module_name, _, attr = text.partition(":")
            spec = {"module": module_name, "attr": attr}
        else:
            spec = {"module": text}
    else:
        spec = {"_label": repr(raw), "enabled": False}
    label = spec.get("_label")
    if not isinstance(label, str) or not label:
        label = str(spec.get("path") or spec.get("module") or spec.get("name") or "tool")
    spec["_label"] = label
    return spec


def load_user_tools(
    specs: Iterable[object],
    *,
    builtin_registry: ToolRegistry | None = None,
    base_capabilities: frozenset[str] = frozenset(),
) -> ToolLoadReport:
    """Load tools with failure isolation; one bad spec never blocks the rest."""

    registry = builtin_registry or ToolRegistry()
    builtin_names = tuple(item.name for item in registry.specs())
    loaded: list[str] = []
    failed: list[tuple[str, str]] = []
    capabilities = set(base_capabilities)
    for raw in specs:
        spec = _normalize_spec(raw)
        label = str(spec.get("_label") or "tool")
        if not spec.get("enabled", True):
            continue
        try:
            load_error = spec.get("_error")
            if isinstance(load_error, str) and load_error:
                raise ValidationError(load_error)
            declared = spec.get("capabilities", ())
            if isinstance(declared, (list, tuple, set, frozenset)):
                bad = [item for item in declared if not str(item).strip() or str(item) == "*"]
                if bad:
                    raise ValidationError("wildcard or empty capability is not allowed")
            value = spec.get("_entry_point")
            if value is None:
                value, label = _load_module(spec)
            tools = _coerce(value, spec)
            items = tools if isinstance(tools, tuple) else (tools,)
            for tool in items:
                tool_spec = tool.describe()
                if tool_spec.name in RESERVED_TOOL_NAMES:
                    raise ValidationError(f"reserved tool name: {tool_spec.name}")
                registry.register(tool)
                loaded.append(tool_spec.name)
                capabilities.update(tool_spec.capabilities)
        except Exception as exc:  # noqa: BLE001 - failure isolation is the contract
            alias = str(spec.get("name") or "")
            display = f"{label} ({alias})" if alias else label
            failed.append((display, f"{type(exc).__name__}: {exc}"))
    return ToolLoadReport(
        builtin=builtin_names,
        loaded=tuple(loaded),
        failed=tuple(failed),
        capabilities=frozenset(capabilities),
        registry=registry,
    )


def resolve_tool_specs(settings: Settings) -> list[object]:
    """Aggregate manifest, env modules/paths and installed entry points."""

    found: list[object] = []
    seen: set[str] = set()

    def add(item: object, key: str) -> None:
        if key not in seen:
            seen.add(key)
            found.append(item)

    manifest_value = settings.tool_manifest
    manifest_path = Path(manifest_value) if manifest_value else Path.cwd() / "tools.json"
    if manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"invalid tool manifest {manifest_path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list):
            raise ConfigurationError(f"tool manifest {manifest_path} needs a tools list")
        for item in raw["tools"]:
            add(item, json.dumps(item, sort_keys=True, default=str))

    for item in settings.tool_modules.split(","):
        text = item.strip()
        if text:
            add(text, f"module:{text}")

    for item in settings.tool_paths.split(os.pathsep):
        text = item.strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if path.is_dir():
            for child in sorted(path.glob("*.py")):
                if child.name != "__init__.py":
                    add(str(child.resolve()), f"path:{child.resolve()}")
        else:
            add(str(path), f"path:{path}")

    discovered_entries: list[object] = []
    try:
        discovered_entries.extend(list(entry_points(group="super_agent.tools")))
    except Exception:  # noqa: BLE001 - optional plugin discovery must not break startup
        pass
    for raw_entry in discovered_entries:
        entry: Any = raw_entry
        entry_name = str(getattr(entry, "name", "unnamed"))
        try:
            value = entry.load()
        except Exception as exc:  # noqa: BLE001 - isolate third-party plugins
            found.append(
                {
                    "_label": f"entry-point:{entry_name}",
                    "enabled": True,
                    "_entry_point": None,
                    "_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        spec: dict[str, Any] = {
            "_label": f"entry-point:{entry_name}",
            "_entry_point": value,
            "name": entry_name,
        }
        add(spec, f"entry-point:{entry_name}")
    return found


def resolve_skill_roots(settings: Settings, workspace_root: Path) -> list[Path]:
    """Return every directory that may contain skills, project first.

    Default is <workspace_root>/skills so a project ships its own playbooks
    next to the code that needs them; SUPER_AGENT_SKILL_PATHS adds more
    (separated by the OS path separator). A path may point at a library folder
    or at one individual skill folder.
    """

    roots: list[Path] = [workspace_root / "skills"]
    for item in settings.skill_paths.split(os.pathsep):
        text = item.strip()
        if text:
            roots.append(Path(text).expanduser())
    return roots


def build_agent_tools(
    settings: Settings,
    *,
    workspace_root: Path | None = None,
) -> tuple[ToolRegistry, frozenset[str], ToolLoadReport]:
    """Build the built-in + user-loaded registry and its granted capabilities.

    The built-in set is deliberately small but must be able to produce work:
    reading alone meant the Worker could describe a file yet never create one,
    so a "build me an HTML page" request could only ever fail. Writes stay
    scoped to ``workspace_root`` and go through the same capability policy.
    """

    root = (workspace_root or Path(settings.workspace_root)).resolve()
    # Workspace-scoped writes are the product, so MEDIUM is pre-approved here;
    # HIGH-risk actions (shell) still require explicit human approval.
    policy = ToolPolicy(approved_risks=frozenset({ToolRisk.LOW, ToolRisk.MEDIUM}))
    registry = ToolRegistry(
        [
            ReadFileTool((root,)),
            WriteFileTool((root,)),
            ListFilesTool((root,)),
        ],
        policy=policy,
    )
    base_capabilities = frozenset({"file.read", "file.write", "recall.read"})
    report = load_user_tools(
        resolve_tool_specs(settings),
        builtin_registry=registry,
        base_capabilities=base_capabilities,
    )

    # Skills are instruction playbooks, not callable actions, but the Worker
    # still needs a way to discover and read them mid-task: a request can match
    # a skill nobody preselected. Registering these two read-only tools costs
    # nothing when the library is empty and keeps selection optional.
    skills, skill_report = build_skill_registry(
        resolve_skill_roots(settings, root), enabled=settings.skills_enabled
    )
    capabilities = set(report.capabilities)
    builtin_names = report.builtin
    if len(skills) > 0:
        registry.register(SkillListTool(skills))
        registry.register(SkillReadTool(skills))
        capabilities.add("skill.read")
        # /api/tools must agree with what the model can actually call. Skill
        # tools are runtime-provided, so append them to the builtin list rather
        # than re-snapshotting the registry (which would also sweep user tools
        # into "builtin" and break that field's meaning).
        builtin_names = (*report.builtin, "skill.list", "skill.read")

    return (
        registry,
        frozenset(capabilities),
        ToolLoadReport(
            builtin=builtin_names,
            loaded=report.loaded,
            failed=report.failed,
            capabilities=frozenset(capabilities),
            registry=report.registry,
            skills=skill_report,
        ),
    )
