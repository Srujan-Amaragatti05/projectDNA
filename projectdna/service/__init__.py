"""ProjectDNA background service package."""

from projectdna.service.daemon import ProjectDNAService
from projectdna.service.ipc import (
    IPCServer,
    send_ipc_request,
    read_service_state,
    ServiceUnavailableError,
    IPCAuthenticationError,
)
from projectdna.service.manager import (
    is_service_running,
    start_service_detached,
    stop_service,
    ensure_service_running,
)

__all__ = [
    "ProjectDNAService",
    "IPCServer",
    "send_ipc_request",
    "read_service_state",
    "ServiceUnavailableError",
    "IPCAuthenticationError",
    "is_service_running",
    "start_service_detached",
    "stop_service",
    "ensure_service_running",
]
