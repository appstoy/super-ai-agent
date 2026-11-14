import json

import pytest

from super_agent.adapters.channels.memory import CliChannelAdapter, WebhookChannelAdapter
from super_agent.delivery.service import DeliveryPolicy, DeliveryService
from super_agent.errors import ValidationError
from super_agent.ports.channel import ChannelType, DeliveryRequest, DeliveryResultStatus


def test_cli_and_webhook_use_different_formats() -> None:
    cli = CliChannelAdapter()
    webhook = WebhookChannelAdapter()
    service = DeliveryService([cli, webhook])

    cli_result = service.send(
        DeliveryRequest(ChannelType.CLI, "cli-1", "short result", {"task_id": "1"})
    )
    webhook_result = service.send(
        DeliveryRequest(ChannelType.WEBHOOK, "webhook-1", "structured result", {"task_id": "1"})
    )

    assert cli_result.status is DeliveryResultStatus.SENT
    assert cli.messages == ["short result"]
    assert json.loads(webhook.messages[0]) == {
        "details": {"task_id": "1"},
        "summary": "structured result",
    }
    assert webhook_result.status is DeliveryResultStatus.SENT


def test_delivery_requires_approval_and_is_idempotent() -> None:
    cli = CliChannelAdapter()
    service = DeliveryService([cli])
    request = DeliveryRequest(ChannelType.CLI, "approval-1", "dangerous", {}, True)

    blocked = service.send(request)
    assert blocked.status is DeliveryResultStatus.APPROVAL_REQUIRED
    service.approve("approval-1")
    sent = service.send(request)
    duplicate = service.send(request)
    assert sent.status is DeliveryResultStatus.SENT
    assert duplicate == sent
    assert cli.messages == ["dangerous"]


def test_webhook_retries_and_policy_rejects_missing_key() -> None:
    webhook = WebhookChannelAdapter(failures_before_success=1)
    service = DeliveryService([webhook], max_attempts=2)
    result = service.send(DeliveryRequest(ChannelType.WEBHOOK, "retry-1", "ok", {}))
    assert result.status is DeliveryResultStatus.SENT
    assert result.attempts == 2

    with pytest.raises(ValidationError, match="idempotency_key"):
        DeliveryPolicy().format(DeliveryRequest(ChannelType.CLI, "", "bad", {}))

