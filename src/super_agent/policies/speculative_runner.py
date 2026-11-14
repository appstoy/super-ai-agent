"""Background speculative execution with human-gated delivery (article §3.4).

``SpeculativeRunner.submit`` validates a request through ``SpeculativePolicy``
(no side effects, bounded budget and TTL), then runs an orchestrator on a
background thread with read-only capabilities. Results stay in an in-memory
dict and are NEVER delivered automatically: only ``promote(speculation_id)``
after human confirmation hands the text to ``DeliveryService``. Expired
entries are dropped.

Wiring points (no API change in this slice):
- An event filter or Router hook calls ``should_speculate(text)`` on incoming
  messages and, when true, ``submit(...)`` with a session factory, gateway,
  and tool registry.
- A CLI/API approval surface lists ``pending()`` and calls ``promote`` or
  ``discard`` after the user reviews the draft.
- A periodic sweeper may call ``prune_expired()`` to bound memory.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.application.orchestrator import AgentOrchestrator, AgentRunResult
from super_agent.delivery.service import DeliveryService
from super_agent.errors import AgentError, ValidationError
from super_agent.ingress.schemas import TriggerInput
from super_agent.policies.speculative import SpeculativePolicy, SpeculativeRequest
from super_agent.ports.channel import ChannelDeliveryResult, ChannelType, DeliveryRequest
from super_agent.ports.model import ModelGateway
from super_agent.ports.tool import ToolEffect
from super_agent.tools.registry import ToolRegistry

_TRIGGER_KEYWORDS = ("bug", "report", "会议", "报错", "复现", "修复", "故障", "回归")


@dataclass(frozen=True, slots=True)
class SpeculationOutcome:
    """One background attempt; ``text`` is a draft until promoted."""

    speculation_id: UUID
    status: str
    text: str
    expires_at: datetime
    error: str | None = None


def should_speculate(text: str) -> bool:
    """Cheap keyword gate for signals worth pre-computing in the background."""

    lowered = text.lower()
    return any(keyword in lowered for keyword in _TRIGGER_KEYWORDS)


class SpeculativeRunner:
    """Run bounded, side-effect-free speculations and gate their delivery."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        gateway: ModelGateway,
        delivery: DeliveryService,
        *,
        policy: SpeculativePolicy | None = None,
        tools: ToolRegistry | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        granted_capabilities: frozenset[str] | None = None,
    ) -> None:
        self._factory = factory
        self._gateway = gateway
        self._delivery = delivery
        self._policy = policy or SpeculativePolicy()
        self._tools = tools or ToolRegistry()
        self._spawn = spawn
        if granted_capabilities is None:
            # Derive from registered specs so user-loaded tools are not
            # silently denied by the capability check inside ToolRegistry.
            # A speculation must stay side-effect-free, so write-capable tools
            # are excluded even though the shared registry offers them.
            derived = {"file.read", "recall.read"}
            for spec in self._tools.specs():
                if spec.effect is ToolEffect.WRITE:
                    continue
                derived.update(spec.capabilities)
            granted_capabilities = frozenset(derived)
        self._granted_capabilities = granted_capabilities
        self._lock = threading.Lock()
        self._entries: dict[UUID, SpeculationOutcome] = {}

    def submit(
        self,
        trigger_input: TriggerInput,
        *,
        task_type: str = "speculative-task",
        budget_tokens: int = 2_000,
        ttl_seconds: float = 300.0,
        wait: bool = False,
    ) -> UUID:
        """Validate, then run one read-only speculation in the background.

        ``wait=True`` runs inline for deterministic tests; production callers
        keep the default and poll ``get``/``pending``.
        """

        request = self._policy.create(
            task_type,
            budget_tokens=budget_tokens,
            ttl_seconds=ttl_seconds,
            has_side_effects=False,
            delivery_condition="human approval",
        )
        speculation_id = uuid4()
        with self._lock:
            self._entries[speculation_id] = SpeculationOutcome(
                speculation_id, "running", "", request.expires_at
            )

        def work() -> None:
            self._execute(speculation_id, trigger_input, request)

        if wait:
            work()
        elif self._spawn is not None:
            self._spawn(work)
        else:
            thread = threading.Thread(
                target=work, name=f"speculation:{speculation_id}", daemon=True
            )
            thread.start()
        return speculation_id

    def get(self, speculation_id: UUID) -> SpeculationOutcome | None:
        """Return one outcome, dropping it first when its TTL has passed."""

        self.prune_expired()
        with self._lock:
            return self._entries.get(speculation_id)

    def pending(self) -> tuple[SpeculationOutcome, ...]:
        """List non-expired outcomes for a human review surface."""

        self.prune_expired()
        with self._lock:
            return tuple(self._entries.values())

    def promote(self, speculation_id: UUID) -> ChannelDeliveryResult:
        """Deliver one completed draft only after explicit human approval.

        Repeat promotions are idempotent: DeliveryService deduplicates on
        the speculation's idempotency key.
        """

        outcome = self.get(speculation_id)
        if outcome is None:
            raise ValidationError("speculation is unknown or expired")
        if outcome.status not in {"completed", "delivered"}:
            raise ValidationError(f"speculation is not deliverable: {outcome.status}")
        result = self._delivery.send(
            DeliveryRequest(
                channel=ChannelType.CLI,
                idempotency_key=f"speculation:{speculation_id}",
                summary=outcome.text,
                details={"speculation_id": str(speculation_id)},
            )
        )
        with self._lock:
            self._entries[speculation_id] = SpeculationOutcome(
                speculation_id, "delivered", outcome.text, outcome.expires_at
            )
        return result

    def discard(self, speculation_id: UUID) -> None:
        """Drop one speculation without delivering anything."""

        with self._lock:
            self._entries.pop(speculation_id, None)

    def prune_expired(self) -> tuple[UUID, ...]:
        """Remove entries whose TTL elapsed; returns the dropped ids."""

        now = datetime.now(UTC)
        with self._lock:
            expired = [
                key for key, entry in self._entries.items() if entry.expires_at <= now
            ]
            for key in expired:
                del self._entries[key]
        return tuple(expired)

    def _execute(
        self,
        speculation_id: UUID,
        trigger_input: TriggerInput,
        request: SpeculativeRequest,
    ) -> None:
        status = "failed"
        text = ""
        error: str | None = None
        try:
            session = self._factory()
            try:
                result: AgentRunResult = AgentOrchestrator(
                    session,
                    self._gateway,
                    tools=self._tools,
                    granted_capabilities=self._granted_capabilities,
                ).submit(trigger_input)
                session.commit()
                text = result.text
                status = "completed" if result.status == "succeeded" else "failed"
                if status == "failed":
                    error = f"run ended with status {result.status}"
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except AgentError as exc:
            error = str(exc)
        except Exception as exc:
            error = str(exc)
        with self._lock:
            existing = self._entries.get(speculation_id)
            if existing is None:
                return  # discarded or expired while running; drop the result
            self._entries[speculation_id] = SpeculationOutcome(
                speculation_id, status, text, request.expires_at, error
            )