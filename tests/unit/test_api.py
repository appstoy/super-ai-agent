from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings


def test_health_endpoint() -> None:
    client = TestClient(create_app(Settings(app_name="test-service", version="1")))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "test-service", "version": "1"}

