"""Configuration and path management for ProjectDNA.

Canonical database ownership policy:
- Default canonical location on Windows: %LOCALAPPDATA%\\ProjectDNA\\
- Under this directory, ProjectDNA stores:
    - dna.db (and SQLite WAL files: dna.db-wal, dna.db-shm)
    - writer.lock (authoritative OS file lock)
    - service.json (local IPC state file)
- Path resolution is strictly independent of the current working directory,
  Python virtual environment, or project paths being indexed.
- The environment variable PROJECTDNA_DATA_DIR serves as an explicit advanced override.
  Configuring different PROJECTDNA_DATA_DIR values intentionally creates distinct,
  isolated data stores (useful for testing or dedicated profiling).
"""

import os
import sys
from pathlib import Path
from typing import Optional


def get_canonical_data_dir() -> Path:
    """
    Get the canonical data directory for the current operating-system user.
    On Windows, defaults to %LOCALAPPDATA%\\ProjectDNA.
    On macOS, defaults to ~/Library/Application Support/ProjectDNA.
    On Linux/POSIX, defaults to $XDG_DATA_HOME/ProjectDNA or ~/.local/share/ProjectDNA.
    
    Resolution is strictly independent of current working directory and virtual environment.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return (Path(local_app_data) / "ProjectDNA").resolve()
        return (Path.home() / "AppData" / "Local" / "ProjectDNA").resolve()
    elif sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "ProjectDNA").resolve()
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return (Path(xdg_data) / "ProjectDNA").resolve()
        return (Path.home() / ".local" / "share" / "ProjectDNA").resolve()


def get_legacy_data_dir() -> Path:
    """Get the legacy data directory (~/.projectdna)."""
    return (Path.home() / ".projectdna").resolve()


def get_data_dir() -> Path:
    """
    Resolve the active data directory for ProjectDNA.
    If the PROJECTDNA_DATA_DIR environment variable is set, it is used as an
    explicit advanced override. Different override values intentionally create
    isolated, distinct data stores.
    Otherwise, returns the canonical data directory for this OS user.
    """
    override = os.environ.get("PROJECTDNA_DATA_DIR")
    if override:
        return Path(override).resolve()
    return get_canonical_data_dir()


def get_db_path() -> Path:
    """Get the path to the SQLite database (dna.db)."""
    return get_data_dir() / "dna.db"


def get_lock_path() -> Path:
    """Get the path to the writer lockfile."""
    return get_data_dir() / "writer.lock"


def get_service_state_path() -> Path:
    """Get the path to the service IPC state file."""
    return get_data_dir() / "service.json"


# Limits
MAX_INDEXABLE_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB limit for individual Python files


def ensure_data_dir(target_dir: Optional[Path] = None) -> Path:
    """Ensure that the target data directory exists."""
    d = (target_dir or get_data_dir()).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


# Dynamic module-level attribute resolution (PEP 562)
# Ensures imports like `from projectdna.config import DB_PATH` dynamically
# reflect the active environment without stale caching.
def __getattr__(name: str):
    if name == "DATA_DIR":
        return get_data_dir()
    if name == "DB_PATH":
        return get_db_path()
    if name == "LOCK_PATH":
        return get_lock_path()
    if name == "SERVICE_STATE_PATH":
        return get_service_state_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
