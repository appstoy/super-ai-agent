"""Error-classified recovery with idempotency and manual stop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from super_agent.errors import ErrorCategory


class RecoveryDecision(StrEnum):
    RETRY = "retry"
    WAIT = "wait"
    PAUSE = "pause"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """One immutable recovery decision."""

    idempotency_key: str
    decision: RecoveryDecision
    reason: str
    attempt: int


class RecoveryManager:
    """Apply bounded error recovery and remember decisions by key."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._max_attempts = max_attempts
        self._actions: dict[str, RecoveryAction] = {}
        self._stopped: set[str] = set()

    def stop(self, idempotency_key: str) -> None:
        """Prevent future recovery actions for a key."""

        self._stopped.add(idempotency_key)

    def decide(
        self,
        idempotency_key: str,
        category: ErrorCategory,
        *,
        attempt: int,
    ) -> RecoveryAction:
        """Return a stable, bounded decision for one failed operation."""

        previous = self._actions.get(idempotency_key)
        if previous is not None:
            return previous
        if idempotency_key in self._stopped:
            action = RecoveryAction(
                idempotency_key, RecoveryDecision.PAUSE, "stopped by operator", attempt
            )
        elif attempt >= self._max_attempts:
            action = RecoveryAction(
                idempotency_key, RecoveryDecision.PAUSE, "retry limit reached", attempt
            )
        elif category in {ErrorCategory.EXTERNAL_DEPENDENCY, ErrorCategory.TIMEOUT}:
            action = RecoveryAction(
                idempotency_key, RecoveryDecision.RETRY, category.value, attempt
            )
        elif category is ErrorCategory.PERMISSION:
            action = RecoveryAction(
                idempotency_key,
                RecoveryDecision.PAUSE,
                "permission requires review",
                attempt,
            )
        elif category is ErrorCategory.CONFIGURATION:
            action = RecoveryAction(
                idempotency_key,
                RecoveryDecision.WAIT,
                "configuration must be fixed",
                attempt,
            )
        else:
            action = RecoveryAction(idempotency_key, RecoveryDecision.FAIL, category.value, attempt)
        self._actions[idempotency_key] = action
        return action

