from super_agent.errors import ErrorCategory
from super_agent.recovery import RecoveryDecision, RecoveryManager


def test_recovery_classifies_errors_and_is_idempotent() -> None:
    manager = RecoveryManager(max_attempts=3)
    retry = manager.decide("run-1", ErrorCategory.TIMEOUT, attempt=1)
    assert retry.decision is RecoveryDecision.RETRY
    assert manager.decide("run-1", ErrorCategory.TIMEOUT, attempt=2) == retry
    assert (
        manager.decide("run-2", ErrorCategory.PERMISSION, attempt=1).decision
        is RecoveryDecision.PAUSE
    )
    assert (
        manager.decide("run-3", ErrorCategory.CONFIGURATION, attempt=1).decision
        is RecoveryDecision.WAIT
    )
    assert (
        manager.decide("run-4", ErrorCategory.INTERNAL, attempt=1).decision
        is RecoveryDecision.FAIL
    )


def test_recovery_stops_and_limits_retries() -> None:
    manager = RecoveryManager(max_attempts=2)
    manager.stop("run-stop")
    assert (
        manager.decide("run-stop", ErrorCategory.TIMEOUT, attempt=1).decision
        is RecoveryDecision.PAUSE
    )
    assert (
        manager.decide("run-limit", ErrorCategory.TIMEOUT, attempt=2).decision
        is RecoveryDecision.PAUSE
    )

