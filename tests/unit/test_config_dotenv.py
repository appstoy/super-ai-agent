from pathlib import Path

from super_agent.config import Settings


def test_settings_loads_dotenv_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "SUPER_AGENT_APP_NAME=dotenv-test\nSUPER_AGENT_PORT=8123\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPER_AGENT_APP_NAME", raising=False)
    monkeypatch.delenv("SUPER_AGENT_PORT", raising=False)
    settings = Settings.from_env()
    assert settings.app_name == "dotenv-test"
    assert settings.port == 8123
