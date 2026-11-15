"""The market endpoints the page relies on.

The install endpoint performs real filesystem work, so these tests use a
throwaway catalogue and library: search returns catalogue rows, install copies
the chosen skill, and a missing catalogue degrades with a message instead of a
stack trace.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings


def _skill(root: Path, name: str, *, description: str = "does a thing") -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return folder


def _catalogue(root: Path, rows: list[dict[str, object]], about: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skills.json").write_text(json.dumps(rows), encoding="utf-8")
    (root / "about.json").write_text(json.dumps(about), encoding="utf-8")
    return root


def _settings(tmp_path: Path, market: Path | None) -> Settings:
    kwargs: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'market.db'}",
        "workspace_root": str(tmp_path),
        "model_name": "test-model",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "test",
        "skill_paths": str(tmp_path / "library"),
    }
    if market is not None:
        kwargs["magpie_market_dir"] = str(market)
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_search_returns_catalogue_rows(tmp_path: Path) -> None:
    market = _catalogue(
        tmp_path / "market",
        [
            {"source": "owner/repo", "skillId": "webapp-testing", "installs": 1234,
             "isOfficial": True},
            {"source": "other/lib", "skillId": "unrelated", "installs": 9},
        ],
        {"owner/repo/webapp-testing": "browser automation"},
    )

    with TestClient(create_app(_settings(tmp_path, market))) as client:
        body = client.get("/api/skills/market", params={"q": "webapp"}).json()

    assert body["total"] == 2
    assert body["count"] == 1
    row = body["skills"][0]
    assert row["ref"] == "owner/repo/webapp-testing"
    assert row["description"] == "browser automation"
    assert row["installs"] == 1234


def test_search_reports_the_catalogue_location(tmp_path: Path) -> None:
    market = _catalogue(tmp_path / "market", [], {})

    with TestClient(create_app(_settings(tmp_path, market))) as client:
        body = client.get("/api/skills/market").json()

    assert body["market_dir"] == str(market)


def test_missing_catalogue_is_a_503_with_instructions(tmp_path: Path) -> None:
    """A machine without Magpie must get an explanation, not a traceback."""

    settings = _settings(tmp_path, None)
    settings = Settings(
        database_url=settings.database_url,
        workspace_root=settings.workspace_root,
        model_name=settings.model_name,
        model_base_url=settings.model_base_url,
        model_api_key=settings.model_api_key,
        skill_paths=settings.skill_paths,
        magpie_market_dir=str(tmp_path / "no-such-market"),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/skills/market")

    # An explicit path that is wrong must not quietly fall back to whatever
    # Magpie is installed on this machine: that would hide a typo.
    assert response.status_code == 503
    assert "Magpie" in response.json()["detail"]


def test_install_from_market_copies_the_chosen_skill(tmp_path: Path) -> None:
    """A repo can hold many skills; only the referenced one may land."""

    repo = tmp_path / "repo"
    _skill(repo, "wanted", description="the referenced one")
    _skill(repo, "unwanted", description="must not be installed")
    market = _catalogue(
        tmp_path / "market",
        [{"source": str(repo), "skillId": "wanted", "installs": 5}],
        {},
    )

    ref = f"{repo}/wanted"
    with TestClient(create_app(_settings(tmp_path, market))) as client:
        response = client.post(
            "/api/skills/install", json={"source": ref, "from_magpie": True}
        )

    assert response.status_code == 200, response.text
    assert response.json()["installed"] == ["wanted"]
    library = tmp_path / "library"
    assert (library / "wanted" / "SKILL.md").is_file()
    assert not (library / "unwanted").exists()


def test_installed_skill_is_listed_immediately(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _skill(repo, "fresh")
    market = _catalogue(
        tmp_path / "market",
        [{"source": str(repo), "skillId": "fresh", "installs": 1}],
        {},
    )

    with TestClient(create_app(_settings(tmp_path, market))) as client:
        before = client.get("/api/skills").json()["count"]
        client.post(
            "/api/skills/install",
            json={"source": f"{repo}/fresh", "from_magpie": True},
        )
        after = client.get("/api/skills").json()

    assert before == 0
    assert after["count"] == 1
    assert after["skills"][0]["name"] == "fresh"


def test_unknown_market_ref_is_a_404(tmp_path: Path) -> None:
    market = _catalogue(tmp_path / "market", [], {})

    with TestClient(create_app(_settings(tmp_path, market))) as client:
        response = client.post(
            "/api/skills/install",
            json={"source": "nobody/nothing/absent", "from_magpie": True},
        )

    assert response.status_code == 404


def test_install_rejects_an_empty_source(tmp_path: Path) -> None:
    market = _catalogue(tmp_path / "market", [], {})

    with TestClient(create_app(_settings(tmp_path, market))) as client:
        response = client.post("/api/skills/install", json={"source": ""})

    assert response.status_code == 422
