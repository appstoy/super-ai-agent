"""A skill library that picks up new folders without a restart.

Skills were read once at process start, so installing one meant editing the
disk and then restarting the server before the page would show it \u2014 a bad
trade for a folder of Markdown. This keeps the parsed registry but re-checks
the library's modification stamps first, and only re-parses when something
actually changed.

The cheap check matters: this sits in front of every task submission, and
re-parsing thirty skills on every keystroke-driven poll would be wasteful.
"""

from __future__ import annotations

import threading
from pathlib import Path

from super_agent.skills.loader import (
    SKILL_FILE_NAME,
    Skill,
    SkillRegistry,
    build_skill_registry,
)


def _stamp(roots: list[Path]) -> tuple[tuple[str, float, int], ...]:
    """Fingerprint the library: each skill folder's mtime plus the root mtimes.

    A new skill folder changes its parent's mtime, and editing a SKILL.md
    changes that folder's mtime, so these two together catch install, edit and
    delete without walking every file on every request.
    """

    entries: list[tuple[str, float, int]] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            root_stat = root.stat()
            entries.append((str(root), root_stat.st_mtime, 0))
            for child in root.iterdir():
                if child.is_dir() and (child / SKILL_FILE_NAME).is_file():
                    try:
                        info = child.stat()
                    except OSError:
                        continue
                    entries.append((str(child), info.st_mtime, 0))
        except OSError:
            continue
    return tuple(sorted(entries))


class LiveSkillLibrary(SkillRegistry):
    """A SkillRegistry that re-reads its folder when it changed on disk.

    Subclasses SkillRegistry so it satisfies every existing annotation: the
    orchestrator, the skill_* tools and the API all expect a registry, and
    only the read methods need to observe the live folder.
    """

    def __init__(self, roots: list[Path], *, enabled: bool = True) -> None:
        self._roots = list(roots)
        self._enabled = enabled
        self._lock = threading.Lock()
        self._stamp: tuple[tuple[str, float, int], ...] | None = None
        self._failed: tuple[tuple[str, str], ...] = ()
        # Seed from a plain registry rather than holding one: the parent keeps
        # the name index, and refresh() simply replaces it.
        registry, report = build_skill_registry(self._roots, enabled=enabled)
        super().__init__(registry.all())
        self._failed = report.failed
        self._stamp = _stamp(self._roots)

    # ---- internal -----------------------------------------------------

    def refresh(self, *, force: bool = False) -> bool:
        """Re-read the folder when its stamp changed. Returns whether it did."""

        with self._lock:
            if not self._enabled:
                return False
            current = _stamp(self._roots)
            if not force and current == self._stamp:
                return False
            registry, report = build_skill_registry(self._roots, enabled=True)
            self._skills = dict(registry._skills)
            self._failed = report.failed
            self._stamp = current
            return True

    def _sync(self) -> None:
        """Refresh, then let the parent answer from the current index."""

        self.refresh()

    # ---- read surface: every accessor observes the live folder ---------

    def __len__(self) -> int:
        self._sync()
        return super().__len__()

    def __contains__(self, name: object) -> bool:
        self._sync()
        return super().__contains__(name)

    def get(self, name: str) -> Skill | None:
        self._sync()
        return super().get(name)

    def names(self) -> tuple[str, ...]:
        self._sync()
        return super().names()

    def all(self) -> tuple[Skill, ...]:
        self._sync()
        return super().all()

    def catalogue(self) -> str:
        self._sync()
        return super().catalogue()

    def register(self, skill: Skill) -> None:
        # An external registration must not be clobbered by the next rescan,
        # but nothing calls this on a live library today; keep it honest.
        super().register(skill)

    @property
    def failed(self) -> tuple[tuple[str, str], ...]:
        """Load failures from the most recent scan."""

        self.refresh()
        return self._failed

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)
