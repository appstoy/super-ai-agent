"""Immutable, worker-scoped context snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """One bounded item in a Worker context."""

    role: str
    content: str
    entry_id: UUID = field(default_factory=uuid4)
    metadata: tuple[tuple[str, str], ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Return a stable representation without exposing mutable state."""

        return {
            "entry_id": str(self.entry_id),
            "role": self.role,
            "content": self.content,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Copy-on-write context owned by exactly one Worker."""

    worker_id: UUID
    task_id: UUID
    entries: tuple[ContextEntry, ...] = ()
    revision: int = 0

    def append(self, entry: ContextEntry) -> WorkerContext:
        """Return a new snapshot; the previous snapshot remains unchanged."""

        return WorkerContext(
            worker_id=self.worker_id,
            task_id=self.task_id,
            entries=(*self.entries, entry),
            revision=self.revision + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a stable context snapshot for logging and replay tests."""

        return {
            "worker_id": str(self.worker_id),
            "task_id": str(self.task_id),
            "revision": self.revision,
            "entries": [entry.to_dict() for entry in self.entries],
        }

