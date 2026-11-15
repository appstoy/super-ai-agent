"""Discovering and importing skills from other agents on this machine.

Magpie's model: every agent keeps skills/<name>/SKILL.md under its own config
dir. The three behaviours worth pinning are the ones that were actually
surprising in the real folders:

* the same skill is often copied under several agents and must collapse to one
  row that names them all
* a folder without SKILL.md (e.g. .gemini/skills/<x> with only assets) is not a
  skill and must not be listed
* importing copies, never moves, and refuses to clobber silently
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_agent.errors import ValidationError
from super_agent.skills.agents import AGENT_LOCATIONS, discover_agent_skills
from super_agent.skills.sources import import_from_agent


def _skill(root: Path, name: str, *, body: str = "body", description: str = "d") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n{body}\n",
        encoding="utf-8",
    )
    return folder


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_finds_skills_under_each_agent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / ".claude" / "skills", "alpha")
    _skill(home / ".agents" / "skills", "beta")

    sources, skills = discover_agent_skills(home)

    by_agent = {s.agent_id: s for s in sources}
    assert by_agent["claude"].exists is True
    assert [s.skill.name for s in by_agent["claude"].skills] == ["alpha"]
    assert [s.skill.name for s in by_agent["agents"].skills] == ["beta"]
    assert {s.skill.name for s in skills} == {"alpha", "beta"}


def test_missing_agent_folders_are_reported_not_hidden(tmp_path: Path) -> None:
    sources, skills = discover_agent_skills(_home(tmp_path))

    assert skills == ()
    assert len(sources) == len(AGENT_LOCATIONS)
    assert all(source.exists is False for source in sources)


def test_a_folder_without_skill_md_is_not_a_skill(tmp_path: Path) -> None:
    """Real case: .gemini/skills can hold asset folders with no SKILL.md."""

    home = _home(tmp_path)
    assets = home / ".gemini" / "skills" / "just-assets"
    assets.mkdir(parents=True)
    (assets / "notes.md").write_text("not a skill", encoding="utf-8")
    _skill(home / ".gemini" / "skills", "real-one")

    _, skills = discover_agent_skills(home)

    assert [s.skill.name for s in skills] == ["real-one"]


def test_the_same_skill_under_several_agents_collapses_to_one_row(tmp_path: Path) -> None:
    """Agents commonly receive a copy of the same folder."""

    home = _home(tmp_path)
    for agent_dir in (".claude/skills", ".gemini/skills", ".agents/skills"):
        _skill(home / agent_dir, "shared", body="identical")

    sources, skills = discover_agent_skills(home)

    assert len(skills) == 1
    entry = skills[0]
    assert entry.skill.name == "shared"
    assert [pair[0] for pair in entry.agents] == ["claude", "gemini", "agents"]
    # each agent still lists it in its own row for the sub-filter
    assert all(len(s.skills) == 1 for s in sources if s.exists)


def test_different_content_with_the_same_name_stays_separate(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / ".claude" / "skills", "same-name", body="one")
    _skill(home / ".agents" / "skills", "same-name", body="two")

    _, skills = discover_agent_skills(home)

    assert len(skills) == 2


def test_import_copies_and_leaves_the_source_alone(tmp_path: Path) -> None:
    home = _home(tmp_path)
    source_folder = _skill(home / ".agents" / "skills", "portable")
    library = tmp_path / "lib"

    outcome = import_from_agent("agents", library, home=home)

    assert outcome.imported == ("portable",)
    assert (library / "portable" / "SKILL.md").is_file()
    assert source_folder.is_dir(), "the other agent's folder must survive"


def test_import_can_take_a_subset(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / ".agents" / "skills", "one")
    _skill(home / ".agents" / "skills", "two")
    library = tmp_path / "lib"

    outcome = import_from_agent("agents", library, names=["two"], home=home)

    assert outcome.imported == ("two",)
    assert not (library / "one").exists()


def test_import_skips_an_existing_name_unless_forced(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _skill(home / ".agents" / "skills", "dup", description="from agent")
    library = tmp_path / "lib"
    _skill(library, "dup", description="already here")

    outcome = import_from_agent("agents", library, home=home)
    assert outcome.imported == ()
    assert outcome.skipped and "已有" in outcome.skipped[0][1]

    forced = import_from_agent("agents", library, force=True, home=home)
    assert forced.imported == ("dup",)
    text = (library / "dup" / "SKILL.md").read_text(encoding="utf-8")
    assert "from agent" in text


def test_unknown_agent_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        import_from_agent("nope", tmp_path / "lib", home=_home(tmp_path))
    assert "未知的 agent" in str(exc.value)


def test_importing_from_a_missing_folder_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        import_from_agent("claude", tmp_path / "lib", home=_home(tmp_path))
    assert "不存在" in str(exc.value)



def test_only_agents_that_exist_are_listed(tmp_path: Path) -> None:
    """A pill for an agent that is not installed is noise."""

    home = _home(tmp_path)
    _skill(home / ".claude" / "skills", "alpha")

    sources, _ = discover_agent_skills(home)

    present = [s.agent_id for s in sources if s.exists]
    assert present == ["claude"]
    assert len(sources) > len(present)  # the rest are reported as absent


def test_a_broken_skill_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """This is what hid appstoy-video: it parsed badly and simply vanished."""

    home = _home(tmp_path)
    folder = home / ".claude" / "skills" / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\ndescription: no body\n---\n", encoding="utf-8")
    _skill(home / ".claude" / "skills", "fine")

    sources, skills = discover_agent_skills(home)

    claude = next(s for s in sources if s.agent_id == "claude")
    assert [s.skill.name for s in claude.skills] == ["fine"]
    assert [name for name, _ in claude.skipped] == ["broken"]
    assert claude.skipped[0][1]  # a reason, not an empty string


def test_a_large_skill_is_listed_rather_than_dropped(tmp_path: Path) -> None:
    """36k chars is a real skill, not a broken one."""

    from super_agent.skills.loader import MAX_BODY_CHARS

    home = _home(tmp_path)
    folder = home / ".claude" / "skills" / "big"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: big\ndescription: big one\n---\n" + "x" * (MAX_BODY_CHARS + 8000),
        encoding="utf-8",
    )

    sources, skills = discover_agent_skills(home)

    assert [s.skill.name for s in skills] == ["big"]
    assert skills[0].skill.on_demand is True
    claude = next(s for s in sources if s.agent_id == "claude")
    assert claude.skipped == ()
