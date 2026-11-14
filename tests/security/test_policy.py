import pytest

from super_agent.errors import PermissionDeniedError
from super_agent.ports.tool import ExecutionContext, ToolSpec
from super_agent.security.policy import ToolPolicy, ToolRisk
from super_agent.tools.builtin import MockTool
from super_agent.tools.registry import ToolRegistry


def test_policy_denies_missing_capability_and_records_audit() -> None:
    policy = ToolPolicy()
    spec = ToolSpec(name="secret", description="secret", capabilities=frozenset({"secret.read"}))

    with pytest.raises(PermissionDeniedError, match="capability"):
        policy.authorize(spec, ExecutionContext())
    assert policy.audit[-1].decision == "deny"
    assert policy.audit[-1].tool_name == "secret"


def test_high_risk_tool_requires_explicit_approval() -> None:
    policy = ToolPolicy(approved_risks=frozenset({ToolRisk.LOW}))
    spec = ToolSpec(name="danger", description="danger", risk=ToolRisk.HIGH.value)
    context = ExecutionContext()

    with pytest.raises(PermissionDeniedError, match="approval"):
        policy.authorize(spec, context)
    policy.authorize(spec, context, approved=True)
    assert policy.audit[-1].decision == "allow"


def test_registry_applies_policy_before_tool_execution() -> None:
    policy = ToolPolicy()
    registry = ToolRegistry([MockTool()], policy=policy)
    result = registry.execute("mock", {}, ExecutionContext())

    assert result.output == "mock result"
    assert policy.audit[-1].decision == "allow"

