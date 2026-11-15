"""Import skills from wherever the user already has them.

Three ways in, all funnelling through the same "find SKILL.md, validate, copy"
path so a failure is reported the same way regardless of origin:

1. another agent's folder on this machine (see agents.py for discovery)
2. a folder the user picks or drops
3. a .zip / .tar.gz archive the user drops

Archives are unpacked in a temp directory first, so a broken or hostile archive
cannot write outside the skill library, and a failed import leaves nothing
behind.
"""

from __future__ import annotations

import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from super_agent.errors import ValidationError
from super_agent.skills.agents import discover_agent_skills, is_inside
from super_agent.skills.installer import install_skill
from super_agent.skills.loader import SKILL_FILE_NAME, parse_skill_file

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
# Guard against a zip bomb or an accidental drive dump.
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4000


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What one import did, in terms the panel can show directly."""

    imported: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    source: str = ""
    kind: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "imported": list(self.imported),
            "skipped": [list(item) for item in self.skipped],
            "source": self.source,
            "kind": self.kind,
            "ok": bool(self.imported) and not self.skipped,
        }


def _is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _safe_extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ValidationError(
                f"压缩包里条目太多（{len(members)}），超过 {MAX_ARCHIVE_ENTRIES} 的上限"
            )
        total = sum(item.file_size for item in members)
        if total > MAX_ARCHIVE_BYTES:
            raise ValidationError(
                f"压缩包解压后有 {total // 1024 // 1024}MB，超过 "
                f"{MAX_ARCHIVE_BYTES // 1024 // 1024}MB 的上限"
            )
        root = target.resolve()
        for item in members:
            # Zip Slip: an entry named ../../x would otherwise escape the target.
            destination = (target / item.filename).resolve()
            if destination != root and root not in destination.parents:
                raise ValidationError(f"压缩包里有越界路径：{item.filename}")
        bundle.extractall(target)


def _safe_extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise ValidationError(
                f"压缩包里条目太多（{len(members)}），超过 {MAX_ARCHIVE_ENTRIES} 的上限"
            )
        total = sum(item.size for item in members if item.isfile())
        if total > MAX_ARCHIVE_BYTES:
            raise ValidationError(
                f"压缩包解压后有 {total // 1024 // 1024}MB，超过 "
                f"{MAX_ARCHIVE_BYTES // 1024 // 1024}MB 的上限"
            )
        root = target.resolve()
        for item in members:
            destination = (target / item.name).resolve()
            if destination != root and root not in destination.parents:
                raise ValidationError(f"压缩包里有越界路径：{item.name}")
            if item.issym() or item.islnk():
                # A link inside an archive can point anywhere on the machine.
                raise ValidationError(f"压缩包里含有链接，出于安全考虑已拒绝：{item.name}")
        bundle.extractall(target)


def _extract_archive(archive: Path, target: Path) -> None:
    if archive.name.lower().endswith(".zip"):
        _safe_extract_zip(archive, target)
    else:
        _safe_extract_tar(archive, target)


def _unwrap_single_root(folder: Path) -> Path:
    """A downloaded archive usually wraps everything in one top folder.

    my-skill.zip typically unpacks to my-skill/SKILL.md, which would otherwise
    install under the wrapper's name instead of the skill's.
    """

    entries = [item for item in folder.iterdir() if item.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return folder


def _skill_folders(root: Path) -> list[Path]:
    """Every folder under root that holds a SKILL.md (root itself included)."""

    if (root / SKILL_FILE_NAME).is_file():
        return [root]
    found: list[Path] = []
    for child in sorted(root.rglob(SKILL_FILE_NAME)):
        if "__pycache__" in child.parts:
            continue
        found.append(child.parent)
    # A skill folder nested inside another skill folder is resource material,
    # not a second skill.
    keep: list[Path] = []
    for folder in found:
        if any(other != folder and other in folder.parents for other in found):
            continue
        keep.append(folder)
    return keep


def _copy_skills(
    root: Path,
    library: Path,
    *,
    force: bool,
    source: str,
    kind: str,
) -> ImportOutcome:
    folders = _skill_folders(root)
    if not folders:
        raise ValidationError(
            f"在 {source} 里没有找到 {SKILL_FILE_NAME}。"
            "一个技能是一个包含 SKILL.md 的文件夹。"
        )
    imported: list[str] = []
    skipped: list[tuple[str, str]] = []
    for folder in folders:
        try:
            skill = parse_skill_file(folder / SKILL_FILE_NAME)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            skipped.append((str(folder), f"{type(exc).__name__}: {exc}"))
            continue
        target = library / skill.name
        if target.exists() and not force:
            skipped.append((skill.name, f"已存在同名技能（{target}），加 force 可覆盖"))
            continue
        result = install_skill(str(folder), library, force=force)
        if result.installed:
            imported.append(skill.name)
        else:
            for _, reason in result.skipped:
                skipped.append((skill.name, reason))
    return ImportOutcome(
        imported=tuple(imported),
        skipped=tuple(skipped),
        source=source,
        kind=kind,
    )


def import_from_path(
    source: str | Path,
    library: Path,
    *,
    force: bool = False,
) -> ImportOutcome:
    """Import a folder or archive the user picked.

    Accepts a skill folder, a folder holding several skills, a .zip or a
    tarball. Everything is validated before it lands; the outcome lists what
    worked and, for each failure, why.
    """

    path = Path(source).expanduser()
    if not path.exists():
        raise ValidationError(f"路径不存在：{path}")
    library = Path(library).expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)

    if _is_archive(path):
        with tempfile.TemporaryDirectory(prefix="saa-skill-import-") as scratch:
            target = Path(scratch) / "unpacked"
            target.mkdir()
            _extract_archive(path, target)
            root = _unwrap_single_root(target)
            return _copy_skills(root, library, force=force, source=str(path), kind="archive")

    root = path.resolve()
    if root.is_file():
        raise ValidationError(
            f"{root.name} 不是文件夹，也不是可识别的压缩包"
            f"（支持 {", ".join(ARCHIVE_SUFFIXES)}）"
        )
    if is_inside(library, root) or is_inside(root, library):
        # Importing the library into itself would recurse and duplicate.
        raise ValidationError("这个目录就是技能库本身，或包含技能库，无法导入")
    return _copy_skills(root, library, force=force, source=str(root), kind="folder")


def import_from_agent(
    agent_id: str,
    library: Path,
    *,
    names: list[str] | None = None,
    force: bool = False,
    home: Path | None = None,
) -> ImportOutcome:
    """Copy skills out of another agent's folder into SAA's library."""

    sources, _ = discover_agent_skills(home)
    source = next((item for item in sources if item.agent_id == agent_id), None)
    if source is None:
        known = ", ".join(item.agent_id for item in sources)
        raise ValidationError(f"未知的 agent：{agent_id}。已知：{known}")
    if not source.exists:
        raise ValidationError(f"{source.label} 的技能目录不存在：{source.path}")

    wanted = {name.strip() for name in (names or []) if name and name.strip()}
    library = Path(library).expanduser().resolve()
    imported: list[str] = []
    skipped: list[tuple[str, str]] = []
    for entry in source.skills:
        if wanted and entry.skill.name not in wanted:
            continue
        target = library / entry.skill.name
        if target.exists() and not force:
            skipped.append((entry.skill.name, "技能库里已有同名技能"))
            continue
        result = install_skill(str(entry.skill.path), library, force=force)
        if result.installed:
            imported.append(entry.skill.name)
        else:
            for _, reason in result.skipped:
                skipped.append((entry.skill.name, reason))
    if not imported and not skipped:
        raise ValidationError(f"{source.label} 里没有找到可导入的技能")
    return ImportOutcome(
        imported=tuple(imported),
        skipped=tuple(skipped),
        source=str(source.path),
        kind=f"agent:{agent_id}",
    )
