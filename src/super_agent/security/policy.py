"""Default-deny capability, risk, and resource policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, ToolSpec


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Immutable security decision record."""

    audit_id: UUID
    tool_name: str
    decision: str
    reason: str
    tenant_id: str
    created_at: datetime


class ToolPolicy:
    """Evaluate tool declarations before invocation and record every decision."""

    def __init__(
        self,
        *,
        policy_version: str = "tool-policy-v1",
        approved_risks: frozenset[ToolRisk] = frozenset({ToolRisk.LOW}),
        max_timeout_seconds: float = 300.0,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if max_timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("resource limits must be positive")
        self.policy_version = policy_version
        self.approved_risks = approved_risks
        self.max_timeout_seconds = max_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.audit: list[AuditEvent] = []

    def authorize(
        self,
        spec: ToolSpec,
        context: ExecutionContext,
        *,
        approved: bool = False,
    ) -> None:
        """Authorize or reject one tool call and always append an audit event."""

        risk = ToolRisk(spec.risk)
        reason = "authorized"
        decision = "allow"
        try:
            if not spec.capabilities.issubset(context.granted_capabilities):
                raise PermissionDeniedError("capability denied: required capability not granted")
            if risk not in self.approved_risks and not approved:
                raise PermissionDeniedError("tool risk requires approval")
            if spec.requires_approval and not approved:
                raise PermissionDeniedError("tool requires explicit approval")
            if spec.max_output_bytes > self.max_output_bytes:
                raise ValidationError("tool output limit exceeds policy")
        except (PermissionDeniedError, ValidationError) as exc:
            decision = "deny"
            reason = str(exc)
            self.audit.append(
                AuditEvent(
                    uuid4(), spec.name, decision, reason, context.tenant_id, datetime.now(UTC)
                )
            )
            raise
        self.audit.append(
            AuditEvent(uuid4(), spec.name, decision, reason, context.tenant_id, datetime.now(UTC))
        )

    def record_denial(self, tool_name: str, context: ExecutionContext, reason: str) -> None:
        """Record a denial raised by a tool after policy authorization."""

        self.audit.append(
            AuditEvent(
                uuid4(), tool_name, "deny", reason, context.tenant_id, datetime.now(UTC)
            )
        )

