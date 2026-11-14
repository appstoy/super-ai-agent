"""MVP tools with no shell, filesystem, network, or write side effects."""

from __future__ import annotations

import json
from uuid import uuid4

from super_agent.ports.tool import ExecutionContext, Tool, ToolEffect, ToolResult, ToolSpec


class MockTool(Tool):
    """Return a fixed response for deterministic Router/Worker tests."""

    def __init__(
        self,
        name: str = "mock",
        output: str = "mock result",
        max_output_bytes: int = 16 * 1024,
    ) -> None:
        self._spec = ToolSpec(
            name=name,
            description="Return a deterministic test result.",
            effect=ToolEffect.MOCK,
            input_schema={},
            max_output_bytes=max_output_bytes,
        )
        self._output = output

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(
            run_id=uuid4(),
            tool_name=self._spec.name,
            output=self._output,
            output_ref=f"tool-result:{self._spec.name}",
        )


class ReadOnlyInputTool(Tool):
    """Read from an explicitly supplied in-memory input map."""

    def __init__(self, values: dict[str, object], *, name: str = "input.read") -> None:
        self._values = dict(values)
        self._spec = ToolSpec(
            name=name,
            description="Read one value from a bounded, preloaded input map.",
            capabilities=frozenset({"input.read"}),
            effect=ToolEffect.READ_ONLY,
            input_schema={"key": "required"},
            max_output_bytes=16 * 1024,
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del context
        key = arguments["key"]
        if not isinstance(key, str):
            raise ValueError("input.read key must be a string")
        value = self._values.get(key)
        output = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return ToolResult(
            run_id=uuid4(),
            tool_name=self._spec.name,
            output=output,
            output_ref=f"input-ref:{key}",
        )

