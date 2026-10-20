"""Unit and integration tests for localhost TCP IPC server and client."""

import json
import socket
import time
from pathlib import Path
import pytest

from projectdna.service.ipc import (
    IPCServer,
    send_ipc_request,
    write_service_state,
    remove_service_state,
    read_service_state,
    ServiceUnavailableError,
    IPCAuthenticationError,
    IPCProtocolError,
    MAX_PAYLOAD_SIZE,
)


def test_ipc_roundtrip(monkeypatch, tmp_path: Path):
    """Authenticated IPC roundtrip request and response."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    def dummy_handler(action: str, payload: dict) -> dict:
        if action == "echo":
            return {"echoed": payload.get("text")}
        raise ValueError(f"Unknown action {action}")

    server = IPCServer(handler=dummy_handler)
    server.start()

    write_service_state(port=server.port, token=server.token, pid=1234)

    try:
        response = send_ipc_request("echo", {"text": "hello_projectdna"})
        assert response.get("echoed") == "hello_projectdna"
    finally:
        server.stop()
        remove_service_state()


def test_ipc_authentication_rejection(monkeypatch, tmp_path: Path):
    """Requests with invalid authentication tokens are rejected."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    server = IPCServer(handler=lambda a, p: {"status": "ok"})
    server.start()

    # Write wrong token in service.json
    write_service_state(port=server.port, token="invalid_token_hex_string", pid=1234)

    try:
        with pytest.raises(IPCAuthenticationError) as exc_info:
            send_ipc_request("ping")
        assert "Authentication" in str(exc_info.value)
    finally:
        server.stop()
        remove_service_state()


def test_ipc_service_unavailable(monkeypatch, tmp_path: Path):
    """When service is not running or port is unreachable, ServiceUnavailableError is raised."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    # Case 1: No service.json
    remove_service_state()
    with pytest.raises(ServiceUnavailableError):
        send_ipc_request("ping")

    # Case 2: Dead port - raises ServiceUnavailableError and unlinks stale state file
    write_service_state(port=59999, token="abc", pid=99999)
    state_file = tmp_path / "service.json"
    assert state_file.exists()
    with pytest.raises(ServiceUnavailableError):
        send_ipc_request("ping")
    assert not state_file.exists(), "Stale service state file was not unlinked on connection failure"


def test_ipc_payload_size_limit(monkeypatch, tmp_path: Path):
    """Oversized payloads exceeding MAX_PAYLOAD_SIZE are rejected."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    server = IPCServer(handler=lambda a, p: {})
    server.start()
    write_service_state(port=server.port, token=server.token, pid=1234)

    try:
        huge_payload = {"data": "x" * (MAX_PAYLOAD_SIZE + 100)}
        with pytest.raises(IPCProtocolError) as exc_info:
            send_ipc_request("huge", huge_payload)
        assert "exceeds max limit" in str(exc_info.value)
    finally:
        server.stop()
        remove_service_state()


def test_ipc_malformed_and_unauthenticated_requests(monkeypatch, tmp_path: Path):
    """Server rejects unauthenticated, malformed JSON, and non-object requests."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    server = IPCServer(handler=lambda a, p: {"result": "ok"})
    server.start()

    def _send_raw(raw_bytes: bytes) -> dict:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect((server.host, server.port))
        try:
            sock.sendall(raw_bytes)
            res = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                res.extend(chunk)
                if b"\n" in res:
                    break
            line = res.split(b"\n")[0].decode("utf-8")
            return json.loads(line)
        finally:
            sock.close()

    try:
        # 1. Malformed JSON
        r1 = _send_raw(b"this is not json\n")
        assert r1["status"] == "error"
        assert r1["code"] == "BAD_REQUEST"

        # 2. Non-dict JSON (e.g. array or primitive)
        r2 = _send_raw(b"[1, 2, 3]\n")
        assert r2["status"] == "error"
        assert r2["code"] == "BAD_REQUEST"

        # 3. Missing/invalid token
        r3 = _send_raw(json.dumps({"token": "wrong_token", "action": "ping"}).encode("utf-8") + b"\n")
        assert r3["status"] == "error"
        assert r3["code"] == "AUTH_FAILED"

        # 4. Valid token but invalid schema (action is integer)
        r4 = _send_raw(json.dumps({"token": server.token, "action": 12345}).encode("utf-8") + b"\n")
        assert r4["status"] == "error"
        assert r4["code"] == "BAD_SCHEMA"

        # 5. Valid request succeeds
        r5 = _send_raw(json.dumps({"token": server.token, "action": "test", "payload": {}}).encode("utf-8") + b"\n")
        assert r5["status"] == "ok"
        assert r5["data"]["result"] == "ok"

    finally:
        server.stop()


def test_service_state_atomic_write_and_read(monkeypatch, tmp_path: Path):
    """write_service_state writes atomically and read_service_state reads valid state."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path))

    state_file = tmp_path / "service.json"
    assert not state_file.exists()

    write_service_state(port=12345, token="tok123", pid=9876)
    assert state_file.exists()

    state = read_service_state()
    assert state is not None
    assert state["port"] == 12345
    assert state["token"] == "tok123"
    assert state["pid"] == 9876

    remove_service_state()
    assert not state_file.exists()
