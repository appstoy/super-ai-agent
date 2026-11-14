"""Stateless BYOM endpoint daemon (article §5.2).

The daemon keeps no session state: it heartbeats, claims HMAC-signed commands,
executes them inside one workspace root, and posts results back. All durable
conversation state stays on the server. HTTP uses only ``urllib.request`` so
the endpoint needs no runtime dependencies beyond the standard library.

The secret is read from configuration (the CLI loads it from an environment
variable) and is used solely to derive signing keys; it is never logged or
included in any result payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID

from super_agent.daemon.protocol import SignedCommand, command_signature, request_signature
from super_agent.errors import PermissionDeniedError, ValidationError

# (method, path, body, headers) -> (status_code, decoded_json)
Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, object]]

_MAX_OUTPUT_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Endpoint configuration; an empty allowlist means read-only capability."""

    server_url: str
    device_id: UUID
    secret_hex: str
    workspace_root: Path
    command_allowlist: frozenset[str] = frozenset()
    poll_interval: float = 2.0
    command_timeout_seconds: float = 30.0
    http_timeout_seconds: float = 10.0
    max_backoff_seconds: float = 60.0


def _urllib_transport(timeout: float) -> Transport:
    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, object]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            decoded: object = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {}
        return status, decoded

    return send


class EndpointDaemon:
    """Poll one server for signed commands and execute them locally."""

    def __init__(self, config: DaemonConfig, *, transport: Transport | None = None) -> None:
        if config.poll_interval <= 0 or config.command_timeout_seconds <= 0:
            raise ValueError("daemon intervals must be positive")
        try:
            secret = bytes.fromhex(config.secret_hex)
        except ValueError as exc:
            raise ValidationError("device secret must be hex") from exc
        if not secret:
            raise ValidationError("device secret must not be empty")
        self._config = config
        # The server persists sha256(secret); header HMACs use that same digest
        # so the raw secret never needs to leave this process.
        self._header_key = hashlib.sha256(secret).digest()
        self._root = config.workspace_root.resolve()
        self._transport = transport or _urllib_transport(config.http_timeout_seconds)
        self.last_error: str | None = None

    @property
    def capabilities(self) -> frozenset[str]:
        """Local capability grant; exec requires a non-empty allowlist."""

        if self._config.command_allowlist:
            return frozenset({"device.read", "device.exec"})
        return frozenset({"device.read"})

    def run_once(self) -> int:
        """Run one heartbeat/claim/execute/report cycle; return commands run.

        Network and server errors are recorded in ``last_error`` instead of
        raising so an intermittently reachable server never kills the daemon.
        """

        self.last_error = None
        device = self._config.device_id
        status, _ = self._signed_request("POST", f"/api/devices/{device}/heartbeat", {})
        if status != 200:
            if status != 0:  # 0 means the transport already recorded the error
                self.last_error = f"heartbeat rejected with status {status}"
            return 0
        status, payload = self._signed_request("GET", f"/api/devices/{device}/commands", None)
        if status != 200:
            if status != 0:
                self.last_error = f"command claim rejected with status {status}"
            return 0
        commands = payload.get("commands") if isinstance(payload, dict) else None
        if not isinstance(commands, list):
            self.last_error = "command claim response is malformed"
            return 0
        processed = 0
        for raw in commands:
            command_id = raw.get("command_id") if isinstance(raw, dict) else None
            try:
                command = self._parse_command(raw)
                output, error = self._execute(command)
            except (PermissionDeniedError, ValidationError, ValueError, KeyError, TypeError) as exc:
                # A rejected envelope still reports back so the server-side
                # waiter observes the denial instead of polling until timeout.
                if not isinstance(command_id, str):
                    continue
                self._post_result(command_id, "", str(exc))
                processed += 1
                continue
            self._post_result(str(command.command_id), output, error)
            processed += 1
        return processed

    def run_forever(self, stop: Event | None = None) -> None:
        """Poll until stopped, backing off exponentially on failures."""

        stop_event = stop or Event()
        backoff = self._config.poll_interval
        while not stop_event.is_set():
            processed = self.run_once()
            if self.last_error is None:
                backoff = self._config.poll_interval
            else:
                backoff = min(backoff * 2, self._config.max_backoff_seconds)
            delay = self._config.poll_interval if processed else backoff
            stop_event.wait(delay)

    def _signed_request(
        self, method: str, path: str, payload: dict[str, object] | None
    ) -> tuple[int, object]:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        )
        timestamp = str(int(time.time()))
        signature = request_signature(
            self._header_key,
            device_id=self._config.device_id,
            timestamp=timestamp,
            method=method,
            path=path,
            body=body or b"",
        )
        headers = {
            "Content-Type": "application/json",
            "X-Device-Timestamp": timestamp,
            "X-Device-Signature": signature,
        }
        url = self._config.server_url.rstrip("/") + path
        try:
            return self._transport(method, url, body, headers)
        except OSError as exc:
            # Status 0 marks a transport failure; run_once keeps this message.
            self.last_error = f"network failure: {exc}"
            return 0, {}

    def _parse_command(self, raw: object) -> SignedCommand:
        if not isinstance(raw, dict):
            raise ValidationError("command envelope is malformed")
        expires_raw = raw["expires_at"]
        if not isinstance(expires_raw, str):
            raise ValidationError("command expiry is malformed")
        expires_at = datetime.fromisoformat(expires_raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        payload = raw["payload"]
        if not isinstance(payload, dict):
            raise ValidationError("command payload is malformed")
        return SignedCommand(
            command_id=UUID(str(raw["command_id"])),
            device_id=UUID(str(raw["device_id"])),
            capability=str(raw["capability"]),
            payload=payload,
            expires_at=expires_at,
            signature=str(raw["signature"]),
        )

    def _execute(self, command: SignedCommand) -> tuple[str, str | None]:
        if command.device_id != self._config.device_id:
            raise PermissionDeniedError("command targets another device")
        self._verify(command)
        if command.capability not in {"device.read", "device.exec"}:
            raise PermissionDeniedError("unsupported capability")
        try:
            if command.capability == "device.exec":
                # The allowlist check inside _exec gives the precise denial
                # reason even when the endpoint runs in read-only mode.
                return self._exec(command.payload), None
            return self._read(command.payload), None
        except PermissionDeniedError as exc:
            return "", str(exc)
        except (ValidationError, subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
            return "", str(exc)

    def _verify(self, command: SignedCommand) -> None:
        if command.expires_at <= datetime.now(UTC):
            raise PermissionDeniedError("command expired")
        expected = command_signature(command, bytes.fromhex(self._config.secret_hex))
        if not hmac.compare_digest(expected, command.signature):
            raise PermissionDeniedError("invalid command signature")

    def _resolve_inside(self, raw_path: str) -> Path:
        candidate = (self._root / raw_path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise PermissionDeniedError("path is outside the workspace root")
        return candidate

    def _read(self, payload: dict[str, object]) -> str:
        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValidationError("device.read requires a path")
        path = self._resolve_inside(raw_path)
        if not path.is_file():
            raise ValidationError("device.read path is not a regular file")
        data = path.read_bytes()[:_MAX_OUTPUT_BYTES]
        return data.decode("utf-8", errors="ignore")

    def _exec(self, payload: dict[str, object]) -> str:
        command = payload.get("command")
        args = payload.get("args", [])
        if not isinstance(command, str) or command not in self._config.command_allowlist:
            raise PermissionDeniedError("command is not allowlisted")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValidationError("command args must be a list of strings")
        cwd_raw = payload.get("cwd")
        cwd = self._root
        if cwd_raw is not None:
            if not isinstance(cwd_raw, str):
                raise ValidationError("command cwd must be a string")
            cwd = self._resolve_inside(cwd_raw)
        completed = subprocess.run(
            [command, *args],
            cwd=cwd,
            capture_output=True,
            shell=False,
            timeout=self._config.command_timeout_seconds,
            check=False,
        )
        stdout = completed.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        stderr = completed.stderr[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        return f"exit={completed.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"

    def _post_result(self, command_id: str, output: str, error: str | None) -> None:
        payload: dict[str, object] = {"command_id": command_id, "output": output, "error": error}
        status, _ = self._signed_request(
            "POST", f"/api/devices/{self._config.device_id}/results", payload
        )
        if status != 200 and status != 0:
            self.last_error = f"result submission rejected with status {status}"