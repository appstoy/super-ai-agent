"""Reading and searching Magpie's skill catalogue.

The catalogue is two cached JSON files owned by Magpie. These tests pin the
behaviour SAA depends on: a broken description file must not cost the whole
catalogue, and search ranking has to put a precise match above a popular one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from super_agent.errors import ValidationError
from super_agent.skills.market import (
    find_market_dir,
    load_market,
    resolve_ref,
    search_market,
)


def _write_market(
    root: Path,
    rows: list[dict[str, object]],
    about: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skills.json").write_text(json.dumps(rows), encoding="utf-8")
    if about is not None:
        (root / "about.json").write_text(json.dumps(about), encoding="utf-8")
    return root


ROWS = [
    {"source": "owner/repo", "skillId": "code-review", "name": "code-review", "installs": 100},
    {
        "source": "owner/repo",
        "skillId": "code-review-deep",
        "name": "code-review-deep",
        "installs": 9999,
    },
    {"source": "other/lib", "skillId": "testing", "name": "testing", "installs": 50},
]


def test_loads_entries_and_joins_descriptions(tmp_path: Path) -> None:
    market = _write_market(tmp_path / "m", ROWS, {"owner/repo/code-review": "does reviews"})

    entries = load_market(market)

    assert len(entries) == 3
    first = entries[0]
    assert first.ref == "owner/repo/code-review"
    assert first.description == "does reviews"
    assert entries[1].description == ""


def test_missing_description_file_is_tolerated(tmp_path: Path) -> None:
    """about.json is optional; losing it must not cost the whole catalogue."""

    market = _write_market(tmp_path / "m", ROWS)

    entries = load_market(market)

    assert len(entries) == 3
    assert all(entry.description == "" for entry in entries)


def test_corrupt_description_file_is_tolerated(tmp_path: Path) -> None:
    market = _write_market(tmp_path / "m", ROWS)
    (market / "about.json").write_text("{not json", encoding="utf-8")

    entries = load_market(market)

    assert len(entries) == 3


def test_rows_without_a_source_or_id_are_skipped(tmp_path: Path) -> None:
    market = _write_market(
        tmp_path / "m",
        [
            {"source": "", "skillId": "orphan"},
            {"source": "owner/repo", "skillId": ""},
            {"source": "owner/repo", "skillId": "keeper"},
            "not a dict",
        ],
    )

    entries = load_market(market)

    assert [entry.skill_id for entry in entries] == ["keeper"]


def test_a_non_list_catalogue_is_rejected(tmp_path: Path) -> None:
    market = tmp_path / "m"
    market.mkdir()
    (market / "skills.json").write_text('{"nope": 1}', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_market(market)


def test_missing_catalogue_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_market(tmp_path / "absent")


def test_exact_name_beats_a_more_popular_prefix_match(tmp_path: Path) -> None:
    """Sorted by installs alone, the precise match would be buried."""

    entries = load_market(_write_market(tmp_path / "m", ROWS))

    hits = search_market(entries, "code-review")

    assert hits[0].skill_id == "code-review"
    assert hits[1].skill_id == "code-review-deep"


def test_search_matches_description(tmp_path: Path) -> None:
    market = _write_market(
        tmp_path / "m", ROWS, {"other/lib/testing": "browser automation and screenshots"}
    )
    entries = load_market(market)

    hits = search_market(entries, "screenshots")

    assert [hit.skill_id for hit in hits] == ["testing"]


def test_official_filter_excludes_community_entries(tmp_path: Path) -> None:
    rows = [
        {"source": "a/b", "skillId": "official-one", "installs": 10, "isOfficial": True},
        {"source": "c/d", "skillId": "community-one", "installs": 99},
    ]
    entries = load_market(_write_market(tmp_path / "m", rows))

    hits = search_market(entries, "", official_only=True)

    assert [hit.skill_id for hit in hits] == ["official-one"]


def test_empty_query_returns_most_installed(tmp_path: Path) -> None:
    entries = load_market(_write_market(tmp_path / "m", ROWS))

    hits = search_market(entries, "")

    assert hits[0].skill_id == "code-review-deep"


def test_limit_is_bounded(tmp_path: Path) -> None:
    entries = load_market(_write_market(tmp_path / "m", ROWS))

    assert len(search_market(entries, "", limit=2)) == 2
    assert len(search_market(entries, "", limit=0)) == 1


def test_resolve_by_full_ref(tmp_path: Path) -> None:
    entries = load_market(_write_market(tmp_path / "m", ROWS))

    assert resolve_ref(entries, "owner/repo/code-review").skill_id == "code-review"


def test_resolve_by_unique_skill_name(tmp_path: Path) -> None:
    entries = load_market(_write_market(tmp_path / "m", ROWS))

    assert resolve_ref(entries, "testing").source == "other/lib"


def test_ambiguous_name_needs_the_full_ref(tmp_path: Path) -> None:
    rows = [
        {"source": "a/one", "skillId": "same", "installs": 1},
        {"source": "b/two", "skillId": "same", "installs": 2},
    ]
    entries = load_market(_write_market(tmp_path / "m", rows))

    with pytest.raises(ValidationError) as exc:
        resolve_ref(entries, "same")
    assert "several repositories" in str(exc.value)


def test_unknown_ref_is_rejected(tmp_path: Path) -> None:
    entries = load_market(_write_market(tmp_path / "m", ROWS))

    with pytest.raises(ValidationError) as exc:
        resolve_ref(entries, "nobody/nothing/absent")
    assert "not in the Magpie catalogue" in str(exc.value)


def test_find_market_dir_accepts_an_explicit_path(tmp_path: Path) -> None:
    market = _write_market(tmp_path / "m", ROWS)

    assert find_market_dir(str(market)) == market


def test_find_market_dir_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAGPIE_MARKET_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "also-nowhere"))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))

    assert find_market_dir(None) is None
