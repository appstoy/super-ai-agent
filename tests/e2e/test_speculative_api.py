"""E2E for article §3.4 speculative execution exposed over the control API."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.ports.model import ModelRequest, ModelResponse


class SpecGateway:
    """Router creates a task; the Worker drafts a silent background answer."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: speculative prep")
        return ModelResponse(text="draft: meeting notes prepared")


def test_speculation_runs_silently_and_promotes_only_on_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "super_agent.api.MagpieModelGateway", lambda *args, **kwargs: SpecGateway()
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'spec.db'}",
        model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/speculations",
            json={"message": "prepare meeting notes", "model": "test-model"},
        )
        assert created.status_code == 200
        speculation_id = created.json()["speculation_id"]

        outcome = None
        for _ in range(50):
            listed = client.get("/api/speculations").json()["speculations"]
            outcome = next(
                (item for item in listed if item["speculation_id"] == speculation_id), None
            )
            if outcome is not None and outcome["status"] == "completed":
                break
            time.sleep(0.05)
        assert outcome is not None and outcome["status"] == "completed"
        assert "draft" in outcome["text"]

        # The draft stays undelivered until a human promotes it.
        listed = client.get("/api/speculations").json()["speculations"]
        assert next(
            item for item in listed if item["speculation_id"] == speculation_id
        )["status"] == "completed"

        promoted = client.post(f"/api/speculations/{speculation_id}/promote")
        assert promoted.status_code == 200
        assert promoted.json()["status"] == "sent"
        listed = client.get("/api/speculations").json()["speculations"]
        assert next(
            item for item in listed if item["speculation_id"] == speculation_id
        )["status"] == "delivered"

        missing = client.post(
            "/api/speculations/00000000-0000-0000-0000-000000000000/promote"
        )
        assert missing.status_code == 404