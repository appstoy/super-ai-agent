"""Discover, parse and select skills from disk.

A skill is a directory containing SKILL.md with YAML-ish frontmatter:

    ---
    name: release-notes
    description: Turn a git log into human-readable release notes.
    when_to_use: the user asks for release notes or a changelog
    tags: docs, writing
    ---
    # Release notes
    ...instructions...

The body is only injected into a model call when the skill is selected, so a
library of thirty skills costs nothing until one is chosen. Everything here is
deliberately tolerant: a malformed skill is reported and skipped, never allowed
to break startup or another skill.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from super_agent.errors import ValidationError

SKILL_FILE_NAME = "SKILL.md"
# A skill body is pasted into the prompt, so it must stay bounded. Long skills
# belong in bundled resource files, which the model reads on demand.
MAX_BODY_CHARS = 24_000
MAX_DESCRIPTION_CHARS = 400
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Skill:
    """One loadable playbook.

    'path' is the skill directory: bundled resources live beside SKILL.md and
    the Worker can read them through the skill_read tool.
    """

    name: str
    description: str
    body: str
    path: Path
    when_to_use: str = ""
    tags: tuple[str, ...] = ()
    source: str = "user"
    resources: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        """First markdown heading in the body, else the name."""
        for line in self.body.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip()
        return self.name

    def to_dict(self, *, include_body: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "tags": list(self.tags),
            "source": self.source,
            "resources": list(self.resources),
        }
        if include_body:
            payload["body"] = self.body
        return payload

    def summary_line(self) -> str:
        """One-line catalogue entry used in the system prompt."""
        hint = f" Use when: {self.when_to_use}" if self.when_to_use else ""
        return f"- {self.name}: {self.description}{hint}"


@dataclass(frozen=True, slots=True)
class SkillLoadReport:
    """Credential-free view of one discovery pass."""

    loaded: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    skills: tuple[Skill, ...] = field(default=(), compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "loaded": list(self.loaded),
            "failed": [list(item) for item in self.failed],
        }


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _parse_frontmatter(raw: str) -> dict[str, str]:
    """Parse the flat key/value frontmatter a skill actually needs.

    Deliberately not a YAML parser: skills are user-authored files, and pulling
    a YAML dependency in for six scalar keys is not worth the failure surface.
    Lists are supported only as comma-separated values.
    """

    fields: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        fields[key.strip().lower()] = _strip_quotes(value)
    return fields


def parse_skill_file(path: Path, *, source: str = "user") -> Skill:
    """Parse one SKILL.md into a Skill, or raise ValidationError."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read {path.name}: {exc}") from exc
    match = _FRONTMATTER_RE.match(text)
    meta: dict[str, str] = {}
    body = text
    if match:
        meta = _parse_frontmatter(match.group(1))
        body = text[match.end() :]
    body = body.strip()
    if not body:
        raise ValidationError("skill body is empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValidationError(
            f"skill body is {len(body)} chars; limit is {MAX_BODY_CHARS}. "
            "Move detail into a bundled resource file and reference it."
        )
    name = (meta.get("name") or path.parent.name).strip().lower()
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"invalid skill name {name!r}: use lowercase letters, digits, dot, dash or underscore"
        )
    description = meta.get("description", "").strip()
    if not description:
        # A skill without a description is invisible in the catalogue, which
        # makes it unselectable - fail loudly instead of loading a dead skill.
        raise ValidationError("skill needs a description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
    tags = tuple(
        item.strip().lower() for item in meta.get("tags", "").split(",") if item.strip()
    )
    resources = tuple(
        sorted(
            str(child.relative_to(path.parent)).replace("\\", "/")
            for child in path.parent.rglob("*")
            if child.is_file()
            and child.name != SKILL_FILE_NAME
            and "__pycache__" not in child.parts
        )
    )
    return Skill(
        name=name,
        description=description,
        body=body,
        path=path.parent,
        when_to_use=meta.get("when_to_use", "").strip(),
        tags=tags,
        source=meta.get("source", source) or source,
        resources=resources,
    )


def discover_skill_dirs(roots: Iterable[Path]) -> list[Path]:
    """Return every directory under the roots that holds a SKILL.md.

    A root may itself be a skill directory (.../skills/my-skill), which is what
    a user points at when they download a single skill folder.
    """

    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidate = Path(root).expanduser()
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.name == SKILL_FILE_NAME:
            resolved = resolved.parent
        if not resolved.is_dir():
            continue
        if (resolved / SKILL_FILE_NAME).is_file():
            directories = [resolved]
        else:
            directories = sorted(
                child.parent
                for child in resolved.rglob(SKILL_FILE_NAME)
                if "__pycache__" not in child.parts
            )
        for directory in directories:
            if directory in seen:
                continue
            seen.add(directory)
            found.append(directory)
    return found


class SkillRegistry:
    """Name-indexed collection of skills with failure isolation."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self._skills.setdefault(skill.name, skill)

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name.strip().lower())

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def all(self) -> tuple[Skill, ...]:
        return tuple(self._skills.values())

    def register(self, skill: Skill) -> None:
        """Add a skill, keeping the first registration of a name."""
        self._skills.setdefault(skill.name, skill)

    def catalogue(self) -> str:
        """Prompt-ready list of every skill, one line each."""
        return "\n".join(skill.summary_line() for skill in self._skills.values())


def build_skill_registry(
    roots: Iterable[Path],
    *,
    enabled: bool = True,
) -> tuple[SkillRegistry, SkillLoadReport]:
    """Load every skill under the given roots, isolating individual failures."""

    if not enabled:
        return SkillRegistry(), SkillLoadReport()
    registry = SkillRegistry()
    loaded: list[str] = []
    failed: list[tuple[str, str]] = []
    for directory in discover_skill_dirs(roots):
        skill_file = directory / SKILL_FILE_NAME
        try:
            skill = parse_skill_file(skill_file)
        except Exception as exc:  # noqa: BLE001 - one bad skill must not block the rest
            failed.append((str(skill_file), f"{type(exc).__name__}: {exc}"))
            continue
        if skill.name in registry:
            # Two roots can expose the same skill (project + user library);
            # the first wins so a project can pin its own version.
            failed.append((str(skill_file), f"duplicate skill name: {skill.name}"))
            continue
        registry.register(skill)
        loaded.append(skill.name)
    return registry, SkillLoadReport(tuple(loaded), tuple(failed), registry.all())


def select_skills(
    registry: SkillRegistry,
    requested: Iterable[str] | None,
) -> tuple[tuple[Skill, ...], tuple[str, ...]]:
    """Resolve user-requested skill names into skills.

    Returns the skills in request order plus the names that were not found, so
    a caller can tell the model (and the user) that a skill is missing instead
    of silently running without it.
    """

    if not requested:
        return (), ()
    chosen: list[Skill] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        skill = registry.get(name)
        if skill is None:
            missing.append(name)
        else:
            chosen.append(skill)
    return tuple(chosen), tuple(missing)
