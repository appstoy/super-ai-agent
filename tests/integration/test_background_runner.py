from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.background import BackgroundAgentRunner
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import create_schema, create_session_factory


def test_background_runner_executes_the_full_runtime() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    runner = BackgroundAgentRunner(factory, max_workers=1)
    gateway = MockModelGateway(
        [ModelResponse(text="TASK: background task"), ModelResponse(text="background done")]
    )
    trigger = TriggerInput(
        source="web",
        actor="user",
        thread_id=uuid4(),
        payload={"message": "background task"},
        occurred_at=datetime.now(UTC),
        dedupe_key="background-1",
    )
    submission = runner.submit(trigger, gateway)
    result = submission.future.result(timeout=5)
    assert result.status == "succeeded"
    runner.shutdown()
