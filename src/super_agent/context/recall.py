"""Bounded, permission-aware chain recall over object references."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ReferenceEdge:
    """One authorized or restricted object reference."""

    source_id: UUID
    target_id: UUID
    relation: str
    tenant_id: str = "default"
    allowed: bool = True


@dataclass(frozen=True, slots=True)
class RecallStep:
    """One traversed edge in a chain recall result."""

    source_id: UUID
    target_id: UUID
    relation: str
    depth: int
    included: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Visited object IDs and the complete bounded traversal audit."""

    object_ids: tuple[UUID, ...]
    steps: tuple[RecallStep, ...]


class ReferenceGraph:
    """In-memory reference graph with tenant and traversal boundaries."""

    def __init__(self) -> None:
        self._edges: dict[UUID, list[ReferenceEdge]] = {}

    def add(self, edge: ReferenceEdge) -> None:
        self._edges.setdefault(edge.source_id, []).append(edge)

    def recall(
        self,
        start_id: UUID,
        *,
        tenant_id: str = "default",
        max_depth: int = 3,
        max_nodes: int = 32,
    ) -> RecallResult:
        """Traverse references while recording cycles and denied edges."""

        if max_depth < 0 or max_nodes < 1:
            raise ValueError("max_depth must be non-negative and max_nodes must be positive")
        visited: set[UUID] = {start_id}
        object_ids: list[UUID] = [start_id]
        steps: list[RecallStep] = []
        queue: deque[tuple[UUID, int]] = deque([(start_id, 0)])
        while queue:
            source_id, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self._edges.get(source_id, []):
                next_depth = depth + 1
                if edge.tenant_id != tenant_id:
                    steps.append(
                        RecallStep(
                            source_id,
                            edge.target_id,
                            edge.relation,
                            next_depth,
                            False,
                            "tenant denied",
                        )
                    )
                    continue
                if not edge.allowed:
                    steps.append(
                        RecallStep(
                            source_id,
                            edge.target_id,
                            edge.relation,
                            next_depth,
                            False,
                            "permission denied",
                        )
                    )
                    continue
                if edge.target_id in visited:
                    steps.append(
                        RecallStep(
                            source_id,
                            edge.target_id,
                            edge.relation,
                            next_depth,
                            False,
                            "cycle detected",
                        )
                    )
                    continue
                if len(object_ids) >= max_nodes:
                    steps.append(
                        RecallStep(
                            source_id,
                            edge.target_id,
                            edge.relation,
                            next_depth,
                            False,
                            "node budget",
                        )
                    )
                    continue
                visited.add(edge.target_id)
                object_ids.append(edge.target_id)
                steps.append(
                    RecallStep(
                        source_id, edge.target_id, edge.relation, next_depth, True, "included"
                    )
                )
                queue.append((edge.target_id, next_depth))
        return RecallResult(tuple(object_ids), tuple(steps))

