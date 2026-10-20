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

    # Case 2: Dead port
    write_service_state(port=59999, token="abc", pid=99999)
    with pytest.raises(ServiceUnavailableError):
        send_ipc_request("ping")


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
