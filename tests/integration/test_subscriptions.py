from datetime import timedelta
from uuid import uuid4

from super_agent.application.subscriptions import (
    DeliveryDisposition,
    SubscriptionRouter,
    SubscriptionStatus,
)
from super_agent.application.worker import WorkerRuntime
from super_agent.domain import Trigger, Worker


def make_trigger(thread_id):
    return Trigger(
        thread_id=thread_id,
        source="test",
        payload_ref="follow-up",
        dedupe_key=str(uuid4()),
    )


def test_thread_can_deliver_to_multiple_workers_and_deduplicate_event() -> None:
    thread_id = uuid4()
    first = WorkerRuntime(Worker(task_id=uuid4()))
    second = WorkerRuntime(Worker(task_id=uuid4()))
    router = SubscriptionRouter()
    router.register_worker(first)
    router.register_worker(second)
    router.subscribe(thread_id, first.worker.id)
    router.subscribe(thread_id, second.worker.id)
    trigger = make_trigger(thread_id)

    first_receipt = router.deliver(trigger)
    duplicate_receipt = router.deliver(trigger)

    assert first_receipt.disposition is DeliveryDisposition.DIRECT
    assert set(first_receipt.worker_ids) == {first.worker.id, second.worker.id}
    assert duplicate_receipt.disposition is DeliveryDisposition.DUPLICATE
    assert len(first.context.entries) == 1
    assert len(second.context.entries) == 1


def test_subscription_cancel_and_expiry_stop_direct_delivery() -> None:
    thread_id = uuid4()
    runtime = WorkerRuntime(Worker(task_id=uuid4()))
    router = SubscriptionRouter()
    router.register_worker(runtime)
    router.subscribe(thread_id, runtime.worker.id, expires_in=timedelta(seconds=-1))
    assert router.deliver(make_trigger(thread_id)).disposition is DeliveryDisposition.NO_MATCH

    router.subscribe(thread_id, runtime.worker.id)
    cancelled = router.cancel(thread_id, runtime.worker.id)
    assert cancelled.status is SubscriptionStatus.CANCELLED
    assert router.deliver(make_trigger(thread_id)).disposition is DeliveryDisposition.NO_MATCH


def test_subscription_renewal_restores_expired_route() -> None:
    thread_id = uuid4()
    runtime = WorkerRuntime(Worker(task_id=uuid4()))
    router = SubscriptionRouter()
    router.register_worker(runtime)
    router.subscribe(thread_id, runtime.worker.id, expires_in=timedelta(seconds=-1))
    router.renew(thread_id, runtime.worker.id, timedelta(minutes=1))

    receipt = router.deliver(make_trigger(thread_id))
    assert receipt.disposition is DeliveryDisposition.DIRECT

