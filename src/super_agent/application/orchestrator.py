"""End-to-end orchestration for one Super-agent task turn."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from super_agent.application.async_tools import (
    AsyncToolDispatcher,
    DurableToolRunner,
    tool_completed_trigger,
    trigger_input_from_outbox,
)
from super_agent.application.outbox import OutboxProcessor
from super_agent.application.router import RouteAction, RouteResult, RouterService
from super_agent.application.run_leases import RunLeaseError, RunLeaseService
from super_agent.application.state import RouterStateStore
from super_agent.application.subscriptions import SubscriptionRouter
from super_agent.application.worker import WorkerRuntime
from super_agent.context.service import InlineSpillContextService
from super_agent.context.worker import ContextEntry, WorkerContext
from super_agent.delivery.service import DeliveryService, channel_for_source
from super_agent.domain import (
    Delivery,
    Task,
    TaskStatus,
    Trigger,
    TriggerFilterDecision,
    Worker,
    WorkerStatus,
)
from super_agent.errors import ValidationError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.policies.events import EventFilter, FilterDecision, NormalizedEvent
from super_agent.ports.channel import DeliveryRequest
from super_agent.ports.context import ContextBuildRequest, ContextCandidate
from super_agent.ports.model import ChatMessage, MessageRole, ModelGateway, ModelRequest, ModelTool
from super_agent.ports.tool import ExecutionContext, Tool, ToolResult
from super_agent.ports.tool_names import normalize_tool_name, resolve_tool_name
from super_agent.skills.loader import Skill, SkillRegistry, select_skills
from super_agent.storage.models import (
    DeliveryRecord,
    OutboxMessage,
    RunRecord,
    SubscriptionRecord,
    TaskRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerEventRecord,
    WorkerRecord,
)
from super_agent.tools.registry import ToolRegistry
from super_agent.tools.workspace import (
    DEFAULT_CACHE_DIR_NAME,
    find_task_workspace,
    task_workspace,
)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Public outcome of one orchestration turn.

    For subscription fan-out the top-level fields describe the primary
    subscriber (the first active subscription); every subscriber's outcome,
    including the primary one, is also listed in ``fanout_results``.
    """

    trigger_id: UUID
    action: RouteAction
    task_id: UUID | None
    worker_id: UUID | None
    status: str
    text: str
    run_id: UUID | None = None
    fanout_results: tuple[AgentRunResult, ...] = ()


# Tool names that produce a durable file. Kept in registry form; the model sees
# the normalized ``file_write`` spelling and the mapping happens elsewhere.
_WRITE_TOOLS = frozenset({"file.write", "file.write_many"})
# The name the model sees (registry names use dots, model APIs use underscores).
_MODEL_WRITE_TOOL = "file_write"

# Signals that the user expects an actual artifact on disk rather than prose.
_ARTIFACT_HINTS = (
    "html",
    "网页",
    "页面",
    "网站",
    "文件",
    "保存",
    "生成",
    "写一个",
    "写个",
    "做一个",
    "做个",
    "脚本",
    "代码文件",
    "markdown",
    "csv",
    "报告",
    "简历",
    "create a file",
    "write a file",
    "save",
    "html page",
    "webpage",
    "script",
    "generate a",
)


# A model that gave up used to narrate its own limits into the conversation
# ("I don't have the ability to write files, please save this yourself"). That
# text is durable context, so continuing the thread fed the next turn a reason
# to refuse again. These phrases mark such a turn as noise rather than history.
_SELF_DEFEATING_PHRASES = (
    "我没有写文件的能力",
    "没有写文件工具",
    "当前会话里我做不到",
    "无法直接写文件",
    "我无法创建文件",
    "请手动保存",
    "请把代码块内容保存",
    "cannot write files",
    "don't have the ability to write",
    "do not have the ability to write",
    "no file writing capability",
    "unable to create files",
)


def _is_self_defeating_answer(role: str, content: str) -> bool:
    """True when an assistant turn is a refusal rather than real progress.

    Dropping these from the replayed history keeps a continued thread from
    inheriting the previous turn's excuse. Only assistant turns qualify: a
    user's own message is never filtered, and a tool result is evidence.
    """

    if role != "assistant":
        return False
    lowered = str(content or "").lower()
    return any(phrase in lowered for phrase in _SELF_DEFEATING_PHRASES)


def _run_input_ref(trigger_id: UUID, payload: dict[str, object] | None) -> str:
    """Persist the task's input metadata without changing the schema.

    The trigger row only keeps the raw message text, so a skill selection used
    to vanish the moment the request returned: the UI could not confirm what
    was applied and a resumed Run had no record of it. ``RunRecord.input_ref``
    is permanent and already stores a trigger reference, so keep that prefix
    and append the selection as a compact JSON suffix.
    """

    raw = (payload or {}).get("skills")
    names: list[str] = []
    if isinstance(raw, str):
        names = [raw.strip()] if raw.strip() else []
    elif isinstance(raw, (list, tuple, set, frozenset)):
        names = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    if not names:
        return f"trigger:{trigger_id}"
    return (
        f"trigger:{trigger_id}|"
        + json.dumps({"skills": names}, ensure_ascii=False, separators=(",", ":"))
    )


def _looks_like_file_deliverable(text: str) -> bool:
    """Heuristically detect a request that must end with a file on disk."""

    lowered = text.lower()
    return any(hint in lowered for hint in _ARTIFACT_HINTS)


def _workspace_defaults() -> tuple[Path | None, bool, str]:
    """Read the per-task workspace policy from the environment.

    The root is deliberately ``None`` when nothing is configured: falling back to
    ``cwd`` here made a test that passes its own ``tmp_path`` registry write into
    the repository instead. Callers resolve the root from the tools they were
    actually given.
    """

    raw_root = os.getenv("SUPER_AGENT_WORKSPACE_ROOT")
    enabled = os.getenv("SUPER_AGENT_TASK_WORKSPACES", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    cache = os.getenv("SUPER_AGENT_CACHE_DIR_NAME") or DEFAULT_CACHE_DIR_NAME
    return (Path(raw_root) if raw_root else None), enabled, cache


class AgentOrchestrator:
    """Connect ingress, Router, Worker, context, model and safe tools."""

    def __init__(
        self,
        session: Session,
        model: ModelGateway,
        *,
        tools: ToolRegistry | None = None,
        context_service: InlineSpillContextService | None = None,
        granted_capabilities: frozenset[str] = frozenset(),
        delivery: DeliveryService | None = None,
        max_rounds: int = 4,
        # A Worker can spend longer than this inside ONE blocking model call
        # (generating a large HTML page takes minutes). If the lease is shorter
        # than that call, the recovery thread reaps the run mid-generation and
        # the task dies with "run lease expired" even though it was working.
        lease_seconds: float = 300.0,
        detach_execution: bool = False,
        tool_runner: DurableToolRunner | None = None,
        recall_tool: Tool | None = None,
        event_filter: EventFilter | None = None,
        router_window_budget: int = 40,
        # Generated artifacts belong in one folder per task instead of the
        # repository root. ``task_workspaces`` is the switch; when it is off the
        # tools keep their legacy "write anywhere in workspace_root" behaviour.
        task_workspaces: bool = True,
        workspace_root: Path | None = None,
        cache_dir_name: str = DEFAULT_CACHE_DIR_NAME,
        # Skills the user can choose per task. Empty means no skill library is
        # wired in, which must behave exactly like the pre-skill runtime.
        skills: SkillRegistry | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._session = session
        self._model = model
        self._tools = tools or ToolRegistry()
        if recall_tool is not None:
            # The same registry is shared across orchestrators (API, CLI,
            # background worker), so registering twice must be a no-op rather
            # than a hard failure that turns every submission into a 500.
            existing = {spec.name for spec in self._tools.specs()}
            if recall_tool.describe().name not in existing:
                self._tools.register(recall_tool)
        self._context = context_service or InlineSpillContextService()
        self._granted_capabilities = granted_capabilities
        self._delivery = delivery
        self._max_rounds = max_rounds
        self._lease_seconds = lease_seconds
        # Background tool threads write on their own connections, so the turn
        # must commit its durable state before execution (single-writer SQLite).
        self._detach_execution = detach_execution or tool_runner is not None
        self._tool_runner = tool_runner
        self._subscriptions = SubscriptionRouter()
        # Fast Path filter (article §3.1). The default policy is permissive:
        # only empty/oversized text is dropped deterministically, everything
        # else becomes a CANDIDATE for the Router's semantic layer.
        self._event_filter = event_filter or EventFilter()
        self._router_window_budget = router_window_budget
        # Explicit arguments win; otherwise fall back to the environment so every
        # construction site (API, CLI, background runner, outbox worker) shares
        # one policy without threading three more parameters through each.
        default_root, default_enabled, default_cache = _workspace_defaults()
        self._task_workspaces = task_workspaces and default_enabled
        self._workspace_root = (
            Path(workspace_root) if workspace_root is not None else default_root
        )
        self._cache_dir_name = cache_dir_name or default_cache
        self._skills = skills

    def submit(
        self, trigger_input: TriggerInput, *, enqueue_outbox: bool = True
    ) -> AgentRunResult:
        """Ingest and process one external event in the same transaction."""

        trigger = TriggerIngressService(self._session).ingest(
            trigger_input, enqueue_outbox=enqueue_outbox
        )
        self._session.flush()
        previous_run = (
            self._session.query(RunRecord)
            .filter(RunRecord.trigger_id == trigger.id)
            .order_by(RunRecord.attempt.desc())
            .first()
        )
        if previous_run is not None and previous_run.status not in {
            "failed",
            "timed_out",
            "cancelled",
        }:
            previous_task = self._session.get(TaskRecord, previous_run.task_id)
            return AgentRunResult(
                trigger.id,
                RouteAction.CREATED,
                previous_run.task_id,
                previous_task.worker_id if previous_task is not None else None,
                previous_run.status,
                "Duplicate trigger ignored; the existing task was preserved.",
                previous_run.id,
            )
        return self.process(trigger, payload=trigger_input.payload)

    def submit_via_outbox(self, trigger_input: TriggerInput) -> AgentRunResult:
        """Ingest, claim, and process one trigger through the durable Outbox path."""

        TriggerIngressService(self._session).ingest(trigger_input)
        self._session.flush()
        result: list[AgentRunResult] = []

        def handle(_message: object, payload: dict[str, object]) -> None:
            result.append(self._process_outbox_payload(payload))

        dedupe_key = f"trigger.received:{trigger_input.dedupe_key}"
        # Ask who owns the event *before* claiming. `process_dedupe` returns None
        # both when it processed the message and when another consumer holds
        # the claim, so its return value alone cannot tell the two apart - and
        # treating "not mine" as "run it directly" is exactly what executed a
        # request twice (two overlapping Runs, artifacts written twice).
        existing = self._session.scalar(
            select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key)
        )
        owned_elsewhere = (
            existing is not None
            and existing.status in {"CLAIMED", "PUBLISHED"}
            and existing.claimed_at is not None
        )
        message = OutboxProcessor(self._session, handle).process_dedupe(dedupe_key)
        if result:
            return result[0]
        if message is not None and message.status == "PENDING":
            raise RuntimeError(message.last_error or "outbox handler failed")
        if message is not None or owned_elsewhere:
            # Another consumer owns this event right now (the durable Outbox
            # worker, or a retry loop). Falling back to submit() ran the same
            # Trigger a second time: the runtime ended up with two overlapping
            # Runs for one request, so a task was executed - and its artifacts
            # written - twice. Report "already being handled" instead; the
            # owner of the claim will record the outcome.
            return AgentRunResult(
                trigger_input.thread_id,
                RouteAction.IGNORED,
                None,
                None,
                "duplicate",
                "This event is already being processed by another worker.",
                None,
            )
        return self.submit(trigger_input)

    def _process_outbox_payload(self, payload: dict[str, object]) -> AgentRunResult:
        raw_trigger = payload.get("trigger")
        raw_payload = payload.get("payload")
        if not isinstance(raw_trigger, dict) or not isinstance(raw_payload, dict):
            raise ValueError("trigger.received payload is invalid")
        trigger_input = TriggerInput.model_validate(
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
        return self.submit(trigger_input)

    @staticmethod
    def from_outbox_payload(
        session: Session,
        model: ModelGateway,
        payload: dict[str, object],
        *,
        tools: ToolRegistry | None = None,
        granted_capabilities: frozenset[str] = frozenset(),
        detach_execution: bool = False,
        topic: str = "trigger.received",
        tool_runner: DurableToolRunner | None = None,
        recall_tool: Tool | None = None,
        delivery: DeliveryService | None = None,
        max_rounds: int = 12,
        skills: SkillRegistry | None = None,
    ) -> AgentRunResult:
        """Re-enter the same runtime from a claimed trigger.received event.

        A tool.completed event rebuilds a synthetic trigger on the original
        thread so the subscribed Worker resumes with the tool result injected.
        """

        if topic == "tool.completed":
            trigger_input = tool_completed_trigger(payload)
        else:
            trigger_input = trigger_input_from_outbox(payload)
        return AgentOrchestrator(
            session,
            model,
            tools=tools,
            granted_capabilities=granted_capabilities,
            detach_execution=detach_execution,
            tool_runner=tool_runner,
            recall_tool=recall_tool,
            delivery=delivery,
            max_rounds=max_rounds,
            skills=skills,
        ).submit(trigger_input, enqueue_outbox=topic != "tool.completed")

    def process(
        self, trigger: Trigger, *, payload: dict[str, object] | None = None
    ) -> AgentRunResult:
        """Filter, then route or fan out a trigger to subscribed Workers.

        Two-level filtering (article §3.1): the deterministic EventFilter runs
        first; IGNORE never reaches the model, ACCEPT (explicit request) skips
        the semantic layer, and CANDIDATE goes to the Router LLM. Subscribed
        threads fan out to every active, unexpired subscriber (article §3.2).
        """

        route_trigger = trigger
        if payload is not None and isinstance(payload.get("message"), str):
            route_trigger = replace(trigger, payload_ref=str(payload["message"]))
        filter_decision = self._event_filter.decide(
            NormalizedEvent(
                source=route_trigger.source,
                scope=str(route_trigger.thread_id),
                actor=route_trigger.actor,
                text=route_trigger.payload_ref,
                explicit_request=bool(payload is not None and payload.get("explicit") is True),
            )
        )
        if filter_decision is FilterDecision.IGNORE:
            self._write_filter_decision(trigger.id, TriggerFilterDecision.IGNORED)
            return AgentRunResult(
                trigger.id,
                RouteAction.IGNORED,
                None,
                None,
                "completed",
                f"filtered by program policy ({self._event_filter.version})",
            )

        subscriptions = self._active_subscriptions(trigger.thread_id)
        if subscriptions:
            return self._fan_out_subscribed(trigger, payload, subscriptions, filter_decision)
        return self._route_unsubscribed(trigger, payload, filter_decision)

    def process_batch(
        self,
        items: Sequence[tuple[Trigger, dict[str, object] | None]],
    ) -> tuple[AgentRunResult, ...]:
        """Batch backlogged cold-start triggers into one Router call (§2.1).

        Fast Path filtering and subscription fan-out stay per-event; only the
        semantic Router decisions for unsubscribed CANDIDATE events are merged
        into a single model call, then each created task executes in order.
        """

        results: list[AgentRunResult | None] = [None] * len(items)
        pending: list[tuple[int, Trigger, dict[str, object] | None]] = []
        for index, (trigger, payload) in enumerate(items):
            route_trigger = trigger
            if payload is not None and isinstance(payload.get("message"), str):
                route_trigger = replace(trigger, payload_ref=str(payload["message"]))
            decision = self._event_filter.decide(
                NormalizedEvent(
                    source=route_trigger.source,
                    scope=str(route_trigger.thread_id),
                    actor=route_trigger.actor,
                    text=route_trigger.payload_ref,
                    explicit_request=bool(payload is not None and payload.get("explicit") is True),
                )
            )
            if decision is FilterDecision.IGNORE:
                self._write_filter_decision(trigger.id, TriggerFilterDecision.IGNORED)
                results[index] = AgentRunResult(
                    trigger.id,
                    RouteAction.IGNORED,
                    None,
                    None,
                    "completed",
                    f"filtered by program policy ({self._event_filter.version})",
                )
                continue
            subscriptions = self._active_subscriptions(trigger.thread_id)
            if subscriptions:
                results[index] = self._fan_out_subscribed(
                    trigger, payload, subscriptions, decision
                )
                continue
            if decision is FilterDecision.ACCEPT:
                results[index] = self._execute_routed(
                    self._accept_explicit_request(route_trigger), trigger, payload
                )
                continue
            pending.append((index, route_trigger, payload))
        if pending:
            resident = RouterService(
                self._model,
                session=self._session,
                tenant_id=pending[0][1].tenant_id,
                window_budget=self._router_window_budget,
            )
            routes = resident.route_batch([trigger for _, trigger, _ in pending])
            for (index, trigger, payload), route in zip(pending, routes, strict=True):
                results[index] = self._execute_routed(route, trigger, payload)
        return tuple(result for result in results if result is not None)

    def _route_unsubscribed(
        self,
        trigger: Trigger,
        payload: dict[str, object] | None,
        filter_decision: FilterDecision,
    ) -> AgentRunResult:
        """Cold-start path: Router decides, then one Worker executes the task.

        CANDIDATE events reach the resident Router session (semantic layer);
        ACCEPT events are explicit human requests and create a task directly
        without another model call.
        """

        route_trigger = trigger
        if payload is not None and isinstance(payload.get("message"), str):
            route_trigger = replace(trigger, payload_ref=str(payload["message"]))
        if filter_decision is FilterDecision.ACCEPT:
            route = self._accept_explicit_request(route_trigger)
        else:
            route = RouterService(
                self._model,
                session=self._session,
                tenant_id=trigger.tenant_id,
                window_budget=self._router_window_budget,
            ).route(route_trigger)
        return self._execute_routed(route, trigger, payload)

    def _execute_routed(
        self,
        route: RouteResult,
        trigger: Trigger,
        payload: dict[str, object] | None,
    ) -> AgentRunResult:
        """Persist one Router decision and run its Worker to a terminal state."""

        RouterStateStore(self._session).persist(route)
        self._session.flush()
        self._write_filter_decision(trigger.id, route.trigger.filter_decision)
        if route.action is not RouteAction.CREATED or route.task is None or route.worker is None:
            return AgentRunResult(trigger.id, route.action, None, None, "completed", route.reason)

        task_record = self._session.get(TaskRecord, route.task.id)
        worker_record = self._session.get(WorkerRecord, route.worker.id)
        if task_record is None or worker_record is None:
            raise RuntimeError("Router result was not persisted")
        task_record.status = TaskStatus.RUNNING.value
        task_record.version += 1
        worker = Worker(
            id=route.worker.id,
            task_id=route.worker.task_id,
            status=route.worker.status,
            capabilities=route.worker.capabilities,
            version=route.worker.version,
        )
        runtime = WorkerRuntime(worker)
        self._subscriptions.register_worker(runtime)
        self._subscriptions.subscribe(trigger.thread_id, runtime.worker.id)
        self._session.add(
            SubscriptionRecord(
                id=uuid4(),
                seq=self._next_subscription_seq(),
                thread_id=trigger.thread_id,
                worker_id=runtime.worker.id,
                status="active",
                version=0,
            )
        )
        runtime.start()
        run_id = uuid4()
        lease_owner = f"worker:{runtime.worker.id}:{run_id}"
        self._session.add(
            RunRecord(
                id=run_id,
                task_id=route.task.id,
                trigger_id=trigger.id,
                status="pending",
                attempt=self._next_attempt(route.task.id, trigger.id),
                input_ref=_run_input_ref(trigger.id, payload),
            )
        )
        self._session.flush()
        RunLeaseService(self._session).claim(
            run_id, lease_owner, lease_seconds=self._lease_seconds
        )
        if self._detach_execution:
            self._session.commit()
        try:
            text = self._execute_worker(
                runtime,
                trigger,
                payload or {"message": trigger.payload_ref},
                run_id=run_id,
                lease_owner=lease_owner,
            )
        except Exception as exc:
            runtime.fail(str(exc))
            worker_record.status = runtime.worker.status.value
            worker_record.version = runtime.worker.version
            task_record.status = "failed"
            delivery = (
                self._session.query(DeliveryRecord)
                .filter(DeliveryRecord.task_id == route.task.id)
                .first()
            )
            if delivery is not None:
                delivery.status = "failed"
                delivery.payload_ref = str(exc)
                delivery.version += 1
            run = self._session.get(RunRecord, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)
                run.finished_at = datetime.now(UTC)
                run.lease_owner = None
                run.lease_expires_at = None
                run.version += 1
            self._persist_worker_events(runtime)
            self._session.flush()
            raise
        self._persist_worker_events(runtime)
        worker_record.status = runtime.worker.status.value
        worker_record.version = runtime.worker.version
        worker_succeeded = runtime.worker.status is WorkerStatus.SUCCEEDED
        worker_stopped = runtime.worker.status is WorkerStatus.STOPPED
        task_status = (
            TaskStatus.CANCELLED.value
            if worker_stopped
            else TaskStatus.SUCCEEDED.value
            if worker_succeeded
            else TaskStatus.FAILED.value
        )
        task_record.status = task_status
        task_record.version += 1
        delivery = (
            self._session.query(DeliveryRecord)
            .filter(DeliveryRecord.task_id == route.task.id)
            .first()
        )
        channel = channel_for_source(trigger.source)
        if delivery is not None:
            delivery.status = (
                "cancelled" if worker_stopped else "sent" if worker_succeeded else "failed"
            )
            delivery.payload_ref = text
            delivery.channel = channel.value
            delivery.version += 1
        if self._delivery is not None and worker_succeeded:
            self._delivery.send(
                DeliveryRequest(
                    channel,
                    f"task:{route.task.id}",
                    text,
                    {"task_id": str(route.task.id), "run_id": str(run_id)},
                )
            )
        run = self._session.get(RunRecord, run_id)
        if run is not None:
            if run.status not in {"cancelled", "timed_out"}:
                run.status = (
                    "cancelled"
                    if runtime.worker.status is WorkerStatus.STOPPED
                    else "succeeded"
                    if worker_succeeded
                    else "failed"
                )
            run.output_ref = f"model:{trigger.id}"
            if not worker_succeeded and run.status != "timed_out":
                run.error = text
            run.finished_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_expires_at = None
            # Persist telemetry so the result can explain its own cost and
            # speed without reading logs: rounds, tokens, cache hits, tools.
            metrics = runtime.metrics
            run.model_rounds = metrics.model_rounds
            run.prompt_tokens = metrics.prompt_tokens
            run.completion_tokens = metrics.completion_tokens
            run.cached_tokens = metrics.cached_tokens
            run.tool_calls_count = metrics.tool_calls
            run.version += 1
        self._session.flush()
        return AgentRunResult(
            trigger.id,
            route.action,
            route.task.id,
            runtime.worker.id,
            task_status,
            text,
            run_id,
        )

    def _write_filter_decision(self, trigger_id: UUID, decision: TriggerFilterDecision) -> None:
        """Persist the two-level filter outcome back onto the TriggerRecord."""

        record = self._session.get(TriggerRecord, trigger_id)
        if record is not None:
            record.filter_decision = decision.value
            self._session.flush()

    @staticmethod
    def _accept_explicit_request(trigger: Trigger) -> RouteResult:
        """Build a CREATED route for an explicit request without a model call.

        An explicit human request (program-layer ACCEPT) must not be dropped by
        the semantic layer, so it deterministically creates a Task and Worker.
        """

        title = trigger.payload_ref.strip()[:200] or "Untitled task"
        accepted = replace(trigger, filter_decision=TriggerFilterDecision.ACCEPTED)
        task = Task(title=title, thread_ids=(trigger.thread_id,), status=TaskStatus.PENDING)
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
            reason=title,
        )

    def _next_subscription_seq(self) -> int:
        """Monotonic insertion order so fan-out keeps a stable primary."""

        current = self._session.query(func.max(SubscriptionRecord.seq)).scalar()
        return (current or 0) + 1

    def _active_subscriptions(self, thread_id: UUID) -> list[SubscriptionRecord]:
        """Return every active, unexpired subscription for one Thread.

        Subscriptions are many-to-many (article §3.2): one Thread can feed
        several Workers, so all matching rows are returned in version order.
        """

        now = datetime.now(UTC)
        rows = (
            self._session.query(SubscriptionRecord)
            .filter(
                SubscriptionRecord.thread_id == thread_id,
                SubscriptionRecord.status == "active",
            )
            # Insertion order keeps the first subscriber as the primary result.
            .order_by(SubscriptionRecord.version.desc(), SubscriptionRecord.seq)
            .all()
        )
        return [
            row
            for row in rows
            if row.expires_at is None or self._as_utc(row.expires_at) > now
        ]

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize a persisted timestamp for comparison (SQLite drops tz)."""

        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _fan_out_subscribed(
        self,
        trigger: Trigger,
        payload: dict[str, object] | None,
        subscriptions: Sequence[SubscriptionRecord],
        filter_decision: FilterDecision,
    ) -> AgentRunResult:
        """Deliver one event to every subscribed Worker (article §3.2).

        Each subscriber gets its own Run and WorkerEvent timeline. A broken or
        failed subscriber never blocks the others; its failure is recorded as
        that subscriber's result. The primary result is the first subscriber.
        """

        results: list[AgentRunResult] = []
        for subscription in subscriptions:
            try:
                result = self._process_subscribed(trigger, payload, subscription)
            except Exception as exc:
                worker_record = self._session.get(WorkerRecord, subscription.worker_id)
                result = AgentRunResult(
                    trigger.id,
                    RouteAction.CREATED,
                    worker_record.task_id if worker_record is not None else None,
                    subscription.worker_id,
                    "failed",
                    str(exc),
                )
            if result is not None:
                results.append(result)
        if results:
            primary = results[0]
            return replace(primary, fanout_results=tuple(results))
        # Every subscriber was stale; fall back to the Router slow path once.
        # The event already passed the Fast Path filter, so route directly.
        return self._route_unsubscribed(trigger, payload, filter_decision)

    def _process_subscribed(
        self,
        trigger: Trigger,
        payload: dict[str, object] | None,
        subscription: SubscriptionRecord,
    ) -> AgentRunResult | None:
        """Resume one persisted Worker for a follow-up event on the Thread.

        Returns ``None`` when the subscription's Worker or Task record is gone;
        the stale subscription is expired and the fan-out loop continues with
        the remaining subscribers.
        """

        worker_record = self._session.get(WorkerRecord, subscription.worker_id)
        if worker_record is None:
            subscription.status = "expired"
            subscription.version += 1
            self._session.flush()
            return None
        task_record = self._session.get(TaskRecord, worker_record.task_id)
        if task_record is None:
            subscription.status = "expired"
            subscription.version += 1
            self._session.flush()
            return None

        try:
            capabilities = tuple(json.loads(worker_record.capabilities))
        except (TypeError, json.JSONDecodeError):
            capabilities = ()
        worker = Worker(
            id=worker_record.id,
            task_id=worker_record.task_id,
            # A follow-up is a new lifecycle attempt on the same durable Worker.
            # Keep the identity and context while restarting from STARTING so
            # terminal state transitions remain terminal in the domain model.
            status=WorkerStatus.STARTING,
            capabilities=capabilities,
            version=worker_record.version,
        )
        entries: list[ContextEntry] = []
        rows = (
            self._session.query(WorkerContextRecord)
            .filter(WorkerContextRecord.worker_id == worker.id)
            .order_by(WorkerContextRecord.revision)
            .all()
        )
        for row in rows:
            try:
                metadata = tuple(json.loads(row.metadata_json).items())
            except (TypeError, json.JSONDecodeError):
                metadata = ()
            entries.append(
                ContextEntry(
                    entry_id=row.id,
                    role=row.role,
                    content=row.content,
                    metadata=tuple((str(key), str(value)) for key, value in metadata),
                    created_at=row.created_at,
                )
            )
        runtime = WorkerRuntime(
            worker,
            context=WorkerContext(
                worker_id=worker.id,
                task_id=worker.task_id,
                entries=tuple(entries),
                revision=len(entries),
            ),
        )
        task_record.status = TaskStatus.RUNNING.value
        task_record.version += 1
        runtime.start()
        run_id = uuid4()
        lease_owner = f"worker:{worker.id}:{run_id}"
        self._session.add(
            RunRecord(
                id=run_id,
                task_id=task_record.id,
                trigger_id=trigger.id,
                status="pending",
                attempt=self._next_attempt(task_record.id, trigger.id),
                input_ref=_run_input_ref(trigger.id, payload),
            )
        )
        self._session.flush()
        RunLeaseService(self._session).claim(
            run_id, lease_owner, lease_seconds=self._lease_seconds
        )
        if self._detach_execution:
            self._session.commit()
        try:
            text = self._execute_worker(
                runtime,
                trigger,
                payload or {"message": trigger.payload_ref},
                run_id=run_id,
                lease_owner=lease_owner,
            )
        except Exception as exc:
            runtime.fail(str(exc))
            worker_record.status = runtime.worker.status.value
            worker_record.version = runtime.worker.version
            task_record.status = TaskStatus.FAILED.value
            run = self._session.get(RunRecord, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)
                run.finished_at = datetime.now(UTC)
                run.lease_owner = None
                run.lease_expires_at = None
                run.version += 1
            self._persist_worker_events(runtime)
            self._session.flush()
            raise

        self._persist_worker_events(runtime)
        worker_record.status = runtime.worker.status.value
        worker_record.version = runtime.worker.version
        worker_succeeded = runtime.worker.status is WorkerStatus.SUCCEEDED
        worker_stopped = runtime.worker.status is WorkerStatus.STOPPED
        task_status = (
            TaskStatus.CANCELLED.value
            if worker_stopped
            else TaskStatus.SUCCEEDED.value
            if worker_succeeded
            else TaskStatus.FAILED.value
        )
        task_record.status = task_status
        task_record.version += 1
        delivery = (
            self._session.query(DeliveryRecord)
            .filter(DeliveryRecord.task_id == task_record.id)
            .first()
        )
        channel = channel_for_source(trigger.source)
        if delivery is not None:
            delivery.status = (
                "cancelled" if worker_stopped else "sent" if worker_succeeded else "failed"
            )
            delivery.payload_ref = text
            delivery.channel = channel.value
            delivery.version += 1
        if self._delivery is not None and worker_succeeded:
            self._delivery.send(
                DeliveryRequest(
                    channel,
                    f"task:{task_record.id}:run:{run_id}",
                    text,
                    {"task_id": str(task_record.id), "run_id": str(run_id)},
                )
            )
        run = self._session.get(RunRecord, run_id)
        if run is not None:
            if run.status not in {"cancelled", "timed_out"}:
                run.status = (
                    "cancelled"
                    if worker_stopped
                    else "succeeded"
                    if worker_succeeded
                    else "failed"
                )
            run.output_ref = f"model:{trigger.id}"
            if not worker_succeeded and run.status != "timed_out":
                run.error = text
            run.finished_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_expires_at = None
            # Persist telemetry so the result can explain its own cost and
            # speed without reading logs: rounds, tokens, cache hits, tools.
            metrics = runtime.metrics
            run.model_rounds = metrics.model_rounds
            run.prompt_tokens = metrics.prompt_tokens
            run.completion_tokens = metrics.completion_tokens
            run.cached_tokens = metrics.cached_tokens
            run.tool_calls_count = metrics.tool_calls
            run.version += 1
        self._session.flush()
        return AgentRunResult(
            trigger.id,
            RouteAction.CREATED,
            task_record.id,
            worker.id,
            task_status,
            text,
            run_id,
        )

    def _persist_worker_events(self, runtime: WorkerRuntime) -> None:
        """Persist the append-only lifecycle timeline emitted by the Worker."""

        sequence_offset = (
            self._session.query(func.max(WorkerEventRecord.sequence))
            .filter(WorkerEventRecord.worker_id == runtime.worker.id)
            .scalar()
            or 0
        )
        for event in runtime.events:
            if self._session.get(WorkerEventRecord, event.event_id) is not None:
                continue
            self._session.add(
                WorkerEventRecord(
                    id=event.event_id,
                    worker_id=event.worker_id,
                    task_id=event.task_id,
                    sequence=sequence_offset + event.sequence,
                    event_type=event.event_type.value,
                    status=event.status.value,
                    summary=event.summary,
                    context_revision=event.context_revision,
                    created_at=event.created_at,
                )
            )
        for revision, entry in enumerate(runtime.context.entries, start=1):
            if self._session.get(WorkerContextRecord, entry.entry_id) is not None:
                continue
            self._session.add(
                WorkerContextRecord(
                    id=entry.entry_id,
                    worker_id=runtime.worker.id,
                    task_id=runtime.worker.task_id,
                    role=entry.role,
                    content=entry.content,
                    metadata_json=json.dumps(dict(entry.metadata), ensure_ascii=False),
                    revision=revision,
                    created_at=entry.created_at,
                )
            )

    def _execute_worker(
        self,
        runtime: WorkerRuntime,
        trigger: Trigger,
        payload: dict[str, object],
        *,
        run_id: UUID,
        lease_owner: str,
    ) -> str:
        runtime.append_context(
            ContextEntry(
                role="trigger",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                metadata=(("trigger_id", str(trigger.id)),),
            )
        )
        dispatcher: AsyncToolDispatcher | None = None
        tool_specs = list(self._tools.specs())
        if self._tool_runner is not None:
            dispatcher = AsyncToolDispatcher(
                self._tool_runner,
                worker_id=runtime.worker.id,
                task_id=runtime.worker.task_id,
                origin_run_id=run_id,
                trigger=trigger,
                granted_capabilities=self._granted_capabilities,
            )
            tool_specs.extend(dispatcher.specs())
        model_tool_names: dict[str, str] = {}
        for spec in tool_specs:
            model_name = self._model_tool_name(spec.name)
            # Keep the first (built-in) registration on collisions, and accept
            # both normalized and original names echoed back by the model.
            # The bare action word is also registered so "write"/"read"/"list"
            # resolve, unless a real tool already claims it.
            bare = model_name.rsplit("_", 1)[-1].lower()
            aliases = (model_name, model_name.lower(), spec.name, spec.name.lower(), bare)
            for alias in aliases:
                model_tool_names.setdefault(alias, spec.name)
        model_tools = tuple(
            ModelTool(
                self._model_tool_name(spec.name),
                spec.description,
                self._tool_parameters(spec.input_schema),
            )
            for spec in tool_specs
        )
        has_write_tool = any(spec.name in _WRITE_TOOLS for spec in tool_specs)
        # Resolve (and create) this Task's own artifact folder. Relative tool
        # paths resolve inside it, so a task can no longer litter the repo root.
        task_dir = self._task_workspace_dir(
            runtime.worker.task_id, self._task_title(runtime.worker.task_id)
        )
        raw_message = payload.get("message")
        payload_text = raw_message if isinstance(raw_message, str) else trigger.payload_ref
        wants_file = has_write_tool and _looks_like_file_deliverable(payload_text)
        # Standard tool-calling conversation: system + user + assistant/tool
        # turns. Durable history is folded into ONE user message; the transcript
        # must not be re-injected on every round or the model sees a wall of
        # duplicated text instead of a proper assistant/tool exchange.
        selected_skills, missing_skills = self._skills_for_payload(payload)
        conversation: list[ChatMessage] = [
            ChatMessage(
                MessageRole.SYSTEM,
                self._system_prompt(
                    model_tools,
                    task_dir,
                    selected=selected_skills,
                    missing=missing_skills,
                ),
            ),
            ChatMessage(MessageRole.USER, self._durable_context(runtime, trigger, payload_text)),
        ]
        nudges = 0
        for round_index in range(self._max_rounds):
            if not self._run_checkpoint(runtime, run_id, lease_owner):
                return "Worker stopped by cancellation or lease policy."
            # Always release the write transaction before a slow model/tool call.
            # Holding it (the old detach-only behaviour) blocks the lease
            # keepalive thread on a second connection, so the Run expires while
            # the model is still generating a long answer.
            self._session.commit()
            written = self._touched_write_tool(runtime)
            # Force the call on the first round of a file task: models otherwise
            # answer with prose and claim the artifact was saved. After a write
            # succeeds we go back to "auto" so it can summarise and finish.
            force_call = bool(wants_file and not written and has_write_tool)
            model_request = ModelRequest(
                request_id=str(trigger.id),
                messages=tuple(conversation),
                tools=model_tools,
                tool_choice="required" if force_call else "auto",
            )
            # One model call can outlive the lease (a large page takes minutes),
            # so renew it from a background thread while this call is in flight.
            with self._lease_keepalive(run_id, lease_owner):
                response = self._model.complete(model_request)
            # Capture token usage at the moment of the call: it exists nowhere
            # else, and a follow-up turn on the same thread should visibly show
            # its prompt-cache hits instead of looking identical to a cold run.
            usage = response.usage
            runtime.metrics.record_model_call(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_tokens,
            )
            if not response.tool_calls:
                # A file task can end with prose only for three reasons: the
                # provider ignored tool_choice, a call failed ("tool is not
                # registered") and the model gave up, or — the case that slipped
                # through — the model DID call a tool but only looked around
                # (`file_list`, `file_read`) and then narrated instead of
                # writing. All three deserve one plain correction.
                needs_write = wants_file and not self._touched_write_tool(runtime)
                if needs_write and nudges < 1:
                    nudges += 1
                    pending = self._strip_leading_code_block(response.text)
                    if pending:
                        conversation.append(ChatMessage(MessageRole.ASSISTANT, pending))
                    conversation.append(ChatMessage(MessageRole.USER, self._write_nudge()))
                    continue
                if not self._run_checkpoint(runtime, run_id, lease_owner):
                    return "Worker stopped by cancellation or lease policy."
                runtime.append_context(
                    ContextEntry(
                        role="assistant",
                        content=response.text,
                        metadata=(("provider_request_id", response.provider_request_id or ""),),
                    )
                )
                runtime.succeed(f"model:{trigger.id}", response.text)
                return response.text
            if round_index + 1 >= self._max_rounds:
                # A model that keeps calling read-only tools burns every round
                # without producing the deliverable ("it didn't write anything,
                # it just listed files"). When the task asked for a file and no
                # write has happened, say so plainly instead of reporting a
                # generic round-limit failure that hides the real problem.
                if wants_file and not self._touched_write_tool(runtime):
                    message = (
                        "reached the model/tool round limit without writing "
                        f"the deliverable; {_MODEL_WRITE_TOOL} was never called "
                        "successfully"
                    )
                    runtime.fail(message)
                    return f"Worker stopped: {message}."
                # max_rounds bounds model calls; a tool request on the final
                # round cannot be acted on, so the turn fails deterministically.
                runtime.fail("maximum model/tool rounds exceeded")
                return "Worker stopped after reaching the model/tool round limit."
            conversation.append(
                ChatMessage(
                    MessageRole.ASSISTANT,
                    response.text,
                    tool_calls=response.tool_calls,
                )
            )
            for call in response.tool_calls:
                model_name = normalize_tool_name(call.name)
                # Exact alias first, then a token-set match so a model that
                # invents "write_file" for "file_write" still reaches the tool
                # instead of getting "tool is not registered" and giving up.
                registry_name = (
                    model_tool_names.get(model_name)
                    or model_tool_names.get(call.name)
                    or model_tool_names.get(call.name.strip())
                    or resolve_tool_name(call.name, model_tool_names)
                    or call.name
                )
                result = self._execute_one_tool(
                    registry_name,
                    call,
                    dispatcher,
                    trigger,
                    runtime,
                    available=tuple(tool.name for tool in model_tools),
                    workspace_dir=task_dir,
                )
                runtime.metrics.record_tool_call()
                runtime.append_context(
                    ContextEntry(
                        role="tool",
                        content=result.output,
                        metadata=(("tool", result.tool_name), ("tool_call_id", call.id)),
                    )
                )
                conversation.append(
                    ChatMessage(
                        MessageRole.TOOL,
                        result.output,
                        name=model_name,
                        tool_call_id=call.id,
                    )
                )
        runtime.fail("maximum model/tool rounds exceeded")
        return "Worker stopped after reaching the model/tool round limit."

    def _execute_one_tool(
        self,
        registry_name: str,
        call: object,
        dispatcher: AsyncToolDispatcher | None,
        trigger: Trigger,
        runtime: WorkerRuntime,
        *,
        available: tuple[str, ...],
        workspace_dir: Path | None = None,
    ) -> ToolResult:
        """Execute one tool call and turn any failure into a model-visible result.

        Models routinely hallucinate tool names, omit arguments, or use the
        wrong type. A thrown exception used to fail the whole Worker; returning
        a short tool result lets the next round self-correct instead.
        """

        name = str(getattr(call, "name", "") or registry_name)
        call_id = str(getattr(call, "id", "") or "")
        arguments = getattr(call, "arguments", {})
        if not isinstance(arguments, dict):
            arguments = {"input": arguments}
        args: dict[str, object] = {str(key): value for key, value in arguments.items()}
        known = {spec.name for spec in self._tools.specs()}
        if dispatcher is not None:
            known.update(spec.name for spec in dispatcher.specs())
        try:
            if registry_name not in known:
                raise ValidationError(
                    f"unknown tool {name!r}; Available tools: {', '.join(available)}"
                )
            if dispatcher is not None and dispatcher.handles(registry_name):
                if self._detach_execution:
                    self._session.commit()
                return dispatcher.execute(registry_name, args)
            return self._tools.execute(
                registry_name,
                args,
                ExecutionContext(
                    tenant_id=trigger.tenant_id,
                    task_id=runtime.worker.task_id,
                    run_id=uuid4(),
                    granted_capabilities=self._granted_capabilities,
                    workspace_dir=workspace_dir,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tool failures must not kill the Worker
            message = " ".join(str(exc).split())[:500]
            hint = f" Available tools: {', '.join(available)}."
            return ToolResult(
                tool_name=registry_name,
                output=f"Error: {type(exc).__name__}: {message}.{hint}",
                output_ref=f"tool-error:{call_id or registry_name}",
            )

    @contextmanager
    def _lease_keepalive(self, run_id: UUID, owner: str) -> Iterator[None]:
        """Keep the Run lease fresh while a long blocking call is in flight.

        Renewal used to happen only between rounds, so a single model call that
        outlived ``lease_seconds`` (generating a 47 KB HTML page easily takes
        90s) let the recovery thread reap a Run that was still working. This
        heartbeats on its own short-lived connection, leaving the Worker session
        untouched (SQLite single-writer).
        """

        bind = self._session.get_bind()
        stop = threading.Event()

        def beat() -> None:
            # Renew at a third of the lease so a single missed beat cannot let
            # the lease lapse. The earlier `max(1.0, ...)` floor made the
            # interval longer than a short lease, so the first beat arrived too
            # late and the Run had already expired.
            interval = max(0.05, self._lease_seconds / 3)
            while not stop.wait(interval):
                try:
                    with Session(bind=bind) as beat_session, beat_session.begin():
                        RunLeaseService(beat_session).heartbeat(
                            run_id, owner, lease_seconds=self._lease_seconds
                        )
                except Exception:  # noqa: BLE001 - a cancelled run must not crash the beat
                    return

        thread = threading.Thread(
            target=beat, name=f"lease-keepalive-{run_id}", daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def _run_checkpoint(self, runtime: WorkerRuntime, run_id: UUID, owner: str) -> bool:
        """Renew the Run lease or stop before another model/tool round."""

        run = self._session.get(RunRecord, run_id)
        if run is None:
            runtime.fail("durable Run disappeared")
            return False
        if run.status in {"cancel_requested", "cancelled"}:
            runtime.stop("cancelled by operator")
            return False
        try:
            RunLeaseService(self._session).heartbeat(
                run_id, owner, lease_seconds=self._lease_seconds
            )
        except RunLeaseError as exc:
            runtime.fail(str(exc))
            return False
        return True

    def _next_attempt(self, task_id: UUID, trigger_id: UUID) -> int:
        """Return the next durable attempt number for one Trigger/Task pair."""

        latest = (
            self._session.query(RunRecord)
            .filter(RunRecord.task_id == task_id, RunRecord.trigger_id == trigger_id)
            .order_by(RunRecord.attempt.desc())
            .first()
        )
        return (latest.attempt + 1) if latest is not None else 1

    def _durable_context(
        self, runtime: WorkerRuntime, trigger: Trigger, payload_text: str
    ) -> str:
        """Render persisted history as ONE user message.

        The previous code rebuilt the whole context (including this run's own
        assistant/tool turns) into a single text block and prepended it to the
        real conversation every round. The model then saw each tool result
        twice, in a shape no OpenAI-compatible endpoint expects, which is why it
        often replied with prose instead of calling a tool.
        """

        earlier = [
            entry
            for entry in runtime.context.entries
            if dict(entry.metadata).get("trigger_id") != str(trigger.id)
            and not _is_self_defeating_answer(entry.role, entry.content)
        ]
        if not earlier:
            return payload_text
        candidates = tuple(
            ContextCandidate(entry.entry_id, entry.role, entry.content, 100 - index)
            for index, entry in enumerate(earlier)
        )
        context = self._context.build(
            ContextBuildRequest(trigger.id, candidates, token_budget=8_000)
        )
        if not context.text:
            return payload_text
        return f"{context.text}\n\n--- current request ---\n{payload_text}"

    @staticmethod
    def _strip_leading_code_block(text: str) -> str:
        """Pull the likely deliverable out of a prose answer.

        When a model pastes a whole document into chat and never calls the
        write tool, that text IS the deliverable. Re-sending it on the nudge
        turn lets the model copy it into ``content`` instead of regenerating a
        long page from scratch — which is slower and loses detail. Returns the
        first fenced block, or the original text when there is nothing to strip.
        """

        if not text:
            return ""
        fence = re.search(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        if fence is not None and len(fence.group(1).strip()) >= 200:
            return fence.group(1).rstrip()
        return text

    @staticmethod
    def _write_nudge() -> str:
        """One plain correction naming the exact tool to call.

        Includes the literal tool name because a model that hallucinated
        ``write_file`` and got "tool is not registered" will otherwise conclude
        no write tool exists at all and paste the document into its answer.
        """

        return (
            f"You have not written the deliverable yet. The exact name to call "
            f"is {_MODEL_WRITE_TOOL} with arguments path and content: pass the "
            "COMPLETE file content, and a path relative to the task folder. "
            "Listing directories or reading files does not produce the "
            "deliverable. Do not invent other tool names, do not claim the "
            "environment has no write capability, and do not paste the "
            "deliverable into the chat. After the write succeeds, reply with a "
            "short summary and the file path."
        )

    def _task_title(self, task_id: UUID | None) -> str:
        """Read one Task's title for its folder name, tolerating a missing row."""

        if task_id is None:
            return ""
        try:
            record = self._session.get(TaskRecord, task_id)
        except Exception:  # noqa: BLE001 - a title is cosmetic, never fatal
            return ""
        return str(getattr(record, "title", "") or "")

    def _effective_workspace_root(self) -> Path | None:
        """Return the directory task folders are created under.

        An explicit setting wins. Otherwise ask the tools themselves: the file
        tools are already pinned to a root, and reusing that root keeps the
        cache folder inside the same sandbox the tools enforce instead of
        guessing ``cwd`` (which sent a test's writes into the repository).
        """

        if self._workspace_root is not None:
            return self._workspace_root
        for spec in self._tools.specs():
            roots = getattr(spec, "allowed_paths", ()) or ()
            for raw in roots:
                try:
                    resolved = Path(raw).resolve()
                except (OSError, ValueError):
                    continue
                if resolved.is_dir():
                    return resolved
        return None

    def _task_workspace_dir(self, task_id: UUID | None, title: str = "") -> Path | None:
        """Resolve the artifact folder this Task's tools may write into.

        Returns ``None`` when per-task folders are disabled or no workspace root
        is known, which leaves the tools on their legacy configured roots.
        """

        root = self._effective_workspace_root()
        if not self._task_workspaces or task_id is None or root is None:
            return None
        try:
            # Reuse the folder a previous attempt of the same Task created so a
            # resumed Worker does not fork a second directory for itself.
            existing = find_task_workspace(
                root, str(task_id), self._cache_dir_name
            )
            if existing is not None:
                return existing
            # Resolve only: the folder is created by the first real write, so a
            # task that never produces a file leaves no empty directory behind.
            return task_workspace(
                root,
                str(task_id),
                title,
                self._cache_dir_name,
                create=False,
            )
        except (OSError, ValueError):
            # A workspace we cannot create must not fail the whole task; the
            # tools fall back to their configured roots.
            return None

    @staticmethod
    def _touched_write_tool(runtime: WorkerRuntime) -> bool:
        """Whether any write tool has already succeeded in this turn."""

        return any(
            entry.role == "tool" and dict(entry.metadata).get("tool") in _WRITE_TOOLS
            for entry in runtime.context.entries
        )

    def _skills_for_payload(
        self, payload: dict[str, object]
    ) -> tuple[tuple[Skill, ...], tuple[str, ...]]:
        """Resolve the skills the user picked for this task.

        An unknown name is reported rather than dropped: silently running
        without the playbook the user chose is worse than saying it is missing.
        """

        if self._skills is None:
            return (), ()
        raw = payload.get("skills")
        requested: list[str] = []
        if isinstance(raw, str):
            requested = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            requested = [str(item) for item in raw]
        if not requested:
            return (), ()
        return select_skills(self._skills, requested)

    def _skill_prompt_block(
        self,
        selected: Sequence[Skill],
        missing: Sequence[str],
    ) -> list[str]:
        """Render the skill section of the system prompt.

        Only the catalogue (one line per skill) is always present; a selected
        skill's full body is inlined because the user explicitly asked for it.
        Everything else stays behind skill_read, which is what keeps a large
        library from eating the context window on every task.
        """

        if self._skills is None or len(self._skills) == 0:
            return []
        lines = [
            "Skills are reusable playbooks of instructions, not tools. "
            "Available skills:\n" + self._skills.catalogue(),
            "When a skill clearly matches the request, call skill_read with its name and "
            "follow its instructions. Reading a skill is cheap; guessing at a procedure "
            "is not.",
        ]
        if selected:
            lines.append(
                "The user selected these skills for this task. Their instructions are "
                "authoritative for HOW to do the work:"
            )
            for skill in selected:
                lines.append(
                    "=== skill: " + skill.name + " ===\n" + skill.body
                    + "\n=== end skill: " + skill.name + " ==="
                )
                if skill.resources:
                    lines.append(
                        "Bundled resources you may read with skill_read(resource=...): "
                        + ", ".join(skill.resources)
                    )
        if missing:
            lines.append(
                "The user asked for skills that are not installed: "
                + ", ".join(missing)
                + ". Do not pretend to follow them; say so in your answer."
            )
        return lines

    def _system_prompt(
        self,
        model_tools: Sequence[ModelTool],
        task_dir: Path | None = None,
        *,
        selected: Sequence[Skill] = (),
        missing: Sequence[str] = (),
    ) -> str:
        """Describe the real toolset, including write access.

        The previous prompt said "use registered read-only tools", which made
        models believe they could not create files: they answered with an HTML
        document pasted into chat and claimed it was saved, while no file ever
        existed. The prompt must state the actual capability and forbid
        claiming a write happened without one.
        """

        names = [tool.name for tool in model_tools]
        has_write = _MODEL_WRITE_TOOL in names
        lines = [
            "You are a persistent Super AI Agent worker with real tools.",
            ("Available tools: " + ", ".join(names) + ".") if names else "No tools are available.",
            "Act with tools instead of describing actions: if the task needs a file read, "
            "a file written, or a directory listed, call the tool.",
            "Never claim to have read, written, created or saved a file unless a tool result "
            "in the transcript confirms it.",
        ]
        if has_write:
            lines.extend(
                [
                    f"{_MODEL_WRITE_TOOL} creates a real file inside the workspace and returns "
                    "its path plus the byte count. Use it whenever the user wants a document, "
                    "page, script, report or any deliverable file. Pass the COMPLETE content.",
                    "Do NOT answer a file request by pasting the deliverable into the chat. "
                    "That leaves no file on disk and is treated as a failed task.",
                    "Do not say a file was saved unless file_write returned success. After a "
                    "successful write, reply with a short summary and the file path.",
                ]
            )
        if task_dir is not None and has_write:
            # State the rule exactly as the tools enforce it: writes are confined
            # to this folder, reads are not. Describing it any other way makes
            # the model either litter the root or believe it cannot read code.
            lines.append(
                f"This task writes into its own output folder: {task_dir}. "
                "For file_write pass a path RELATIVE to that folder (for example "
                "\"report.html\" or \"assets/style.css\"); an absolute path or a "
                "\"..\" climb is rejected. Reading is NOT restricted: file_read and "
                "file_list accept paths relative to the workspace root."
            )
        lines.append(
            "If a tool call fails, its error is returned as the tool result; read it, fix the "
            "arguments or the name, and try again."
        )
        lines.extend(self._skill_prompt_block(selected, missing))
        return " ".join(lines)

    @staticmethod
    def _tool_parameters(input_schema: dict[str, object]) -> dict[str, object]:
        """Translate a compact or standard ToolSpec schema into JSON Schema."""

        if "properties" in input_schema:
            return dict(input_schema)
        properties: dict[str, object] = {}
        required: list[str] = []
        for name, requirement in input_schema.items():
            if name in {"type", "properties", "required", "$schema"}:
                continue
            if isinstance(requirement, dict):
                schema = dict(requirement)
                marker = schema.pop("required", None)
                properties[name] = schema
                if marker in (True, "required", "true", 1):
                    required.append(name)
                continue
            properties[name] = {"type": "string"}
            if requirement == "required":
                required.append(name)
        result: dict[str, object] = {"type": "object", "properties": properties}
        declared = input_schema.get("required")
        if isinstance(declared, (list, tuple)):
            required.extend(str(item) for item in declared)
        if required:
            result["required"] = list(dict.fromkeys(required))
        return result

    @staticmethod
    def _model_tool_name(registry_name: str) -> str:
        """Use the shared identifier grammar required by model APIs."""

        return normalize_tool_name(registry_name)
