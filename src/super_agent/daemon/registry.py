"""Persistent BYOM device registry backed by the devices table.

Device identity and revocation state live in the database while the command
queue and replay bookkeeping stay in memory. Only sha256(secret) is stored;
the raw secret is returned once at registration and never logged, serialized,
or injected into model context.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.daemon.protocol import SignedCommand, command_signature, request_signature
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.storage.models import DeviceRecord


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite datetimes, which lose tzinfo on round-trip."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Device metadata safe for API responses and logs; carries no secret."""

    device_id: UUID
    name: str
    capabilities: frozenset[str]
    revoked: bool
    last_seen_at: datetime | None


class PersistentDeviceRegistry:
    """Register devices durably and verify one-time signed commands.

    The HMAC semantics match ``daemon.protocol.DeviceRegistry`` exactly, but
    device state is authoritative in the database so restarts and multiple
    server processes share the same registration and revocation decisions.
    The replay-protection set is intentionally per-process and bounded by the
    command TTL window.
    """

    def __init__(
        self,
        session: Session,
        *,
        max_command_ttl: float = 60.0,
        online_window_seconds: float = 90.0,
        used_commands: dict[UUID, datetime] | None = None,
    ) -> None:
        if max_command_ttl <= 0 or online_window_seconds <= 0:
            raise ValueError("registry windows must be positive")
        self._session = session
        self._max_ttl = max_command_ttl
        self._online_window = online_window_seconds
        # Callers serving many requests share one dict so replay protection
        # survives across per-request registry instances.
        self._used_commands: dict[UUID, datetime] = (
            used_commands if used_commands is not None else {}
        )

    def register(self, name: str, capabilities: Iterable[str]) -> tuple[UUID, str]:
        """Create a device row and return (device_id, one-time secret hex)."""

        if not name.strip():
            raise ValidationError("device name is required")
        caps = frozenset(capabilities)
        if any(not cap for cap in caps):
            raise ValidationError("device capability must not be empty")
        secret = secrets.token_bytes(32)
        device_id = uuid4()
        self._session.add(
            DeviceRecord(
                id=device_id,
                name=name,
                capabilities=json.dumps(sorted(caps), ensure_ascii=False),
                secret_hash=hashlib.sha256(secret).hexdigest(),
                revoked=0,
            )
        )
        self._session.flush()
        return device_id, secret.hex()

    def renew_lease(self, device_id: UUID) -> DeviceInfo:
        """Record a heartbeat for a live device and refresh its online window."""

        row = self._require_row(device_id)
        if row.revoked:
            raise PermissionDeniedError("device is revoked")
        row.last_seen_at = datetime.now(UTC)
        self._session.flush()
        return self._info(row)

    def revoke(self, device_id: UUID) -> None:
        """Revoke a device so future heartbeats and commands are denied."""

        row = self._require_row(device_id)
        row.revoked = 1
        self._session.flush()

    def is_online(self, device_id: UUID, *, now: datetime | None = None) -> bool:
        """Report whether the device heartbeat is inside the online window."""

        row = self._session.get(DeviceRecord, device_id)
        if row is None or row.revoked or row.last_seen_at is None:
            return False
        current = now or datetime.now(UTC)
        age = (current - _as_utc(row.last_seen_at)).total_seconds()
        return age <= self._online_window

    def get(self, device_id: UUID) -> DeviceInfo | None:
        """Return safe device metadata or None for unknown devices."""

        row = self._session.get(DeviceRecord, device_id)
        return self._info(row) if row is not None else None

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        """Return every registered device without secret material."""

        rows = self._session.scalars(select(DeviceRecord).order_by(DeviceRecord.created_at)).all()
        return tuple(self._info(row) for row in rows)

    def authenticate_request(
        self,
        device_id: UUID,
        *,
        timestamp: str,
        method: str,
        path: str,
        body: bytes,
        signature_hex: str,
        max_skew_seconds: float = 300.0,
    ) -> None:
        """Verify a heartbeat/results header signature for a live device.

        The header HMAC is keyed by sha256(secret) — the same digest the
        server persists — so verification never needs the raw secret. The
        daemon derives this key locally from its own secret. The timestamp
        must be a unix seconds string inside the allowed clock-skew window.
        """

        row = self._require_row(device_id)
        if row.revoked:
            raise PermissionDeniedError("device is revoked")
        try:
            issued_at = float(timestamp)
        except ValueError as exc:
            raise PermissionDeniedError("invalid request timestamp") from exc
        if abs(datetime.now(UTC).timestamp() - issued_at) > max_skew_seconds:
            raise PermissionDeniedError("request timestamp outside allowed window")
        expected = request_signature(
            bytes.fromhex(row.secret_hash),
            device_id=device_id,
            timestamp=timestamp,
            method=method,
            path=path,
            body=body,
        )
        if not hmac.compare_digest(expected, signature_hex):
            raise PermissionDeniedError("invalid request signature")

    def sign(
        self,
        device_id: UUID,
        secret_hex: str,
        capability: str,
        payload: dict[str, object],
        *,
        ttl_seconds: float = 30.0,
    ) -> SignedCommand:
        """Create a bounded command for a live device with the capability."""

        if ttl_seconds <= 0 or ttl_seconds > self._max_ttl:
            raise ValidationError("command TTL exceeds policy")
        row = self._require_row(device_id)
        if row.revoked or capability not in self._capabilities(row):
            raise PermissionDeniedError("device capability denied")
        secret = self._validated_secret(row, secret_hex)
        command = SignedCommand(
            uuid4(),
            device_id,
            capability,
            payload,
            datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            "",
        )
        signature = command_signature(command, secret)
        return SignedCommand(
            command.command_id,
            command.device_id,
            command.capability,
            command.payload,
            command.expires_at,
            signature,
        )

    def verify_and_consume(self, command: SignedCommand, secret_hex: str) -> dict[str, object]:
        """Verify device state, replay, expiry, capability, and signature once."""

        row = self._require_row(command.device_id)
        if row.revoked:
            raise PermissionDeniedError("device is unknown or revoked")
        self._prune_used()
        if command.command_id in self._used_commands:
            raise PermissionDeniedError("command replay detected")
        if _as_utc(command.expires_at) <= datetime.now(UTC):
            raise PermissionDeniedError("command expired")
        if command.capability not in self._capabilities(row):
            raise PermissionDeniedError("device capability denied")
        secret = self._validated_secret(row, secret_hex)
        expected = command_signature(command, secret)
        if not hmac.compare_digest(expected, command.signature):
            raise PermissionDeniedError("invalid command signature")
        self._used_commands[command.command_id] = _as_utc(command.expires_at)
        return command.payload

    def verify_local(self, command: SignedCommand, secret_hex: str) -> dict[str, object]:
        """Endpoint-side check without DB access or replay consumption.

        The daemon keeps no session state, so revocation and one-time replay
        remain server-side decisions; the endpoint still refuses tampered,
        expired, or wrong-device envelopes before touching the workspace.
        """

        if command.expires_at.tzinfo is None:
            raise ValidationError("command expiry must include a timezone")
        if _as_utc(command.expires_at) <= datetime.now(UTC):
            raise PermissionDeniedError("command expired")
        expected = command_signature(command, bytes.fromhex(secret_hex))
        if not hmac.compare_digest(expected, command.signature):
            raise PermissionDeniedError("invalid command signature")
        return command.payload

    def _validated_secret(self, row: DeviceRecord, secret_hex: str) -> bytes:
        try:
            secret = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise PermissionDeniedError("invalid device secret") from exc
        if not hmac.compare_digest(hashlib.sha256(secret).hexdigest(), row.secret_hash):
            raise PermissionDeniedError("invalid device secret")
        return secret

    def _prune_used(self) -> None:
        now = datetime.now(UTC)
        expired = [key for key, expiry in self._used_commands.items() if expiry <= now]
        for key in expired:
            del self._used_commands[key]

    def _require_row(self, device_id: UUID) -> DeviceRecord:
        row = self._session.get(DeviceRecord, device_id)
        if row is None:
            raise PermissionDeniedError("device is unknown or revoked")
        return row

    @staticmethod
    def _capabilities(row: DeviceRecord) -> frozenset[str]:
        try:
            raw = json.loads(row.capabilities)
        except json.JSONDecodeError as exc:
            raise ValidationError("device capabilities are corrupted") from exc
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValidationError("device capabilities are corrupted")
        return frozenset(raw)

    @staticmethod
    def _info(row: DeviceRecord) -> DeviceInfo:
        return DeviceInfo(
            device_id=row.id,
            name=row.name,
            capabilities=PersistentDeviceRegistry._capabilities(row),
            revoked=bool(row.revoked),
            last_seen_at=_as_utc(row.last_seen_at) if row.last_seen_at else None,
        )