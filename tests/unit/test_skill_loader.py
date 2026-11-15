"""Skill discovery, parsing and selection.

A skill is a folder with SKILL.md. These tests pin the behaviours the feature
depends on: a bad skill never blocks a good one, the body is bounded, and
selecting an unknown name is reported instead of silently ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_agent.errors import ValidationError
from super_agent.skills.loader import (
    MAX_BODY_CHARS,
    SkillRegistry,
    build_skill_registry,
    discover_skill_dirs,
    parse_skill_file,
    select_skills,
)


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "does a thing",
    body: str = "# Do it",
) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"when_to_use: the user asks\ntags: one, two\n---\n{body}\n",
        encoding="utf-8",
    )
    return folder


def test_parses_frontmatter_and_body(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path, "release-notes", body="# Release notes\n\nBe brief.")
    skill = parse_skill_file(folder / "SKILL.md")

    assert skill.name == "release-notes"
    assert skill.description == "does a thing"
    assert skill.when_to_use == "the user asks"
    assert skill.tags == ("one", "two")
    assert skill.title == "Release notes"
    assert skill.path == folder


def test_frontmatter_is_optional_but_description_is_not(tmp_path: Path) -> None:
    """A folder name can supply the name; nothing can supply a description.

    Without one the skill is invisible in the catalogue and therefore
    unselectable, so it must be rejected at load time rather than shipped dead.
    """

    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# Plain\n\nJust instructions.", encoding="utf-8")

    with pytest.raises(ValidationError) as exc:
        parse_skill_file(folder / "SKILL.md")
    assert "description" in str(exc.value)


def test_missing_description_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "nodesc"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# No description\n\nbody", encoding="utf-8")

    with pytest.raises(ValidationError) as exc:
        parse_skill_file(folder / "SKILL.md")
    assert "description" in str(exc.value)


def test_oversized_body_loads_on_demand_instead_of_vanishing(tmp_path: Path) -> None:
    """A long skill is real, not invalid.

    appstoy-video is 36k chars of routing tables. Rejecting it made it absent
    from the list with no explanation; now it loads and only its head is
    inlined, with the rest available through skill_read.
    """

    folder = _write_skill(tmp_path, "huge", body="x" * (MAX_BODY_CHARS + 500))

    skill = parse_skill_file(folder / "SKILL.md")

    assert skill.on_demand is True
    assert len(skill.body) == MAX_BODY_CHARS + 500
    excerpt = skill.excerpt()
    assert len(excerpt) < len(skill.body)
    assert "skill_read" in excerpt


def test_a_normal_body_is_inlined_whole(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path, "short", body="# Short\n\nDo it.")


    skill = parse_skill_file(folder / "SKILL.md")

    assert skill.on_demand is False
    assert skill.excerpt() == skill.body


def test_invalid_name_is_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: Bad Name\ndescription: x\n---\nbody", encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        parse_skill_file(folder / "SKILL.md")


def test_one_broken_skill_does_not_block_the_rest(tmp_path: Path) -> None:
    _write_skill(tmp_path, "good")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\ndescription: no body\n---\n", encoding="utf-8")

    registry, report = build_skill_registry([tmp_path])

    assert registry.names() == ("good",)
    assert len(report.failed) == 1
    assert "broken" in report.failed[0][0]


def test_duplicate_names_keep_the_first(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    _write_skill(first, "dup", description="first")
    _write_skill(second, "dup", description="second")

    registry, report = build_skill_registry([first, second])

    skill = registry.get("dup")
    assert skill is not None
    assert skill.description == "first"
    assert any("duplicate" in reason for _, reason in report.failed)


def test_discover_accepts_a_single_skill_folder(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path, "solo")

    assert discover_skill_dirs([folder]) == [folder]


def test_disabled_library_loads_nothing(tmp_path: Path) -> None:
    _write_skill(tmp_path, "good")

    registry, report = build_skill_registry([tmp_path], enabled=False)

    assert registry.names() == ()
    assert report.loaded == ()


def test_select_reports_unknown_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, "known")
    registry, _ = build_skill_registry([tmp_path])

    chosen, missing = select_skills(registry, ["known", "nope", "KNOWN"])

    assert [skill.name for skill in chosen] == ["known"]
    assert missing == ("nope",)


def test_select_keeps_request_order_and_dedupes(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    registry, _ = build_skill_registry([tmp_path])

    chosen, missing = select_skills(registry, ["beta", "alpha", "beta"])

    assert [skill.name for skill in chosen] == ["beta", "alpha"]
    assert missing == ()


def test_catalogue_lists_every_skill(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", description="first skill")
    _write_skill(tmp_path, "beta", description="second skill")
    registry, _ = build_skill_registry([tmp_path])

    catalogue = registry.catalogue()

    assert "alpha: first skill" in catalogue
    assert "beta: second skill" in catalogue


def test_bundled_resources_are_listed(tmp_path: Path) -> None:
    folder = _write_skill(tmp_path, "with-assets")
    (folder / "template.md").write_text("template body", encoding="utf-8")

    skill = parse_skill_file(folder / "SKILL.md")

    assert "template.md" in skill.resources


def test_empty_registry_catalogue_is_empty() -> None:
    assert SkillRegistry().catalogue() == ""
