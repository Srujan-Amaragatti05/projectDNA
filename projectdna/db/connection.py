"""Database connection management for ProjectDNA."""

import sqlite3
from pathlib import Path
from typing import Union
from projectdna.config import DB_PATH, ensure_data_dir


def get_rw_connection(db_path: Union[str, Path] = DB_PATH) -> sqlite3.Connection:
    """
    Get a read-write SQLite connection.
    Intended exclusively for the background service (writer).
    Configures WAL mode, enables foreign keys, and sets busy_timeout.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None  # Explicit transaction management (autocommit off)
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def get_ro_connection(db_path: Union[str, Path] = DB_PATH) -> sqlite3.Connection:
    """
    Get a strict read-only SQLite connection.
    Used by CLI read commands and MCP server.
    """
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Database does not exist at {path}. Background service has not initialized it.")
    
    # Use URI mode to guarantee read-only access
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
