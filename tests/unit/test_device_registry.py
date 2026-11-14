"""Persistent BYOM device registry: HMAC semantics, replay, revocation, secrets."""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from super_agent.daemon.protocol import SignedCommand, request_signature
from super_agent.daemon.registry import PersistentDeviceRegistry
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.storage.database import create_schema, create_session_factory
from super_agent.storage.models import DeviceRecord


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    # StaticPool keeps one connection so the schema and heartbeats are shared.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_schema(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _registry(session: Session) -> PersistentDeviceRegistry:
    return PersistentDeviceRegistry(session)


def test_register_stores_only_hash_and_returns_secret_once(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret_hex = registry.register("dev-box", ["device.read", "device.exec"])
        row = session.scalar(select(DeviceRecord).where(DeviceRecord.id == device_id))
        assert row is not None
        assert row.secret_hash == hashlib.sha256(bytes.fromhex(secret_hex)).hexdigest()
        assert secret_hex not in row.secret_hash
        assert row.revoked == 0
        info = registry.get(device_id)
        assert info is not None
        assert info.capabilities == frozenset({"device.read", "device.exec"})
        assert "secret" not in {field.name for field in dataclasses.fields(info)}


def test_register_rejects_empty_name(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        with pytest.raises(ValidationError, match="name"):
            registry.register("  ", ["device.read"])


def test_sign_verify_and_replay_protection(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {"path": "a.txt"})
        assert registry.verify_and_consume(command, secret) == {"path": "a.txt"}
        with pytest.raises(PermissionDeniedError, match="replay"):
            registry.verify_and_consume(command, secret)


def test_shared_used_dict_blocks_replay_across_instances(
    factory: sessionmaker[Session],
) -> None:
    used: dict = {}
    with factory() as session, session.begin():
        first = PersistentDeviceRegistry(session, used_commands=used)
        device_id, secret = first.register("dev", ["device.read"])
        command = first.sign(device_id, secret, "device.read", {"path": "a.txt"})
        first.verify_and_consume(command, secret)
    with factory() as session, session.begin():
        second = PersistentDeviceRegistry(session, used_commands=used)
        with pytest.raises(PermissionDeniedError, match="replay"):
            second.verify_and_consume(command, secret)


def test_tampered_signature_and_capability_are_denied(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {"path": "a.txt"})
        tampered = SignedCommand(
            command.command_id,
            command.device_id,
            command.capability,
            {"path": "other.txt"},
            command.expires_at,
            command.signature,
        )
        with pytest.raises(PermissionDeniedError, match="signature"):
            registry.verify_and_consume(tampered, secret)
        escalated = SignedCommand(
            command.command_id,
            command.device_id,
            "device.exec",
            command.payload,
            command.expires_at,
            command.signature,
        )
        with pytest.raises(PermissionDeniedError, match="capability"):
            registry.verify_and_consume(escalated, secret)


def test_expired_command_and_ttl_policy(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {"path": "a.txt"})
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
        with pytest.raises(ValidationError, match="TTL"):
            registry.sign(device_id, secret, "device.read", {}, ttl_seconds=0)
        with pytest.raises(ValidationError, match="TTL"):
            registry.sign(device_id, secret, "device.read", {}, ttl_seconds=61)


def test_revocation_blocks_sign_verify_and_lease(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {"path": "a.txt"})
        registry.revoke(device_id)
        with pytest.raises(PermissionDeniedError, match="revoked"):
            registry.verify_and_consume(command, secret)
        with pytest.raises(PermissionDeniedError, match="capability"):
            registry.sign(device_id, secret, "device.read", {})
        with pytest.raises(PermissionDeniedError, match="revoked"):
            registry.renew_lease(device_id)
        assert not registry.is_online(device_id)


def test_wrong_secret_is_denied(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {})
        with pytest.raises(PermissionDeniedError, match="secret"):
            registry.verify_and_consume(command, "aa" * 32)
        with pytest.raises(PermissionDeniedError, match="secret"):
            registry.sign(device_id, "not-hex", "device.read", {})


def test_unknown_device_is_denied(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        with pytest.raises(PermissionDeniedError, match="unknown"):
            registry.renew_lease(uuid4())
        assert registry.get(uuid4()) is None


def test_online_window_follows_heartbeat(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = PersistentDeviceRegistry(session, online_window_seconds=60)
        device_id, _ = registry.register("dev", ["device.read"])
        assert not registry.is_online(device_id)
        registry.renew_lease(device_id)
        assert registry.is_online(device_id)
        stale = datetime.now(UTC) - timedelta(seconds=120)
        assert not registry.is_online(device_id, now=stale + timedelta(seconds=180))


def test_authenticate_request_uses_stored_hash(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        timestamp = str(int(datetime.now(UTC).timestamp()))
        body = b'{"hello": 1}'
        key = hashlib.sha256(bytes.fromhex(secret)).digest()
        signature = request_signature(
            key,
            device_id=device_id,
            timestamp=timestamp,
            method="POST",
            path=f"/api/devices/{device_id}/heartbeat",
            body=body,
        )
        registry.authenticate_request(
            device_id,
            timestamp=timestamp,
            method="POST",
            path=f"/api/devices/{device_id}/heartbeat",
            body=body,
            signature_hex=signature,
        )
        with pytest.raises(PermissionDeniedError, match="signature"):
            registry.authenticate_request(
                device_id,
                timestamp=timestamp,
                method="POST",
                path=f"/api/devices/{device_id}/heartbeat",
                body=body,
                signature_hex="00" * 32,
            )
        with pytest.raises(PermissionDeniedError, match="timestamp"):
            registry.authenticate_request(
                device_id,
                timestamp="1",
                method="POST",
                path=f"/api/devices/{device_id}/heartbeat",
                body=body,
                signature_hex=signature,
            )


def test_verify_local_checks_signature_without_db(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        registry = _registry(session)
        device_id, secret = registry.register("dev", ["device.read"])
        command = registry.sign(device_id, secret, "device.read", {"path": "x"})
        assert registry.verify_local(command, secret) == {"path": "x"}
        # Local verification does not consume; the server keeps replay state.
        assert registry.verify_local(command, secret) == {"path": "x"}
        bad = SignedCommand(
            command.command_id,
            command.device_id,
            command.capability,
            command.payload,
            command.expires_at,
            "ff" * 32,
        )
        with pytest.raises(PermissionDeniedError, match="signature"):
            registry.verify_local(bad, secret)