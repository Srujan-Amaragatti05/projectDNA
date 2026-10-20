"""Database connection management for ProjectDNA."""

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from projectdna.config import (
    get_db_path,
    get_data_dir,
    ensure_data_dir,
)
from projectdna.db.migration import (
    check_and_migrate_database,
    verify_database_integrity,
)


def get_rw_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """
    Get a read-write SQLite connection.
    Intended exclusively for the background service (writer).
    Performs migration/adoption safety checks when using the canonical database.
    Configures WAL mode, enables foreign keys, and sets busy_timeout.
    """
    if db_path is None:
        if os.environ.get("PROJECTDNA_DATA_DIR"):
            # Explicit advanced override: do not perform automatic legacy adoption
            target_dir = ensure_data_dir()
            path = target_dir / "dna.db"
            if path.exists():
                verify_database_integrity(path)
        else:
            # Canonical path: inspect and safely adopt legacy database if present
            path = check_and_migrate_database()
            path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = Path(db_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            verify_database_integrity(path)

    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None  # Explicit transaction management
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def get_ro_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """
    Get a strict read-only SQLite connection.
    Used by CLI read commands and MCP server.
    """
    if db_path is None:
        if not os.environ.get("PROJECTDNA_DATA_DIR"):
            check_and_migrate_database()
        path = get_db_path().resolve()
    else:
        path = Path(db_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Database does not exist at {path}. Background service has not initialized it.")

    # Verify that the existing database can be opened cleanly
    verify_database_integrity(path)

    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=5.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn
