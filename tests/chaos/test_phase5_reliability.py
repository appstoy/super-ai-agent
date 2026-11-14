from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine

from super_agent.application.runs import ToolRun
from super_agent.daemon.protocol import DeviceRegistry
from super_agent.errors import ErrorCategory, PermissionDeniedError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.observability.telemetry import Telemetry
from super_agent.recovery import RecoveryDecision, RecoveryManager
from super_agent.scheduler import Lease
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import OutboxMessage
from super_agent.watchdog import HealthSnapshot, Watchdog


def test_phase5_reliability_regression() -> None:
    now = datetime.now(UTC)
    lease = Lease(uuid4(), uuid4(), "repo", now - timedelta(seconds=1), "worker")
    run = ToolRun(uuid4(), "slow", "RUNNING", 1, started_at=now - timedelta(seconds=301))
    findings = Watchdog(stalled_after_seconds=300).inspect(HealthSnapshot(now, (lease,), (run,)))
    assert {finding.kind.value for finding in findings} == {"expired_lease", "stalled_run"}

    recovery = RecoveryManager(max_attempts=2)
    assert (
        recovery.decide("run", ErrorCategory.TIMEOUT, attempt=1).decision
        is RecoveryDecision.RETRY
    )
    assert (
        recovery.decide("run", ErrorCategory.TIMEOUT, attempt=2).decision
        is RecoveryDecision.RETRY
    )
    assert (
        recovery.decide("run-limit", ErrorCategory.TIMEOUT, attempt=2).decision
        is RecoveryDecision.PAUSE
    )

    telemetry = Telemetry()
    assert telemetry.account(tokens=100, cost=1.0, max_tokens=100, max_cost=1.0).allowed
    assert not telemetry.account(tokens=1, cost=0.01, max_tokens=100, max_cost=1.0).allowed

    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    event = TriggerInput(
        source="chaos",
        thread_id=uuid4(),
        actor="test",
        payload={"message": "duplicate"},
        occurred_at=now,
        dedupe_key="chaos-duplicate",
    )
    with session_scope(factory) as session:
        service = TriggerIngressService(session)
        service.ingest(event)
        service.ingest(event)
    with session_scope(factory) as session:
        assert len(session.query(OutboxMessage).all()) == 1

    devices = DeviceRegistry()
    device, secret = devices.register(frozenset({"file.read"}))
    command = devices.sign(device.device_id, secret, "file.read", {"path": "x"})
    devices.revoke(device.device_id)
    try:
        devices.verify_and_consume(command, secret)
    except PermissionDeniedError:
        pass
    else:
        raise AssertionError("revoked device command was accepted")

