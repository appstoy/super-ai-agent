from uuid import uuid4

import pytest

from super_agent.context.service import InlineSpillContextService
from super_agent.errors import ValidationError
from super_agent.ports.context import (
    ContextBuildRequest,
    ContextCandidate,
    ContextPlacement,
)


def test_context_inlines_current_trigger_first_and_spills_over_budget() -> None:
    trigger_id = uuid4()
    candidates = (
        ContextCandidate(
            uuid4(), "thread", "thread context", priority=100, read_ref="thread:1"
        ),
        ContextCandidate(
            trigger_id, "trigger", "current trigger", priority=1, read_ref="trigger:1"
        ),
        ContextCandidate(
            uuid4(), "artifact", "large artifact content", priority=90, read_ref="artifact:1"
        ),
    )

    built = InlineSpillContextService().build(
        ContextBuildRequest(trigger_id, candidates, token_budget=4)
    )

    assert built.manifest[0].object_id == trigger_id
    assert built.manifest[0].placement is ContextPlacement.INLINE
    assert any(item.placement is ContextPlacement.SPILL for item in built.manifest)
    assert all(item.read_ref for item in built.manifest if item.placement is ContextPlacement.SPILL)


def test_context_excludes_unauthorized_candidates_without_inlining_content() -> None:
    trigger_id = uuid4()
    secret = ContextCandidate(
        uuid4(), "artifact", "secret content", priority=100, authorized=False, read_ref="secret:1"
    )

    built = InlineSpillContextService().build(
        ContextBuildRequest(trigger_id, (secret,), token_budget=10)
    )

    assert "secret content" not in built.text
    assert built.manifest[0].placement is ContextPlacement.EXCLUDED
    assert built.manifest[0].reason == "not authorized"


def test_context_rejects_non_positive_budget() -> None:
    with pytest.raises(ValidationError, match="token_budget"):
        InlineSpillContextService().build(ContextBuildRequest(uuid4(), (), token_budget=0))

