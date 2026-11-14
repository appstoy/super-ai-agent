"""Tools that let the Worker browse and read skills at runtime.

Selection by the user is not the only way a skill becomes relevant: a task can
match a skill the person did not pick. Giving the model `skill_list` and
`skill_read` means it can discover one mid-task instead of failing because
nobody preselected it.

Both tools are read-only and confined to the skill library, so they can run
without approval and cannot become a filesystem escape hatch.
"""

from __future__ import annotations

from pathlib import Path

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, Tool, ToolEffect, ToolResult, ToolSpec
from super_agent.skills.loader import SkillRegistry


class SkillListTool(Tool):
    """List the available skills with their one-line descriptions."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._spec = ToolSpec(
            name="skill.list",
            description=(
                "List the skills available in this workspace. Each entry is a reusable "
                "playbook of instructions. Call skill_read to load one before using it."
            ),
            capabilities=frozenset({"skill.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={"type": "object", "properties": {}},
            max_output_bytes=8 * 1024,
            risk="low",
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        skills = self._registry.all()
        if not skills:
            return ToolResult(
                tool_name=self._spec.name,
                output="No skills are installed.",
                output_ref="skill:list",
            )
        lines = [f"{len(skills)} skill(s) available:"]
        lines.extend(skill.summary_line() for skill in skills)
        return ToolResult(
            tool_name=self._spec.name,
            output="\n".join(lines),
            output_ref="skill:list",
        )


class SkillReadTool(Tool):
    """Read one skill's instructions, or one resource bundled with it."""

    def __init__(self, registry: SkillRegistry, *, max_bytes: int = 24 * 1024) -> None:
        self._registry = registry
        self._max_bytes = max_bytes
        self._spec = ToolSpec(
            name="skill.read",
            description=(
                "Load the full instructions of one skill by name. Pass 'resource' to read "
                "a file bundled inside that skill's folder (see the skill's resource list)."
            ),
            capabilities=frozenset({"skill.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name, for example 'release-notes'.",
                    },
                    "resource": {
                        "type": "string",
                        "description": "Optional path inside the skill folder, e.g. 'template.md'.",
                    },
                },
                "required": ["name"],
            },
            max_output_bytes=self._max_bytes,
            risk="low",
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del context
        raw_name = arguments.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValidationError("skill.read name must be a non-empty string")
        skill = self._registry.get(raw_name)
        if skill is None:
            available = ", ".join(self._registry.names()) or "(none installed)"
            raise ValidationError(f"unknown skill {raw_name!r}. Available skills: {available}")
        raw_resource = arguments.get("resource")
        if isinstance(raw_resource, str) and raw_resource.strip():
            return self._read_resource(skill.path, raw_resource.strip(), skill.name)
        body = skill.body
        header = [f"# skill: {skill.name}", f"(folder: {skill.path})"]
        if skill.resources:
            header.append("bundled resources: " + ", ".join(skill.resources))
        return ToolResult(
            tool_name=self._spec.name,
            output="\n".join(header) + "\n\n" + body,
            output_ref=f"skill:{skill.name}",
        )

    def _read_resource(self, root: Path, relative: str, skill_name: str) -> ToolResult:
        """Read a file inside the skill folder, refusing to leave it."""

        resolved_root = root.resolve()
        candidate = (resolved_root / relative).resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents:
            raise PermissionDeniedError("skill resource path is outside the skill folder")
        if not candidate.is_file():
            raise ValidationError(f"skill {skill_name} has no resource {relative!r}")
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError(f"cannot read skill resource: {exc}") from exc
        return ToolResult(
            tool_name=self._spec.name,
            output=text,
            output_ref=f"skill:{skill_name}:{relative}",
        )
