"""Localhost TCP IPC server and client for ProjectDNA."""

import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from projectdna.config import get_service_state_path

MAX_PAYLOAD_SIZE = 1024 * 1024  # 1 MB max payload
DEFAULT_IPC_TIMEOUT = 10.0


class ServiceUnavailableError(Exception):
    """Raised when the ProjectDNA background service is unreachable or not running."""
    pass


class IPCAuthenticationError(Exception):
    """Raised when an IPC request fails authentication."""
    pass


class IPCProtocolError(Exception):
    """Raised when an IPC request or response violates protocol specifications."""
    pass


def read_service_state() -> Optional[Dict[str, Any]]:
    """Read service state file if it exists and is valid JSON."""
    state_path = get_service_state_path()
    if not state_path.exists():
        return None
    try:
        content = state_path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        return json.loads(content)
    except Exception:
        return None


def write_service_state(port: int, token: str, pid: int) -> None:
    """Write service state to service.json with atomic replacement and secure permissions."""
    state_path = get_service_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".json.tmp")

    state_data = {
        "host": "127.0.0.1",
        "port": port,
        "token": token,
        "pid": pid,
        "started_at": time.time(),
    }
    temp_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, state_path)
    try:
        os.chmod(state_path, 0o600)
    except OSError:
        pass


def remove_service_state() -> None:
    """Safely remove service state file."""
    state_path = get_service_state_path()
    if state_path.exists():
        try:
            state_path.unlink()
        except OSError:
            pass


def send_ipc_request(
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_IPC_TIMEOUT,
) -> Dict[str, Any]:
    """
    Send an authenticated request to the running background service via localhost TCP.
    Returns the parsed response dictionary or raises ServiceUnavailableError / IPCAuthenticationError.
    """
    state = read_service_state()
    if not state:
        raise ServiceUnavailableError(
            "ProjectDNA background service is not running (service state file not found)."
        )

    host = state.get("host", "127.0.0.1")
    port = state.get("port")
    token = state.get("token")

    if not port or not token:
        remove_service_state()
        raise ServiceUnavailableError("Invalid or corrupted ProjectDNA service state file.")

    request_data = {
        "token": token,
        "action": action,
        "payload": payload or {},
    }
    encoded = json.dumps(request_data).encode("utf-8") + b"\n"
    if len(encoded) > MAX_PAYLOAD_SIZE:
        raise IPCProtocolError(f"Request payload exceeds max limit of {MAX_PAYLOAD_SIZE} bytes.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(encoded)

        # Read response line
        response_bytes = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes.extend(chunk)
            if b"\n" in response_bytes or len(response_bytes) > MAX_PAYLOAD_SIZE:
                break

        if not response_bytes:
            raise ServiceUnavailableError("Service closed connection without returning a response.")

        line = response_bytes.split(b"\n")[0].decode("utf-8", errors="replace")
        response = json.loads(line)
    except (ConnectionRefusedError, TimeoutError, socket.timeout) as e:
        remove_service_state()
        raise ServiceUnavailableError(f"Could not connect to ProjectDNA service at {host}:{port}: {e}")
    except json.JSONDecodeError as e:
        raise IPCProtocolError(f"Malformed response from service: {e}")
    finally:
        try:
            sock.close()
        except OSError:
            pass

    status = response.get("status")
    if status == "error":
        err_msg = response.get("message", "Unknown service error")
        code = response.get("code")
        if code == "AUTH_FAILED":
            raise IPCAuthenticationError(f"Authentication rejected by service: {err_msg}")
        raise RuntimeError(err_msg)

    return response.get("data", {})


class IPCServer:
    """
    Threaded localhost TCP server managing IPC request dispatching with token validation.
    """

    def __init__(
        self,
        handler: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        host: str = "127.0.0.1",
        token: Optional[str] = None,
    ):
        self.host = host
        self.token = token or secrets.token_hex(32)
        self.handler = handler

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, 0))
        self._server_sock.listen(16)
        self._server_sock.settimeout(1.0)  # Allows checking self._running periodically
        self.port = self._server_sock.getsockname()[1]

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the IPC server in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="ProjectDNA-IPC-Server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the IPC server and close listening socket."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self._server_sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while self._running:
            try:
                client_sock, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client_thread = threading.Thread(
                target=self._handle_client, args=(client_sock,), daemon=True
            )
            client_thread.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        client_sock.settimeout(5.0)
        try:
            raw_data = bytearray()
            while True:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                raw_data.extend(chunk)
                if b"\n" in raw_data or len(raw_data) > MAX_PAYLOAD_SIZE:
                    break

            if not raw_data:
                return

            if len(raw_data) > MAX_PAYLOAD_SIZE:
                resp = {"status": "error", "message": "Payload exceeds maximum limit", "code": "PAYLOAD_TOO_LARGE"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            line = raw_data.split(b"\n")[0].decode("utf-8", errors="replace")
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                resp = {"status": "error", "message": "Malformed JSON request", "code": "BAD_REQUEST"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            if not isinstance(req, dict):
                resp = {"status": "error", "message": "Request must be a JSON object", "code": "BAD_REQUEST"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            # Validate authentication token using constant-time comparison
            req_token = req.get("token", "")
            if not isinstance(req_token, str) or not secrets.compare_digest(req_token, self.token):
                resp = {"status": "error", "message": "Authentication failed", "code": "AUTH_FAILED"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            action = req.get("action")
            payload = req.get("payload", {})
            if not isinstance(action, str) or not isinstance(payload, dict):
                resp = {"status": "error", "message": "Invalid request schema", "code": "BAD_SCHEMA"}
                client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                return

            # Dispatch action
            try:
                data = self.handler(action, payload)
                resp = {"status": "ok", "data": data}
            except Exception as e:
                resp = {"status": "error", "message": str(e), "code": "HANDLER_ERROR"}

            client_sock.sendall(json.dumps(resp).encode("utf-8") + b"\n")
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except OSError:
                pass
