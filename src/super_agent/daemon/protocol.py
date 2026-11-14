"""Minimal HMAC-signed, replay-protected endpoint protocol."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from super_agent.errors import PermissionDeniedError, ValidationError


@dataclass(frozen=True, slots=True)
class SignedCommand:
    """Command envelope accepted by a registered endpoint."""

    command_id: UUID
    device_id: UUID
    capability: str
    payload: dict[str, object]
    expires_at: datetime
    signature: str


@dataclass(frozen=True, slots=True)
class Device:
    """Registered endpoint metadata without session or secret persistence."""

    device_id: UUID
    capabilities: frozenset[str]
    revoked: bool = False
    lease_expires_at: datetime | None = None


class DeviceRegistry:
    """Register devices and verify one-time signed commands."""

    def __init__(self, *, max_command_ttl: float = 60.0) -> None:
        if max_command_ttl <= 0:
            raise ValueError("max_command_ttl must be positive")
        self._max_ttl = max_command_ttl
        self._devices: dict[UUID, Device] = {}
        self._secrets: dict[UUID, bytes] = {}
        self._used_commands: set[UUID] = set()

    def register(self, capabilities: frozenset[str]) -> tuple[Device, str]:
        """Register a device and return its ID plus one-time secret."""

        device = Device(uuid4(), capabilities)
        secret = secrets.token_bytes(32)
        self._devices[device.device_id] = device
        self._secrets[device.device_id] = secret
        return device, secret.hex()

    def renew_lease(self, device_id: UUID, seconds: float = 60.0) -> Device:
        """Extend a registered device lease."""

        device = self._devices[device_id]
        if device.revoked:
            raise PermissionDeniedError("device is revoked")
        updated = Device(
            device.device_id,
            device.capabilities,
            device.revoked,
            datetime.now(UTC) + timedelta(seconds=seconds),
        )
        self._devices[device_id] = updated
        return updated

    def revoke(self, device_id: UUID) -> None:
        """Revoke a device and invalidate future commands."""

        device = self._devices[device_id]
        self._devices[device_id] = Device(device.device_id, device.capabilities, True)

    def sign(
        self,
        device_id: UUID,
        secret_hex: str,
        capability: str,
        payload: dict[str, object],
        *,
        ttl_seconds: float = 30.0,
    ) -> SignedCommand:
        """Create a command with a bounded expiry."""

        if ttl_seconds <= 0 or ttl_seconds > self._max_ttl:
            raise ValidationError("command TTL exceeds policy")
        device = self._devices[device_id]
        if device.revoked or capability not in device.capabilities:
            raise PermissionDeniedError("device capability denied")
        command = SignedCommand(
            uuid4(),
            device_id,
            capability,
            payload,
            datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            "",
        )
        signature = self._signature(command, bytes.fromhex(secret_hex))
        return SignedCommand(
            command.command_id,
            command.device_id,
            command.capability,
            command.payload,
            command.expires_at,
            signature,
        )

    def verify_and_consume(self, command: SignedCommand, secret_hex: str) -> dict[str, object]:
        """Verify expiry, device state, capability, signature, and replay uniqueness."""

        device = self._devices.get(command.device_id)
        if device is None or device.revoked:
            raise PermissionDeniedError("device is unknown or revoked")
        if command.command_id in self._used_commands:
            raise PermissionDeniedError("command replay detected")
        if command.expires_at <= datetime.now(UTC):
            raise PermissionDeniedError("command expired")
        if command.capability not in device.capabilities:
            raise PermissionDeniedError("device capability denied")
        expected = self._signature(command, bytes.fromhex(secret_hex))
        if not hmac.compare_digest(expected, command.signature):
            raise PermissionDeniedError("invalid command signature")
        self._used_commands.add(command.command_id)
        return command.payload

    @staticmethod
    def _signature(command: SignedCommand, secret: bytes) -> str:
        return command_signature(command, secret)


def command_signature(command: SignedCommand, secret: bytes) -> str:
    """Return the canonical HMAC-SHA256 signature of one command envelope."""

    body = "|".join(
        [
            str(command.command_id),
            str(command.device_id),
            command.capability,
            str(sorted(command.payload.items())),
            command.expires_at.isoformat(),
        ]
    ).encode("utf-8")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def request_signature(
    secret: bytes,
    *,
    device_id: UUID,
    timestamp: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    """Return the HMAC header signature binding one HTTP request to a device."""

    message = f"{device_id}|{timestamp}|{method.upper()}|{path}|".encode() + body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()

