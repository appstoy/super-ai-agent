"""Resident Router session: durable cross-task memory with batching (article §2.1).

The Router is one long-lived conversation per tenant. Every routing turn appends
the trigger summary (user role) and the model decision (assistant role) to the
``router_messages`` table, so later turns remember earlier plans even across
tasks and channels. The transcript window is bounded with Inline & Spill
(article §3.3): the oldest entry stays inline as an anchor, the most recent
entries stay inline, and everything in between is compressed into one reference
summary that carries recallable ``router_messages`` IDs.

Backlogged triggers are merged into a single model call (``route_batch``): the
model answers one ``<n>: decision`` line per trigger, reusing the existing
IGNORE / CLARIFY: / TASK: contract.

When no database ``Session`` is supplied the transcript lives in memory only.
That keeps the legacy ``RouterService(model)`` entry point usable without a
database while still routing through the same resident-session logic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from super_agent.application.router import RouteAction, RouteResult
from super_agent.domain import (
    Delivery,
    Task,
    TaskStatus,
    Thread,
    Trigger,
    TriggerFilterDecision,
    Worker,
)
from super_agent.ports.model import ChatMessage, MessageRole, ModelGateway, ModelRequest
from super_agent.storage.models import RouterMessageRecord

ROUTER_SYSTEM_PROMPT = (
    "Return exactly one route: IGNORE, CLARIFY: question, or TASK: title. "
    "You are the resident Super-agent Router: a persistent global coordinator with "
    "cross-task memory. You only dispatch work to isolated Worker sessions and talk "
    "to humans; you never execute tasks or tools yourself. Use the earlier "
    "conversation to keep decisions consistent (for example when the user replaces "
    "plan A with plan B)."
)

ROUTER_BATCH_SYSTEM_PROMPT = (
    "Return exactly one route per input line, formatted '<n>: IGNORE', "
    "'<n>: CLARIFY: question', or '<n>: TASK: title', where <n> is the 1-based "
    "trigger number. You are the resident Super-agent Router: a persistent global "
    "coordinator with cross-task memory. You only dispatch work to isolated Worker "
    "sessions and talk to humans; you never execute tasks or tools yourself. Use "
    "the earlier conversation to keep decisions consistent."
)

_BATCH_LINE = re.compile(r"^\s*(\d+)\s*[:.]\s*(.+?)\s*$")
_SPILL_PREVIEW_CHARS = 60
_SPILL_SUMMARY_CHAR_CAP = 2_000
_SPILL_ID_CAP = 50


@dataclass(frozen=True, slots=True)
class RouterBatchMetrics:
    """Counters and latency for resident Router model calls."""

    total_calls: int = 0
    total_triggers: int = 0
    batched_calls: int = 0
    last_batch_size: int = 0
    max_batch_size: int = 0
    last_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0

    @property
    def average_batch_size(self) -> float:
        """Mean number of triggers merged into one model call."""

        return self.total_triggers / self.total_calls if self.total_calls else 0.0


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One durable Router transcript message loaded for the context window."""

    message_id: UUID
    sequence: int
    role: str
    content: str


class ResidentRouterSession:
    """One persistent Router conversation per tenant, backed by router_messages."""

    def __init__(
        self,
        session: Session | None,
        model: ModelGateway,
        *,
        tenant_id: str = "default",
        window_budget: int = 40,
        ignored_sources: frozenset[str] = frozenset({"noise", "healthcheck"}),
    ) -> None:
        if window_budget < 3:
            raise ValueError("window_budget must be at least 3")
        self._session = session
        self._model = model
        self._tenant_id = tenant_id
        self._window_budget = window_budget
        self._ignored_sources = ignored_sources
        self._metrics = RouterBatchMetrics()
        self._memory: list[TranscriptEntry] = []

    @property
    def metrics(self) -> RouterBatchMetrics:
        """Snapshot of batching counters and latency for observability."""

        return self._metrics

    def route(self, trigger: Trigger, *, thread: Thread | None = None) -> RouteResult:
        """Route one trigger through the resident conversation."""

        return self.route_batch((trigger,), threads={trigger.id: thread})[0]

    def route_batch(
        self,
        triggers: Sequence[Trigger],
        *,
        threads: dict[UUID, Thread | None] | None = None,
    ) -> tuple[RouteResult, ...]:
        """Route a backlog of triggers with one model call for all candidates.

        Deterministic Fast Path ignores never reach the model. Candidate
        triggers are numbered and merged into a single request; the model must
        answer one ``<n>: decision`` line per trigger. Missing or unparseable
        lines fall back to a TASK decision so a real user request is never
        dropped at the Router.
        """

        if not triggers:
            return ()
        thread_map = threads or {}
        results: list[RouteResult | None] = [None] * len(triggers)
        pending: list[tuple[int, Trigger]] = []
        for index, trigger in enumerate(triggers):
            if self._should_ignore_fast(trigger):
                results[index] = self._ignored_result(
                    trigger, reason="filtered by deterministic policy"
                )
            else:
                pending.append((index, trigger))
        if pending:
            self._route_pending(pending, results, thread_map)
        return tuple(result for result in results if result is not None)

    def transcript_window(self) -> tuple[ChatMessage, ...]:
        """Build the bounded multi-turn window from the durable transcript.

        Inline & Spill: when the transcript exceeds ``window_budget`` entries,
        keep the first entry (global anchor) plus the most recent entries and
        compress the middle into one reference summary carrying recallable
        ``router_messages`` IDs.
        """

        entries = self._load_transcript()
        if len(entries) <= self._window_budget:
            return tuple(self._as_chat_message(entry) for entry in entries)
        head = entries[0]
        tail = entries[-(self._window_budget - 2) :]
        spilled = entries[1 : len(entries) - len(tail)]
        return (
            self._as_chat_message(head),
            ChatMessage(MessageRole.USER, self._spill_summary(spilled)),
            *(self._as_chat_message(entry) for entry in tail),
        )

    def _route_pending(
        self,
        pending: list[tuple[int, Trigger]],
        results: list[RouteResult | None],
        thread_map: dict[UUID, Thread | None],
    ) -> None:
        """Ask the model once for every pending trigger and persist the turn."""

        summaries = [
            self._format_trigger(trigger, thread=thread_map.get(trigger.id))
            for _, trigger in pending
        ]
        batch = len(pending) > 1
        if batch:
            numbered = "\n".join(f"{n}: {text}" for n, text in enumerate(summaries, start=1))
            user_content = (
                f"Route the following {len(pending)} backlogged triggers. Answer with "
                f"exactly {len(pending)} lines '<n>: decision', one per trigger:\n{numbered}"
            )
            system_prompt = ROUTER_BATCH_SYSTEM_PROMPT
        else:
            user_content = summaries[0]
            system_prompt = ROUTER_SYSTEM_PROMPT
        request = ModelRequest(
            messages=(
                ChatMessage(MessageRole.SYSTEM, system_prompt),
                *self.transcript_window(),
                ChatMessage(MessageRole.USER, user_content),
            ),
            request_id=str(pending[0][1].id),
        )
        started = perf_counter()
        response = self._model.complete(request)
        duration = perf_counter() - started
        self._record_metrics(len(pending), duration)
        decisions = self._parse_batch_decisions(response.text, pending, batch=batch)
        for (index, trigger), (action, value) in zip(pending, decisions, strict=True):
            results[index] = self._build_result(trigger, action, value)
        self._append_turn(user_content, self._format_decisions(decisions))

    def _build_result(self, trigger: Trigger, action: RouteAction, value: str) -> RouteResult:
        """Materialize domain outputs for one parsed model decision."""

        if action is RouteAction.IGNORED:
            return self._ignored_result(trigger, reason=value)
        if action is RouteAction.CLARIFY:
            accepted = replace(trigger, filter_decision=TriggerFilterDecision.ACCEPTED)
            delivery = Delivery(
                task_id=UUID(int=0),
                channel=trigger.source,
                idempotency_key=f"clarify:{trigger.dedupe_key}",
                payload_ref=value,
            )
            return RouteResult(action, accepted, delivery=delivery, reason=value)
        accepted = replace(trigger, filter_decision=TriggerFilterDecision.ACCEPTED)
        task = Task(title=value, thread_ids=(trigger.thread_id,), status=TaskStatus.PENDING)
        worker = Worker(task_id=task.id)
        task = replace(task, worker_id=worker.id)
        delivery = Delivery(
            task_id=task.id,
            channel=trigger.source,
            idempotency_key=f"task:{task.id}",
            payload_ref=f"task-created:{task.id}",
        )
        return RouteResult(
            action=RouteAction.CREATED,
            trigger=accepted,
            task=task,
            worker=worker,
            delivery=delivery,
            reason=value,
        )

    @staticmethod
    def _ignored_result(trigger: Trigger, *, reason: str) -> RouteResult:
        """Return the Fast Path ignore result without any model involvement."""

        ignored = replace(trigger, filter_decision=TriggerFilterDecision.IGNORED)
        return RouteResult(action=RouteAction.IGNORED, trigger=ignored, reason=reason)

    def _parse_batch_decisions(
        self,
        text: str,
        pending: list[tuple[int, Trigger]],
        *,
        batch: bool,
    ) -> list[tuple[RouteAction, str]]:
        """Parse one decision per pending trigger with a safe TASK fallback."""

        if not batch:
            decision, value = self._parse_decision(text)
            # Providers sometimes answer the routing prompt as a normal chat
            # turn. Treat that non-contract output as a task so the Worker can
            # answer the user instead of ending the run at the Router.
            if decision is RouteAction.CLARIFY and not text.strip().upper().startswith(
                "CLARIFY:"
            ):
                decision = RouteAction.CREATED
                value = pending[0][1].payload_ref.strip()[:200] or "Untitled task"
            return [(decision, value)]
        parsed: dict[int, tuple[RouteAction, str]] = {}
        for line in text.splitlines():
            match = _BATCH_LINE.match(line)
            if match is None:
                continue
            number = int(match.group(1))
            if number in parsed:
                continue
            parsed[number] = self._parse_decision(match.group(2))
        decisions: list[tuple[RouteAction, str]] = []
        for number, (_, trigger) in enumerate(pending, start=1):
            if number in parsed:
                decisions.append(parsed[number])
            else:
                decisions.append(
                    (
                        RouteAction.CREATED,
                        trigger.payload_ref.strip()[:200] or "Untitled task",
                    )
                )
        return decisions

    @staticmethod
    def _parse_decision(text: str) -> tuple[RouteAction, str]:
        """Parse the single-line IGNORE / CLARIFY: / TASK: contract."""

        normalized = text.strip()
        upper = normalized.upper()
        if upper == "IGNORE":
            return RouteAction.IGNORED, "model selected ignore"
        if upper.startswith("CLARIFY:"):
            return RouteAction.CLARIFY, normalized.split(":", 1)[1].strip()
        if upper.startswith("TASK:"):
            title = normalized.split(":", 1)[1].strip()
            return RouteAction.CREATED, title or "Untitled task"
        return RouteAction.CLARIFY, normalized or "Please clarify the requested work."

    @staticmethod
    def _format_decisions(decisions: Sequence[tuple[RouteAction, str]]) -> str:
        """Serialize the model decisions for the durable assistant turn."""

        if len(decisions) == 1:
            action, value = decisions[0]
            if action is RouteAction.IGNORED:
                return "IGNORE"
            if action is RouteAction.CLARIFY:
                return f"CLARIFY: {value}"
            return f"TASK: {value}"
        lines: list[str] = []
        for number, (action, value) in enumerate(decisions, start=1):
            if action is RouteAction.IGNORED:
                lines.append(f"{number}: IGNORE")
            elif action is RouteAction.CLARIFY:
                lines.append(f"{number}: CLARIFY: {value}")
            else:
                lines.append(f"{number}: TASK: {value}")
        return "\n".join(lines)

    def _should_ignore_fast(self, trigger: Trigger) -> bool:
        """Deterministic pre-model rules shared with the legacy Router."""

        message = trigger.payload_ref.strip()
        return trigger.source in self._ignored_sources or not message

    @staticmethod
    def _format_trigger(trigger: Trigger, *, thread: Thread | None) -> str:
        """Render one trigger summary with recallable object IDs inline."""

        thread_hint = f" thread={thread.id}" if thread is not None else ""
        return (
            f"trigger={trigger.id} source={trigger.source} actor={trigger.actor} "
            f"thread={trigger.thread_id}{thread_hint} payload_ref={trigger.payload_ref}"
        )

    def _load_transcript(self) -> list[TranscriptEntry]:
        """Load this tenant's transcript ordered by sequence (DB or memory)."""

        if self._session is None:
            return list(self._memory)
        rows = (
            self._session.query(RouterMessageRecord)
            .filter(RouterMessageRecord.tenant_id == self._tenant_id)
            .order_by(RouterMessageRecord.sequence)
            .all()
        )
        return [TranscriptEntry(row.id, row.sequence, row.role, row.content) for row in rows]

    def _append_turn(self, user_content: str, assistant_content: str) -> None:
        """Persist the trigger summary and model decision as one durable turn."""

        if self._session is None:
            base = len(self._memory)
            self._memory.append(
                TranscriptEntry(uuid4(), base + 1, MessageRole.USER.value, user_content)
            )
            self._memory.append(
                TranscriptEntry(uuid4(), base + 2, MessageRole.ASSISTANT.value, assistant_content)
            )
            return
        next_sequence = (
            self._session.query(func.max(RouterMessageRecord.sequence))
            .filter(RouterMessageRecord.tenant_id == self._tenant_id)
            .scalar()
            or 0
        )
        self._session.add(
            RouterMessageRecord(
                id=uuid4(),
                tenant_id=self._tenant_id,
                sequence=next_sequence + 1,
                role=MessageRole.USER.value,
                content=user_content,
            )
        )
        self._session.add(
            RouterMessageRecord(
                id=uuid4(),
                tenant_id=self._tenant_id,
                sequence=next_sequence + 2,
                role=MessageRole.ASSISTANT.value,
                content=assistant_content,
            )
        )
        self._session.flush()

    def _record_metrics(self, batch_size: int, duration: float) -> None:
        """Accumulate batching counters and latency after one model call."""

        metrics = self._metrics
        self._metrics = RouterBatchMetrics(
            total_calls=metrics.total_calls + 1,
            total_triggers=metrics.total_triggers + batch_size,
            batched_calls=metrics.batched_calls + (1 if batch_size > 1 else 0),
            last_batch_size=batch_size,
            max_batch_size=max(metrics.max_batch_size, batch_size),
            last_duration_seconds=duration,
            total_duration_seconds=metrics.total_duration_seconds + duration,
        )

    @staticmethod
    def _as_chat_message(entry: TranscriptEntry) -> ChatMessage:
        """Convert a durable transcript row into a provider chat message."""

        role = MessageRole.ASSISTANT if entry.role == "assistant" else MessageRole.USER
        return ChatMessage(role, entry.content)

    @staticmethod
    def _spill_summary(spilled: Sequence[TranscriptEntry]) -> str:
        """Compress spilled history into one bounded reference summary.

        The summary keeps a short preview per entry plus the recallable
        ``router_messages`` IDs so the Router can chain-recall the full content
        later (article §3.3) instead of inlining the whole history.
        """

        lines = [
            f"[spilled router history: {len(spilled)} earlier messages omitted from the "
            "window; recall full rows from router_messages by id]"
        ]
        shown = spilled[:_SPILL_ID_CAP]
        for entry in shown:
            preview = " ".join(entry.content.split())[:_SPILL_PREVIEW_CHARS]
            lines.append(f"- router_message={entry.message_id} {entry.role}: {preview}")
        if len(spilled) > len(shown):
            lines.append(f"- ... {len(spilled) - len(shown)} more spilled messages")
        summary = "\n".join(lines)
        if len(summary) > _SPILL_SUMMARY_CHAR_CAP:
            summary = summary[:_SPILL_SUMMARY_CHAR_CAP] + "\n- [summary truncated]"
        return summary