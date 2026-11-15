"""Importing a folder or an archive the user picked or dropped.

What matters: a wrapped archive installs under the skill's own name, a folder
holding several skills installs all of them, anything without SKILL.md is
refused with a readable reason, and a malicious archive cannot write outside
the temp tree.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from super_agent.errors import ValidationError
from super_agent.skills.sources import import_from_path


def _skill(root: Path, name: str, *, description: str = "d") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return folder


def test_imports_a_single_skill_folder(tmp_path: Path) -> None:
    source = _skill(tmp_path / "src", "solo")
    library = tmp_path / "lib"

    outcome = import_from_path(source, library)

    assert outcome.imported == ("solo",)
    assert (library / "solo" / "SKILL.md").is_file()


def test_imports_every_skill_in_a_folder(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _skill(src, "one")
    _skill(src, "two")
    library = tmp_path / "lib"

    outcome = import_from_path(src, library)

    assert set(outcome.imported) == {"one", "two"}


def test_a_wrapped_archive_installs_under_the_skill_name(tmp_path: Path) -> None:
    """A download unpacks to wrapper/SKILL.md; the wrapper is not the name."""

    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "my-wrapper/SKILL.md",
            "---\nname: inner-name\ndescription: d\n---\n# Inner\n",
        )
        bundle.writestr("my-wrapper/notes.md", "# notes")
    library = tmp_path / "lib"

    outcome = import_from_path(archive, library)

    assert outcome.imported == ("inner-name",)
    assert (library / "inner-name" / "SKILL.md").is_file()
    assert not (library / "my-wrapper").exists()


def test_imports_a_tarball(tmp_path: Path) -> None:
    src = _skill(tmp_path / "src", "tarred")
    archive = tmp_path / "pkg.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(src, arcname="tarred")
    library = tmp_path / "lib"

    outcome = import_from_path(archive, library)

    assert outcome.imported == ("tarred",)


def test_a_folder_without_skill_md_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValidationError) as exc:
        import_from_path(empty, tmp_path / "lib")
    assert "SKILL.md" in str(exc.value)


def test_a_missing_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        import_from_path(tmp_path / "nope", tmp_path / "lib")
    assert "不存在" in str(exc.value)


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    """An entry named ../../x must not be extracted anywhere."""

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../evil.md", "pwned")

    with pytest.raises(ValidationError) as exc:
        import_from_path(archive, tmp_path / "lib")
    assert "越界" in str(exc.value)
    assert not (tmp_path.parent / "evil.md").exists()


def test_tar_links_are_rejected(tmp_path: Path) -> None:
    """A link inside an archive can point anywhere on the machine."""

    archive = tmp_path / "linky.tar"
    with tarfile.open(archive, "w") as bundle:
        info = tarfile.TarInfo("evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        bundle.addfile(info)

    with pytest.raises(ValidationError) as exc:
        import_from_path(archive, tmp_path / "lib")
    assert "链接" in str(exc.value)


def test_an_existing_name_is_skipped_unless_forced(tmp_path: Path) -> None:
    source = _skill(tmp_path / "src", "dup", description="new")
    library = tmp_path / "lib"
    _skill(library, "dup", description="old")

    outcome = import_from_path(source, library)
    assert outcome.imported == ()
    assert "已存在" in outcome.skipped[0][1]

    forced = import_from_path(source, library, force=True)
    assert forced.imported == ("dup",)
    text = (library / "dup" / "SKILL.md").read_text(encoding="utf-8")
    assert "new" in text


def test_a_nested_skill_folder_is_not_counted_twice(tmp_path: Path) -> None:
    """A resource folder inside a skill is material, not a second skill."""

    outer = _skill(tmp_path / "src", "outer")
    inner = outer / "examples" / "inner"
    inner.mkdir(parents=True)
    (inner / "SKILL.md").write_text(
        "---\nname: inner\ndescription: d\n---\n# Inner\n", encoding="utf-8"
    )
    library = tmp_path / "lib"

    outcome = import_from_path(tmp_path / "src", library)

    assert outcome.imported == ("outer",)

