"""Durable asynchronous tool runs for the full-duplex ReAct loop (article 4.1).

The Worker never blocks on a slow tool: ``task.start`` persists a ToolRunRecord,
returns ``Started`` immediately, and a background thread executes the tool.
When the run reaches a terminal state a ``tool.completed`` Outbox event is
written so the same Worker can resume through the existing subscription
fast-path and receive the result as a new event.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from threading import RLock, Timer
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.domain import Trigger
from super_agent.errors import ValidationError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.tool import ExecutionContext, ToolEffect, ToolResult, ToolSpec
from super_agent.storage.models import ToolRunRecord
from super_agent.storage.outbox import OutboxRepository
from super_agent.tools.registry import ToolRegistry

TOOL_COMPLETED_TOPIC = "tool.completed"
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
_CONTROL_NAMES = frozenset({"task.start", "task.stop", "task.status"})


@dataclass(frozen=True, slots=True)
class ToolOrigin:
    """Trigger fields required to rebuild a continuation event."""

    source: str
    tenant_id: str
    thread_id: UUID
    actor: str

    @classmethod
    def from_trigger(cls, trigger: Trigger) -> ToolOrigin:
        return cls(
            source=trigger.source,
            tenant_id=trigger.tenant_id,
            thread_id=trigger.thread_id,
            actor=trigger.actor,
        )


@dataclass(frozen=True, slots=True)
class ToolRunSnapshot:
    """Read-only view of one durable tool run."""

    id: UUID
    task_id: UUID
    worker_id: UUID
    tool_name: str
    status: str
    result_text: str | None
    error: str | None
    cancel_requested: bool
    timeout_seconds: float | None


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """Result of one background registry.execute call."""

    output: str | None = None
    error: str | None = None
    cancelled: bool = False


class DurableToolRunner:
    """Thread-pool executor persisting tool runs and completion events."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        registry: ToolRegistry,
        *,
        max_workers: int = 4,
        result_preview_chars: int = 2_000,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if result_preview_chars < 1:
            raise ValueError("result_preview_chars must be positive")
        self._factory = factory
        self._registry = registry
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool-run")
        self._lock = RLock()
        self._origins: dict[UUID, ToolOrigin] = {}
        self._futures: dict[UUID, Future[_CallOutcome]] = {}
        self._timers: dict[UUID, Timer] = {}
        self._preview_chars = result_preview_chars

    def start(
        self,
        tool_name: str,
        arguments: dict[str, object],
        context: ExecutionContext,
        *,
        worker_id: UUID,
        origin: ToolOrigin,
        origin_run_id: UUID | None = None,
        timeout_seconds: float | None = None,
    ) -> UUID:
        """Persist a running ToolRunRecord and return before the tool finishes."""

        if context.task_id is None:
            raise ValidationError("task_id is required to start a background tool")
        timeout = timeout_seconds if timeout_seconds is not None else context.timeout_seconds
        if timeout is not None and timeout <= 0:
            raise ValidationError("timeout_seconds must be positive")
        run_id = uuid4()
        with self._factory() as session, session.begin():
            session.add(
                ToolRunRecord(
                    id=run_id,
                    task_id=context.task_id,
                    worker_id=worker_id,
                    origin_run_id=origin_run_id,
                    tool_name=tool_name,
                    arguments_json=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    status="running",
                    timeout_seconds=str(timeout) if timeout is not None else None,
                    started_at=datetime.now(UTC),
                )
            )
        self._origins[run_id] = origin
        future = self._executor.submit(self._call, run_id, tool_name, arguments, context)
        self._futures[run_id] = future
        future.add_done_callback(partial(self._on_done, run_id))
        if timeout is not None:
            timer = Timer(timeout, self._on_timeout, args=(run_id,))
            timer.daemon = True
            self._timers[run_id] = timer
            timer.start()
        return run_id

    def stop(self, run_id: UUID) -> ToolRunSnapshot:
        """Request cooperative cancellation; completion lands as cancelled."""

        with self._lock, self._factory() as session, session.begin():
            record = session.get(ToolRunRecord, run_id)
            if record is None:
                raise ValidationError(f"unknown tool run: {run_id}")
            record.cancel_requested = 1
        return self.status(run_id)

    def status(self, run_id: UUID) -> ToolRunSnapshot:
        """Return the current durable state of one tool run."""

        with self._factory() as session:
            record = session.get(ToolRunRecord, run_id)
            if record is None:
                raise ValidationError(f"unknown tool run: {run_id}")
            return self._snapshot(record)

    def wait(
        self, run_id: UUID, *, timeout: float = 5.0, poll_interval: float = 0.01
    ) -> ToolRunSnapshot:
        """Block until the run reaches a terminal state (test/diagnostic aid)."""

        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.status(run_id)
            if snapshot.status in _TERMINAL_STATUSES:
                return snapshot
            if time.monotonic() >= deadline:
                raise TimeoutError(f"tool run {run_id} did not finish within {timeout}s")
            time.sleep(poll_interval)

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and release timer threads."""

        for timer in self._timers.values():
            timer.cancel()
        self._executor.shutdown(wait=wait)

    def _call(
        self,
        run_id: UUID,
        tool_name: str,
        arguments: dict[str, object],
        context: ExecutionContext,
    ) -> _CallOutcome:
        # Check the cancellation flag before doing any work so a run stopped
        # while queued never touches the tool.
        with self._factory() as session:
            record = session.get(ToolRunRecord, run_id)
            if record is None or record.cancel_requested or record.status != "running":
                return _CallOutcome(cancelled=True)
        try:
            result = self._registry.execute(tool_name, arguments, context)
        except Exception as exc:
            return _CallOutcome(error=str(exc) or exc.__class__.__name__)
        return _CallOutcome(output=result.output)

    def _on_done(self, run_id: UUID, future: Future[_CallOutcome]) -> None:
        timer = self._timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()
        try:
            outcome = future.result()
        except Exception as exc:  # defensive: _call already captures tool errors
            outcome = _CallOutcome(error=str(exc) or exc.__class__.__name__)
        self._finalize(run_id, outcome)

    def _on_timeout(self, run_id: UUID) -> None:
        self._timers.pop(run_id, None)
        self._finalize(run_id, _CallOutcome(), timed_out=True)

    def _finalize(self, run_id: UUID, outcome: _CallOutcome, *, timed_out: bool = False) -> None:
        """Land one terminal state and enqueue the completion event atomically."""

        with self._lock, self._factory() as session, session.begin():
            record = session.get(ToolRunRecord, run_id)
            if record is None or record.status != "running":
                return
            if record.cancel_requested or outcome.cancelled:
                status = "cancelled"
                result_text: str | None = None
                error: str | None = "cancelled by operator"
            elif timed_out:
                status = "timed_out"
                result_text = None
                error = f"tool timed out after {record.timeout_seconds}s"
            elif outcome.error is not None:
                status = "failed"
                result_text = None
                error = outcome.error
            else:
                status = "succeeded"
                result_text = outcome.output
                error = None
            record.status = status
            record.result_text = result_text
            record.error = error
            record.finished_at = datetime.now(UTC)
            origin = self._origins.get(run_id)
            if origin is None:
                raise ValidationError(f"tool run {run_id} lost its trigger origin")
            OutboxRepository(session).enqueue(
                topic=TOOL_COMPLETED_TOPIC,
                dedupe_key=f"{TOOL_COMPLETED_TOPIC}:{run_id}",
                payload={
                    "tool_run_id": str(run_id),
                    "tool_name": record.tool_name,
                    "status": status,
                    "result_summary": (result_text or "")[: self._preview_chars],
                    "error": error,
                    "task_id": str(record.task_id),
                    "worker_id": str(record.worker_id),
                    "origin_run_id": (
                        str(record.origin_run_id) if record.origin_run_id is not None else None
                    ),
                    "trigger": {
                        "schema_version": "1",
                        "source": origin.source,
                        "tenant_id": origin.tenant_id,
                        "thread_id": str(origin.thread_id),
                        "actor": origin.actor,
                    },
                },
            )
        self._origins.pop(run_id, None)
        self._futures.pop(run_id, None)

    @staticmethod
    def _snapshot(record: ToolRunRecord) -> ToolRunSnapshot:
        timeout: float | None = None
        if record.timeout_seconds is not None:
            try:
                timeout = float(record.timeout_seconds)
            except ValueError:
                timeout = None
        return ToolRunSnapshot(
            id=record.id,
            task_id=record.task_id,
            worker_id=record.worker_id,
            tool_name=record.tool_name,
            status=record.status,
            result_text=record.result_text,
            error=record.error,
            cancel_requested=bool(record.cancel_requested),
            timeout_seconds=timeout,
        )


class AsyncToolDispatcher:
    """Model-visible task.start/task.stop/task.status controls for one turn."""

    def __init__(
        self,
        runner: DurableToolRunner,
        *,
        worker_id: UUID,
        task_id: UUID,
        origin_run_id: UUID,
        trigger: Trigger,
        granted_capabilities: frozenset[str] = frozenset(),
        status_preview_chars: int = 400,
    ) -> None:
        self._runner = runner
        self._worker_id = worker_id
        self._task_id = task_id
        self._origin_run_id = origin_run_id
        self._origin = ToolOrigin.from_trigger(trigger)
        self._tenant_id = trigger.tenant_id
        self._granted_capabilities = granted_capabilities
        self._preview_chars = status_preview_chars

    def specs(self) -> tuple[ToolSpec, ...]:
        """Contracts appended to the model tool list for this turn."""

        return (
            ToolSpec(
                name="task.start",
                description=(
                    "Start a registered tool in the background. Returns Started with a "
                    "tool_run_id immediately; the result arrives later as a new event."
                ),
                effect=ToolEffect.READ_ONLY,
                input_schema={
                    "tool_name": "required",
                    "arguments_json": "optional",
                    "timeout_seconds": "optional",
                },
            ),
            ToolSpec(
                name="task.stop",
                description="Request cancellation of one background tool run.",
                effect=ToolEffect.READ_ONLY,
                input_schema={"tool_run_id": "required"},
            ),
            ToolSpec(
                name="task.status",
                description="Return the status and result summary of one background tool run.",
                effect=ToolEffect.READ_ONLY,
                input_schema={"tool_run_id": "required"},
            ),
        )

    def handles(self, name: str) -> bool:
        return name in _CONTROL_NAMES

    def execute(self, name: str, arguments: dict[str, object]) -> ToolResult:
        """Dispatch one control call, returning model-recoverable errors."""

        try:
            if name == "task.start":
                return self._start(arguments)
            if name == "task.stop":
                return self._stop(arguments)
            if name == "task.status":
                return self._status(arguments)
        except ValidationError as exc:
            return ToolResult(tool_name=name, output=f"Error: {exc}")
        raise ValidationError(f"unknown async control: {name}")

    def _start(self, arguments: dict[str, object]) -> ToolResult:
        tool_name = arguments.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValidationError("task.start requires a non-empty tool_name")
        if tool_name in _CONTROL_NAMES:
            raise ValidationError("async control tools cannot be started in the background")
        parsed = self._parse_arguments(arguments.get("arguments_json", "{}"))
        timeout = self._parse_timeout(arguments.get("timeout_seconds"))
        context = ExecutionContext(
            tenant_id=self._tenant_id,
            task_id=self._task_id,
            run_id=uuid4(),
            granted_capabilities=self._granted_capabilities,
        )
        run_id = self._runner.start(
            tool_name,
            parsed,
            context,
            worker_id=self._worker_id,
            origin=self._origin,
            origin_run_id=self._origin_run_id,
            timeout_seconds=timeout,
        )
        return ToolResult(
            tool_name="task.start",
            output=(
                f"Started {tool_name} (tool_run_id={run_id}). The tool runs in the "
                "background; you can reply to the user now. Its result will arrive "
                "as a new event, or poll with task.status."
            ),
            output_ref=f"tool-run:{run_id}",
        )

    def _stop(self, arguments: dict[str, object]) -> ToolResult:
        snapshot = self._owned_snapshot(arguments)
        self._runner.stop(snapshot.id)
        return ToolResult(
            tool_name="task.stop",
            output=f"Stop requested for tool_run_id={snapshot.id} (status={snapshot.status}).",
            output_ref=f"tool-run:{snapshot.id}",
        )

    def _status(self, arguments: dict[str, object]) -> ToolResult:
        snapshot = self._owned_snapshot(arguments)
        lines = [f"tool_run_id={snapshot.id} tool={snapshot.tool_name} status={snapshot.status}"]
        if snapshot.cancel_requested:
            lines.append("cancel_requested=true")
        if snapshot.result_text:
            preview = snapshot.result_text[: self._preview_chars]
            if len(snapshot.result_text) > self._preview_chars:
                preview += " ... (truncated)"
            lines.append(f"result: {preview}")
        if snapshot.error:
            lines.append(f"error: {snapshot.error}")
        return ToolResult(
            tool_name="task.status",
            output="\n".join(lines),
            output_ref=f"tool-run:{snapshot.id}",
        )

    def _owned_snapshot(self, arguments: dict[str, object]) -> ToolRunSnapshot:
        raw = arguments.get("tool_run_id")
        if not isinstance(raw, str):
            raise ValidationError("tool_run_id is required")
        try:
            run_id = UUID(raw)
        except ValueError as exc:
            raise ValidationError(f"invalid tool_run_id: {raw}") from exc
        snapshot = self._runner.status(run_id)
        if snapshot.worker_id != self._worker_id:
            raise ValidationError(f"unknown tool run: {run_id}")
        return snapshot

    @staticmethod
    def _parse_arguments(raw: object) -> dict[str, object]:
        if isinstance(raw, dict):
            return {str(key): value for key, value in raw.items()}
        if not isinstance(raw, str):
            raise ValidationError("arguments_json must be a JSON object string")
        try:
            parsed: Any = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"arguments_json is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("arguments_json must decode to an object")
        return {str(key): value for key, value in parsed.items()}

    @staticmethod
    def _parse_timeout(raw: object) -> float | None:
        if raw is None:
            return None
        if not isinstance(raw, (str, int, float)):
            raise ValidationError(f"invalid timeout_seconds: {raw}")
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise ValidationError(f"invalid timeout_seconds: {raw}") from exc
        if timeout <= 0:
            raise ValidationError("timeout_seconds must be positive")
        return timeout


def tool_completed_trigger(payload: dict[str, object]) -> TriggerInput:
    """Rebuild a synthetic continuation trigger from a tool.completed event."""

    raw_trigger = payload.get("trigger")
    tool_run_id = payload.get("tool_run_id")
    status = payload.get("status")
    if not isinstance(raw_trigger, dict) or not isinstance(tool_run_id, str) or not isinstance(
        status, str
    ):
        raise ValidationError("tool.completed payload is invalid")
    tool_name = str(payload.get("tool_name", "unknown"))
    summary = str(payload.get("result_summary", ""))
    error = payload.get("error")
    message = f"Background tool {tool_name} (tool_run_id={tool_run_id}) finished: status={status}."
    if summary:
        message += f" Result: {summary}"
    if isinstance(error, str) and error:
        message += f" Error: {error}"
    return TriggerInput.model_validate(
        {
            "schema_version": raw_trigger.get("schema_version", "1"),
            "source": raw_trigger.get("source", "unknown"),
            "tenant_id": raw_trigger.get("tenant_id", "default"),
            "thread_id": raw_trigger.get("thread_id"),
            "actor": raw_trigger.get("actor", "unknown"),
            "payload": {
                "message": message,
                "tool_run_id": tool_run_id,
                "tool_name": tool_name,
                "tool_status": status,
                "tool_result": summary,
            },
            "occurred_at": datetime.now(UTC),
            "dedupe_key": f"{TOOL_COMPLETED_TOPIC}:{tool_run_id}",
        }
    )


def trigger_input_from_outbox(payload: dict[str, object]) -> TriggerInput:
    """Rebuild a TriggerInput from a trigger.received Outbox payload."""

    raw_trigger = payload.get("trigger")
    raw_payload = payload.get("payload")
    if not isinstance(raw_trigger, dict) or not isinstance(raw_payload, dict):
        raise ValidationError("trigger.received payload is invalid")
    return TriggerInput.model_validate(
        {
            "schema_version": raw_trigger.get("schema_version", "1"),
            "source": raw_trigger.get("source", "unknown"),
            "tenant_id": raw_trigger.get("tenant_id", "default"),
            "thread_id": raw_trigger.get("thread_id"),
            "actor": raw_trigger.get("actor", "unknown"),
            "payload": raw_payload,
            "occurred_at": raw_trigger.get("occurred_at"),
            "dedupe_key": raw_trigger.get("dedupe_key"),
        }
    )