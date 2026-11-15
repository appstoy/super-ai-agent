"""Install skills from a local folder or a git repository.

Installing is a copy, not a move, and it validates before it lands: a skill
that cannot be parsed is rejected at install time instead of silently showing
up broken in the catalogue. Uninstalling only ever removes a folder the
installer itself owns (recorded in a ledger), so a hand-written skill can never
be deleted by accident.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from super_agent.errors import ValidationError
from super_agent.skills.loader import (
    SKILL_FILE_NAME,
    Skill,
    SkillRegistry,
    discover_skill_dirs,
    parse_skill_file,
)

# Which skills this installer put in the library. Anything not listed here was
# created by hand and is off-limits to uninstall.
LEDGER_NAME = ".installed.json"


@dataclass(frozen=True, slots=True)
class InstallResult:
    """One install attempt: what landed, and what was skipped and why."""

    installed: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    destination: Path | None = None
    source: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": list(self.installed),
            "skipped": [list(item) for item in self.skipped],
            "destination": str(self.destination) if self.destination else None,
            "source": self.source,
        }


def _read_ledger(library: Path) -> dict[str, str]:
    """Return {installed_name: original_source} for installer-owned skills."""

    path = library / LEDGER_NAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt ledger must not block installs; treat it as empty rather
        # than crashing, since it is only used to guard uninstall.
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("skills")
    if not isinstance(entries, dict):
        return {}
    return {str(key): str(value) for key, value in entries.items()}


def _write_ledger(library: Path, entries: dict[str, str]) -> None:
    library.mkdir(parents=True, exist_ok=True)
    path = library / LEDGER_NAME
    payload = {"version": 1, "skills": entries}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _clone_repository(url: str, destination: Path, *, ref: str | None) -> Path:
    """Clone a repo into destination, shallow when no ref is pinned."""

    git = shutil.which("git")
    if git is None:
        raise ValidationError(
            "git is not installed or not on PATH; install git, or pass a local "
            "folder path instead of a repository URL"
        )
    command = [git, "clone", "--depth", "1"]
    if ref:
        # A branch can be fetched shallowly; an arbitrary commit cannot, so pin
        # by branch/tag here and let the caller use a local clone for commits.
        command += ["--branch", ref]
    command += [url, str(destination)]
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=180
    )
    if completed.returncode != 0:
        reason = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = reason[-1] if reason else f"exit {completed.returncode}"
        raise ValidationError(f"git clone failed: {detail}")
    return destination


def _looks_like_git_url(value: str) -> bool:
    """Distinguish a repository URL from a filesystem path."""

    text = value.strip()
    if text.startswith(("http://", "https://", "git@", "ssh://")):
        return True
    if text.startswith("git://"):
        return True
    # "owner/repo" is a shorthand people type for GitHub; a real local path
    # that looks like this still exists on disk, so check that first.
    if Path(text).expanduser().exists():
        return False
    return bool(text.count("/") == 1 and not text.startswith((".", "/", "~"))) and (
        "." not in text.split("/")[0]
    )


def _normalize_github_shorthand(value: str) -> str:
    """Turn "owner/repo" into a clone URL; leave everything else alone."""

    text = value.strip()
    if _looks_like_git_url(text) and not text.startswith(
        ("http://", "https://", "git@", "ssh://", "git://")
    ):
        return f"https://github.com/{text}.git"
    return text


def install_skill(
    source: str,
    library: Path,
    *,
    ref: str | None = None,
    force: bool = False,
) -> InstallResult:
    """Copy one or several skills from source into the library.

    source may be a local folder holding skills, a single skill folder, or a git
    repository (which may hold one skill at the root or many in subfolders).
    """

    text = str(source).strip()
    if not text:
        raise ValidationError("install source must not be empty")
    library = Path(library).expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="super-agent-skill-") as scratch:
        local = Path(text).expanduser()
        if local.exists():
            root = local.resolve()
            origin = str(root)
        else:
            url = _normalize_github_shorthand(text)
            if not _looks_like_git_url(url) and not url.startswith("https://"):
                raise ValidationError(
                    f"no such folder: {local}. Pass an existing path, a git URL, "
                    "or 'owner/repo'."
                )
            root = _clone_repository(url, Path(scratch) / "repo", ref=ref)
            origin = url

        found = discover_skill_dirs([root])
        if not found:
            raise ValidationError(
                f"no {SKILL_FILE_NAME} found in {origin}. A skill is a folder "
                f"containing {SKILL_FILE_NAME}."
            )

        existing = SkillRegistry()
        for name in library.iterdir():
            skill_file = name / SKILL_FILE_NAME
            if skill_file.is_file():
                try:
                    existing.register(parse_skill_file(skill_file))
                except Exception:  # noqa: BLE001 - a broken file cannot collide
                    continue

        ledger = _read_ledger(library)
        installed: list[str] = []
        skipped: list[tuple[str, str]] = []
        for folder in found:
            try:
                skill = parse_skill_file(folder / SKILL_FILE_NAME)
            except Exception as exc:  # noqa: BLE001 - report, do not abort the batch
                skipped.append((str(folder), f"{type(exc).__name__}: {exc}"))
                continue
            target = library / skill.name
            clash = existing.get(skill.name)
            if clash is not None and not force:
                skipped.append(
                    (
                        skill.name,
                        f"already installed from {clash.path}; pass --force to overwrite",
                    )
                )
                continue
            if target.exists():
                # Only replace a folder we own, unless the caller insists.
                if skill.name not in ledger and not force:
                    skipped.append(
                        (skill.name, f"{target} exists and was not installed here")
                    )
                    continue
                shutil.rmtree(target)
            shutil.copytree(folder, target)
            existing.register(parse_skill_file(target / SKILL_FILE_NAME))
            ledger[skill.name] = origin
            installed.append(skill.name)

        if installed:
            _write_ledger(library, ledger)
        return InstallResult(
            installed=tuple(installed),
            skipped=tuple(skipped),
            destination=library,
            source=origin,
        )


def uninstall_skill(name: str, library: Path) -> Path:
    """Remove an installer-owned skill. Hand-written skills are refused."""

    library = Path(library).expanduser().resolve()
    ledger = _read_ledger(library)
    key = str(name).strip().lower()
    if not key:
        raise ValidationError("skill name must not be empty")
    if key not in ledger:
        raise ValidationError(
            f"{key} was not installed by 'super-agent skills install', so it is "
            "not removed automatically. Delete the folder yourself if you are sure: "
            f"{library / key}"
        )
    target = library / key
    if not target.is_dir():
        ledger.pop(key, None)
        _write_ledger(library, ledger)
        raise ValidationError(f"{key} was recorded but its folder is already gone")
    shutil.rmtree(target)
    ledger.pop(key, None)
    _write_ledger(library, ledger)
    return target


def describe_skill_folder(path: Path) -> Skill | None:
    """Parse a folder for display, or None when it is not a skill."""

    skill_file = Path(path) / SKILL_FILE_NAME
    if not skill_file.is_file():
        return None
    try:
        return parse_skill_file(skill_file)
    except Exception:  # noqa: BLE001 - callers only want a display record
        return None
