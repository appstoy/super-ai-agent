from datetime import UTC, datetime, timedelta

import pytest

from super_agent.daemon.protocol import DeviceRegistry, SignedCommand
from super_agent.errors import PermissionDeniedError


def test_device_command_is_signed_and_single_use() -> None:
    registry = DeviceRegistry()
    device, secret = registry.register(frozenset({"file.read"}))
    command = registry.sign(device.device_id, secret, "file.read", {"path": "README.md"})

    assert registry.verify_and_consume(command, secret) == {"path": "README.md"}
    with pytest.raises(PermissionDeniedError, match="replay"):
        registry.verify_and_consume(command, secret)


def test_device_rejects_capability_tampering_and_revocation() -> None:
    registry = DeviceRegistry()
    device, secret = registry.register(frozenset({"file.read"}))
    command = registry.sign(device.device_id, secret, "file.read", {"path": "README.md"})
    tampered = SignedCommand(
        command.command_id,
        command.device_id,
        "command.run",
        command.payload,
        command.expires_at,
        command.signature,
    )
    with pytest.raises(PermissionDeniedError, match="capability"):
        registry.verify_and_consume(tampered, secret)
    registry.revoke(device.device_id)
    with pytest.raises(PermissionDeniedError, match="revoked"):
        registry.verify_and_consume(command, secret)


def test_device_rejects_expired_commands() -> None:
    registry = DeviceRegistry()
    device, secret = registry.register(frozenset({"file.read"}))
    command = registry.sign(device.device_id, secret, "file.read", {"path": "README.md"})
    expired = SignedCommand(
        command.command_id,
        command.device_id,
        command.capability,
        command.payload,
        datetime.now(UTC) - timedelta(seconds=1),
        command.signature,
    )
    with pytest.raises(PermissionDeniedError, match="expired"):
        registry.verify_and_consume(expired, secret)

