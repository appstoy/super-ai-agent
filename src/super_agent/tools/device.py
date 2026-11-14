"""Model-facing tool that executes commands on a BYOM endpoint device.

The tool never runs anything locally: it enqueues a signed command through
``DeviceCommandService`` and polls for the endpoint's result. It declares a
WRITE effect with high risk and ``requires_approval``, so the default tool
policy denies it until the user explicitly approves the capability.

Not registered by default. Wiring point: after a device is registered and the
user grants ``device.exec``, construct ``DeviceExecTool(service, device_id)``
and add it to the Worker's ``ToolRegistry`` alongside a granted capability
set containing ``device.exec``.
"""

from __future__ import annotations

import time
from uuid import UUID

from super_agent.daemon.service import DeviceCommandService
from super_agent.errors import AgentTimeoutError, ValidationError
from super_agent.ports.tool import ExecutionContext, Tool, ToolEffect, ToolResult, ToolSpec

_MAX_WAIT_SECONDS = 300.0


class DeviceExecTool(Tool):
    """Run one allowlisted command on a registered endpoint and await output."""

    def __init__(
        self,
        service: DeviceCommandService,
        device_id: UUID,
        *,
        poll_interval: float = 0.2,
        max_output_bytes: int = 16 * 1024,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._service = service
        self._device_id = device_id
        self._poll_interval = poll_interval
        self._spec = ToolSpec(
            name="device.exec",
            description=(
                "Execute an allowlisted command on the user's registered device "
                "and return its bounded output."
            ),
            capabilities=frozenset({"device.exec"}),
            effect=ToolEffect.WRITE,
            input_schema={"command": "required", "args": "optional", "cwd": "optional"},
            max_output_bytes=max_output_bytes,
            risk="high",
            requires_approval=True,
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command:
            raise ValidationError("device.exec command must be a non-empty string")
        args = arguments.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValidationError("device.exec args must be a list of strings")
        payload: dict[str, object] = {"command": command, "args": list(args)}
        cwd = arguments.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ValidationError("device.exec cwd must be a string")
            payload["cwd"] = cwd
        wait_seconds = min(max(context.timeout_seconds, self._poll_interval), _MAX_WAIT_SECONDS)
        signed = self._service.enqueue_command(
            self._device_id, "device.exec", payload, ttl_seconds=min(wait_seconds, 60.0)
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            result = self._service.get_result(signed.command_id)
            if result is not None:
                if result.error is not None:
                    return ToolResult(
                        tool_name=self._spec.name,
                        output=f"device error: {result.error}",
                        output_ref=f"device:{self._device_id}:{signed.command_id}",
                    )
                return ToolResult(
                    tool_name=self._spec.name,
                    output=result.output,
                    output_ref=f"device:{self._device_id}:{signed.command_id}",
                )
            time.sleep(self._poll_interval)
        raise AgentTimeoutError(
            f"device {self._device_id} did not return a result for command {signed.command_id}"
        )