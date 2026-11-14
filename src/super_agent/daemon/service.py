"""Server-side command queue for BYOM endpoint daemons.

Design trade-off: the command queue, submitted results, and the raw device
secrets used for command signing live in process memory only. Device identity,
capabilities, revocation, and heartbeats are durable in the ``devices`` table.
After a server restart, queued commands and results are dropped and previously
registered devices must re-register to receive new commands; an endpoint that
reconnects simply heartbeats and claims whatever is pending again. This keeps
the daemon stateless and avoids persisting secret material next to the queue.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from super_agent.daemon.protocol import SignedCommand
from super_agent.daemon.registry import DeviceInfo, PersistentDeviceRegistry
from super_agent.errors import PermissionDeniedError, ValidationError

_MAX_RESULT_CHARS = 64 * 1024


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One endpoint submission for a previously issued command."""

    command_id: UUID
    device_id: UUID
    output: str
    error: str | None
    submitted_at: datetime


class DeviceCommandStore:
    """Process-wide, lock-protected queue/result/secret bookkeeping."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[UUID, list[SignedCommand]] = {}
        self._issued: dict[UUID, UUID] = {}
        self._results: dict[UUID, CommandResult] = {}
        self._secrets: dict[UUID, str] = {}
        self.used_commands: dict[UUID, datetime] = {}

    def clear(self) -> None:
        """Drop all in-memory state (used by tests and server shutdown)."""

        with self._lock:
            self._pending.clear()
            self._issued.clear()
            self._results.clear()
            self._secrets.clear()
            self.used_commands.clear()

    def remember_secret(self, device_id: UUID, secret_hex: str) -> None:
        with self._lock:
            self._secrets[device_id] = secret_hex

    def secret_for(self, device_id: UUID) -> str:
        with self._lock:
            secret = self._secrets.get(device_id)
        if secret is None:
            raise PermissionDeniedError(
                "device secret unavailable in this process; re-register the device"
            )
        return secret

    def forget_device(self, device_id: UUID) -> None:
        with self._lock:
            self._secrets.pop(device_id, None)
            self._pending.pop(device_id, None)

    def push(self, command: SignedCommand) -> None:
        with self._lock:
            self._pending.setdefault(command.device_id, []).append(command)
            self._issued[command.command_id] = command.device_id

    def pop_pending(self, device_id: UUID) -> list[SignedCommand]:
        now = datetime.now(UTC)
        with self._lock:
            queued = self._pending.pop(device_id, [])
        return [command for command in queued if command.expires_at > now]

    def device_for(self, command_id: UUID) -> UUID | None:
        with self._lock:
            return self._issued.get(command_id)

    def put_result(self, result: CommandResult) -> CommandResult:
        with self._lock:
            existing = self._results.get(result.command_id)
            if existing is not None:
                return existing
            self._results[result.command_id] = result
            return result

    def result_for(self, command_id: UUID) -> CommandResult | None:
        with self._lock:
            return self._results.get(command_id)


DEFAULT_STORE = DeviceCommandStore()


class DeviceCommandService:
    """Enqueue signed commands, claim them for live devices, accept results."""

    def __init__(
        self,
        session: Session,
        *,
        store: DeviceCommandStore | None = None,
        registry: PersistentDeviceRegistry | None = None,
        online_window_seconds: float = 90.0,
    ) -> None:
        self._session = session
        self._store = store if store is not None else DEFAULT_STORE
        self._registry = registry or PersistentDeviceRegistry(
            session,
            online_window_seconds=online_window_seconds,
            used_commands=self._store.used_commands,
        )

    @property
    def store(self) -> DeviceCommandStore:
        return self._store

    def register_device(self, name: str, capabilities: frozenset[str]) -> tuple[UUID, str]:
        """Persist a device and keep its secret in process memory for signing."""

        device_id, secret_hex = self._registry.register(name, capabilities)
        self._store.remember_secret(device_id, secret_hex)
        return device_id, secret_hex

    def revoke_device(self, device_id: UUID) -> None:
        """Revoke durably and drop queued commands plus the cached secret."""

        self._registry.revoke(device_id)
        self._store.forget_device(device_id)

    def enqueue_command(
        self,
        device_id: UUID,
        capability: str,
        payload: dict[str, object],
        *,
        ttl_seconds: float = 30.0,
    ) -> SignedCommand:
        """Sign one bounded command and queue it for the device to claim."""

        secret_hex = self._store.secret_for(device_id)
        command = self._registry.sign(
            device_id, secret_hex, capability, payload, ttl_seconds=ttl_seconds
        )
        self._store.push(command)
        return command

    def claim_pending(self, device_id: UUID) -> tuple[SignedCommand, ...]:
        """Hand out pending commands only to a live, non-revoked device.

        Each command is consumed server-side at claim time so a re-enqueued
        or replayed envelope can never be handed out twice.
        """

        info = self._registry.get(device_id)
        if info is None or info.revoked:
            raise PermissionDeniedError("device is unknown or revoked")
        if not self._registry.is_online(device_id):
            raise PermissionDeniedError("device is offline; heartbeat first")
        secret_hex = self._store.secret_for(device_id)
        claimed: list[SignedCommand] = []
        for command in self._store.pop_pending(device_id):
            self._registry.verify_and_consume(command, secret_hex)
            claimed.append(command)
        return tuple(claimed)

    def submit_result(
        self, command_id: UUID, output: str, error: str | None = None
    ) -> CommandResult:
        """Record one endpoint result; repeat submissions stay idempotent."""

        device_id = self._store.device_for(command_id)
        if device_id is None:
            raise ValidationError("unknown command id")
        bounded_output = output[:_MAX_RESULT_CHARS]
        bounded_error = error[:_MAX_RESULT_CHARS] if error is not None else None
        result = CommandResult(
            command_id=command_id,
            device_id=device_id,
            output=bounded_output,
            error=bounded_error,
            submitted_at=datetime.now(UTC),
        )
        return self._store.put_result(result)

    def get_result(self, command_id: UUID) -> CommandResult | None:
        """Return the submitted result for polling callers, if any."""

        return self._store.result_for(command_id)

    def heartbeat(self, device_id: UUID) -> DeviceInfo:
        """Renew the device online window after header authentication."""

        return self._registry.renew_lease(device_id)

    def authenticate_request(
        self,
        device_id: UUID,
        *,
        timestamp: str,
        method: str,
        path: str,
        body: bytes,
        signature_hex: str,
    ) -> None:
        """Verify daemon header HMACs without ever loading the raw secret."""

        self._registry.authenticate_request(
            device_id,
            timestamp=timestamp,
            method=method,
            path=path,
            body=body,
            signature_hex=signature_hex,
        )

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        """Return device metadata without any secret material."""

        return self._registry.list_devices()

    def is_online(self, device_id: UUID) -> bool:
        """Report device liveness from the durable heartbeat window."""

        return self._registry.is_online(device_id)