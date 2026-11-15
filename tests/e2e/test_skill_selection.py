"""End-to-end: a skill the user picks must reach the model prompt.

The loader tests prove a SKILL.md parses. These prove the selection actually
changes what the model is told - the part that would silently break if the
payload key or the prompt wiring drifted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.ports.model import (
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from super_agent.skills.loader import SkillRegistry, build_skill_registry
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
)
from super_agent.tools.loading import build_agent_tools


class RecordingGateway(ModelGateway):
    """Model stub that remembers every prompt it was given."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append("\n".join(message.content for message in request.messages))
        return ModelResponse(
            text="done",
            usage=ModelUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            provider_request_id="req-1",
        )


def _skill(root: Path, name: str, *, description: str, body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return folder


def _settings(tmp_path: Path, skill_root: Path | None = None) -> Settings:
    kwargs: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "workspace_root": str(tmp_path),
        "model_name": "test-model",
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_api_key": "test",
    }
    if skill_root is not None:
        kwargs["skill_paths"] = str(skill_root)
    return Settings(**kwargs)  # type: ignore[arg-type]


def test_api_lists_installed_skills(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "alpha", description="first skill", body="# Alpha")

    with TestClient(create_app(_settings(tmp_path, library))) as client:
        body = client.get("/api/skills").json()

    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["skills"][0]["name"] == "alpha"
    assert body["skills"][0]["description"] == "first skill"


def test_api_reports_a_broken_skill_instead_of_hiding_it(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    broken = library / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no description", encoding="utf-8")

    with TestClient(create_app(_settings(tmp_path, library))) as client:
        body = client.get("/api/skills").json()

    assert body["count"] == 0
    assert len(body["failed"]) == 1


def test_api_lists_no_skills_when_the_library_is_empty(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        body = client.get("/api/skills").json()

    assert body["count"] == 0
    assert body["skills"] == []


def test_selected_skill_body_reaches_the_system_prompt(tmp_path: Path) -> None:
    """The whole point: picking a skill must change the model's instructions."""

    library = tmp_path / "lib"
    _skill(
        library,
        "strict-format",
        description="enforce an exact output format",
        body="ALWAYS answer with exactly three bullet points.",
    )
    settings = _settings(tmp_path, library)
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    registry, capabilities, report = build_agent_tools(
        settings, workspace_root=Path(settings.workspace_root)
    )
    skills = SkillRegistry(report.skills.skills)
    gateway = RecordingGateway()

    from super_agent.ingress.schemas import TriggerInput

    trigger = TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={
            "message": "写点东西",
            "model": "test-model",
            "skills": ["strict-format"],
        },
        dedupe_key="skill-selection-1",
    )
    with factory() as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=1,
            skills=skills,
        ).submit(trigger)
        session.commit()

    assert gateway.prompts, "the model was never called"
    # The first call is the Router deciding whether this is a task; the Worker
    # call carries the system prompt under test.
    prompt = gateway.prompts[-1]
    assert "ALWAYS answer with exactly three bullet points." in prompt
    assert "strict-format" in prompt


def test_unselected_skill_body_is_not_injected(tmp_path: Path) -> None:
    """A large library must not cost context until a skill is chosen."""

    library = tmp_path / "lib"
    _skill(
        library,
        "expensive",
        description="long instructions",
        body="THIS BODY MUST NOT APPEAR UNLESS SELECTED.",
    )
    settings = _settings(tmp_path, library)
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    registry, capabilities, report = build_agent_tools(
        settings, workspace_root=Path(settings.workspace_root)
    )
    skills = SkillRegistry(report.skills.skills)
    gateway = RecordingGateway()

    from super_agent.ingress.schemas import TriggerInput

    trigger = TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"message": "写点东西", "model": "test-model"},
        dedupe_key="skill-selection-2",
    )
    with factory() as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=1,
            skills=skills,
        ).submit(trigger)
        session.commit()

    prompt = gateway.prompts[-1]
    assert "THIS BODY MUST NOT APPEAR UNLESS SELECTED." not in prompt
    # The catalogue line still advertises it, which is how the model discovers
    # a skill nobody preselected.
    assert "expensive: long instructions" in prompt


def test_unknown_skill_name_is_surfaced_to_the_model(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "real", description="exists", body="# Real")
    settings = _settings(tmp_path, library)
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    registry, capabilities, report = build_agent_tools(
        settings, workspace_root=Path(settings.workspace_root)
    )
    skills = SkillRegistry(report.skills.skills)
    gateway = RecordingGateway()

    from super_agent.ingress.schemas import TriggerInput

    trigger = TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"message": "写点东西", "model": "test-model", "skills": ["ghost"]},
        dedupe_key="skill-selection-3",
    )
    with factory() as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=1,
            skills=skills,
        ).submit(trigger)
        session.commit()

    prompt = gateway.prompts[-1]
    assert "not installed" in prompt
    assert "ghost" in prompt


def test_skill_tools_exist_even_on_an_empty_library(tmp_path: Path) -> None:
    """A skill installed while the server runs must be readable right away.

    Registering the tools only when the startup scan found something meant a
    fresh install stayed invisible to the model until a restart.
    """

    empty = build_agent_tools(_settings(tmp_path), workspace_root=tmp_path)[0]
    names = {spec.name for spec in empty.specs()}
    assert "skill.read" in names
    assert "skill.list" in names

    library = tmp_path / "lib"
    _skill(library, "alpha", description="first", body="# Alpha")
    populated = build_agent_tools(
        _settings(tmp_path, library), workspace_root=tmp_path
    )[0]
    assert {"skill.read", "skill.list"} <= {spec.name for spec in populated.specs()}


def test_skill_read_tool_returns_the_body(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "alpha", description="first", body="# Alpha\n\nDo the thing.")
    registry, _ = build_skill_registry([library])
    tools = build_agent_tools(_settings(tmp_path, library), workspace_root=tmp_path)[0]

    from super_agent.ports.tool import ExecutionContext

    result = tools.execute(
        "skill.read",
        {"name": "alpha"},
        ExecutionContext(granted_capabilities=frozenset({"skill.read"})),
    )

    assert "Do the thing." in result.output


def test_skill_read_tool_rejects_an_unknown_name(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    _skill(library, "alpha", description="first", body="# Alpha")
    tools = build_agent_tools(_settings(tmp_path, library), workspace_root=tmp_path)[0]

    from super_agent.ports.tool import ExecutionContext

    # The registry surfaces the validation error; the orchestrator turns it
    # into a model-visible tool result so the model can retry.
    with pytest.raises(Exception) as exc:
        tools.execute(
            "skill.read",
            {"name": "nope"},
            ExecutionContext(granted_capabilities=frozenset({"skill.read"})),
        )
    assert "unknown skill" in str(exc.value)


def test_task_request_rejects_too_many_skills(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/tasks/async",
            json={"message": "hi", "skills": [f"s{i}" for i in range(20)]},
        )

    assert response.status_code == 422


def test_run_records_the_selected_skills_for_later_results(tmp_path: Path) -> None:
    """A selection that vanishes from storage cannot be shown after a refresh."""

    import sqlite3

    library = tmp_path / "lib"
    _skill(library, "alpha", description="first", body="# Alpha")
    settings = _settings(tmp_path, library)
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    registry, capabilities, report = build_agent_tools(
        settings, workspace_root=Path(settings.workspace_root)
    )
    skills = SkillRegistry(report.skills.skills)
    gateway = RecordingGateway()

    from super_agent.ingress.schemas import TriggerInput

    trigger = TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"message": "写点东西", "model": "test-model", "skills": ["alpha"]},
        dedupe_key="skill-persist-1",
    )
    with factory() as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=1,
            skills=skills,
        ).submit(trigger)
        session.commit()

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute("select input_ref from runs").fetchone()
    conn.close()
    assert row is not None
    assert "trigger:" in row[0]
    assert '"skills":["alpha"]' in row[0]


def test_plain_task_keeps_the_legacy_input_ref(tmp_path: Path) -> None:
    """No selection must not change the stored shape for existing clients."""

    import sqlite3

    settings = _settings(tmp_path)
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    registry, capabilities, _ = build_agent_tools(
        settings, workspace_root=Path(settings.workspace_root)
    )
    gateway = RecordingGateway()

    from super_agent.ingress.schemas import TriggerInput

    trigger = TriggerInput(
        source="web",
        actor="tester",
        thread_id=uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"message": "写点东西", "model": "test-model"},
        dedupe_key="skill-persist-2",
    )
    with factory() as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=1,
        ).submit(trigger)
        session.commit()

    conn = sqlite3.connect(tmp_path / "test.db")
    row = conn.execute("select input_ref from runs").fetchone()
    conn.close()
    assert row is not None
    assert "|" not in row[0]
    assert row[0].startswith("trigger:")


def test_api_tools_report_includes_the_skill_tools(tmp_path: Path) -> None:
    """/api/tools must list what the model can actually call, not a stale snapshot."""

    library = tmp_path / "lib"
    _skill(library, "alpha", description="first", body="# Alpha")

    with TestClient(create_app(_settings(tmp_path, library))) as client:
        body = client.get("/api/tools").json()

    assert "skill.list" in body["builtin"]
    assert "skill.read" in body["builtin"]
    assert body["skills"]["loaded"] == ["alpha"]
