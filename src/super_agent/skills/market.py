"""Read Magpie's skill market so SAA can install from it.

Magpie keeps a local catalogue of skills in its market folder:

    market/skills.json   [{source, skillId, name, installs, isOfficial}]
    market/about.json    {"<source>/<skillId>": "<description>"}

A "source" is a GitHub repository (owner/repo) and a skillId is the folder
holding SKILL.md inside it, so an entry maps directly onto
install_skill("owner/repo") - the installer already understands that shape.

This module only reads the two cached files. It deliberately does not scrape
Magpie's process or require it to be running: the catalogue is a local cache
that Magpie refreshes itself, and if it is missing we simply say so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from super_agent.errors import ValidationError

SKILLS_INDEX = "skills.json"
SKILLS_ABOUT = "about.json"
MAX_RESULTS = 50


@dataclass(frozen=True, slots=True)
class MarketEntry:
    """One catalogue row, joined with its description when available."""

    source: str
    skill_id: str
    name: str
    installs: int
    official: bool
    description: str = ""

    @property
    def ref(self) -> str:
        """The 'source/skillId' key Magpie uses."""

        return f"{self.source}/{self.skill_id}"

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "skill_id": self.skill_id,
            "name": self.name,
            "installs": self.installs,
            "official": self.official,
            "description": self.description,
            "ref": self.ref,
        }


def find_market_dir(explicit: str | None = None) -> Path | None:
    """Locate Magpie's market folder, or None when it is not on this machine.

    Searched in order: the explicit setting, MAGPIE_MARKET_DIR, then the two
    per-user locations Magpie uses on Windows and elsewhere.
    """

    candidates: list[Path] = []
    if explicit:
        # A configured path is a statement of intent: if it is wrong, say so
        # rather than silently reading some other Magpie on this machine, which
        # would make a typo in configuration look like it worked.
        configured = Path(explicit).expanduser()
        if not (configured / SKILLS_INDEX).is_file():
            return None
        return configured
    env = os.getenv("MAGPIE_MARKET_DIR")
    if env:
        configured = Path(env).expanduser()
        if not (configured / SKILLS_INDEX).is_file():
            return None
        return configured
    local = os.getenv("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "magpie" / "market")
    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "magpie" / "market",
            home / ".magpie" / "market",
            home / "Library" / "Application Support" / "magpie" / "market",
            home / ".local" / "share" / "magpie" / "market",
        ]
    )
    for candidate in candidates:
        try:
            if (candidate / SKILLS_INDEX).is_file():
                return candidate
        except OSError:
            continue
    return None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read Magpie catalogue {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Magpie catalogue {path} is not valid JSON: {exc}") from exc


def load_market(market_dir: Path) -> list[MarketEntry]:
    """Read and join Magpie's two catalogue files."""

    index_path = market_dir / SKILLS_INDEX
    if not index_path.is_file():
        raise ValidationError(f"Magpie skill catalogue not found: {index_path}")
    raw = _read_json(index_path)
    if not isinstance(raw, list):
        raise ValidationError(f"Magpie catalogue {index_path} must be a list")

    about: dict[str, str] = {}
    about_path = market_dir / SKILLS_ABOUT
    if about_path.is_file():
        try:
            loaded = _read_json(about_path)
        except ValidationError:
            # A broken description file must not cost us the whole catalogue:
            # entries stay installable, they just lose their blurb.
            loaded = None
        if isinstance(loaded, dict):
            about = {str(k): str(v) for k, v in loaded.items()}

    entries: list[MarketEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        skill_id = str(item.get("skillId") or item.get("skill_id") or "").strip()
        if not source or not skill_id:
            continue
        name = str(item.get("name") or skill_id).strip()
        try:
            installs = int(item.get("installs") or 0)
        except (TypeError, ValueError):
            installs = 0
        entry = MarketEntry(
            source=source,
            skill_id=skill_id,
            name=name,
            installs=installs,
            official=bool(item.get("isOfficial")),
        )
        entries.append(
            MarketEntry(
                source=entry.source,
                skill_id=entry.skill_id,
                name=entry.name,
                installs=entry.installs,
                official=entry.official,
                description=about.get(entry.ref, ""),
            )
        )
    return entries


def search_market(
    entries: list[MarketEntry],
    query: str = "",
    *,
    limit: int = MAX_RESULTS,
    official_only: bool = False,
) -> list[MarketEntry]:
    """Rank entries against a query.

    Ranking is what a person means by "search here": an exact name match, then
    a name prefix, then a name substring, then a description hit, and popularity
    breaks ties. Sorting by installs alone buries a precise match.
    """

    text = (query or "").strip().lower()
    bounded = max(1, min(limit, 200))
    scored: list[tuple[int, int, MarketEntry]] = []
    for entry in entries:
        if official_only and not entry.official:
            continue
        if not text:
            scored.append((0, -entry.installs, entry))
            continue
        name = entry.name.lower()
        skill_lower = entry.skill_id.lower()
        if name == text or skill_lower == text:
            rank = 0
        elif name.startswith(text) or skill_lower.startswith(text):
            rank = 1
        elif text in name or text in skill_lower:
            rank = 2
        elif text in entry.description.lower() or text in entry.source.lower():
            rank = 3
        else:
            continue
        scored.append((rank, -entry.installs, entry))
    scored.sort(key=lambda item: (item[0], item[1], item[2].name))
    return [entry for _, _, entry in scored[:bounded]]


def resolve_ref(entries: list[MarketEntry], ref: str) -> MarketEntry:
    """Find one entry by 'source/skillId', or by a unique skill name."""

    text = (ref or "").strip()
    if not text:
        raise ValidationError("a market reference must not be empty")
    lowered = text.lower()
    for entry in entries:
        if entry.ref.lower() == lowered:
            return entry
    # A bare skill name is a reasonable thing to type; accept it when unique.
    by_name = [entry for entry in entries if entry.skill_id.lower() == lowered]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        sources = ", ".join(sorted({entry.source for entry in by_name})[:5])
        raise ValidationError(
            f"{text!r} exists in several repositories ({sources}); "
            "pass the full 'owner/repo/skill' reference"
        )
    raise ValidationError(f"{text!r} is not in the Magpie catalogue")
