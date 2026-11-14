"""Example user-loaded tool file for Super AI Agent.

Three supported styles (all optional — export whichever fits):

1. a plain Python function -> wrapped into a FunctionTool automatically
2. a factory -> ``make_tool()`` / ``create_tool()`` / ``build_tool()``
3. module-level exports -> ``tool`` / ``TOOL`` / ``tools`` / ``TOOLS``

Try it:

    SUPER_AGENT_TOOL_PATHS=examples/tools super-agent tools
    SUPER_AGENT_TOOL_PATHS=examples/tools super-agent submit "echo hello three times"
"""

from __future__ import annotations

from uuid import uuid4

from super_agent.ports.tool import ExecutionContext, ToolResult, ToolSpec
from super_agent.sdk import FunctionTool


def echo(text: str, repeat: int = 1) -> str:
    """Repeat a short text (demo plain-function tool)."""

    return " ".join([text] * max(1, min(int(repeat), 10)))


class UppercaseTool:
    """A hand-written Tool class (describe/execute)."""

    def describe(self) -> ToolSpec:
        return ToolSpec(
            name="uppercase",
            description="Uppercase the supplied text.",
            capabilities=frozenset({"custom.uppercase"}),
            input_schema={"text": "required"},
        )

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del context
        text = str(arguments.get("text", ""))
        return ToolResult(
            run_id=uuid4(),
            tool_name="uppercase",
            output=text.upper(),
            output_ref="custom-tool:uppercase",
        )


def make_tool() -> FunctionTool:
    """Factory style: build a Tool with an explicit name and capability."""

    return FunctionTool(
        echo,
        name="echo.custom",
        description="Echo text with a bounded repeat count.",
        capabilities=frozenset({"custom.echo"}),
    )


# Module-level export: everything in this tuple is registered.
tools = (make_tool(), UppercaseTool())
TOOLS = tools
