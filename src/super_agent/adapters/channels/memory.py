"""CLI and webhook adapters without real external network calls."""

from __future__ import annotations

from super_agent.ports.channel import (
    ChannelDeliveryResult,
    ChannelType,
    DeliveryResultStatus,
    FormattedDelivery,
)


class CliChannelAdapter:
    """Capture short text deliveries for local CLI output."""

    channel = ChannelType.CLI

    def __init__(self) -> None:
        self.messages: list[str] = []

    def deliver(self, delivery: FormattedDelivery) -> ChannelDeliveryResult:
        self.messages.append(delivery.body)
        return ChannelDeliveryResult(
            DeliveryResultStatus.SENT,
            delivery.idempotency_key,
            1,
            delivery.body,
        )


class WebhookChannelAdapter:
    """Capture structured webhook payloads for integration tests."""

    channel = ChannelType.WEBHOOK

    def __init__(self, failures_before_success: int = 0) -> None:
        self.messages: list[str] = []
        self._remaining_failures = failures_before_success

    def deliver(self, delivery: FormattedDelivery) -> ChannelDeliveryResult:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("temporary webhook failure")
        self.messages.append(delivery.body)
        return ChannelDeliveryResult(
            DeliveryResultStatus.SENT,
            delivery.idempotency_key,
            1,
            delivery.body,
        )

