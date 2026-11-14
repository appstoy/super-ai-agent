"""Per-task artifact directories inside one shared cache root.

Every task writes into its own folder so generated artifacts never litter the
repository root. The layout is::

    <workspace_root>/<cache_dir_name>/<task-folder>/

``task-folder`` is ``<short task id>-<slug of the task title>``. It is derived
deterministically from the Task so a resumed Worker (which only knows
``task_id``) lands in the folder the first attempt created, and a human reading
the directory listing can still tell what each folder was for.
"""

from __future__ import annotations

import re
from pathlib import Path

# Default name of the folder that holds every task folder. Configurable through
# ``SUPER_AGENT_CACHE_DIR_NAME`` because a project may want it localised.
DEFAULT_CACHE_DIR_NAME = "项目缓存文件"

_SLUG_STRIP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")
_SLUG_COLLAPSE = re.compile(r"[-_.]{2,}")
# Filesystems cap a single path component at 255 bytes; leave room for the
# ``<id>-`` prefix and stay well below the limit in UTF-8 multi-byte cases.
_MAX_SLUG_CHARS = 40


def task_slug(title: str, *, max_chars: int = _MAX_SLUG_CHARS) -> str:
    """Reduce a task title to a short, filesystem-safe fragment.

    Non-ASCII letters are kept (a Chinese title should not collapse to an empty
    slug) while path separators, whitespace runs and shell-hostile characters
    are folded away.
    """

    cleaned = _SLUG_STRIP.sub("-", title.strip())
    cleaned = _SLUG_COLLAPSE.sub("-", cleaned).strip("-._")
    if not cleaned:
        return "task"
    if len(cleaned) <= max_chars:
        return cleaned
    # Prefer cutting on a separator so the slug does not end mid-word.
    head = cleaned[: max_chars + 1]
    for separator in ("-", "_", "."):
        cut = head.rfind(separator)
        if cut >= max_chars // 2:
            return head[:cut].strip("-._")
    return cleaned[:max_chars].strip("-._") or "task"


def task_folder_name(task_id: str, title: str = "") -> str:
    """Return the per-task folder name for one Task id and title.

    The id fragment is a prefix of the UUID so the folder is stable and unique
    without pasting a full 32-character id into every path.
    """

    compact = str(task_id).replace("-", "")
    short = compact[:8] or "task"
    slug = task_slug(title)
    return f"{short}-{slug}" if slug and slug != "task" else short


def cache_root(workspace_root: Path, cache_dir_name: str = DEFAULT_CACHE_DIR_NAME) -> Path:
    """Resolve the directory that holds every task folder."""

    name = (cache_dir_name or DEFAULT_CACHE_DIR_NAME).strip() or DEFAULT_CACHE_DIR_NAME
    # A separator or "." here would let the cache escape the workspace.
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("cache directory name must be a single path component")
    root = (Path(workspace_root).resolve() / name).resolve()
    return root


def task_workspace(
    workspace_root: Path,
    task_id: str,
    title: str = "",
    cache_dir_name: str = DEFAULT_CACHE_DIR_NAME,
    *,
    create: bool = False,
) -> Path:
    """Resolve (and optionally create) the artifact directory for one Task.

    The result is always inside ``workspace_root``; callers pass it to the file
    tools as the single allowed root so a task can only touch its own folder.
    """

    root = cache_root(workspace_root, cache_dir_name)
    target = (root / task_folder_name(task_id, title)).resolve()
    # Defence in depth: the folder name is generated, but never let a resolved
    # path walk out of the cache root.
    if target != root and root not in target.parents:
        raise ValueError("task workspace escaped the cache root")
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


def find_task_workspace(
    workspace_root: Path,
    task_id: str,
    cache_dir_name: str = DEFAULT_CACHE_DIR_NAME,
) -> Path | None:
    """Locate an already-created task folder when only the id is known.

    A resumed Worker has the Task id but not necessarily its original title, so
    look for the ``<short id>-*`` folder the earlier attempt created. When more
    than one matches (a title was edited between attempts) the newest wins.
    """

    root = cache_root(workspace_root, cache_dir_name)
    if not root.is_dir():
        return None
    short = str(task_id).replace("-", "")[:8]
    if not short:
        return None
    candidates = [
        child
        for child in root.iterdir()
        if child.is_dir() and (child.name == short or child.name.startswith(f"{short}-"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda child: child.stat().st_mtime)
