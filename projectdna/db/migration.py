"""Database migration, integrity verification, and canonical adoption for ProjectDNA."""

import os
import shutil
import sqlite3
import sys
import time
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


class FileLock:
    """
    OS-level exclusive file lock.
    Uses msvcrt.locking on Windows and fcntl.flock on POSIX.
    Serves as the authoritative single-writer synchronization mechanism.
    """

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._file = None

    def acquire(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.lock_path, "a+b")
        start = time.time()
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Record writer metadata (PID and timestamp) for diagnostics
                self._file.seek(0)
                self._file.truncate()
                self._file.write(f"pid={os.getpid()}\ntime={time.time()}\n".encode("utf-8"))
                self._file.flush()
                return self
            except (OSError, IOError):
                if time.time() - start >= self.timeout:
                    self.release()
                    raise TimeoutError(
                        f"Could not acquire exclusive writer lock on '{self.lock_path}' within {self.timeout}s"
                    )
                time.sleep(0.05)

    def release(self) -> None:
        if self._file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            try:
                self._file.close()
            except (OSError, IOError):
                pass
            self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def verify_database_integrity(db_path: Path) -> None:
    """
    Verify that an existing database file can be opened and passes SQLite integrity check.
    Raises DatabaseCorruptedError if corrupt, unreadable, or not a valid SQLite database.
    Strictly read-only; never mutates files.
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
    Strictly read-only.
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


def _do_check_and_migrate(
    target_canonical_dir: Path,
    target_legacy_dir: Path,
    canonical_db: Path,
    legacy_db: Path,
    staging_db: Path,
) -> Path:
    """
    Internal execution of migration checks and staging-based adoption.
    Must always execute under the exclusive writer lock.
    """
    # Clean up incomplete staging file from any prior failed/interrupted attempt
    if staging_db.exists():
        try:
            staging_db.unlink()
        except OSError:
            pass

    canonical_exists = canonical_db.exists()
    legacy_exists = legacy_db.exists()

    # Case 1: Both exist
    if canonical_exists and legacy_exists:
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

        if canonical_has_data:
            return canonical_db

        # If canonical is empty/unpopulated but legacy has data, adopt legacy
        if legacy_has_data and not canonical_has_data:
            pass

    elif canonical_exists:
        verify_database_integrity(canonical_db)
        return canonical_db

    # Case 2: Only legacy exists (or canonical is unpopulated and legacy has data)
    if legacy_exists:
        # Verify legacy database integrity before attempting migration
        verify_database_integrity(legacy_db)

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

        # 2. Checkpoint WAL and copy live DB into staging file using SQLite Backup API
        src_conn = sqlite3.connect(str(legacy_db), timeout=10.0)
        dst_conn = sqlite3.connect(str(staging_db), timeout=10.0)
        try:
            try:
                src_conn.execute("PRAGMA wal_checkpoint(FULL);")
            except Exception:
                pass
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # 3. Verify staging database integrity before promotion
        try:
            verify_database_integrity(staging_db)
        except Exception:
            if staging_db.exists():
                try:
                    staging_db.unlink()
                except OSError:
                    pass
            raise

        # 4. Double check that canonical DB does not already exist with populated data
        if canonical_db.exists() and is_database_populated(canonical_db):
            if staging_db.exists():
                try:
                    staging_db.unlink()
                except OSError:
                    pass
            raise DatabaseConflictError(
                f"Canonical database '{canonical_db}' already exists and contains data; "
                "aborting staging promotion to prevent overwrite."
            )

        # 5. Atomically promote staging database to canonical database
        os.replace(staging_db, canonical_db)

        # 6. Verify newly promoted canonical database integrity
        verify_database_integrity(canonical_db)

        # 7. Mark legacy database and WAL files as migrated so future runs do not conflict
        migrated_marker = target_legacy_dir / f"dna.db.migrated_{timestamp}"
        legacy_db.rename(migrated_marker)
        if legacy_wal.exists():
            try:
                legacy_wal.rename(target_legacy_dir / f"dna.db-wal.migrated_{timestamp}")
            except OSError:
                pass
        if legacy_shm.exists():
            try:
                legacy_shm.rename(target_legacy_dir / f"dna.db-shm.migrated_{timestamp}")
            except OSError:
                pass

        return canonical_db

    return canonical_db


def check_and_migrate_database(
    canonical_dir: Optional[Path] = None,
    legacy_dir: Optional[Path] = None,
    lock_timeout: float = 10.0,
    existing_lock: Optional[FileLock] = None,
) -> Path:
    """
    Inspect canonical and legacy database locations under exclusive writer lock.
    Only called by background service / writer initialization.

    Phase 3 Service Ownership Contract:
    Phase 3 must coordinate service startup and database initialization under
    continuous writer-lock ownership. The background service daemon acquires
    the authoritative writer lock at startup and passes its existing FileLock
    instance via `existing_lock` into `check_and_migrate_database()`, avoiding
    competing lock handles or an unlocked interval between migration and daemon loop.
    If `existing_lock` is None, this function acquires and releases the writer lock.

    Crash-Safety:
    Uses an atomic staging file (dna.db.tmp) in the canonical directory. The staging
    database is populated via the SQLite Online Backup API and its integrity is verified
    before atomic promotion via os.replace. If migration fails or is interrupted, the
    legacy database remains untouched and incomplete staging files are safely cleaned up.
    """
    target_canonical_dir = (canonical_dir or get_canonical_data_dir()).resolve()
    target_legacy_dir = (legacy_dir or get_legacy_data_dir()).resolve()

    canonical_db = target_canonical_dir / "dna.db"
    legacy_db = target_legacy_dir / "dna.db"
    staging_db = target_canonical_dir / "dna.db.tmp"

    # If canonical and legacy point to the same folder, no migration needed
    if target_canonical_dir == target_legacy_dir:
        if canonical_db.exists():
            verify_database_integrity(canonical_db)
        return canonical_db

    target_canonical_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_canonical_dir / "writer.lock"

    if existing_lock is not None:
        return _do_check_and_migrate(
            target_canonical_dir, target_legacy_dir, canonical_db, legacy_db, staging_db
        )

    with FileLock(lock_path, timeout=lock_timeout):
        return _do_check_and_migrate(
            target_canonical_dir, target_legacy_dir, canonical_db, legacy_db, staging_db
        )
