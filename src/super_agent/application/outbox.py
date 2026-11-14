"""Durable Outbox consumption primitives."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from super_agent.storage.models import OutboxMessage
from super_agent.storage.outbox import OutboxRepository

OutboxHandler = Callable[[OutboxMessage, dict[str, Any]], None]


class OutboxProcessor:
    """Claim, dispatch, and finalize one durable event transactionally."""

    def __init__(
        self,
        session: Session,
        handler: OutboxHandler,
        *,
        max_attempts: int | None = None,
        retry_backoff: Callable[[int], float] | None = None,
    ) -> None:
        self._session = session
        self._handler = handler
        self._repository = OutboxRepository(session)
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff

    def process_next(self) -> OutboxMessage | None:
        """Process the oldest pending event, or return None when idle."""

        message = self._repository.claim_next(max_attempts=self._max_attempts)
        return self._process_claimed(message)

    def process_dedupe(self, dedupe_key: str) -> OutboxMessage | None:
        """Process one pending event without consuming an unrelated older event."""

        message = self._repository.claim_next(dedupe_key=dedupe_key)
        return self._process_claimed(message)

    def _process_claimed(self, message: OutboxMessage | None) -> OutboxMessage | None:
        """Dispatch and finalize a message already claimed by this transaction."""

        if message is None:
            return None
        try:
            payload = json.loads(message.payload)
            if not isinstance(payload, dict):
                raise ValueError("outbox payload must be an object")
            self._handler(message, payload)
        except Exception as exc:
            retry_in = self._retry_backoff(message.attempts) if self._retry_backoff else 0.0
            self._repository.mark_failed(
                message,
                f"{type(exc).__name__}: {exc}",
                retry_in_seconds=retry_in,
                max_attempts=self._max_attempts,
            )
            return message
        self._repository.mark_published(message)
        return message
