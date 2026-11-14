from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from super_agent.scheduler import Scheduler, ScheduleRequest


def test_scheduler_respects_priority_capacity_and_mutex() -> None:
    scheduler = Scheduler(capacity=1)
    first = ScheduleRequest(uuid4(), priority=1, mutex_key="repo")
    high = ScheduleRequest(uuid4(), priority=10, mutex_key="repo")
    second = ScheduleRequest(uuid4(), priority=5, mutex_key="other")
    scheduler.submit(first)
    scheduler.submit(high)
    scheduler.submit(second)

    lease = scheduler.acquire(owner="worker-1")
    assert lease is not None
    assert lease.task_id == high.task_id
    assert scheduler.acquire(owner="worker-2") is None
    scheduler.release(lease.lease_id)
    next_lease = scheduler.acquire(owner="worker-2")
    assert next_lease is not None
    assert next_lease.task_id == second.task_id


def test_scheduler_prevents_same_mutex_and_reaps_expired_leases() -> None:
    scheduler = Scheduler(capacity=2, lease_seconds=1)
    first = ScheduleRequest(uuid4(), mutex_key="repo")
    second = ScheduleRequest(uuid4(), mutex_key="repo")
    scheduler.submit(first)
    scheduler.submit(second)
    lease = scheduler.acquire(owner="worker-1")
    assert lease is not None
    assert scheduler.acquire(owner="worker-2") is None

    expired = scheduler.reap_expired(datetime.now(UTC) + timedelta(seconds=2))
    assert expired == (lease,)
    recovered = scheduler.acquire(owner="worker-2")
    assert recovered is not None


def test_scheduler_validates_configuration() -> None:
    with pytest.raises(ValueError):
        Scheduler(capacity=0)
    with pytest.raises(ValueError):
        Scheduler(lease_seconds=0)

