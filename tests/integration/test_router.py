from datetime import UTC, datetime
from uuid import uuid4

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouteAction, RouterService
from super_agent.domain import Thread, Trigger
from super_agent.ports.model import ModelResponse


def make_trigger(*, source: str = "cli", payload_ref: str = "inspect repository") -> Trigger:
    return Trigger(
        source=source,
        actor="user",
        thread_id=uuid4(),
        payload_ref=payload_ref,
        occurred_at=datetime.now(UTC),
        dedupe_key=f"{source}-{uuid4()}",
    )


def test_router_fast_path_ignores_noise_without_model_call() -> None:
    model = MockModelGateway([ModelResponse(text="TASK: should not run")])
    result = RouterService(model).route(make_trigger(source="noise"))

    assert result.action is RouteAction.IGNORED
    assert result.task is None
    assert model.calls == []


def test_router_creates_task_worker_and_delivery() -> None:
    model = MockModelGateway([ModelResponse(text="TASK: inspect repository")])
    trigger = make_trigger()
    result = RouterService(model).route(trigger, thread=Thread(id=trigger.thread_id))

    assert result.action is RouteAction.CREATED
    assert result.task is not None
    assert result.worker is not None
    assert result.task.worker_id == result.worker.id
    assert result.worker.task_id == result.task.id
    assert result.delivery is not None
    assert result.delivery.task_id == result.task.id
    assert result.trigger.filter_decision.value == "accepted"


def test_router_returns_clarification_delivery() -> None:
    model = MockModelGateway([ModelResponse(text="CLARIFY: Which repository should I inspect?")])
    result = RouterService(model).route(make_trigger())

    assert result.action is RouteAction.CLARIFY
    assert result.task is None
    assert result.delivery is not None
    assert result.reason == "Which repository should I inspect?"


def test_router_falls_back_to_a_task_for_normal_chat_model_output() -> None:
    model = MockModelGateway([ModelResponse(text="I can help with that request.")])
    trigger = make_trigger(payload_ref="prepare a project plan")

    result = RouterService(model).route(trigger)

    assert result.action is RouteAction.CREATED
    assert result.task is not None
    assert result.task.title == "prepare a project plan"

