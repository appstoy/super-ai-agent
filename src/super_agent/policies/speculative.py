"""Safety gate for background speculative tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from super_agent.errors import PermissionDeniedError, ValidationError


@dataclass(frozen=True, slots=True)
class SpeculativeRequest:
    task_type: str
    budget_tokens: int
    expires_at: datetime
    has_side_effects: bool
    delivery_condition: str


class SpeculativePolicy:
    """Allow only bounded, side-effect-free speculative work."""

    def __init__(self, *, max_budget_tokens: int = 10_000, max_ttl_seconds: float = 900.0) -> None:
        self.max_budget_tokens = max_budget_tokens
        self.max_ttl_seconds = max_ttl_seconds

    def create(
        self,
        task_type: str,
        *,
        budget_tokens: int,
        ttl_seconds: float,
        has_side_effects: bool = False,
        delivery_condition: str = "human approval",
    ) -> SpeculativeRequest:
        if not task_type or budget_tokens <= 0 or budget_tokens > self.max_budget_tokens:
            raise ValidationError("speculative budget or task type is invalid")
        if ttl_seconds <= 0 or ttl_seconds > self.max_ttl_seconds:
            raise ValidationError("speculative TTL exceeds policy")
        if has_side_effects:
            raise PermissionDeniedError("speculative tasks cannot have side effects")
        if not delivery_condition:
            raise ValidationError("speculative delivery condition is required")
        return SpeculativeRequest(
            task_type,
            budget_tokens,
            datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            has_side_effects,
            delivery_condition,
        )

