"""GitHub, IM, and email event adapters without external network calls."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from super_agent.errors import ExternalDependencyError, PermissionDeniedError
from super_agent.ingress.schemas import TriggerInput


@dataclass(frozen=True, slots=True)
class RawChannelEvent:
    """Provider-neutral representation of one inbound channel event."""

    source: str
    external_id: str
    thread_key: str
    actor: str
    text: str
    tenant_id: str = "default"
    signature: str | None = None


class ChannelFeature:
    """Independent feature flag for one external adapter."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


class RateLimiter:
    """Small deterministic request counter used before real provider limits."""

    def __init__(self, limit: int = 60) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self._counts: dict[str, int] = {}

    def allow(self, key: str) -> bool:
        count = self._counts.get(key, 0)
        if count >= self.limit:
            return False
        self._counts[key] = count + 1
        return True


class WorkChannelAdapter:
    """Normalize one work channel with HMAC verification and Thread mapping."""

    def __init__(
        self,
        source: str,
        secret: bytes,
        *,
        feature: ChannelFeature | None = None,
        rate_limit: int = 60,
    ) -> None:
        self.source = source
        self._secret = secret
        self.feature = feature or ChannelFeature()
        self.rate_limiter = RateLimiter(rate_limit)

    def sign(self, event: RawChannelEvent) -> str:
        """Create the signature expected from the provider webhook."""

        return hmac.new(self._secret, self._canonical(event), hashlib.sha256).hexdigest()

    def ingest(self, event: RawChannelEvent) -> TriggerInput:
        """Verify and normalize an inbound event for Trigger Ingress."""

        if not self.feature.enabled:
            raise PermissionDeniedError(f"channel disabled: {self.source}")
        if event.source != self.source:
            raise PermissionDeniedError("channel source mismatch")
        if not self.rate_limiter.allow(event.actor):
            raise ExternalDependencyError("channel rate limit exceeded")
        expected = self.sign(event)
        if event.signature is None or not hmac.compare_digest(expected, event.signature):
            raise PermissionDeniedError("invalid channel signature")
        thread_id = uuid5(NAMESPACE_URL, f"{self.source}:{event.tenant_id}:{event.thread_key}")
        return TriggerInput(
            source=self.source,
            tenant_id=event.tenant_id,
            thread_id=thread_id,
            actor=event.actor,
            payload={
                "external_id": event.external_id,
                "thread_key": event.thread_key,
                "text": event.text,
            },
            occurred_at=datetime.now(UTC),
            dedupe_key=f"{self.source}:{event.external_id}",
        )

    @staticmethod
    def _canonical(event: RawChannelEvent) -> bytes:
        values = (
            event.source,
            event.external_id,
            event.thread_key,
            event.actor,
            event.text,
            event.tenant_id,
        )
        return "|".join(values).encode("utf-8")


class GitHubChannelAdapter(WorkChannelAdapter):
    def __init__(
        self,
        secret: bytes,
        *,
        feature: ChannelFeature | None = None,
        rate_limit: int = 60,
    ) -> None:
        super().__init__("github", secret, feature=feature, rate_limit=rate_limit)


class IMChannelAdapter(WorkChannelAdapter):
    def __init__(
        self,
        secret: bytes,
        *,
        feature: ChannelFeature | None = None,
        rate_limit: int = 60,
    ) -> None:
        super().__init__("im", secret, feature=feature, rate_limit=rate_limit)


class EmailChannelAdapter(WorkChannelAdapter):
    def __init__(
        self,
        secret: bytes,
        *,
        feature: ChannelFeature | None = None,
        rate_limit: int = 60,
    ) -> None:
        super().__init__("email", secret, feature=feature, rate_limit=rate_limit)

