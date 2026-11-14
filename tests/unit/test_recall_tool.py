"""Chain-of-recall tool: next-hop IDs, tenant isolation, Inline & Spill."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from super_agent.context.recall import ReferenceGraph
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.ports.tool import ExecutionContext, ToolEffect
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import (
    DeliveryRecord,
    RunRecord,
    SubscriptionRecord,
    TaskRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerRecord,
)
from super_agent.tools.recall import RecallTool, build_reference_edges
from super_agent.tools.registry import ToolRegistry


def make_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'recall.db'}")
    create_schema(engine)
    return create_session_factory(engine)


def seed_chain(factory: sessionmaker[Session], *, tenant: str = "default") -> dict[str, UUID]:
    """Persist trigger→thread→worker→task→run→delivery plus one context entry."""

    ids = {
        "trigger": uuid4(),
        "thread": uuid4(),
        "task": uuid4(),
        "worker": uuid4(),
        "run": uuid4(),
        "delivery": uuid4(),
        "context": uuid4(),
    }
    now = datetime.now(UTC)
    with session_scope(factory) as session:
        session.add(
            TriggerRecord(
                id=ids["trigger"],
                schema_version="1",
                source="github",
                tenant_id=tenant,
                thread_id=ids["thread"],
                actor="alice",
                payload_ref="review the change",
                occurred_at=now,
                received_at=now,
                dedupe_key=f"recall-{ids['trigger']}",
                filter_decision="accepted",
            )
        )
        session.add(
            SubscriptionRecord(
                id=uuid4(),
                thread_id=ids["thread"],
                worker_id=ids["worker"],
                status="active",
                version=0,
            )
        )
        session.add(
            TaskRecord(
                id=ids["task"],
                title="review the change",
                status="succeeded",
                thread_ids=json.dumps([str(ids["thread"])]),
                worker_id=ids["worker"],
                version=1,
            )
        )
        session.add(
            WorkerRecord(
                id=ids["worker"],
                task_id=ids["task"],
                status="succeeded",
                capabilities="[]",
                version=1,
            )
        )
        session.add(
            RunRecord(
                id=ids["run"],
                task_id=ids["task"],
                trigger_id=ids["trigger"],
                status="succeeded",
                attempt=1,
                input_ref=f"trigger:{ids['trigger']}",
                output_ref="model:done",
            )
        )
        session.add(
            DeliveryRecord(
                id=ids["delivery"],
                task_id=ids["task"],
                channel="github",
                idempotency_key=f"task:{ids['task']}",
                payload_ref="review completed",
                status="sent",
                version=1,
            )
        )
        session.add(
            WorkerContextRecord(
                id=ids["context"],
                worker_id=ids["worker"],
                task_id=ids["task"],
                role="assistant",
                content="review completed",
                metadata_json="{}",
                revision=1,
                created_at=now,
            )
        )
    return ids


def recall(
    tool: RecallTool, object_id: UUID, *, tenant: str = "default", kind: str | None = None
) -> str:
    arguments: dict[str, object] = {"object_id": str(object_id)}
    if kind is not None:
        arguments["kind"] = kind
    result = tool.execute(
        arguments,
        ExecutionContext(tenant_id=tenant, granted_capabilities=frozenset({"recall.read"})),
    )
    return result.output


def test_recall_tool_contract_is_read_only_and_capability_gated(tmp_path: Path) -> None:
    tool = RecallTool(make_factory(tmp_path))
    spec = tool.describe()
    assert spec.name == "recall.read"
    assert spec.effect is ToolEffect.READ_ONLY
    assert spec.capabilities == frozenset({"recall.read"})
    registry = ToolRegistry([tool])
    assert registry.describe("recall.read").name == "recall.read"
    with pytest.raises(PermissionDeniedError):
        registry.execute(
            "recall.read",
            {"object_id": str(uuid4())},
            ExecutionContext(granted_capabilities=frozenset()),
        )


def test_recall_trigger_inlines_thread_id_and_next_hop(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    ids = seed_chain(factory)
    tool = RecallTool(factory)
    output = recall(tool, ids["trigger"])
    assert "kind: trigger" in output
    assert f"id: {ids['trigger']}" in output
    assert f"thread_id: {ids['thread']}" in output
    assert "next_ids:" in output
    assert str(ids["thread"]) in output
    assert "用 recall.read 继续读取" in output


def test_recall_walks_the_chain_hop_by_hop(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    ids = seed_chain(factory)
    tool = RecallTool(factory)
    # thread → worker via subscription, worker → task, task → run/delivery.
    # A bare thread ID is a grouping key, so an explicit kind is required.
    thread_out = recall(tool, ids["thread"])
    assert str(ids["worker"]) in thread_out
    worker_out = recall(tool, ids["worker"])
    assert f"task_id: {ids['task']}" in worker_out
    assert str(ids["context"]) in worker_out  # worker_context next hop
    task_out = recall(tool, ids["task"])
    assert str(ids["delivery"]) in task_out
    assert str(ids["run"]) in task_out
    delivery_out = recall(tool, ids["delivery"])
    assert f"task_id: {ids['task']}" in delivery_out
    run_out = recall(tool, ids["run"])
    assert f"trigger_id: {ids['trigger']}" in run_out
    context_out = recall(tool, ids["context"], kind="worker_context")
    assert "role=assistant" in context_out
    assert str(ids["worker"]) in context_out


def test_recall_rejects_cross_tenant_objects(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    ids = seed_chain(factory, tenant="tenant-a")
    tool = RecallTool(factory)
    with pytest.raises(PermissionDeniedError, match="tenant"):
        recall(tool, ids["trigger"], tenant="tenant-b")
    with pytest.raises(PermissionDeniedError, match="tenant"):
        recall(tool, ids["task"], tenant="tenant-b")
    with pytest.raises(PermissionDeniedError, match="tenant"):
        recall(tool, ids["worker"], tenant="tenant-b")
    # The owning tenant can read the same objects.
    assert "kind: trigger" in recall(tool, ids["trigger"], tenant="tenant-a")
    assert "kind: task" in recall(tool, ids["task"], tenant="tenant-a")


def test_recall_truncates_long_content_with_continuation_hint(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    ids = seed_chain(factory)
    long_content = "detail " * 2_000
    with session_scope(factory) as session:
        entry = session.get(WorkerContextRecord, ids["context"])
        assert entry is not None
        entry.content = long_content
    tool = RecallTool(factory, max_inline_chars=200)
    output = recall(tool, ids["context"], kind="worker_context")
    assert "content truncated" in output
    assert "用 recall.read 继续读取" in output
    assert long_content not in output
    assert len(output) < 1_500


def test_recall_validates_arguments_and_unknown_ids(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    tool = RecallTool(factory)
    context = ExecutionContext(granted_capabilities=frozenset({"recall.read"}))
    with pytest.raises(ValidationError):
        tool.execute({}, context)
    with pytest.raises(ValidationError):
        tool.execute({"object_id": "not-a-uuid"}, context)
    with pytest.raises(ValidationError):
        tool.execute({"object_id": str(uuid4()), "kind": "bogus"}, context)
    with pytest.raises(ValidationError, match="not found"):
        tool.execute({"object_id": str(uuid4())}, context)


def test_build_reference_edges_forms_the_full_chain(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    ids = seed_chain(factory)
    with session_scope(factory) as session:
        graph = build_reference_edges(session)
    assert isinstance(graph, ReferenceGraph)
    result = graph.recall(ids["trigger"], max_depth=6, max_nodes=32)
    for key in ("thread", "worker", "task", "run", "delivery", "context"):
        assert ids[key] in result.object_ids, f"{key} missing from chain recall"


def test_build_reference_edges_keeps_tenants_apart(tmp_path: Path) -> None:
    factory = make_factory(tmp_path)
    mine = seed_chain(factory, tenant="tenant-a")
    with session_scope(factory) as session:
        graph = build_reference_edges(session)
    result = graph.recall(mine["trigger"], tenant_id="tenant-b", max_depth=6)
    assert result.object_ids == (mine["trigger"],)
    assert any(step.reason == "tenant denied" for step in result.steps)