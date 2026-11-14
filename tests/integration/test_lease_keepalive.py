"""A slow model call must not be reaped as a dead Run.

Reported symptom: a task showed "starting / running" in the drawer and then the
file appeared but the task was reported failed with "Worker stopped by
cancellation or lease policy". Root cause: the Run lease was 60s and was only
renewed *between* rounds, so one blocking model call (a 47 KB HTML page takes
~90s) let the recovery thread time the Run out mid-generation.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import RunRecord
from super_agent.tools.loading import build_agent_tools

HTML = "<!doctype html><html><body><h1>slow</h1></body></html>"


class SlowGateway(MockModelGateway):
    """Router answers instantly; the Worker call blocks like a real long answer."""

    def __init__(self, delay: float) -> None:
        super().__init__([])
        self._delay = delay
        self._worker_turns = 0

    def complete(self, request: ModelRequest) -> ModelResponse:
        system = request.messages[0].content
        if system.startswith("Return exactly one route") or "backlogged" in system:
            return ModelResponse(text="TASK: slow build")
        self._worker_turns += 1
        time.sleep(self._delay)
        if self._worker_turns == 1:
            from super_agent.ports.model import ToolCall

            return ModelResponse(
                text="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="file_write",
                        arguments={"path": "slow.html", "content": HTML},
                    ),
                ),
            )
        return ModelResponse(text="done")


def make_factory(tmp_path) -> sessionmaker[Session]:
    engine = create_engine_for_url("sqlite:///" + (tmp_path / "lease.db").as_posix())
    create_schema(engine)
    return create_session_factory(engine)


def test_slow_model_call_keeps_the_lease_alive(tmp_path, artifact) -> None:
    """The heartbeat must run DURING the call, not only between rounds."""

    factory = make_factory(tmp_path)
    registry, capabilities, _ = build_agent_tools(
        Settings(workspace_root=str(tmp_path)), workspace_root=tmp_path
    )
    delay = 1.2
    # A lease shorter than one model call: without a real keepalive the Run
    # expires while the model is still thinking.
    loop = {"n": 0}

    with session_scope(factory) as session:
        from super_agent.application.run_leases import RunLeaseService

        orchestrator = AgentOrchestrator(
            session,
            SlowGateway(delay),
            tools=registry,
            granted_capabilities=capabilities,
            lease_seconds=0.5,
            max_rounds=4,
        )

        seen: list[str] = []
        original = RunLeaseService.heartbeat

        def counting_heartbeat(self, run_id, owner, *, lease_seconds=60.0):
            loop["n"] += 1
            return original(self, run_id, owner, lease_seconds=lease_seconds)

        RunLeaseService.heartbeat = counting_heartbeat  # type: ignore[method-assign]
        try:
            result = orchestrator.submit(
                TriggerInput(
                    source="web",
                    actor="user",
                    thread_id=uuid4(),
                    payload={"message": "做一个 HTML 页面", "explicit": True},
                    occurred_at=datetime.now(UTC),
                    dedupe_key="slow-" + uuid4().hex,
                )
            )
        finally:
            RunLeaseService.heartbeat = original  # type: ignore[method-assign]

        seen.append(result.status)
        runs = session.query(RunRecord).filter(RunRecord.task_id == result.task_id).all()

    # The artifact exists and the turn was not killed by the lease policy.
    assert artifact(tmp_path, "slow.html").read_text(encoding="utf-8") == HTML
    assert result.status == "succeeded", result.text
    assert all(run.status == "succeeded" for run in runs), [r.status for r in runs]
    # More than one heartbeat: the keepalive ran during the blocked call.
    assert loop["n"] > 2


def test_default_lease_outlives_a_long_model_call() -> None:
    """A default-configured Worker must tolerate a multi-minute answer."""

    from super_agent.application.orchestrator import AgentOrchestrator as AO

    defaults = AO.__init__.__defaults__ or ()
    # lease_seconds is keyword-only, so read it off the signature instead.
    import inspect

    params = inspect.signature(AO.__init__).parameters
    assert params["lease_seconds"].default >= 180.0
    del defaults
