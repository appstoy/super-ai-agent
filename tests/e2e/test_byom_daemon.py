"""Full BYOM chain: register -> sign -> claim -> execute -> report via TestClient.

The EndpointDaemon runs against the real FastAPI app through an in-process
urllib-shaped transport, so HMAC headers, replay protection, workspace
sandboxing, and secret hygiene are all exercised end to end.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.daemon.agent import DaemonConfig, EndpointDaemon
from super_agent.daemon.protocol import SignedCommand, request_signature
from super_agent.daemon.service import DeviceCommandService, DeviceCommandStore
from super_agent.errors import PermissionDeniedError, ValidationError
from super_agent.storage.database import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite:///{tmp_path / 'byom.db'}"
    app = create_app(Settings(database_url=database_url, workspace_root=str(tmp_path)))
    with TestClient(app) as test_client:
        yield test_client


def _make_daemon(
    client: TestClient,
    device_id: UUID,
    secret: str,
    workspace: Path,
    *,
    allow: frozenset[str] = frozenset(),
) -> EndpointDaemon:
    def transport(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, object]:
        parts = urlsplit(url)
        response = client.request(method, parts.path, content=body, headers=headers)
        try:
            decoded: object = response.json()
        except json.JSONDecodeError:
            decoded = {}
        return response.status_code, decoded

    config = DaemonConfig(
        server_url="http://testserver",
        device_id=device_id,
        secret_hex=secret,
        workspace_root=workspace,
        command_allowlist=allow,
        poll_interval=0.05,
    )
    return EndpointDaemon(config, transport=transport)


def _register(client: TestClient, capabilities: list[str]) -> tuple[UUID, str]:
    response = client.post(
        "/api/devices/register", json={"name": "e2e-box", "capabilities": capabilities}
    )
    assert response.status_code == 201
    payload = response.json()
    return UUID(payload["device_id"]), payload["secret"]


def _enqueue(client: TestClient, device_id: UUID, capability: str, payload: dict) -> UUID:
    response = client.post(
        f"/api/devices/{device_id}/commands/enqueue",
        json={"capability": capability, "payload": payload, "ttl_seconds": 30},
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["command_id"])


def _python_payload(args: list[str]) -> dict[str, object]:
    return {"command": sys.executable, "args": args}


def _auth_headers(secret: str, device_id: UUID, method: str, path: str, body: bytes) -> dict:
    timestamp = str(int(time.time()))
    key = hashlib.sha256(bytes.fromhex(secret)).digest()
    signature = request_signature(
        key, device_id=device_id, timestamp=timestamp, method=method, path=path, body=body
    )
    return {
        "Content-Type": "application/json",
        "X-Device-Timestamp": timestamp,
        "X-Device-Signature": signature,
    }


def test_full_chain_read_then_exec(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello byom", encoding="utf-8")
    device_id, secret = _register(client, ["device.read", "device.exec"])
    read_id = _enqueue(client, device_id, "device.read", {"path": "notes.txt"})
    exec_id = _enqueue(
        client, device_id, "device.exec", _python_payload(["-c", "print('exec-ok')"])
    )
    daemon = _make_daemon(client, device_id, secret, workspace, allow=frozenset({sys.executable}))
    assert daemon.run_once() == 2
    assert daemon.last_error is None

    read_result = client.get(f"/api/devices/{device_id}/results/{read_id}").json()
    assert read_result["status"] == "completed"
    assert read_result["output"] == "hello byom"
    assert read_result["error"] is None
    exec_result = client.get(f"/api/devices/{device_id}/results/{exec_id}").json()
    assert "exec-ok" in exec_result["output"]
    assert "exit=0" in exec_result["output"]

    listed = client.get("/api/devices").json()["devices"]
    entry = next(item for item in listed if item["device_id"] == str(device_id))
    assert entry["online"] is True
    assert entry["revoked"] is False


def test_secret_never_leaks_through_api(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "f.txt").write_text("content", encoding="utf-8")
    device_id, secret = _register(client, ["device.read"])
    command_id = _enqueue(client, device_id, "device.read", {"path": "f.txt"})
    daemon = _make_daemon(client, device_id, secret, workspace)
    assert daemon.run_once() == 1

    responses = [
        client.get("/api/devices"),
        client.get(f"/api/devices/{device_id}/results/{command_id}"),
    ]
    for response in responses:
        assert response.status_code == 200
        assert secret not in response.text
        assert "secret_hash" not in response.text
    # The only place the secret ever appears is the one-time registration reply.
    register_again = client.post(
        "/api/devices/register", json={"name": "second", "capabilities": []}
    )
    assert register_again.json()["secret"] != secret


def test_path_traversal_is_refused_and_reported(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    device_id, secret = _register(client, ["device.read"])
    command_id = _enqueue(client, device_id, "device.read", {"path": "../outside.txt"})
    daemon = _make_daemon(client, device_id, secret, workspace)
    assert daemon.run_once() == 1
    result = client.get(f"/api/devices/{device_id}/results/{command_id}").json()
    assert result["output"] == ""
    assert result["error"] is not None
    assert "outside" in result["error"]
    assert "private" not in json.dumps(result)
    assert outside.read_text(encoding="utf-8") == "private"


def test_exec_outside_allowlist_is_denied(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    device_id, secret = _register(client, ["device.read", "device.exec"])
    command_id = _enqueue(client, device_id, "device.exec", _python_payload(["-c", "print(1)"]))
    daemon = _make_daemon(client, device_id, secret, workspace)  # empty allowlist: read-only
    assert daemon.run_once() == 1
    result = client.get(f"/api/devices/{device_id}/results/{command_id}").json()
    assert "allowlist" in (result["error"] or "")


def test_exec_cwd_escape_is_denied(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    device_id, secret = _register(client, ["device.exec"])
    command_id = _enqueue(
        client,
        device_id,
        "device.exec",
        {"command": sys.executable, "args": ["-c", "print(1)"], "cwd": ".."},
    )
    daemon = _make_daemon(client, device_id, secret, workspace, allow=frozenset({sys.executable}))
    assert daemon.run_once() == 1
    result = client.get(f"/api/devices/{device_id}/results/{command_id}").json()
    assert "outside" in (result["error"] or "")


def test_tampered_command_signature_never_executes(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = workspace / "marker.txt"
    device_id, secret = _register(client, ["device.exec"])
    _enqueue(
        client,
        device_id,
        "device.exec",
        _python_payload(
            ["-c", "from pathlib import Path; Path('marker.txt').write_text('pwned')"]
        ),
    )
    daemon = _make_daemon(client, device_id, secret, workspace, allow=frozenset({sys.executable}))
    original = daemon._transport

    def tampering(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, object]:
        status, payload = original(method, url, body, headers)
        if url.endswith("/commands") and isinstance(payload, dict):
            commands = payload.get("commands")
            if isinstance(commands, list):
                for command in commands:
                    command["signature"] = "00" * 32
        return status, payload

    daemon._transport = tampering
    assert daemon.run_once() == 1
    assert not marker.exists()


def test_expired_command_is_refused_locally(tmp_path: Path) -> None:
    config = DaemonConfig(
        server_url="http://x",
        device_id=uuid4(),
        secret_hex="ab" * 32,
        workspace_root=tmp_path,
    )
    daemon = EndpointDaemon(config, transport=lambda *args: (200, {}))
    expired = SignedCommand(
        uuid4(),
        config.device_id,
        "device.read",
        {"path": "x.txt"},
        datetime.now(UTC) - timedelta(seconds=5),
        "00" * 32,
    )
    with pytest.raises(PermissionDeniedError, match="expired"):
        daemon._execute(expired)


def test_foreign_device_command_is_refused(tmp_path: Path) -> None:
    config = DaemonConfig(
        server_url="http://x",
        device_id=uuid4(),
        secret_hex="ab" * 32,
        workspace_root=tmp_path,
    )
    daemon = EndpointDaemon(config, transport=lambda *args: (200, {}))
    foreign = SignedCommand(
        uuid4(),
        uuid4(),
        "device.read",
        {"path": "x.txt"},
        datetime.now(UTC) + timedelta(seconds=30),
        "00" * 32,
    )
    with pytest.raises(PermissionDeniedError, match="another device"):
        daemon._execute(foreign)


def test_revoked_device_is_rejected(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    device_id, secret = _register(client, ["device.read"])
    _enqueue(client, device_id, "device.read", {"path": "x.txt"})
    store: DeviceCommandStore = client.app.state.device_command_store
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'byom.db'}")
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        DeviceCommandService(session, store=store).revoke_device(device_id)
    engine.dispose()
    daemon = _make_daemon(client, device_id, secret, workspace)
    assert daemon.run_once() == 0
    assert daemon.last_error is not None
    assert "401" in daemon.last_error


def test_heartbeat_and_results_require_valid_hmac(client: TestClient) -> None:
    device_id, secret = _register(client, ["device.read"])
    path = f"/api/devices/{device_id}/heartbeat"
    assert client.post(path, content=b"{}").status_code == 401
    bad = client.post(
        path,
        content=b"{}",
        headers={"X-Device-Timestamp": "1", "X-Device-Signature": "00" * 32},
    )
    assert bad.status_code == 401
    good_headers = _auth_headers(secret, device_id, "POST", path, b"{}")
    assert client.post(path, content=b"{}", headers=good_headers).status_code == 200
    # A signature for another path must not authenticate this one.
    wrong_path_headers = _auth_headers(secret, device_id, "POST", "/other", b"{}")
    assert client.post(path, content=b"{}", headers=wrong_path_headers).status_code == 401


def test_results_from_another_device_are_rejected(client: TestClient) -> None:
    device_a, secret_a = _register(client, ["device.read"])
    device_b, secret_b = _register(client, ["device.read"])
    command_id = _enqueue(client, device_a, "device.read", {"path": "x"})
    body = json.dumps({"command_id": str(command_id), "output": "spoof", "error": None}).encode()
    path = f"/api/devices/{device_b}/results"
    headers = _auth_headers(secret_b, device_b, "POST", path, body)
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 403
    assert secret_a not in response.text


def test_claim_requires_heartbeat_first(client: TestClient) -> None:
    device_id, secret = _register(client, ["device.read"])
    path = f"/api/devices/{device_id}/commands"
    headers = _auth_headers(secret, device_id, "GET", path, b"")
    response = client.get(path, headers=headers)
    assert response.status_code == 403
    assert "offline" in response.json()["detail"]


def test_replayed_claim_returns_nothing(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "f.txt").write_text("one-shot", encoding="utf-8")
    device_id, secret = _register(client, ["device.read"])
    _enqueue(client, device_id, "device.read", {"path": "f.txt"})
    daemon = _make_daemon(client, device_id, secret, workspace)
    assert daemon.run_once() == 1
    assert daemon.run_once() == 0
    assert daemon.last_error is None


def test_result_submission_is_idempotent(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "f.txt").write_text("v1", encoding="utf-8")
    device_id, secret = _register(client, ["device.read"])
    command_id = _enqueue(client, device_id, "device.read", {"path": "f.txt"})
    daemon = _make_daemon(client, device_id, secret, workspace)
    assert daemon.run_once() == 1
    body = json.dumps({"command_id": str(command_id), "output": "v2", "error": None}).encode()
    path = f"/api/devices/{device_id}/results"
    headers = _auth_headers(secret, device_id, "POST", path, body)
    assert client.post(path, content=body, headers=headers).status_code == 200
    result = client.get(f"/api/devices/{device_id}/results/{command_id}").json()
    assert result["output"] == "v1"  # first submission wins


def test_network_failure_does_not_crash(tmp_path: Path) -> None:
    def broken(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, object]:
        raise OSError("connection refused")

    daemon = EndpointDaemon(
        DaemonConfig(
            server_url="http://127.0.0.1:1",
            device_id=uuid4(),
            secret_hex="ab" * 32,
            workspace_root=tmp_path,
        ),
        transport=broken,
    )
    assert daemon.run_once() == 0
    assert daemon.last_error is not None
    assert "network failure" in daemon.last_error


def test_config_rejects_bad_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="hex"):
        EndpointDaemon(
            DaemonConfig(
                server_url="http://x",
                device_id=uuid4(),
                secret_hex="zz",
                workspace_root=tmp_path,
            )
        )


def test_exec_timeout_is_reported(client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    device_id, secret = _register(client, ["device.exec"])
    command_id = _enqueue(
        client,
        device_id,
        "device.exec",
        _python_payload(["-c", "import time; time.sleep(5)"]),
    )
    daemon = _make_daemon(client, device_id, secret, workspace, allow=frozenset({sys.executable}))
    daemon._config = DaemonConfig(
        server_url="http://testserver",
        device_id=device_id,
        secret_hex=secret,
        workspace_root=workspace,
        command_allowlist=frozenset({sys.executable}),
        command_timeout_seconds=0.5,
    )
    assert daemon.run_once() == 1
    result = client.get(f"/api/devices/{device_id}/results/{command_id}").json()
    assert result["error"] is not None
    assert "timed out" in result["error"].lower()