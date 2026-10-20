"""Database migration, integrity verification, and canonical adoption for ProjectDNA."""

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from projectdna.config import (
    get_canonical_data_dir,
    get_legacy_data_dir,
    get_data_dir,
)


class DatabaseConflictError(Exception):
    """Raised when both canonical and legacy databases exist with data."""
    pass


class DatabaseCorruptedError(Exception):
    """Raised when a database exists but cannot be opened or fails integrity checks."""
    pass


def verify_database_integrity(db_path: Path) -> None:
    """
    Verify that an existing database file can be opened and passes SQLite integrity check.
    Raises DatabaseCorruptedError if corrupt, unreadable, or not a valid SQLite database.
    """
    if not db_path.exists():
        return

    try:
        # Open in URI read-only mode to prevent unintended writes during inspection
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        rows = cur.fetchall()
        conn.close()

        if not rows or rows[0][0] != "ok":
            raise DatabaseCorruptedError(
                f"Database at '{db_path}' failed integrity check: {rows}"
            )
    except sqlite3.DatabaseError as e:
        raise DatabaseCorruptedError(
            f"Database at '{db_path}' cannot be opened: {str(e)}"
        ) from e
    except Exception as e:
        if isinstance(e, DatabaseCorruptedError):
            raise
        raise DatabaseCorruptedError(
            f"Failed to verify database at '{db_path}': {str(e)}"
        ) from e


def is_database_populated(db_path: Path) -> bool:
    """
    Check if an existing database contains user data.
    Returns True if user tables exist and contain records.
    """
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False

    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name NOT LIKE '%_fts%'
            """
        )
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            conn.close()
            return False

        for table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = cur.fetchone()[0]
                if count > 0:
                    conn.close()
                    return True
            except Exception:
                continue

        conn.close()
        return False
    except Exception:
        # If file is non-empty but cannot be read normally, treat as having content
        return db_path.stat().st_size > 0


def check_and_migrate_database(
    canonical_dir: Optional[Path] = None,
    legacy_dir: Optional[Path] = None,
) -> Path:
    """
    Inspect canonical and legacy database locations.
    - If only legacy database exists: safely adopts it into the canonical location using
      the SQLite Online Backup API (accounting for active WAL state), preserves a backup,
      and verifies integrity.
    - If both exist and contain data: raises DatabaseConflictError.
    - If canonical database exists: verifies integrity. If corrupted, raises DatabaseCorruptedError.
    """
    target_canonical_dir = (canonical_dir or get_canonical_data_dir()).resolve()
    target_legacy_dir = (legacy_dir or get_legacy_data_dir()).resolve()

    canonical_db = target_canonical_dir / "dna.db"
    legacy_db = target_legacy_dir / "dna.db"

    # If canonical and legacy point to the same folder, no migration needed
    if target_canonical_dir == target_legacy_dir:
        if canonical_db.exists():
            verify_database_integrity(canonical_db)
        return canonical_db

    canonical_exists = canonical_db.exists()
    legacy_exists = legacy_db.exists()

    # Case 1: Both exist
    if canonical_exists and legacy_exists:
        # Verify integrity of canonical DB first
        verify_database_integrity(canonical_db)

        canonical_has_data = is_database_populated(canonical_db)
        legacy_has_data = is_database_populated(legacy_db)

        if canonical_has_data and legacy_has_data:
            raise DatabaseConflictError(
                f"Conflicting databases detected: both canonical database at '{canonical_db}' "
                f"and legacy database at '{legacy_db}' exist and contain data. "
                "ProjectDNA will not silently choose one, overwrite either, or create a new empty database. "
                "Please resolve this conflict manually (e.g. backup or remove one database) "
                "or specify the PROJECTDNA_DATA_DIR environment variable."
            )

        # If canonical has data and legacy does not, canonical is authoritative
        if canonical_has_data:
            return canonical_db

        # If canonical is empty/unpopulated but legacy has data, adopt legacy
        if legacy_has_data and not canonical_has_data:
            # We will migrate legacy into canonical below
            pass

    # Case 2: Only canonical exists
    elif canonical_exists:
        verify_database_integrity(canonical_db)
        return canonical_db

    # Case 3: Only legacy exists (or canonical is unpopulated and legacy has data)
    if legacy_exists:
        # Verify legacy database integrity before attempting migration
        verify_database_integrity(legacy_db)

        # Ensure canonical directory exists
        target_canonical_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 1. Preserve file backups in legacy directory
        backup_file = target_legacy_dir / f"dna.db.backup_{timestamp}"
        shutil.copy2(legacy_db, backup_file)

        legacy_wal = target_legacy_dir / "dna.db-wal"
        legacy_shm = target_legacy_dir / "dna.db-shm"
        if legacy_wal.exists():
            shutil.copy2(legacy_wal, target_legacy_dir / f"dna.db-wal.backup_{timestamp}")
        if legacy_shm.exists():
            shutil.copy2(legacy_shm, target_legacy_dir / f"dna.db-shm.backup_{timestamp}")

        # 2. Use SQLite Backup API to copy live DB including WAL transactions
        src_conn = sqlite3.connect(str(legacy_db))
        dst_conn = sqlite3.connect(str(canonical_db))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # 3. Verify newly created canonical database integrity
        verify_database_integrity(canonical_db)

        # 4. Mark legacy database as migrated so future runs do not conflict
        migrated_marker = target_legacy_dir / f"dna.db.migrated_{timestamp}"
        legacy_db.rename(migrated_marker)
        if legacy_wal.exists():
            try:
                legacy_wal.unlink()
            except OSError:
                pass
        if legacy_shm.exists():
            try:
                legacy_shm.unlink()
            except OSError:
                pass

        return canonical_db

    # Neither exists yet
    return canonical_db
