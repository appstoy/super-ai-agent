from datetime import UTC, datetime

import pytest

from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.policies.events import EventFilter, FilterDecision, NormalizedEvent
from super_agent.policies.speculative import SpeculativePolicy


def test_event_filter_fast_path_rules_and_version() -> None:
    event_filter = EventFilter(
        allowed_sources=frozenset({"github"}),
        allowed_scopes=frozenset({"repo-1"}),
        ignored_actors=frozenset({"bot"}),
    )
    assert event_filter.version == "fast-path-v1"
    assert event_filter.decide(
        NormalizedEvent("github", "repo-1", "user", "hello")
    ) is FilterDecision.CANDIDATE
    assert event_filter.decide(
        NormalizedEvent("github", "repo-1", "user", "do it", True)
    ) is FilterDecision.ACCEPT
    assert event_filter.decide(
        NormalizedEvent("slack", "repo-1", "user", "hello")
    ) is FilterDecision.IGNORE
    assert event_filter.decide(
        NormalizedEvent("github", "repo-1", "bot", "hello")
    ) is FilterDecision.IGNORE


def test_speculative_policy_enforces_budget_ttl_and_no_side_effects() -> None:
    policy = SpeculativePolicy(max_budget_tokens=100, max_ttl_seconds=60)
    request = policy.create("prepare-summary", budget_tokens=50, ttl_seconds=30)
    assert request.expires_at > datetime.now(UTC)
    with pytest.raises(PermissionDeniedError, match="side effects"):
        policy.create("send-email", budget_tokens=10, ttl_seconds=10, has_side_effects=True)
    with pytest.raises(ValidationError, match="budget"):
        policy.create("too-large", budget_tokens=101, ttl_seconds=10)
    with pytest.raises(ValidationError, match="TTL"):
        policy.create("too-long", budget_tokens=10, ttl_seconds=61)

