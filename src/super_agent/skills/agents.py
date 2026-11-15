"""Find skills that other AI agents already have on this machine.

Every agent tool follows the same convention: a skills/ folder under its own
config directory, one sub-folder per skill holding a SKILL.md.

    ~/.claude/skills/<name>/SKILL.md
    ~/.codex/skills/<name>/SKILL.md
    ~/.gemini/skills/<name>/SKILL.md
    ~/.agents/skills/<name>/SKILL.md     (the shared "agents" convention)
    ~/.vscode/skills/...                 (some editors)

Two details that are easy to get wrong and both matter here:

* Entries are often symlinks. Magpie links its own library into every agent's
  folder, so a scan that does not follow links sees an empty library.
* A folder is not a skill. .gemini/skills can hold folders with no SKILL.md at
  all. Reporting those as skills would fill the list with things that cannot be
  used, so presence of SKILL.md is checked, not assumed.

Nothing here is copied: discovery only reports what exists and where it lives,
so the panel can offer to import a skill without touching the other agent's
folder.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from super_agent.skills.loader import SKILL_FILE_NAME, Skill, parse_skill_file

# (id, label, path relative to home). Order is the order the UI shows them.
AGENT_LOCATIONS: tuple[tuple[str, str, str], ...] = (
    ("claude", "Claude Code", ".claude/skills"),
    ("codex", "Codex", ".codex/skills"),
    ("gemini", "Gemini", ".gemini/skills"),
    ("agents", "Agents (共享约定)", ".agents/skills"),
    ("cursor", "Cursor", ".cursor/skills"),
    ("opencode", "OpenCode", ".opencode/skills"),
    ("vscode", "VS Code", ".vscode/skills"),
    ("magpie", "Magpie 库", ".config/magpie/library/skills"),
)


@dataclass(frozen=True, slots=True)
class AgentSkill:
    """One skill found in another agent's folder."""

    skill: Skill
    agent_id: str
    agent_label: str
    # Where the skill really lives after resolving symlinks; used to collapse
    # the same folder appearing under several agents.
    real_path: str
    linked: bool
    # Every agent folder holding this same skill. Agents are commonly given a
    # copy of the same folder, so "installed in 3 agents" is one skill.
    agents: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.skill.name,
            "title": self.skill.title,
            "description": self.skill.description,
            "path": str(self.skill.path),
            "real_path": self.real_path,
            "agent": self.agent_id,
            "agent_label": self.agent_label,
            "agents": [list(item) for item in self.agents],
            "agent_ids": [item[0] for item in self.agents],
            "linked": self.linked,
        }


@dataclass(frozen=True, slots=True)
class AgentSource:
    """A discovered agent folder and what was found in it."""

    agent_id: str
    label: str
    path: Path
    exists: bool
    skills: tuple[AgentSkill, ...] = ()
    # Folders that look like a skill but could not be read. Reporting these is
    # the difference between "you have no such skill" and "this one is broken".
    skipped: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.agent_id,
            "label": self.label,
            "path": str(self.path),
            "exists": self.exists,
            "count": len(self.skills),
            "skipped": [list(item) for item in self.skipped],
        }


def _agent_roots(home: Path) -> list[tuple[str, str, Path]]:
    return [(agent_id, label, home / rel) for agent_id, label, rel in AGENT_LOCATIONS]


def _fingerprint(skill: Skill) -> str:
    """Identity of a skill by content, so copies under several agents collapse.

    Agents routinely receive a full copy of the same folder, so path equality is
    not enough: the same SKILL.md can live in .claude, .gemini and .agents.
    """

    digest = hashlib.sha256()
    digest.update(skill.body.encode("utf-8", errors="ignore"))
    digest.update(bytes([0]))
    digest.update(skill.description.encode("utf-8", errors="ignore"))
    for name in skill.resources:
        digest.update(name.encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def _read_skill(folder: Path) -> tuple[Skill | None, str]:
    """Parse a folder's SKILL.md.

    Returns (skill, "") on success, (None, "") when the folder simply is not a
    skill, and (None, reason) when it looks like one but cannot be read — that
    last case used to be swallowed, which made a broken skill look absent.
    """

    skill_file = folder / SKILL_FILE_NAME
    try:
        if not skill_file.is_file():  # is_file() follows symlinks, unlike islink
            return None, ""
    except OSError:
        return None, ""
    try:
        return parse_skill_file(skill_file), ""
    except Exception as exc:  # noqa: BLE001 - another agent's broken skill is not ours
        return None, f"{type(exc).__name__}: {exc}"


def discover_agent_skills(
    home: Path | None = None,
) -> tuple[tuple[AgentSource, ...], tuple[AgentSkill, ...]]:
    """Scan every known agent folder.

    Returns the per-agent sources (for the sub-filter in the UI) and the full
    list of skills with the same folder found under several agents collapsed.
    """

    root = Path(home) if home is not None else Path.home()
    sources: list[AgentSource] = []
    # Accumulate by fingerprint so copies of the same skill collapse into one
    # row that names every agent carrying it.
    by_key: dict[str, dict[str, object]] = {}
    order: list[str] = []

    for agent_id, label, folder in _agent_roots(root):
        exists = folder.is_dir()
        found: list[AgentSkill] = []
        skipped: list[tuple[str, str]] = []
        if exists:
            try:
                children = sorted(folder.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                children = []
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                skill, reason = _read_skill(child)
                if skill is None:
                    if reason:
                        skipped.append((child.name, reason))
                    continue
                key = _fingerprint(skill)
                pair = (agent_id, label)
                if key in by_key:
                    bucket = by_key[key]
                    agents_list = bucket["agents"]
                    assert isinstance(agents_list, list)
                    if pair not in agents_list:
                        agents_list.append(pair)
                    # This agent still carries the skill: its own source row must
                    # list it, even when another agent already contributed it.
                    found.append(
                        AgentSkill(
                            skill=skill,
                            agent_id=agent_id,
                            agent_label=label,
                            real_path=str(child),
                            linked=False,
                            agents=(pair,),
                        )
                    )
                    continue
                entry = {
                    "skill": skill,
                    "agent_id": agent_id,
                    "agent_label": label,
                    "real_path": str(child),
                    "agents": [pair],
                }
                by_key[key] = entry
                order.append(key)
                found.append(
                    AgentSkill(
                        skill=skill,
                        agent_id=agent_id,
                        agent_label=label,
                        real_path=str(child),
                        linked=False,
                        agents=(pair,),
                    )
                )
        sources.append(
            AgentSource(
                agent_id=agent_id,
                label=label,
                path=folder,
                exists=exists,
                skills=tuple(found),
                skipped=tuple(skipped),
            )
        )

    ordered = tuple(
        AgentSkill(
            skill=by_key[key]["skill"],  # type: ignore[arg-type]
            agent_id=str(by_key[key]["agent_id"]),
            agent_label=str(by_key[key]["agent_label"]),
            real_path=str(by_key[key]["real_path"]),
            linked=False,
            agents=tuple(by_key[key]["agents"]),  # type: ignore[arg-type]
        )
        for key in order
    )
    return tuple(sources), ordered


def agent_roots_exist(home: Path | None = None) -> int:
    """How many agent folders are present; 0 means the feature is not useful."""

    root = Path(home) if home is not None else Path.home()
    return sum(1 for _, _, folder in _agent_roots(root) if folder.is_dir())


def is_inside(path: Path, parent: Path) -> bool:
    """True when path is parent or below it, resolving symlinks first."""

    try:
        child = path.resolve()
        base = parent.resolve()
    except OSError:
        return False
    return child == base or base in child.parents


def same_source(a: Path, b: Path) -> bool:
    """True when two paths point at the same file on disk (symlink-aware)."""

    try:
        return os.path.samefile(a, b)
    except OSError:
        return False
