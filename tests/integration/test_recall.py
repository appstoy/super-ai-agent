from uuid import uuid4

import pytest

from super_agent.context.recall import ReferenceEdge, ReferenceGraph


def test_recall_follows_chain_and_records_cycle() -> None:
    graph = ReferenceGraph()
    first, second, third = uuid4(), uuid4(), uuid4()
    graph.add(ReferenceEdge(first, second, "thread-task"))
    graph.add(ReferenceEdge(second, third, "task-worker"))
    graph.add(ReferenceEdge(third, first, "worker-thread"))

    result = graph.recall(first, max_depth=4)

    assert result.object_ids == (first, second, third)
    assert any(step.reason == "cycle detected" for step in result.steps)


def test_recall_enforces_tenant_permission_depth_and_node_boundaries() -> None:
    graph = ReferenceGraph()
    start, allowed, denied, foreign = uuid4(), uuid4(), uuid4(), uuid4()
    graph.add(ReferenceEdge(start, allowed, "allowed"))
    graph.add(ReferenceEdge(start, denied, "denied", allowed=False))
    graph.add(ReferenceEdge(start, foreign, "foreign", tenant_id="other"))

    result = graph.recall(start, tenant_id="default", max_depth=1, max_nodes=2)

    assert result.object_ids == (start, allowed)
    assert any(step.reason == "permission denied" for step in result.steps)
    assert any(step.reason == "tenant denied" for step in result.steps)


def test_recall_validates_limits() -> None:
    graph = ReferenceGraph()
    with pytest.raises(ValueError):
        graph.recall(uuid4(), max_depth=-1)
    with pytest.raises(ValueError):
        graph.recall(uuid4(), max_nodes=0)

