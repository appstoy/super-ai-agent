import asyncio
from uuid import uuid4

from super_agent.application.runs import AsyncToolExecutor
from super_agent.application.subscriptions import DeliveryDisposition, SubscriptionRouter
from super_agent.application.worker import WorkerRuntime
from super_agent.domain import Trigger, Worker
from super_agent.ports.tool import ExecutionContext, ToolResult, ToolSpec
from super_agent.scheduler import Scheduler, ScheduleRequest
from super_agent.tools.registry import ToolRegistry


class FastTool:
    def describe(self):
        return ToolSpec(name="fast", description="fast")

    def execute(self, arguments, context):
        del arguments, context
        return ToolResult(tool_name="fast", output="ok", output_ref="fast:1")


def test_phase2_multi_task_thread_follow_up_and_slow_run() -> None:
    asyncio.run(_run_phase2_flow())


async def _run_phase2_flow() -> None:
    thread_id = uuid4()
    first = WorkerRuntime(Worker(task_id=uuid4()))
    second = WorkerRuntime(Worker(task_id=uuid4()))
    first.start()
    second.start()

    subscriptions = SubscriptionRouter()
    subscriptions.register_worker(first)
    subscriptions.register_worker(second)
    subscriptions.subscribe(thread_id, first.worker.id)
    subscriptions.subscribe(thread_id, second.worker.id)
    follow_up = Trigger(
        thread_id=thread_id,
        source="test",
        payload_ref="follow-up",
        dedupe_key="follow-up",
    )

    direct = subscriptions.deliver(follow_up)
    duplicate = subscriptions.deliver(follow_up)
    assert direct.disposition is DeliveryDisposition.DIRECT
    assert duplicate.disposition is DeliveryDisposition.DUPLICATE
    assert len(first.context.entries) == 1
    assert len(second.context.entries) == 1

    scheduler = Scheduler(capacity=2)
    task_a = uuid4()
    task_b = uuid4()
    scheduler.submit(ScheduleRequest(task_a, priority=1, mutex_key="task-a"))
    scheduler.submit(ScheduleRequest(task_b, priority=1, mutex_key="task-b"))
    lease_a = scheduler.acquire(owner="worker-a")
    lease_b = scheduler.acquire(owner="worker-b")
    assert lease_a is not None
    assert lease_b is not None
    assert {lease_a.task_id, lease_b.task_id} == {task_a, task_b}

    executor = AsyncToolExecutor(ToolRegistry([FastTool()]))
    started = await executor.start("fast", {}, ExecutionContext())
    assert started.status == "RUNNING"
    second_event = Trigger(
        thread_id=uuid4(), source="test", payload_ref="independent", dedupe_key="independent"
    )
    assert second_event.payload_ref == "independent"
    completed = await executor.wait(started.id)
    assert completed.status == "SUCCEEDED"

