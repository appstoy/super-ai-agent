
import asyncio

from super_agent.application.runs import AsyncToolExecutor, RunEventType
from super_agent.errors import ExternalDependencyError
from super_agent.ports.tool import ExecutionContext, ToolResult
from super_agent.tools.registry import ToolRegistry


class SlowTool:
    def describe(self):
        from super_agent.ports.tool import ToolSpec

        return ToolSpec(name="slow", description="slow", max_output_bytes=100)

    def execute(self, arguments, context):
        del arguments, context
        import time

        time.sleep(0.05)
        return ToolResult(tool_name="slow", output="done", output_ref="slow:1")


class FlakyTool:
    def __init__(self):
        self.calls = 0

    def describe(self):
        from super_agent.ports.tool import ToolSpec

        return ToolSpec(name="flaky", description="flaky", max_output_bytes=100)

    def execute(self, arguments, context):
        del arguments, context
        self.calls += 1
        if self.calls == 1:
            raise ExternalDependencyError("temporary")
        return ToolResult(tool_name="flaky", output="ok", output_ref="flaky:1")


def test_async_start_returns_before_completion_and_emits_result() -> None:
    asyncio.run(_test_async_start_returns_before_completion_and_emits_result())


async def _test_async_start_returns_before_completion_and_emits_result() -> None:
    executor = AsyncToolExecutor(ToolRegistry([SlowTool()]))
    run = await executor.start("slow", {}, ExecutionContext())

    assert run.status == "RUNNING"
    assert executor.events[0].event_type is RunEventType.STARTED
    completed = await executor.wait(run.id)
    assert completed.status == "SUCCEEDED"
    assert executor.events[-1].event_type is RunEventType.COMPLETED


def test_async_stop_and_timeout_are_terminal_and_idempotent() -> None:
    asyncio.run(_test_async_stop_and_timeout_are_terminal_and_idempotent())


async def _test_async_stop_and_timeout_are_terminal_and_idempotent() -> None:
    executor = AsyncToolExecutor(ToolRegistry([SlowTool()]))
    stopped = await executor.start("slow", {}, ExecutionContext())
    await executor.stop(stopped.id)
    await executor.stop(stopped.id)
    assert executor.get(stopped.id).status == "CANCELLED"

    timed = await executor.start("slow", {}, ExecutionContext(), timeout=0.001)
    await executor.wait(timed.id)
    assert executor.get(timed.id).status == "TIMED_OUT"


def test_retryable_tool_error_retries_and_completes() -> None:
    asyncio.run(_test_retryable_tool_error_retries_and_completes())


async def _test_retryable_tool_error_retries_and_completes() -> None:
    flaky = FlakyTool()
    executor = AsyncToolExecutor(ToolRegistry([flaky]))
    run = await executor.start("flaky", {}, ExecutionContext(), max_attempts=2)

    await executor.wait(run.id)
    assert executor.get(run.id).status == "SUCCEEDED"
    assert executor.get(run.id).attempt == 2

