"""Provider-neutral output channel contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ChannelType(StrEnum):
    CLI = "cli"
    WEBHOOK = "webhook"
    IM = "im"
    GITHUB = "github"
    EMAIL = "email"


class DeliveryResultStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """Channel-neutral output before formatting and approval."""

    channel: ChannelType
    idempotency_key: str
    summary: str
    details: dict[str, object]
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class FormattedDelivery:
    """Channel-specific body selected by DeliveryPolicy."""

    channel: ChannelType
    idempotency_key: str
    body: str
    content_type: str
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class ChannelDeliveryResult:
    """Result returned by an adapter after one send attempt."""

    status: DeliveryResultStatus
    idempotency_key: str
    attempts: int
    body: str
    error: str | None = None


class ChannelAdapter(Protocol):
    """Port implemented by CLI, webhook, and future external channels."""

    channel: ChannelType

    def deliver(self, delivery: FormattedDelivery) -> ChannelDeliveryResult:
        """Send one already-formatted delivery."""

