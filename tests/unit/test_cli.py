import json

from super_agent.cli import main
from super_agent.config import Settings
from super_agent.ports.model import ModelResponse


def test_health_command_returns_json(capsys) -> None:
    assert main(["health"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["service"] == "super-ai-agent"


def test_version_flag(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("argparse did not exit after --version")
    assert capsys.readouterr().out.strip() == "0.1.0.dev0"


def test_serve_rejects_port_zero(monkeypatch) -> None:
    monkeypatch.setattr("super_agent.cli.uvicorn.run", lambda *args, **kwargs: None)

    try:
        main(["serve", "--port", "0"])
    except SystemExit as exc:
        assert exc.code == "--port must be between 1 and 65535"
    else:
        raise AssertionError("port zero was accepted")


def test_submit_command_uses_the_agent_runtime(tmp_path, monkeypatch, capsys) -> None:
    from super_agent.adapters.models.mock import MockModelGateway

    monkeypatch.setattr(
        "super_agent.cli.Settings.from_env",
        lambda: Settings(
            database_url=f"sqlite:///{tmp_path / 'submit.db'}",
            model_name="mock-model",
        ),
    )
    monkeypatch.setattr(
        "super_agent.cli.MagpieModelGateway",
        lambda *args, **kwargs: MockModelGateway(
            [ModelResponse(text="TASK: cli task"), ModelResponse(text="cli done")]
        ),
    )

    assert main(["submit", "run from cli"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "succeeded"
    assert output["text"] == "cli done"

