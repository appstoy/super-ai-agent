import pytest

from super_agent.config import Settings
from super_agent.domain.health import HealthStatus
from super_agent.errors import ConfigurationError


def test_health_status_is_framework_independent() -> None:
    status = HealthStatus(status="ok", service="test", version="0")

    assert status.to_dict() == {"status": "ok", "service": "test", "version": "0"}


def test_settings_reject_invalid_port(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_AGENT_PORT", "0")

    with pytest.raises(ConfigurationError, match="between 1 and 65535"):
        Settings.from_env()

