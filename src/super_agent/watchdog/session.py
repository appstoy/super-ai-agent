"""Model-driven Watchdog Session: the external Control Loop of article §6.

The rule layer (``PersistentRunWatchdog``) always runs first and its results
stand on their own. A model then judges whether active Runs show real progress
or drift from the goal, answering with one of:

- ``HEALTHY``
- ``INTERVENE: <reason>``  -> bounded recovery via RunRecoveryService
- ``ESCALATE: <reason>``   -> pending CLI DeliveryRecord requesting a human

Any model failure degrades gracefully: the rule-layer outcome is returned
unchanged and the error is reported, never raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.application.run_leases import RecoveredRun, RunLeaseService
from super_agent.application.run_recovery import RetryDecision, RunRecoveryService
from super_agent.ports.model import ChatMessage, MessageRole, ModelGateway, ModelRequest
from super_agent.storage.models import DeliveryRecord, RunRecord, WorkerEventRecord
from super_agent.watchdog.rules import WatchdogFinding
from super_agent.watchdog.runtime import PersistentRunWatchdog

_SYSTEM_PROMPT = (
    "You are the external Watchdog of a persistent agent system. Judge whether the "
    "active runs show substantive progress toward their goals. Reply with exactly "
    "one line: HEALTHY, or INTERVENE: <reason>, or ESCALATE: <reason>. "
    "INTERVENE means bounded automatic recovery may help; ESCALATE means a human "
    "must review. Never request new capabilities or tools."
)


@dataclass(frozen=True, slots=True)
class WatchdogTickResult:
    """One Control Loop iteration, including degraded model outcomes."""

    findings: tuple[WatchdogFinding, ...]
    expired: tuple[RecoveredRun, ...]
    retries: tuple[RetryDecision, ...]
    decision: str
    reason: str
    intervention_retries: tuple[RetryDecision, ...] = ()
    escalation_id: UUID | None = None
    model_error: str | None = None


class WatchdogSession:
    """Combine rule inspection with one bounded model judgement per tick."""

    def __init__(
        self,
        watchdog: PersistentRunWatchdog | None = None,
        *,
        max_attempts: int = 3,
        event_limit: int = 5,
        summary_chars: int = 200,
    ) -> None:
        if event_limit < 1 or summary_chars < 1:
            raise ValueError("watchdog context limits must be positive")
        self._watchdog = watchdog or PersistentRunWatchdog(max_attempts=max_attempts)
        self._max_attempts = max_attempts
        self._event_limit = event_limit
        self._summary_chars = summary_chars

    def tick(self, session: Session, model: ModelGateway) -> WatchdogTickResult:
        """Run the rule layer, then ask the model for a health verdict.

        The rule layer always runs first and stands on its own: findings
        plus unconditional expired-lease reaping (the lease half of
        ``PersistentRunWatchdog.recover``). Bounded retry requeueing is the
        model-gated intervention: INTERVENE runs ``RunRecoveryService`` on
        the Runs the rule layer just moved to timed_out, so a stalled Run
        is recovered and retried in one tick without unbounded loops.
        """

        findings = self._watchdog.inspect(session)
        expired = RunLeaseService(session).recover_expired()
        retries: tuple[RetryDecision, ...] = ()
        context = self._build_context(session, findings)
        try:
            response = model.complete(
                ModelRequest(
                    messages=(
                        ChatMessage(MessageRole.SYSTEM, _SYSTEM_PROMPT),
                        ChatMessage(MessageRole.USER, context),
                    ),
                    request_id=f"watchdog:{uuid4()}",
                )
            )
        except Exception as exc:
            # The external loop must survive provider outages; rules already ran.
            return WatchdogTickResult(
                findings, expired, retries, "healthy", "model unavailable", model_error=str(exc)
            )
        decision, reason = self._parse_verdict(response.text)
        intervention: tuple[RetryDecision, ...] = ()
        escalation_id: UUID | None = None
        if decision == "intervene":
            intervention = RunRecoveryService(
                session, max_attempts=self._max_attempts
            ).requeue_retryable()
        elif decision == "escalate":
            escalation_id = self._request_human(session, reason)
        return WatchdogTickResult(
            findings,
            expired,
            retries,
            decision,
            reason,
            intervention_retries=intervention,
            escalation_id=escalation_id,
        )

    def ask(self, session: Session, model: ModelGateway, question: str) -> str:
        """Answer a maintainer's natural-language question from one LLM call."""

        findings = self._watchdog.inspect(session)
        context = self._build_context(session, findings)
        try:
            response = model.complete(
                ModelRequest(
                    messages=(
                        ChatMessage(
                            MessageRole.SYSTEM,
                            "You are the Watchdog of a persistent agent system. Answer the "
                            "maintainer using only the health snapshot below; be concise.",
                        ),
                        ChatMessage(MessageRole.USER, f"{context}\n\nQuestion: {question}"),
                    ),
                    request_id=f"watchdog-ask:{uuid4()}",
                )
            )
        except Exception as exc:
            return f"model unavailable ({exc}); rule findings: {self._format_findings(findings)}"
        return response.text

    def _build_context(self, session: Session, findings: tuple[WatchdogFinding, ...]) -> str:
        lines = [f"checked_at={datetime.now(UTC).isoformat()}", "findings:"]
        lines.extend(self._format_findings(findings).splitlines())
        runs = session.scalars(
            select(RunRecord)
            .where(RunRecord.status.in_({"pending", "running", "failed", "timed_out"}))
            .order_by(RunRecord.started_at)
        ).all()
        lines.append("active_runs:")
        for run in runs:
            heartbeat = run.heartbeat_at.isoformat() if run.heartbeat_at else "none"
            lines.append(
                f"- run={run.id} task={run.task_id} status={run.status} "
                f"attempt={run.attempt} heartbeat={heartbeat}"
            )
            events = (
                session.scalars(
                    select(WorkerEventRecord)
                    .where(WorkerEventRecord.task_id == run.task_id)
                    .order_by(WorkerEventRecord.sequence.desc())
                    .limit(self._event_limit)
                )
                .all()
            )
            for event in reversed(events):
                summary = event.summary[: self._summary_chars]
                lines.append(
                    f"  event[{event.sequence}] {event.event_type}/{event.status}: {summary}"
                )
        return "\n".join(lines)

    def _request_human(self, session: Session, reason: str) -> UUID:
        delivery_id = uuid4()
        session.add(
            DeliveryRecord(
                id=delivery_id,
                task_id=UUID(int=0),
                channel="cli",
                idempotency_key=f"watchdog-escalate:{delivery_id}",
                payload_ref=reason,
                status="pending",
                version=0,
            )
        )
        session.flush()
        return delivery_id

    @staticmethod
    def _format_findings(findings: tuple[WatchdogFinding, ...]) -> str:
        return "\n".join(
            f"- {finding.kind.value} subject={finding.subject_id} "
            f"evidence={finding.evidence} action={finding.action}"
            for finding in findings
        )

    @staticmethod
    def _parse_verdict(text: str) -> tuple[str, str]:
        stripped = text.strip()
        upper = stripped.upper()
        if upper.startswith("INTERVENE:"):
            return "intervene", stripped.split(":", 1)[1].strip() or "model requested recovery"
        if upper.startswith("ESCALATE:"):
            return "escalate", stripped.split(":", 1)[1].strip() or "model requested human review"
        if upper.startswith("HEALTHY"):
            return "healthy", stripped
        # Non-contract output never triggers mutations; treat it as degraded.
        return "healthy", f"unrecognized model verdict: {stripped[:200]}"