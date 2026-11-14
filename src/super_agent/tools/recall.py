"""Chain-of-recall tool over durable objects (article 3.3, Inline & Spill).

Every recalled object returns a bounded summary plus the IDs of its next-hop
references, so the model can expand context one hop at a time. Long content is
spilled with an explicit continuation hint instead of flooding the window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.context.recall import ReferenceEdge, ReferenceGraph
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, ToolEffect, ToolResult, ToolSpec
from super_agent.storage.models import (
    DeliveryRecord,
    RunRecord,
    SubscriptionRecord,
    TaskRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerRecord,
)

_CONTINUE_HINT = "用 recall.read 继续读取"
_KINDS = ("trigger", "thread", "task", "worker", "run", "worker_context", "delivery")


@dataclass(frozen=True, slots=True)
class _Recalled:
    """One loaded object with its inline summary and next-hop references."""

    kind: str
    summary: str
    references: tuple[tuple[str, UUID], ...]


def build_reference_edges(session: Session) -> ReferenceGraph:
    """Build the durable reference graph: trigger→thread→worker→task→run→delivery."""

    graph = ReferenceGraph()
    trigger_threads: dict[UUID, str] = {}
    for trigger in session.query(TriggerRecord).all():
        trigger_threads[trigger.thread_id] = trigger.tenant_id
        graph.add(
            ReferenceEdge(trigger.id, trigger.thread_id, "trigger-thread", trigger.tenant_id)
        )
    subscriptions = session.query(SubscriptionRecord).all()
    for subscription in subscriptions:
        tenant = trigger_threads.get(subscription.thread_id, "default")
        graph.add(
            ReferenceEdge(subscription.thread_id, subscription.worker_id, "thread-worker", tenant)
        )
    task_tenants: dict[UUID, str] = {}
    for task in session.query(TaskRecord).all():
        tenant = "default"
        try:
            thread_ids = json.loads(task.thread_ids)
        except (TypeError, json.JSONDecodeError):
            thread_ids = []
        if isinstance(thread_ids, list):
            for raw in thread_ids:
                try:
                    thread_id = UUID(str(raw))
                except ValueError:
                    continue
                tenant = trigger_threads.get(thread_id, tenant)
                graph.add(ReferenceEdge(thread_id, task.id, "thread-task", tenant))
        task_tenants[task.id] = tenant
        if task.worker_id is not None:
            graph.add(ReferenceEdge(task.id, task.worker_id, "task-worker", tenant))
    worker_tenants: dict[UUID, str] = {}
    for worker in session.query(WorkerRecord).all():
        tenant = task_tenants.get(worker.task_id, "default")
        worker_tenants[worker.id] = tenant
        graph.add(ReferenceEdge(worker.id, worker.task_id, "worker-task", tenant))
    for run in session.query(RunRecord).all():
        tenant = task_tenants.get(run.task_id, "default")
        graph.add(ReferenceEdge(run.task_id, run.id, "task-run", tenant))
        if run.trigger_id is not None:
            graph.add(ReferenceEdge(run.id, run.trigger_id, "run-trigger", tenant))
    for entry in session.query(WorkerContextRecord).all():
        tenant = worker_tenants.get(entry.worker_id, "default")
        graph.add(ReferenceEdge(entry.worker_id, entry.id, "worker-context", tenant))
    for delivery in session.query(DeliveryRecord).all():
        tenant = task_tenants.get(delivery.task_id, "default")
        graph.add(ReferenceEdge(delivery.id, delivery.task_id, "delivery-task", tenant))
        graph.add(ReferenceEdge(delivery.task_id, delivery.id, "task-delivery", tenant))
    return graph


class RecallTool:
    """recall.read: bounded per-object recall with next-hop IDs (Inline & Spill)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        max_inline_chars: int = 400,
    ) -> None:
        if max_inline_chars < 1:
            raise ValueError("max_inline_chars must be positive")
        self._factory = factory
        self._max_inline_chars = max_inline_chars
        self._spec = ToolSpec(
            name="recall.read",
            description=(
                "Recall one durable object (trigger/task/worker/run/worker_context/delivery) "
                "by ID. Returns a bounded summary plus next-hop object IDs; call again "
                "with a next-hop ID to expand context along the reference chain."
            ),
            capabilities=frozenset({"recall.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={"object_id": "required", "kind": "optional"},
            max_output_bytes=16 * 1024,
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        raw_id = arguments.get("object_id")
        if not isinstance(raw_id, str):
            raise ValidationError("recall.read requires object_id")
        try:
            object_id = UUID(raw_id)
        except ValueError as exc:
            raise ValidationError(f"invalid object_id: {raw_id}") from exc
        kind = arguments.get("kind")
        if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
            raise ValidationError(f"unsupported recall kind: {kind}")
        with self._factory() as session:
            recalled, tenant = self._load(session, object_id, kind)
            if recalled is None:
                raise ValidationError(f"object not found: {object_id}")
            if tenant is not None and tenant != context.tenant_id:
                raise PermissionDeniedError("recall denied: object belongs to another tenant")
            hop_ids = self._next_hop_ids(session, object_id, tenant or context.tenant_id)
        output = self._render(object_id, recalled, hop_ids)
        return ToolResult(
            run_id=uuid4(),
            tool_name=self._spec.name,
            output=output,
            output_ref=f"recall:{object_id}",
        )

    def _load(
        self, session: Session, object_id: UUID, kind: str | None
    ) -> tuple[_Recalled | None, str | None]:
        """Load the object, returning (content, owning tenant or None if global)."""

        order = (kind,) if kind is not None else _KINDS
        for candidate in order:
            if candidate == "trigger":
                trigger = session.get(TriggerRecord, object_id)
                if trigger is not None:
                    summary = (
                        f"source={trigger.source} actor={trigger.actor} "
                        f"thread_id={trigger.thread_id} dedupe_key={trigger.dedupe_key} "
                        f"payload_ref={trigger.payload_ref}"
                    )
                    trigger_refs = (("thread_id", trigger.thread_id),)
                    return _Recalled("trigger", summary, trigger_refs), trigger.tenant_id
            elif candidate == "thread":
                loaded = self._load_thread(session, object_id)
                if loaded is not None:
                    return loaded
            elif candidate == "task":
                task = session.get(TaskRecord, object_id)
                if task is not None:
                    summary = (
                        f"title={task.title} status={task.status} "
                        f"thread_ids={task.thread_ids} worker_id={task.worker_id}"
                    )
                    task_refs = self._task_refs(task)
                    return _Recalled("task", summary, task_refs), self._task_tenant(session, task)
            elif candidate == "worker":
                worker = session.get(WorkerRecord, object_id)
                if worker is not None:
                    summary = (
                        f"task_id={worker.task_id} status={worker.status} "
                        f"capabilities={worker.capabilities}"
                    )
                    worker_refs = (("task_id", worker.task_id),)
                    task = session.get(TaskRecord, worker.task_id)
                    tenant = self._task_tenant(session, task) if task is not None else None
                    return _Recalled("worker", summary, worker_refs), tenant
            elif candidate == "run":
                run = session.get(RunRecord, object_id)
                if run is not None:
                    summary = (
                        f"task_id={run.task_id} status={run.status} attempt={run.attempt} "
                        f"input_ref={run.input_ref} output_ref={run.output_ref} "
                        f"error={run.error or ''}"
                    )
                    run_refs = [("task_id", run.task_id)]
                    if run.trigger_id is not None:
                        run_refs.append(("trigger_id", run.trigger_id))
                    task = session.get(TaskRecord, run.task_id)
                    tenant = self._task_tenant(session, task) if task is not None else None
                    return _Recalled("run", summary, tuple(run_refs)), tenant
            elif candidate == "worker_context":
                entry = session.get(WorkerContextRecord, object_id)
                if entry is not None:
                    summary = (
                        f"worker_id={entry.worker_id} task_id={entry.task_id} "
                        f"role={entry.role} revision={entry.revision} content={entry.content}"
                    )
                    entry_refs = (("worker_id", entry.worker_id), ("task_id", entry.task_id))
                    task = session.get(TaskRecord, entry.task_id)
                    tenant = self._task_tenant(session, task) if task is not None else None
                    return _Recalled("worker_context", summary, entry_refs), tenant
            elif candidate == "delivery":
                delivery = session.get(DeliveryRecord, object_id)
                if delivery is not None:
                    summary = (
                        f"task_id={delivery.task_id} channel={delivery.channel} "
                        f"status={delivery.status} payload_ref={delivery.payload_ref}"
                    )
                    delivery_refs = (("task_id", delivery.task_id),)
                    task = session.get(TaskRecord, delivery.task_id)
                    tenant = self._task_tenant(session, task) if task is not None else None
                    return _Recalled("delivery", summary, delivery_refs), tenant
        return None, None

    @staticmethod
    def _load_thread(session: Session, thread_id: UUID) -> tuple[_Recalled, str | None] | None:
        """Threads are grouping keys: summarize their triggers and subscriptions."""

        triggers = (
            session.query(TriggerRecord)
            .filter(TriggerRecord.thread_id == thread_id)
            .order_by(TriggerRecord.received_at)
            .all()
        )
        subscriptions = (
            session.query(SubscriptionRecord)
            .filter(SubscriptionRecord.thread_id == thread_id)
            .all()
        )
        if not triggers and not subscriptions:
            return None
        tenant = triggers[0].tenant_id if triggers else None
        refs: list[tuple[str, UUID]] = [
            ("trigger_id", trigger.id) for trigger in triggers[:4]
        ]
        refs.extend(("worker_id", item.worker_id) for item in subscriptions)
        summary = (
            f"triggers={len(triggers)} subscriptions={len(subscriptions)} "
            f"latest_payload_ref={triggers[-1].payload_ref if triggers else ''}"
        )
        return _Recalled("thread", summary, tuple(refs)), tenant

    def _task_refs(self, task: TaskRecord) -> tuple[tuple[str, UUID], ...]:
        refs: list[tuple[str, UUID]] = []
        if task.worker_id is not None:
            refs.append(("worker_id", task.worker_id))
        try:
            thread_ids: Any = json.loads(task.thread_ids)
        except (TypeError, json.JSONDecodeError):
            thread_ids = []
        if isinstance(thread_ids, list):
            for raw in thread_ids:
                try:
                    refs.append(("thread_id", UUID(str(raw))))
                except ValueError:
                    continue
        return tuple(refs)

    @staticmethod
    def _task_tenant(session: Session, task: TaskRecord) -> str | None:
        """Resolve the owning tenant through the task's threads; None = global."""

        try:
            thread_ids: Any = json.loads(task.thread_ids)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(thread_ids, list):
            return None
        for raw in thread_ids:
            try:
                thread_id = UUID(str(raw))
            except ValueError:
                continue
            trigger = (
                session.query(TriggerRecord)
                .filter(TriggerRecord.thread_id == thread_id)
                .order_by(TriggerRecord.received_at)
                .first()
            )
            if trigger is not None:
                return trigger.tenant_id
        return None

    def _next_hop_ids(self, session: Session, object_id: UUID, tenant: str) -> tuple[UUID, ...]:
        graph = build_reference_edges(session)
        result = graph.recall(object_id, tenant_id=tenant, max_depth=1)
        return tuple(item for item in result.object_ids if item != object_id)

    def _render(self, object_id: UUID, recalled: _Recalled, hop_ids: tuple[UUID, ...]) -> str:
        summary = recalled.summary
        truncated = len(summary) > self._max_inline_chars
        if truncated:
            summary = summary[: self._max_inline_chars]
        lines = [
            f"kind: {recalled.kind}",
            f"id: {object_id}",
            f"summary: {summary}",
        ]
        if truncated:
            lines.append(f"content truncated; {_CONTINUE_HINT}（object_id={object_id}）")
        for relation, target in recalled.references:
            lines.append(f"{relation}: {target}")
        if hop_ids:
            lines.append("next_ids: " + ", ".join(str(item) for item in hop_ids))
        else:
            lines.append("next_ids: (none)")
        lines.append(f"沿 next_ids 逐跳展开；{_CONTINUE_HINT}")
        return "\n".join(lines)