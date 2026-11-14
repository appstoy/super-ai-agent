from datetime import UTC, datetime, timedelta
from uuid import uuid4

from super_agent.application.runs import ToolRun
from super_agent.scheduler import Lease
from super_agent.watchdog import HealthSnapshot, Watchdog


def test_watchdog_reports_expired_lease_stalled_run_and_repeated_failure() -> None:
    now = datetime.now(UTC)
    lease = Lease(uuid4(), uuid4(), "repo", now - timedelta(seconds=1), "worker")
    stalled = ToolRun(uuid4(), "slow", "RUNNING", 1, started_at=now - timedelta(seconds=301))
    failed = ToolRun(uuid4(), "flaky", "FAILED", 3, started_at=now - timedelta(seconds=1))

    findings = Watchdog(stalled_after_seconds=300, failure_threshold=3).inspect(
        HealthSnapshot(now, (lease,), (stalled, failed))
    )
    kinds = {finding.kind.value for finding in findings}
    assert kinds == {"expired_lease", "stalled_run", "repeated_failure"}


def test_watchdog_reports_healthy_without_mutation() -> None:
    now = datetime.now(UTC)
    findings = Watchdog().inspect(HealthSnapshot(now, (), ()))
    assert findings[0].kind.value == "healthy"
    assert findings[0].action == "none"

