from dataclasses import replace

import pytest

from super_agent.adapters.channels.work import (
    ChannelFeature,
    EmailChannelAdapter,
    GitHubChannelAdapter,
    IMChannelAdapter,
    RawChannelEvent,
)
from super_agent.errors import ExternalDependencyError, PermissionDeniedError


def make_event(source: str, external_id: str = "evt-1") -> RawChannelEvent:
    return RawChannelEvent(
        source=source,
        external_id=external_id,
        thread_key="thread-1",
        actor="alice",
        text="please investigate",
        tenant_id="tenant-a",
    )


@pytest.mark.parametrize(
    ("adapter_type", "source"),
    [(GitHubChannelAdapter, "github"), (IMChannelAdapter, "im"), (EmailChannelAdapter, "email")],
)
def test_work_channel_normalizes_signed_thread_event(adapter_type, source) -> None:
    adapter = adapter_type(b"test-secret")
    unsigned = make_event(source)
    signed = replace(unsigned, signature=adapter.sign(unsigned))

    trigger = adapter.ingest(signed)

    assert trigger.source == source
    assert trigger.actor == "alice"
    assert trigger.dedupe_key == f"{source}:evt-1"
    assert trigger.payload["thread_key"] == "thread-1"


def test_work_channel_rejects_bad_signature_and_disabled_feature() -> None:
    adapter = GitHubChannelAdapter(b"secret")
    event = make_event("github")
    with pytest.raises(PermissionDeniedError, match="signature"):
        adapter.ingest(event)

    disabled = GitHubChannelAdapter(b"secret", feature=ChannelFeature(False))
    signed = replace(event, signature=disabled.sign(event))
    with pytest.raises(PermissionDeniedError, match="disabled"):
        disabled.ingest(signed)


def test_work_channel_rate_limit_is_explicit() -> None:
    adapter = IMChannelAdapter(b"secret", rate_limit=1)
    first = make_event("im", "one")
    first = replace(first, signature=adapter.sign(first))
    adapter.ingest(first)
    second = make_event("im", "two")
    second = replace(second, signature=adapter.sign(second))
    with pytest.raises(ExternalDependencyError, match="rate limit"):
        adapter.ingest(second)

