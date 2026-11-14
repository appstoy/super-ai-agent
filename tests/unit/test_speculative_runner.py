"""SpeculativeRunner: safety gate, TTL expiry, and human-gated delivery."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from super_agent.adapters.channels.memory import CliChannelAdapter
from super_agent.adapters.models.mock import MockModelGateway
from super_agent.delivery.service import DeliveryService
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ingress.schemas import TriggerInput
from super_agent.policies.speculative import SpeculativePolicy
from super_agent.policies.speculative_runner import SpeculativeRunner, should_speculate
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import create_schema, create_session_factory


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _trigger(text: str) -> TriggerInput:
    return TriggerInput(
        source="test",
        actor="tester",
        thread_id=uuid4(),
        payload={"message": text},
        occurred_at=datetime.now(UTC),
        dedupe_key=f"spec:{uuid4()}",
    )


def _runner(
    factory: sessionmaker[Session],
    *,
    responses: list[ModelResponse] | None = None,
    policy: SpeculativePolicy | None = None,
) -> tuple[SpeculativeRunner, CliChannelAdapter, MockModelGateway]:
    adapter = CliChannelAdapter()
    delivery = DeliveryService([adapter])
    model = MockModelGateway(
        responses
        if responses is not None
        else [ModelResponse(text="TASK: draft"), ModelResponse(text="draft answer")]
    )
    runner = SpeculativeRunner(factory, model, delivery, policy=policy)
    return runner, adapter, model


def test_should_speculate_keyword_gate() -> None:
    assert should_speculate("There is a BUG in the login flow")
    assert should_speculate("会议邀约：周五评审")
    assert should_speculate("please report this crash")
    assert not should_speculate("hello, how are you?")
    assert not should_speculate("")


def test_side_effect_requests_are_rejected(factory: sessionmaker[Session]) -> None:
    runner, adapter, _ = _runner(factory)
    policy = SpeculativePolicy()
    with pytest.raises(PermissionDeniedError, match="side effects"):
        policy.create("send-email", budget_tokens=10, ttl_seconds=10, has_side_effects=True)
    # The runner itself always forces has_side_effects=False through the gate.
    speculation_id = runner.submit(_trigger("bug report"), wait=True)
    outcome = runner.get(speculation_id)
    assert outcome is not None
    assert outcome.status == "completed"
    assert adapter.messages == []  # nothing delivered without promote


def test_budget_and_ttl_limits_are_enforced(factory: sessionmaker[Session]) -> None:
    runner, _, _ = _runner(
        factory, policy=SpeculativePolicy(max_budget_tokens=100, max_ttl_seconds=60)
    )
    with pytest.raises(ValidationError, match="budget"):
        runner.submit(_trigger("bug"), budget_tokens=101, wait=True)
    with pytest.raises(ValidationError, match="TTL"):
        runner.submit(_trigger("bug"), budget_tokens=50, ttl_seconds=61, wait=True)


def test_completed_speculation_is_not_delivered_until_promote(
    factory: sessionmaker[Session],
) -> None:
    runner, adapter, _ = _runner(factory)
    speculation_id = runner.submit(_trigger("bug: login fails"), wait=True)
    outcome = runner.get(speculation_id)
    assert outcome is not None
    assert outcome.status == "completed"
    assert outcome.text == "draft answer"
    assert adapter.messages == []

    result = runner.promote(speculation_id)
    assert result.status.value == "sent"
    assert adapter.messages == ["draft answer"]
    promoted = runner.get(speculation_id)
    assert promoted is not None
    assert promoted.status == "delivered"
    # Promoting twice stays idempotent through DeliveryService.
    again = runner.promote(speculation_id)
    assert again.status.value == "sent"
    assert adapter.messages == ["draft answer"]


def test_expired_speculation_is_dropped(factory: sessionmaker[Session]) -> None:
    runner, adapter, _ = _runner(factory)
    speculation_id = runner.submit(_trigger("meeting prep"), ttl_seconds=300, wait=True)
    # Force expiry by rewriting the stored deadline.
    with runner._lock:
        entry = runner._entries[speculation_id]
        runner._entries[speculation_id] = type(entry)(
            entry.speculation_id,
            entry.status,
            entry.text,
            datetime.now(UTC) - timedelta(seconds=1),
            entry.error,
        )
    dropped = runner.prune_expired()
    assert speculation_id in dropped
    assert runner.get(speculation_id) is None
    with pytest.raises(ValidationError, match="expired"):
        runner.promote(speculation_id)
    assert adapter.messages == []


def test_discard_never_delivers(factory: sessionmaker[Session]) -> None:
    runner, adapter, _ = _runner(factory)
    speculation_id = runner.submit(_trigger("bug draft"), wait=True)
    runner.discard(speculation_id)
    assert runner.get(speculation_id) is None
    with pytest.raises(ValidationError, match="unknown"):
        runner.promote(speculation_id)
    assert adapter.messages == []


def test_failed_run_is_not_promotable(factory: sessionmaker[Session]) -> None:
    runner, adapter, _ = _runner(
        factory,
        responses=[ModelResponse(text="TASK: draft"), ModelResponse(text="")],
    )
    # Router creates the task; an empty worker answer still succeeds, so
    # force a failure through a model error on the worker round instead.
    from super_agent.errors import AgentError

    runner2, adapter2, _ = _runner(
        factory,
        responses=[ModelResponse(text="TASK: draft"), AgentError("provider exploded")],
    )
    speculation_id = runner2.submit(_trigger("bug"), wait=True)
    outcome = runner2.get(speculation_id)
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error is not None
    with pytest.raises(ValidationError, match="not deliverable"):
        runner2.promote(speculation_id)
    assert adapter.messages == []
    assert adapter2.messages == []
    assert runner is not None


def test_background_thread_execution(factory: sessionmaker[Session], tmp_path: Path) -> None:
    runner, adapter, _ = _runner(factory)
    speculation_id = runner.submit(_trigger("bug in background"))
    outcome = None
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        outcome = runner.get(speculation_id)
        if outcome is not None and outcome.status != "running":
            break
        time.sleep(0.02)
    assert outcome is not None
    assert outcome.status == "completed"
    assert adapter.messages == []
    runner.promote(speculation_id)
    assert adapter.messages == ["draft answer"]


def test_pending_lists_non_expired_only(factory: sessionmaker[Session]) -> None:
    runner, _, _ = _runner(factory)
    first = runner.submit(_trigger("bug one"), wait=True)
    second = runner.submit(_trigger("bug two"), wait=True)
    runner.discard(second)
    pending = runner.pending()
    assert [item.speculation_id for item in pending] == [first]

def test_speculative_runner_derives_capabilities_for_user_loaded_tools(
    factory: sessionmaker[Session],
) -> None:
    from super_agent.ports.model import ToolCall
    from super_agent.sdk import FunctionTool
    from super_agent.tools.registry import ToolRegistry

    def shout(text: str) -> str:
        """Shout text."""

        return text.upper() + "!"

    registry = ToolRegistry([FunctionTool(shout, capabilities=frozenset({"custom.shout"}))])
    adapter = CliChannelAdapter()
    model = MockModelGateway(
        [
            ModelResponse(text="TASK: draft a bug report"),
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="shout", arguments={"text": "hi"}),)
            ),
            ModelResponse(text="draft with tool result"),
        ]
    )
    runner = SpeculativeRunner(
        factory, model, DeliveryService([adapter]), tools=registry
    )
    assert runner._granted_capabilities >= frozenset({"file.read", "custom.shout"})
    speculation_id = runner.submit(_trigger("bug report: use shout"), wait=True)
    assert runner.get(speculation_id).status == "completed"
    assert runner.get(speculation_id).error is None
