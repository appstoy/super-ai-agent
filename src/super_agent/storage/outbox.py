"""Transactional Outbox repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from super_agent.storage.models import OutboxMessage


class OutboxRepository:
    """Write and claim durable messages with application-level idempotency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, *, topic: str, dedupe_key: str, payload: object) -> OutboxMessage:
        """Insert one pending message; duplicate keys raise a database error."""

        message = OutboxMessage(
            topic=topic,
            dedupe_key=dedupe_key,
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            available_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        self._session.add(message)
        self._session.flush()
        return message

    def claim_next(
        self,
        *,
        dedupe_key: str | None = None,
        lease_seconds: float = 60.0,
        max_attempts: int | None = None,
    ) -> OutboxMessage | None:
        """Claim pending work or reclaim a message whose consumer lease expired.

        ``available_at`` is honoured so a failed message backs off instead of
        being retried in a hot loop, and ``max_attempts`` stops a permanently
        broken message (poison pill) from blocking the queue head forever.
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)
        eligible = and_(
            or_(
                OutboxMessage.status == "PENDING",
                and_(
                    OutboxMessage.status == "CLAIMED",
                    OutboxMessage.claimed_at.is_not(None),
                    OutboxMessage.claimed_at < stale_before,
                ),
            ),
            OutboxMessage.available_at <= now,
        )
        statement = select(OutboxMessage).where(eligible)
        if max_attempts is not None:
            statement = statement.where(OutboxMessage.attempts < max_attempts)
        if dedupe_key is not None:
            statement = statement.where(OutboxMessage.dedupe_key == dedupe_key)
        statement = (
            statement.order_by(OutboxMessage.created_at, OutboxMessage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        message = self._session.scalar(statement)
        if message is None:
            return None
        claimed = cast(
            CursorResult[Any],
            self._session.execute(
                update(OutboxMessage)
                .execution_options(synchronize_session=False)
                .where(
                    OutboxMessage.id == message.id,
                    OutboxMessage.attempts == message.attempts,
                    eligible,
                )
                .values(
                    status="CLAIMED",
                    attempts=OutboxMessage.attempts + 1,
                    claimed_at=now,
                )
            ),
        )
        if claimed.rowcount != 1:
            self._session.expire_all()
            return None
        self._session.refresh(message)
        return message

    def mark_published(self, message: OutboxMessage) -> None:
        """Mark a claimed message as published."""

        message.status = "PUBLISHED"
        message.published_at = datetime.now(UTC)
        self._session.flush()

    def mark_failed(
        self,
        message: OutboxMessage,
        error: str,
        *,
        retry_in_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> None:
        """Return a message to pending state with backoff, or park it as FAILED.

        A message that already exhausted ``max_attempts`` becomes ``FAILED`` so
        it stops competing for the queue head; otherwise one permanently broken
        event would stall every later message forever.
        """

        message.last_error = error[:2000]
        message.claimed_at = None
        if max_attempts is not None and message.attempts >= max_attempts:
            message.status = "FAILED"
        else:
            message.status = "PENDING"
            message.available_at = datetime.now(UTC) + timedelta(
                seconds=max(0.0, retry_in_seconds)
            )
        self._session.flush()

    def retire_exhausted(self, *, max_attempts: int) -> tuple[OutboxMessage, ...]:
        """Park messages that already used up their attempts.

        Backoff keeps a retrying message out of ``claim_next`` but does not by
        itself make it terminal: a message whose budget is spent must be moved
        to ``FAILED`` so the queue depth reflects reality instead of looking
        permanently "pending".
        """

        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        rows = self._session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.status == "PENDING",
                OutboxMessage.attempts >= max_attempts,
            )
        ).all()
        for message in rows:
            message.status = "FAILED"
            if not message.last_error:
                message.last_error = f"gave up after {message.attempts} attempts"
            self._session.flush()
        return tuple(rows)

    def requeue_published(self, dedupe_key: str) -> OutboxMessage | None:
        """Requeue one published event for a bounded Run retry."""

        message = self._session.scalar(
            select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key)
        )
        if message is None or message.status != "PUBLISHED":
            return None
        message.status = "PENDING"
        message.available_at = datetime.now(UTC)
        message.claimed_at = None
        message.published_at = None
        message.last_error = None
        self._session.flush()
        return message

