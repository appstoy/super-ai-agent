"""Validated input contracts for external triggers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PAYLOAD_BYTES = 64 * 1024


class TriggerInput(BaseModel):
    """Normalized event input accepted by the application ingress."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1", min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(default="default", min_length=1, max_length=255)
    thread_id: UUID
    actor: str = Field(min_length=1, max_length=255)
    payload: dict[str, object]
    occurred_at: datetime
    dedupe_key: str = Field(min_length=1, max_length=255)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("payload")
    @classmethod
    def limit_payload(cls, value: dict[str, object]) -> dict[str, object]:
        size = len(cls.model_construct(payload=value).model_dump_json().encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return value

