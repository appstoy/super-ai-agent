"""Small in-memory scheduler used before durable lease storage."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class ScheduleRequest:
    """A task waiting for execution capacity."""

    task_id: UUID
    priority: int = 0
    mutex_key: str | None = None


@dataclass(frozen=True, slots=True)
class Lease:
    """Exclusive execution lease with heartbeat expiry."""

    lease_id: UUID
    task_id: UUID
    mutex_key: str | None
    expires_at: datetime
    owner: str

    def active(self, now: datetime | None = None) -> bool:
        return self.expires_at > (now or datetime.now(UTC))


class Scheduler:
    """Priority scheduler with global capacity and task mutexes."""

    def __init__(self, *, capacity: int = 1, lease_seconds: float = 30.0) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._queue: list[tuple[int, int, ScheduleRequest]] = []
        self._counter = 0
        self._leases: dict[UUID, Lease] = {}
        self._tasks: dict[UUID, UUID] = {}

    @property
    def active_leases(self) -> tuple[Lease, ...]:
        self.reap_expired()
        return tuple(self._leases.values())

    def submit(self, request: ScheduleRequest) -> None:
        """Queue a request unless it is already queued or leased."""

        already_queued = any(item[2].task_id == request.task_id for item in self._queue)
        if request.task_id in self._tasks or already_queued:
            return
        self._counter += 1
        heapq.heappush(self._queue, (-request.priority, self._counter, request))

    def acquire(self, *, owner: str) -> Lease | None:
        """Grant the highest-priority runnable request if capacity permits."""

        self.reap_expired()
        if len(self._leases) >= self._capacity:
            return None
        deferred: list[tuple[int, int, ScheduleRequest]] = []
        selected: ScheduleRequest | None = None
        while self._queue:
            item = heapq.heappop(self._queue)
            request = item[2]
            if request.mutex_key and any(
                lease.mutex_key == request.mutex_key for lease in self._leases.values()
            ):
                deferred.append(item)
                continue
            selected = request
            break
        for item in deferred:
            heapq.heappush(self._queue, item)
        if selected is None:
            return None
        lease = Lease(
            lease_id=uuid4(),
            task_id=selected.task_id,
            mutex_key=selected.mutex_key,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            owner=owner,
        )
        self._leases[lease.lease_id] = lease
        self._tasks[selected.task_id] = lease.lease_id
        return lease

    def heartbeat(self, lease_id: UUID) -> Lease:
        """Extend an active lease."""

        self.reap_expired()
        current = self._leases[lease_id]
        updated = replace(
            current,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
        )
        self._leases[lease_id] = updated
        return updated

    def release(self, lease_id: UUID) -> None:
        """Release a lease and allow its task to be scheduled again."""

        lease = self._leases.pop(lease_id)
        self._tasks.pop(lease.task_id, None)

    def reap_expired(self, now: datetime | None = None) -> tuple[Lease, ...]:
        """Remove expired leases and return them for recovery handling."""

        current = now or datetime.now(UTC)
        expired = tuple(lease for lease in self._leases.values() if not lease.active(current))
        for lease in expired:
            self._leases.pop(lease.lease_id, None)
            self._tasks.pop(lease.task_id, None)
        return expired

