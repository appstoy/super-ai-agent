"""Channel formatting, approval, retry, and idempotency services."""

from __future__ import annotations

import json

from super_agent.errors import ValidationError
from super_agent.ports.channel import (
    ChannelAdapter,
    ChannelDeliveryResult,
    ChannelType,
    DeliveryRequest,
    DeliveryResultStatus,
    FormattedDelivery,
)


def channel_for_source(source: str) -> ChannelType:
    """Map an inbound trigger source to its outbound delivery channel."""

    normalized = source.strip().lower()
    if normalized == "github":
        return ChannelType.GITHUB
    if normalized in {"im", "telegram"}:
        return ChannelType.IM
    if normalized == "email":
        return ChannelType.EMAIL
    return ChannelType.CLI


class DeliveryPolicy:
    """Format output according to the channel's information bandwidth."""

    def format(self, request: DeliveryRequest) -> FormattedDelivery:
        if not request.idempotency_key:
            raise ValidationError("delivery idempotency_key is required")
        if request.channel in {ChannelType.CLI, ChannelType.IM}:
            words = request.summary.split()
            summary = " ".join(words[:100])
            body = summary if len(words) <= 100 else f"{summary} ..."
            return FormattedDelivery(
                request.channel,
                request.idempotency_key,
                body,
                "text/plain",
                request.requires_approval,
            )
        if request.channel is ChannelType.WEBHOOK:
            body = json.dumps(
                {"summary": request.summary, "details": request.details},
                ensure_ascii=False,
                sort_keys=True,
            )
            return FormattedDelivery(
                request.channel,
                request.idempotency_key,
                body,
                "application/json",
                request.requires_approval,
            )
        if request.channel in {ChannelType.GITHUB, ChannelType.EMAIL}:
            return FormattedDelivery(
                request.channel,
                request.idempotency_key,
                self._structured_markdown(request),
                "text/markdown",
                request.requires_approval,
            )
        raise ValidationError(f"unsupported delivery channel: {request.channel}")

    @staticmethod
    def _structured_markdown(request: DeliveryRequest) -> str:
        """High-density asynchronous delivery: full summary, details, verification."""

        lines = ["## Summary", "", request.summary, "", "## Details", ""]
        for key, value in request.details.items():
            if key == "verification":
                continue
            rendered = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, sort_keys=True
            )
            lines.append(f"- **{key}**: {rendered}")
        verification = request.details.get("verification")
        lines.extend(["", "## Verification", ""])
        if verification is None:
            lines.append("- Not provided.")
        elif isinstance(verification, str):
            lines.append(verification)
        elif isinstance(verification, (list, tuple)):
            lines.extend(f"- {item}" for item in verification)
        else:
            lines.append(json.dumps(verification, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)


class DeliveryService:
    """Coordinate adapters while preserving idempotent delivery semantics."""

    def __init__(
        self,
        adapters: list[ChannelAdapter],
        *,
        policy: DeliveryPolicy | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._adapters = {adapter.channel: adapter for adapter in adapters}
        self._policy = policy or DeliveryPolicy()
        self._max_attempts = max_attempts
        self._results: dict[str, ChannelDeliveryResult] = {}
        self._approved: set[str] = set()

    def approve(self, idempotency_key: str) -> None:
        """Allow a previously blocked delivery to be sent."""

        self._approved.add(idempotency_key)
        previous = self._results.get(idempotency_key)
        if previous is not None and previous.status is DeliveryResultStatus.APPROVAL_REQUIRED:
            self._results.pop(idempotency_key, None)

    def send(self, request: DeliveryRequest) -> ChannelDeliveryResult:
        """Format and send with approval, retry, and idempotency controls."""

        previous = self._results.get(request.idempotency_key)
        if previous is not None:
            return previous
        formatted = self._policy.format(request)
        if formatted.requires_approval and request.idempotency_key not in self._approved:
            result = ChannelDeliveryResult(
                DeliveryResultStatus.APPROVAL_REQUIRED,
                request.idempotency_key,
                0,
                formatted.body,
            )
            self._results[request.idempotency_key] = result
            return result
        adapter = self._adapters.get(request.channel)
        if adapter is None:
            # A missing channel adapter must not break delivery; fall back to CLI.
            adapter = self._adapters.get(ChannelType.CLI)
        if adapter is None:
            raise ValidationError(f"no adapter registered for {request.channel}")
        result = self._attempt(adapter, formatted)
        self._results[request.idempotency_key] = result
        return result

    def _attempt(
        self, adapter: ChannelAdapter, formatted: FormattedDelivery
    ) -> ChannelDeliveryResult:
        last: ChannelDeliveryResult | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = adapter.deliver(formatted)
            except Exception as exc:
                last = ChannelDeliveryResult(
                    DeliveryResultStatus.FAILED,
                    formatted.idempotency_key,
                    attempt,
                    formatted.body,
                    str(exc),
                )
                continue
            if result.status is DeliveryResultStatus.SENT:
                return ChannelDeliveryResult(
                    result.status,
                    formatted.idempotency_key,
                    attempt,
                    formatted.body,
                    result.error,
                )
            last = ChannelDeliveryResult(
                result.status,
                formatted.idempotency_key,
                attempt,
                formatted.body,
                result.error,
            )
        assert last is not None
        return last

