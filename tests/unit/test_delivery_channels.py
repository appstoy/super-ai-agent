"""Channel-tiered delivery formatting, source mapping, and adapter fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from super_agent.adapters.channels.memory import CliChannelAdapter, WebhookChannelAdapter
from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.delivery.service import DeliveryPolicy, DeliveryService, channel_for_source
from super_agent.errors import ValidationError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.channel import (
    ChannelAdapter,
    ChannelDeliveryResult,
    ChannelType,
    DeliveryRequest,
    DeliveryResultStatus,
    FormattedDelivery,
)
from super_agent.ports.model import ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import DeliveryRecord


class RecordingAdapter:
    """Capture deliveries for any channel type."""

    def __init__(self, channel: ChannelType) -> None:
        self.channel = channel
        self.messages: list[FormattedDelivery] = []

    def deliver(self, delivery: FormattedDelivery) -> ChannelDeliveryResult:
        self.messages.append(delivery)
        return ChannelDeliveryResult(
            DeliveryResultStatus.SENT, delivery.idempotency_key, 1, delivery.body
        )


def long_summary(words: int = 150) -> str:
    return " ".join(f"word{i}" for i in range(words))


def test_im_delivery_is_lightweight_and_bounded_to_100_words() -> None:
    adapter = RecordingAdapter(ChannelType.IM)
    service = DeliveryService([adapter])
    result = service.send(DeliveryRequest(ChannelType.IM, "im-1", long_summary(), {"x": "y"}))
    assert result.status is DeliveryResultStatus.SENT
    body = adapter.messages[0].body
    assert len(body.split()) <= 101  # 100 words plus the ellipsis marker
    assert body.endswith("...")
    assert adapter.messages[0].content_type == "text/plain"


@pytest.mark.parametrize("channel", [ChannelType.GITHUB, ChannelType.EMAIL])
def test_async_channels_emit_structured_markdown_without_truncation(channel: ChannelType) -> None:
    adapter = RecordingAdapter(channel)
    service = DeliveryService([adapter])
    summary = long_summary(150)
    details = {
        "task_id": "task-1",
        "changes": ["src/a.py", "src/b.py"],
        "verification": "pytest -q: 115 passed",
    }
    result = service.send(DeliveryRequest(channel, f"{channel}-1", summary, details))
    assert result.status is DeliveryResultStatus.SENT
    body = adapter.messages[0].body
    assert "## Summary" in body
    assert "## Details" in body
    assert "## Verification" in body
    assert summary in body  # no 100-word truncation
    assert "task-1" in body
    assert "src/a.py" in body
    assert "pytest -q: 115 passed" in body
    assert adapter.messages[0].content_type == "text/markdown"


def test_structured_markdown_handles_missing_verification() -> None:
    formatted = DeliveryPolicy().format(
        DeliveryRequest(ChannelType.EMAIL, "email-2", "done", {"task_id": "t"})
    )
    assert "## Verification" in formatted.body
    assert "Not provided." in formatted.body


def test_cli_and_webhook_formats_are_unchanged() -> None:
    policy = DeliveryPolicy()
    cli = policy.format(DeliveryRequest(ChannelType.CLI, "cli-1", long_summary(), {}))
    assert len(cli.body.split()) <= 101
    assert cli.content_type == "text/plain"
    webhook = policy.format(
        DeliveryRequest(ChannelType.WEBHOOK, "wh-1", "structured", {"task_id": "1"})
    )
    assert json.loads(webhook.body) == {"details": {"task_id": "1"}, "summary": "structured"}
    assert webhook.content_type == "application/json"
    with pytest.raises(ValidationError, match="idempotency_key"):
        policy.format(DeliveryRequest(ChannelType.IM, "", "bad", {}))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("github", ChannelType.GITHUB),
        ("im", ChannelType.IM),
        ("telegram", ChannelType.IM),
        ("email", ChannelType.EMAIL),
        ("cli", ChannelType.CLI),
        ("web", ChannelType.CLI),
        ("webhook", ChannelType.CLI),
        ("unknown", ChannelType.CLI),
    ],
)
def test_channel_for_source_mapping(source: str, expected: ChannelType) -> None:
    assert channel_for_source(source) is expected


def test_missing_adapter_falls_back_to_cli() -> None:
    cli = CliChannelAdapter()
    service = DeliveryService([cli])
    result = service.send(
        DeliveryRequest(ChannelType.GITHUB, "gh-fallback", "structured body", {})
    )
    assert result.status is DeliveryResultStatus.SENT
    # Formatting stays channel-tiered; only the transport falls back to CLI.
    assert len(cli.messages) == 1
    assert "## Summary" in cli.messages[0]
    assert "structured body" in cli.messages[0]


def test_no_adapter_at_all_still_raises() -> None:
    service = DeliveryService([])
    with pytest.raises(ValidationError, match="no adapter"):
        service.send(DeliveryRequest(ChannelType.IM, "im-x", "hello", {}))


def make_trigger(source: str, dedupe_key: str) -> TriggerInput:
    return TriggerInput(
        source=source,
        actor="user",
        thread_id=uuid4(),
        payload={"message": "deliver across channels"},
        occurred_at=datetime.now(UTC),
        dedupe_key=dedupe_key,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("github", ChannelType.GITHUB),
        ("im", ChannelType.IM),
        ("telegram", ChannelType.IM),
        ("email", ChannelType.EMAIL),
        ("web", ChannelType.CLI),
    ],
)
def test_orchestrator_delivery_uses_source_mapped_channel(
    tmp_path: Path, source: str, expected: ChannelType
) -> None:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / f'{source}.db'}")
    create_schema(engine)
    factory = create_session_factory(engine)
    adapter = RecordingAdapter(expected)
    model = MockModelGateway(
        [ModelResponse(text="TASK: channel delivery"), ModelResponse(text="final answer")]
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session, model, delivery=DeliveryService([adapter])
        ).submit(make_trigger(source, f"channel-{source}"))
        assert result.status == "succeeded"
    assert len(adapter.messages) == 1
    assert adapter.messages[0].channel is expected
    with session_scope(factory) as session:
        delivery = session.scalar(select(DeliveryRecord))
        assert delivery is not None
        assert delivery.channel == expected.value
        assert delivery.status == "sent"


def test_orchestrator_delivery_falls_back_to_cli_adapter(tmp_path: Path) -> None:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'fallback.db'}")
    create_schema(engine)
    factory = create_session_factory(engine)
    cli = CliChannelAdapter()
    webhook = WebhookChannelAdapter()
    model = MockModelGateway(
        [ModelResponse(text="TASK: fallback delivery"), ModelResponse(text="delivered via cli")]
    )
    with session_scope(factory) as session:
        result = AgentOrchestrator(
            session, model, delivery=DeliveryService([cli, webhook])
        ).submit(make_trigger("github", "channel-fallback"))
        assert result.status == "succeeded"
    assert len(cli.messages) == 1
    assert "## Summary" in cli.messages[0]
    assert "delivered via cli" in cli.messages[0]
    assert webhook.messages == []
    with session_scope(factory) as session:
        delivery = session.scalar(select(DeliveryRecord))
        assert delivery is not None and delivery.channel == "github"


def test_recording_adapter_satisfies_channel_adapter_protocol() -> None:
    adapter: ChannelAdapter = RecordingAdapter(ChannelType.IM)
    assert adapter.channel is ChannelType.IM