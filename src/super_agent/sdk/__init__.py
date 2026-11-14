"""Stable extension SDK for third-party plugins and user-loaded tools."""

from super_agent.ports.tool import (
    ExecutionContext,
    Tool,
    ToolEffect,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from super_agent.sdk.plugins import PluginLoader, PluginMetadata, PluginResult
from super_agent.tools.loading import (
    RESERVED_TOOL_NAMES,
    FunctionTool,
    ToolLoadReport,
    build_agent_tools,
    load_user_tools,
    resolve_tool_specs,
)

__all__ = [
    "ExecutionContext",
    "FunctionTool",
    "PluginLoader",
    "PluginMetadata",
    "PluginResult",
    "RESERVED_TOOL_NAMES",
    "Tool",
    "ToolEffect",
    "ToolLoadReport",
    "ToolResult",
    "ToolRisk",
    "ToolSpec",
    "build_agent_tools",
    "load_user_tools",
    "resolve_tool_specs",
]
