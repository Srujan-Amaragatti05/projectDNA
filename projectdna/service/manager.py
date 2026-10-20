"""Service lifecycle manager for starting, stopping, and probing the background service."""

import os
import subprocess
import sys
import time
from typing import Optional

from projectdna.service.ipc import (
    send_ipc_request,
    read_service_state,
    remove_service_state,
    ServiceUnavailableError,
)


def is_service_running(probe_timeout: float = 1.0) -> bool:
    """
    Check if the background service is actively running and responding to authenticated IPC.
    Cleans up stale state files if process is dead or unresponsive.
    """
    state = read_service_state()
    if not state:
        return False

    try:
        data = send_ipc_request("ping", timeout=probe_timeout)
        return data.get("message") == "pong"
    except (ServiceUnavailableError, Exception):
        # Stale state file or dead service
        remove_service_state()
        return False


def start_service_detached(timeout: float = 5.0) -> bool:
    """
    Spawn the background service daemon as an independent detached process.
    Waits up to timeout seconds for the service to become ready.
    """
    if is_service_running():
        return True

    cmd = [sys.executable, "-m", "projectdna.service.daemon"]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    # Run detached in background
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )

    # Poll until ready
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_service_running(probe_timeout=0.5):
            return True
        time.sleep(0.2)

    return False


def stop_service(timeout: float = 5.0) -> bool:
    """
    Gracefully stop the running background service via IPC.
    Waits up to timeout seconds for termination.
    """
    if not is_service_running():
        remove_service_state()
        return True

    try:
        send_ipc_request("stop", timeout=2.0)
    except Exception:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_service_running(probe_timeout=0.5):
            return True
        time.sleep(0.2)

    remove_service_state()
    return False


def ensure_service_running(timeout: float = 5.0) -> None:
    """
    Ensure the background service is running; spawn detached if needed.
    Raises ServiceUnavailableError if the service fails to start.
    """
    if not is_service_running():
        success = start_service_detached(timeout=timeout)
        if not success:
            raise ServiceUnavailableError(
                "ProjectDNA background service could not be started. "
                "Ensure no orphaned writer lock exists and try again."
            )
