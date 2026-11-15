"""Installing and removing skills.

The contract worth pinning: an install validates before it lands, a duplicate
never silently overwrites, and an uninstall can only ever remove a folder the
installer itself created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from super_agent.errors import ValidationError
from super_agent.skills.installer import (
    LEDGER_NAME,
    install_skill,
    uninstall_skill,
)


def _skill(root: Path, name: str, *, description: str = "does a thing") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return folder


def test_installs_a_skill_from_a_local_folder(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "commit-msg")
    library = tmp_path / "library"

    result = install_skill(str(source), library)

    assert result.installed == ("commit-msg",)
    assert (library / "commit-msg" / "SKILL.md").is_file()
    assert result.skipped == ()


def test_installs_every_skill_in_a_library_folder(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "alpha")
    _skill(source, "beta")
    library = tmp_path / "library"

    result = install_skill(str(source), library)

    assert set(result.installed) == {"alpha", "beta"}


def test_installs_a_single_skill_folder(tmp_path: Path) -> None:
    source = tmp_path / "src"
    folder = _skill(source, "solo")
    library = tmp_path / "library"

    result = install_skill(str(folder), library)

    assert result.installed == ("solo",)
    assert (library / "solo" / "SKILL.md").is_file()


def test_a_broken_skill_is_reported_and_the_rest_still_install(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "good")
    broken = source / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no description", encoding="utf-8")
    library = tmp_path / "library"

    result = install_skill(str(source), library)

    assert result.installed == ("good",)
    assert len(result.skipped) == 1
    assert "broken" in result.skipped[0][0]


def test_existing_skill_is_not_overwritten_without_force(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "dup", description="incoming")
    library = tmp_path / "library"
    _skill(library, "dup", description="already here")

    result = install_skill(str(source), library)

    assert result.installed == ()
    assert len(result.skipped) == 1
    text = (library / "dup" / "SKILL.md").read_text(encoding="utf-8")
    assert "already here" in text


def test_force_overwrites_an_existing_skill(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "dup", description="incoming")
    library = tmp_path / "library"
    _skill(library, "dup", description="already here")

    result = install_skill(str(source), library, force=True)

    assert result.installed == ("dup",)
    text = (library / "dup" / "SKILL.md").read_text(encoding="utf-8")
    assert "incoming" in text


def test_install_records_provenance(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "tracked")
    library = tmp_path / "library"

    install_skill(str(source), library)

    ledger = (library / LEDGER_NAME).read_text(encoding="utf-8")
    assert "tracked" in ledger


def test_a_folder_without_a_skill_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()

    with pytest.raises(ValidationError) as exc:
        install_skill(str(source), tmp_path / "library")
    assert "SKILL.md" in str(exc.value)


def test_missing_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        install_skill(str(tmp_path / "nope"), tmp_path / "library")
    assert "no such folder" in str(exc.value).lower() or "no such" in str(exc.value).lower()


def test_uninstall_removes_an_installed_skill(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "gone")
    library = tmp_path / "library"
    install_skill(str(source), library)

    removed = uninstall_skill("gone", library)

    assert removed == (library / "gone").resolve()
    assert not (library / "gone").exists()


def test_uninstall_refuses_a_hand_written_skill(tmp_path: Path) -> None:
    """Deleting someone's own folder because of a typo is unrecoverable."""

    library = tmp_path / "library"
    _skill(library, "handmade")

    with pytest.raises(ValidationError) as exc:
        uninstall_skill("handmade", library)
    assert (library / "handmade").is_dir()
    assert "not installed" in str(exc.value)


def test_uninstall_prunes_the_ledger(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _skill(source, "gone")
    library = tmp_path / "library"
    install_skill(str(source), library)

    uninstall_skill("gone", library)

    ledger = (library / LEDGER_NAME).read_text(encoding="utf-8")
    assert "gone" not in ledger
