"""Created artifacts must be visible to the user, not just written to disk."""

from __future__ import annotations

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
)


def make_client(tmp_path, monkeypatch) -> TestClient:
    from super_agent.adapters.models.mock import MockModelGateway

    database_url = "sqlite:///" + (tmp_path / "app.db").as_posix()
    create_schema(create_engine_for_url(database_url))
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway",
        lambda *a, **k: MockModelGateway([]),
    )
    settings = Settings(
        database_url=database_url,
        model_name="test-model",
        workspace_root=str(tmp_path),
    )
    return TestClient(create_app(settings))


def test_created_html_artifact_is_listable_and_previewable(tmp_path, monkeypatch) -> None:
    """The end-to-end user story: write an HTML file, then open it."""

    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        "<!doctype html><html><body><h1>generated</h1></body></html>",
        encoding="utf-8",
    )

    with make_client(tmp_path, monkeypatch) as client:
        listing = client.get("/api/files", params={"path": "."}).json()
        names = {entry["name"] for entry in listing["entries"]}
        assert "site" in names

        inner = client.get("/api/files", params={"path": "site"}).json()
        html_entry = next(e for e in inner["entries"] if e["name"] == "index.html")
        assert html_entry["previewable"] is True
        assert html_entry["path"] == "site/index.html"

        content = client.get("/api/files/content", params={"path": "site/index.html"})
        assert content.status_code == 200
        assert "generated" in content.json()["text"]

        # A rendered HTML preview, not a JSON dump.
        served = client.get("/files/site/index.html")
        assert served.status_code == 200
        assert "generated" in served.text


def test_file_endpoints_refuse_paths_outside_the_workspace(tmp_path, monkeypatch) -> None:
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/files", params={"path": ".."}).status_code == 403
        assert (
            client.get("/api/files/content", params={"path": "../../etc/passwd"}).status_code
            == 403
        )
        assert client.get("/files/../../secret.html").status_code in {403, 404}


def test_only_html_is_served_directly(tmp_path, monkeypatch) -> None:
    (tmp_path / "notes.txt").write_text("plain", encoding="utf-8")

    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/files/notes.txt").status_code == 415
        # Text is still readable through the JSON endpoint.
        payload = client.get("/api/files/content", params={"path": "notes.txt"}).json()
        assert payload["text"] == "plain"


def test_missing_file_returns_404(tmp_path, monkeypatch) -> None:
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/files/content", params={"path": "nope.html"}).status_code == 404
