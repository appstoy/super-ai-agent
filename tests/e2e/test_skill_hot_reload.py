"""Picking up a newly installed skill without restarting the server.

Installing used to require a restart because the library was scanned once at
startup. These tests pin the two halves of the fix: a new folder appears on the
next read, and an unchanged library does not get re-parsed on every request.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.skills.library import LiveSkillLibrary
from super_agent.skills.loader import build_skill_registry


def _skill(root: Path, name: str, *, description: str = "does a thing") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return folder


def _settings(tmp_path: Path, library: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        workspace_root=str(tmp_path),
        model_name="test-model",
        model_base_url="http://127.0.0.1:9/v1",
        model_api_key="test",
        skill_paths=str(library),
    )


def test_a_new_folder_appears_without_a_restart(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "first")
    view = LiveSkillLibrary([library])

    assert view.names() == ("first",)

    _skill(library, "second")
    # Directory mtimes have coarse resolution on some filesystems; nudge the
    # root so the change is always observable.
    os.utime(library, None)
    time.sleep(0.01)
    os.utime(library, None)

    assert set(view.names()) == {"first", "second"}


def test_a_removed_folder_disappears(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    folder = _skill(library, "temporary")
    view = LiveSkillLibrary([library])
    assert "temporary" in view.names()

    for child in sorted(folder.rglob("*"), reverse=True):
        child.unlink()
    folder.rmdir()
    os.utime(library, None)

    assert "temporary" not in view.names()


def test_an_edited_skill_body_is_reread(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    folder = _skill(library, "edited", description="before")
    view = LiveSkillLibrary([library])
    assert view.get("edited") is not None
    assert view.get("edited").description == "before"  # type: ignore[union-attr]

    (folder / "SKILL.md").write_text(
        "---\nname: edited\ndescription: after\n---\n# edited\n", encoding="utf-8"
    )
    # Touch the folder so its mtime moves even on coarse-timestamp filesystems.
    os.utime(folder, None)

    skill = view.get("edited")
    assert skill is not None
    assert skill.description == "after"


def test_an_unchanged_library_is_not_reparsed(tmp_path: Path, monkeypatch) -> None:
    """A re-parse per request would be wasteful; the stamp check must hold."""

    library = tmp_path / "lib"
    _skill(library, "stable")
    view = LiveSkillLibrary([library])

    calls: list[int] = []
    real = build_skill_registry

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003 - test shim
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr("super_agent.skills.library.build_skill_registry", counting)

    for _ in range(5):
        view.names()

    assert calls == [], "an unchanged library must not be re-scanned"


def test_a_broken_skill_is_reported_not_fatal(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "good")
    broken = library / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no description", encoding="utf-8")

    view = LiveSkillLibrary([library])

    assert view.names() == ("good",)
    assert len(view.failed) == 1


def test_disabled_library_stays_empty(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "ignored")

    view = LiveSkillLibrary([library], enabled=False)

    assert view.names() == ()


def test_api_lists_a_skill_installed_after_startup(tmp_path: Path) -> None:
    """End to end: the page must show a folder that landed after boot."""

    library = tmp_path / "lib"
    _skill(library, "started-with")

    with TestClient(create_app(_settings(tmp_path, library))) as client:
        before = client.get("/api/skills").json()
        assert [s["name"] for s in before["skills"]] == ["started-with"]

        _skill(library, "added-later")
        os.utime(library, None)

        after = client.get("/api/skills").json()

    assert set(s["name"] for s in after["skills"]) == {"started-with", "added-later"}
    assert after["count"] == 2
