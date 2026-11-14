from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, select

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouteAction, RouterService
from super_agent.application.state import RouterStateStore
from super_agent.domain import Trigger
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import DeliveryRecord, TaskRecord, WorkerRecord


def test_phase1_router_result_survives_service_rebuild() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    trigger = Trigger(
        source="cli",
        actor="user",
        thread_id=uuid4(),
        payload_ref="inspect repository",
        occurred_at=datetime.now(UTC),
        dedupe_key="phase1-e2e-1",
    )

    with session_scope(factory) as session:
        model = MockModelGateway([ModelResponse(text="TASK: inspect repository")])
        result = RouterService(model).route(trigger)
        assert result.action is RouteAction.CREATED
        RouterStateStore(session).persist(result)

    with session_scope(factory) as session:
        task = session.scalar(select(TaskRecord))
        worker = session.scalar(select(WorkerRecord))
        delivery = session.scalar(select(DeliveryRecord))
        assert task is not None
        assert worker is not None
        assert delivery is not None
        assert task.id == worker.task_id == delivery.task_id
        assert task.title == "inspect repository"

