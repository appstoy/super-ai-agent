"""In-process background execution bridge for long-running Agent tasks."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Any

from sqlalchemy.orm import sessionmaker

from super_agent.application.async_tools import (
    DurableToolRunner,
    tool_completed_trigger,
    trigger_input_from_outbox,
)
from super_agent.application.orchestrator import AgentOrchestrator, AgentRunResult
from super_agent.application.outbox import OutboxProcessor
from super_agent.application.run_leases import RunLeaseService
from super_agent.application.run_recovery import RunRecoveryService
from super_agent.domain import Trigger
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.policies.speculative_runner import SpeculativeRunner, should_speculate
from super_agent.ports.model import ModelGateway
from super_agent.ports.tool import Tool
from super_agent.storage.models import OutboxMessage
from super_agent.storage.outbox import OutboxRepository
from super_agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class BackgroundSubmission:
    """Accepted background request before its durable Run exists."""

    dedupe_key: str
    future: Future[AgentRunResult]


class BackgroundAgentRunner:
    """Execute full Agent turns outside the HTTP request thread."""

    def __init__(
        self,
        factory: sessionmaker[Any],
        *,
        tools: ToolRegistry | None = None,
        granted_capabilities: frozenset[str] = frozenset(),
        max_workers: int = 4,
        tool_runner: DurableToolRunner | None = None,
        recall_tool: Tool | None = None,
        max_rounds: int = 12,
    ) -> None:
        self._factory = factory
        self._tools = tools
        self._granted_capabilities = granted_capabilities
        self._max_rounds = max_rounds
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: dict[str, Future[AgentRunResult]] = {}
        self._tool_runner = tool_runner
        self._recall_tool = recall_tool

    def submit(self, trigger_input: TriggerInput, gateway: ModelGateway) -> BackgroundSubmission:
        """Queue a trigger for the same Outbox -> Router -> Worker path."""

        # Commit the durable trigger before model work starts. A failed worker can
        # then be retried from the pending Outbox message after the process restarts.
        with self._factory() as session:
            with session.begin():
                TriggerIngressService(session).ingest(trigger_input)
        future = self._executor.submit(self._run, trigger_input, gateway)
        self._futures[trigger_input.dedupe_key] = future
        return BackgroundSubmission(trigger_input.dedupe_key, future)

    def future(self, dedupe_key: str) -> Future[AgentRunResult] | None:
        """Return the local future for diagnostics when the process is alive."""

        return self._futures.get(dedupe_key)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and release the in-process worker threads."""

        self._executor.shutdown(wait=wait)

    def _run(
        self,
        trigger_input: TriggerInput,
        gateway: ModelGateway,
    ) -> AgentRunResult:
        with self._factory() as session:
            try:
                result = AgentOrchestrator(
                    session,
                    gateway,
                    tools=self._tools,
                    granted_capabilities=self._granted_capabilities,
                    detach_execution=True,
                    tool_runner=self._tool_runner,
                    recall_tool=self._recall_tool,
                ).submit_via_outbox(trigger_input)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise


GatewayFactory = Callable[[str | None], ModelGateway]

MAX_OUTBOX_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 60.0


def outbox_backoff(attempts: int, *, base_seconds: float = 1.0) -> float:
    """Exponential backoff so a failing event stops hogging the queue head."""

    if base_seconds <= 0:
        raise ValueError("base_seconds must be positive")
    return min(
        _MAX_BACKOFF_SECONDS,
        base_seconds * float(2 ** max(0, attempts - 1)),
    )


def _payload_model(payload: dict[str, object]) -> str | None:
    """Read the requested model name out of one queued trigger payload."""

    inner = payload.get("payload")
    if isinstance(inner, dict):
        model = inner.get("model")
        if isinstance(model, str):
            return model
    return None


def _log_failure(factory: Any, message: OutboxMessage, exc: Exception) -> None:
    """Persist the failure reason on the message so the console can show it."""

    try:
        with factory() as session, session.begin():
            record = session.get(OutboxMessage, message.id)
            if record is not None:
                record.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    except Exception:  # noqa: BLE001 - diagnostics must never break the loop
        pass


class DurableOutboxWorker:
    """Poll durable Trigger events so another process can resume accepted work."""

    def __init__(
        self,
        factory: sessionmaker[Any],
        gateway_factory: GatewayFactory,
        *,
        tools: ToolRegistry | None = None,
        granted_capabilities: frozenset[str] = frozenset(),
        tool_runner: DurableToolRunner | None = None,
        recall_tool: Tool | None = None,
        speculative_runner: SpeculativeRunner | None = None,
        batch_limit: int = 1,
        max_rounds: int = 12,
        max_attempts: int = MAX_OUTBOX_ATTEMPTS,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("batch_limit must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_backoff_seconds <= 0:
            raise ValueError("retry_backoff_seconds must be positive")
        self._factory = factory
        self._gateway_factory = gateway_factory
        self._tools = tools
        self._granted_capabilities = granted_capabilities
        self._tool_runner = tool_runner
        self._recall_tool = recall_tool
        self._speculative_runner = speculative_runner
        self._batch_limit = batch_limit
        self._max_rounds = max_rounds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self.router_metrics: dict[str, float | int] = {}
        # Last background failure, surfaced through /api/runtime/summary so a
        # stuck worker explains itself instead of silently looping.
        self.last_error: str | None = None

    def _retry_backoff(self, attempts: int) -> float:
        return outbox_backoff(attempts, base_seconds=self._retry_backoff_seconds)

    def _record_loop_failure(self, exc: Exception) -> None:
        """Remember the most recent loop-level failure for the console."""

        self.last_error = f"{type(exc).__name__}: {exc}"[:2000]

    def process_once(self) -> bool:
        """Claim and process one pending event, returning whether work was found."""

        with self._factory() as session:
            try:
                processed = False
                recovered = RunLeaseService(session).recover_expired()
                retries = RunRecoveryService(session).requeue_retryable()

                def handle(message: OutboxMessage, payload: dict[str, object]) -> None:
                    nonlocal processed
                    processed = True
                    raw_payload = payload.get("payload")
                    model_name: str | None = None
                    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("model"), str):
                        model_name = raw_payload["model"]
                    AgentOrchestrator.from_outbox_payload(
                        session,
                        self._gateway_factory(model_name),
                        payload,
                        tools=self._tools,
                        granted_capabilities=self._granted_capabilities,
                        detach_execution=True,
                        topic=message.topic,
                        tool_runner=self._tool_runner,
                        recall_tool=self._recall_tool,
                        max_rounds=self._max_rounds,
                    )

                message = OutboxProcessor(
                    session,
                    handle,
                    max_attempts=self._max_attempts,
                    retry_backoff=self._retry_backoff,
                ).process_next()
                retired = OutboxRepository(session).retire_exhausted(
                    max_attempts=self._max_attempts
                )
                if message is not None and message.status == "FAILED":
                    self.last_error = message.last_error
                elif retired:
                    self.last_error = retired[0].last_error
                session.commit()
                return (
                    bool(recovered)
                    or bool(retries)
                    or message is not None
                    or processed
                    or bool(retired)
                )
            except Exception as exc:
                session.rollback()
                self._record_loop_failure(exc)
                raise

    def process_batch(self) -> bool:
        """Drain a backlog with one merged Router call for cold-start events.

        Article §2.1 batching: while the Router is busy, arriving triggers
        accumulate; this path claims up to ``batch_limit`` pending events and
        routes the unsubscribed candidates in a single model call. Follow-up
        events on subscribed threads still fan out per Worker, and
        tool.completed continuations keep their single-event semantics.
        """

        with self._factory() as session:
            try:
                recovered = RunLeaseService(session).recover_expired()
                retries = RunRecoveryService(session).requeue_retryable()
                repository = OutboxRepository(session)
                retired = repository.retire_exhausted(max_attempts=self._max_attempts)
                claimed: list[tuple[OutboxMessage, dict[str, object]]] = []
                for _ in range(self._batch_limit):
                    message = repository.claim_next(max_attempts=self._max_attempts)
                    if message is None:
                        break
                    try:
                        payload = json.loads(message.payload)
                    except (TypeError, json.JSONDecodeError) as exc:
                        # A corrupt payload must be parked for this message only;
                        # letting it escape rolls back the whole batch and makes
                        # the queue head immortal.
                        repository.mark_failed(
                            message,
                            f"outbox payload is not valid JSON: {exc}",
                            max_attempts=self._max_attempts,
                        )
                        continue
                    if not isinstance(payload, dict):
                        repository.mark_failed(
                            message,
                            "outbox payload must be an object",
                            max_attempts=self._max_attempts,
                        )
                        continue
                    claimed.append((message, payload))
                if not claimed:
                    session.commit()
                    return bool(recovered) or bool(retries) or bool(retired)

                cold: list[tuple[Trigger, dict[str, object] | None, OutboxMessage]] = []
                try:
                    # Gateway construction used to happen outside any per-message
                    # try: with no model configured it raised, the whole batch
                    # rolled back (including the attempts increment), and the
                    # queue head was re-picked forever. Treat it as a batch-level
                    # failure that is recorded per message instead.
                    default_gateway = self._gateway_factory(None)
                except Exception as exc:
                    for message, payload in claimed:
                        model_name = _payload_model(payload)
                        repository.mark_failed(
                            message,
                            f"model gateway unavailable for {model_name or 'default'}: "
                            f"{type(exc).__name__}: {exc}",
                            retry_in_seconds=self._retry_backoff(message.attempts),
                            max_attempts=self._max_attempts,
                        )
                    self._record_loop_failure(exc)
                    session.commit()
                    return False
                orchestrator = AgentOrchestrator(
                    session,
                    default_gateway,
                    tools=self._tools,
                    granted_capabilities=self._granted_capabilities,
                    detach_execution=True,
                    tool_runner=self._tool_runner,
                    recall_tool=self._recall_tool,
                    max_rounds=self._max_rounds,
                )
                for message, payload in claimed:
                    try:
                        if message.topic == "tool.completed":
                            trigger_input = tool_completed_trigger(payload)
                            orchestrator.submit(trigger_input, enqueue_outbox=False)
                        else:
                            trigger_input = trigger_input_from_outbox(payload)
                            trigger = TriggerIngressService(session).ingest(
                                trigger_input, enqueue_outbox=False
                            )
                            session.flush()
                            # Cold-start events are published only after their
                            # routing/execution outcome is known, so publishing
                            # moved into _route_and_execute_batch.
                            cold.append((trigger, trigger_input.payload, message))
                            self._maybe_speculate(trigger_input)
                            continue
                        self._maybe_speculate(trigger_input)
                        repository.mark_published(message)
                    except Exception as exc:
                        # One poison event must not take the whole batch (or the
                        # queue head) down with it: record the error on that
                        # message, back it off, and keep draining the rest.
                        _log_failure(self._factory, message, exc)
                        repository.mark_failed(
                            message,
                            f"{type(exc).__name__}: {exc}",
                            retry_in_seconds=self._retry_backoff(message.attempts),
                            max_attempts=self._max_attempts,
                        )
                if cold:
                    self._route_and_execute_batch(session, orchestrator, cold, repository)
                self.router_metrics = {
                    "claimed": len(claimed),
                    "cold_start_batch": len(cold),
                }
                session.commit()
                return True
            except Exception as exc:
                session.rollback()
                self._record_loop_failure(exc)
                raise

    def _route_and_execute_batch(
        self,
        session: Any,
        orchestrator: AgentOrchestrator,
        cold: list[tuple[Trigger, dict[str, object] | None, OutboxMessage]],
        repository: OutboxRepository,
    ) -> None:
        """Route a batch, isolating per-event failure so one event never blocks the rest.

        Previously ``process_batch`` ran the whole batch inside one transaction:
        any failure (a missing model credential, a provider outage, one bad
        payload) rolled everything back and the queue head was retried forever.
        Now each event either publishes or is parked with its own error.
        """

        batch_payloads = [(trigger, payload) for trigger, payload, _ in cold]
        try:
            results = orchestrator.process_batch(batch_payloads)
            result_by_trigger = {result.trigger_id: result for result in results}
        except Exception as exc:
            # The merged Router call failed for the whole batch (typically the
            # provider is down). Fail every event in the batch rather than
            # silently retrying the same head message.
            for _trigger, _payload, message in cold:
                _log_failure(self._factory, message, exc)
                repository.mark_failed(
                    message,
                    f"{type(exc).__name__}: {exc}",
                    retry_in_seconds=self._retry_backoff(message.attempts),
                    max_attempts=self._max_attempts,
                )
            return
        for trigger, _payload, message in cold:
            result = result_by_trigger.get(trigger.id)
            if result is not None and result.status == "failed":
                repository.mark_failed(
                    message,
                    f"worker failed: {result.text[:500]}",
                    retry_in_seconds=self._retry_backoff(message.attempts),
                    max_attempts=self._max_attempts,
                )
            else:
                repository.mark_published(message)

    def _maybe_speculate(self, trigger_input: TriggerInput) -> None:
        """Article §3.4: silently pre-compute drafts for signal-like events."""

        if self._speculative_runner is None:
            return
        message = trigger_input.payload.get("message")
        if not isinstance(message, str) or not should_speculate(message):
            return
        speculation_input = trigger_input.model_copy(
            update={"dedupe_key": f"speculation:{trigger_input.dedupe_key}"}
        )
        try:
            self._speculative_runner.submit(speculation_input)
        except Exception:
            # Speculation is best-effort background work; it must never break
            # the durable event path.
            pass

    def run_forever(self, stop_event: Event, *, poll_interval: float = 0.5) -> None:
        """Keep polling until the caller requests shutdown."""

        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        while not stop_event.is_set():
            try:
                if self._batch_limit > 1:
                    processed = self.process_batch()
                else:
                    processed = self.process_once()
                if processed:
                    self.last_error = None
            except Exception as exc:
                # A missing credential or transient provider error must not kill
                # the recovery loop; the next tick can retry it. The reason is
                # recorded so the console shows why nothing is progressing.
                self._record_loop_failure(exc)
                processed = False
                # Back off before retrying a loop-level failure; otherwise a
                # down provider turns into a hot retry loop with no signal.
                stop_event.wait(max(poll_interval, outbox_backoff(1)))
                continue
            if not processed:
                stop_event.wait(poll_interval)
