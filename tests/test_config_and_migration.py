"""Tests for canonical database ownership, virtual environment independence, and safe migration."""

import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from projectdna.config import (
    get_canonical_data_dir,
    get_legacy_data_dir,
    get_data_dir,
    get_db_path,
    ensure_data_dir,
)
from projectdna.db.connection import get_rw_connection, get_ro_connection
from projectdna.db.schema import init_schema
from projectdna.db.repository import ProjectRepository
from projectdna.db.migration import (
    check_and_migrate_database,
    verify_database_integrity,
    is_database_populated,
    DatabaseConflictError,
    DatabaseCorruptedError,
)


def test_canonical_path_resolution_working_directories(monkeypatch, tmp_path: Path):
    """Canonical path resolution must be invariant to current working directory."""
    monkeypatch.delenv("PROJECTDNA_DATA_DIR", raising=False)
    orig_cwd = Path.cwd()

    dir1 = tmp_path / "working_dir_1"
    dir2 = tmp_path / "working_dir_2"
    dir1.mkdir()
    dir2.mkdir()

    try:
        os.chdir(dir1)
        res1 = get_canonical_data_dir()

        os.chdir(dir2)
        res2 = get_canonical_data_dir()

        assert res1 == res2
        if sys.platform == "win32":
            expected = Path(os.environ["LOCALAPPDATA"]) / "ProjectDNA"
            assert res1 == expected.resolve()
    finally:
        os.chdir(orig_cwd)


def test_virtual_environment_independence(monkeypatch):
    """Canonical data directory must be invariant to virtual environment."""
    monkeypatch.delenv("PROJECTDNA_DATA_DIR", raising=False)

    # Simulate Virtual Environment A
    monkeypatch.setenv("VIRTUAL_ENV", "C:\\envs\\virtualenv_a")
    monkeypatch.setattr(sys, "prefix", "C:\\envs\\virtualenv_a")
    res_a = get_canonical_data_dir()

    # Simulate Virtual Environment B
    monkeypatch.setenv("VIRTUAL_ENV", "D:\\other\\virtualenv_b")
    monkeypatch.setattr(sys, "prefix", "D:\\other\\virtualenv_b")
    res_b = get_canonical_data_dir()

    assert res_a == res_b


def test_explicit_projectdna_data_dir_override(monkeypatch, tmp_path: Path):
    """PROJECTDNA_DATA_DIR must serve as an explicit advanced override creating an isolated store."""
    custom_dir = (tmp_path / "custom_store").resolve()
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(custom_dir))

    assert get_data_dir() == custom_dir
    assert get_db_path() == custom_dir / "dna.db"

    # Verify dynamic module attribute access
    from projectdna.config import DB_PATH, DATA_DIR
    assert DATA_DIR == custom_dir
    assert DB_PATH == custom_dir / "dna.db"


def test_legacy_only_migration_and_adoption(tmp_path: Path):
    """Legacy-only database must be safely adopted into canonical location with backups."""
    legacy_dir = tmp_path / "legacy"
    canonical_dir = tmp_path / "canonical"
    legacy_dir.mkdir()

    legacy_db = legacy_dir / "dna.db"

    # Create and populate legacy database
    conn = get_rw_connection(legacy_db)
    init_schema(conn)
    ProjectRepository.create_project(conn, "LegacyProject", "/path/to/legacy")
    conn.close()

    assert legacy_db.exists()
    assert not (canonical_dir / "dna.db").exists()

    # Run migration
    adopted_db = check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    assert adopted_db == canonical_dir / "dna.db"
    assert adopted_db.exists()

    # Verify integrity and data preservation in canonical location
    verify_database_integrity(adopted_db)
    ro_conn = get_ro_connection(adopted_db)
    projects = ProjectRepository.list_projects(ro_conn)
    assert len(projects) == 1
    assert projects[0].name == "LegacyProject"
    ro_conn.close()

    # Verify that backup was created in legacy directory and original legacy DB was preserved
    backups = list(legacy_dir.glob("dna.db.backup_*"))
    migrated_markers = list(legacy_dir.glob("dna.db.migrated_*"))
    assert len(backups) >= 1
    assert len(migrated_markers) >= 1


def test_conflicting_populated_databases_no_data_loss(tmp_path: Path):
    """Conflicting populated databases must raise DatabaseConflictError without data loss."""
    legacy_dir = tmp_path / "legacy_conflict"
    canonical_dir = tmp_path / "canonical_conflict"
    legacy_dir.mkdir()
    canonical_dir.mkdir()

    # Create populated legacy DB
    conn_leg = get_rw_connection(legacy_dir / "dna.db")
    init_schema(conn_leg)
    ProjectRepository.create_project(conn_leg, "ProjLegacy", "/path/legacy")
    conn_leg.close()

    # Create populated canonical DB
    conn_can = get_rw_connection(canonical_dir / "dna.db")
    init_schema(conn_can)
    ProjectRepository.create_project(conn_can, "ProjCanonical", "/path/canonical")
    conn_can.close()

    with pytest.raises(DatabaseConflictError) as exc_info:
        check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    assert "Conflicting databases detected" in str(exc_info.value)

    # Verify neither database was lost or overwritten
    ro_can = get_ro_connection(canonical_dir / "dna.db")
    assert ProjectRepository.list_projects(ro_can)[0].name == "ProjCanonical"
    ro_can.close()

    ro_leg = get_ro_connection(legacy_dir / "dna.db")
    assert ProjectRepository.list_projects(ro_leg)[0].name == "ProjLegacy"
    ro_leg.close()


def test_invalid_unopenable_database_causes_error_not_silent_fallback(tmp_path: Path):
    """An unopenable database must raise a clear error and never silently fall back to a new database."""
    bad_db = tmp_path / "corrupted.db"
    bad_db.write_bytes(b"CORRUPTED_NOT_A_VALID_SQLITE_HEADER_DATA")

    # Attempting to open in read-write must fail with DatabaseCorruptedError
    with pytest.raises(DatabaseCorruptedError) as exc_info_rw:
        get_rw_connection(bad_db)
    assert "cannot be opened" in str(exc_info_rw.value) or "failed integrity check" in str(exc_info_rw.value)

    # Attempting to open in read-only must also fail
    with pytest.raises(DatabaseCorruptedError) as exc_info_ro:
        get_ro_connection(bad_db)
    assert "cannot be opened" in str(exc_info_ro.value) or "failed integrity check" in str(exc_info_ro.value)

    # Verify the corrupted file was not overwritten with a new empty SQLite database
    assert bad_db.read_bytes() == b"CORRUPTED_NOT_A_VALID_SQLITE_HEADER_DATA"


def test_ro_connection_never_triggers_migration_or_directory_creation(monkeypatch, tmp_path: Path):
    """get_ro_connection must never create directories, migrate databases, or mutate files."""
    canonical_dir = tmp_path / "nonexistent_canonical"
    legacy_dir = tmp_path / "legacy_dir"
    legacy_dir.mkdir()

    # Create populated legacy DB
    conn_leg = get_rw_connection(legacy_dir / "dna.db")
    init_schema(conn_leg)
    ProjectRepository.create_project(conn_leg, "LegacyProj", "/p")
    conn_leg.close()

    # Configure canonical path to point to non-existent folder
    monkeypatch.setattr("projectdna.config.get_canonical_data_dir", lambda: canonical_dir)
    monkeypatch.setattr("projectdna.config.get_legacy_data_dir", lambda: legacy_dir)
    monkeypatch.delenv("PROJECTDNA_DATA_DIR", raising=False)

    # Calling get_ro_connection when canonical does not exist must raise FileNotFoundError
    with pytest.raises(FileNotFoundError) as exc_info:
        get_ro_connection()

    assert "Database does not exist" in str(exc_info.value)
    assert "background service has not initialized it" in str(exc_info.value)

    # Verify canonical directory was NOT created
    assert not canonical_dir.exists()

    # Verify legacy database was NOT migrated or modified
    assert (legacy_dir / "dna.db").exists()
    assert len(list(legacy_dir.glob("dna.db.migrated_*"))) == 0
    assert len(list(legacy_dir.glob("dna.db.backup_*"))) == 0


def test_ro_connection_cannot_mutate_database(tmp_path: Path):
    """Read-only connections must reject any write attempt."""
    db_file = tmp_path / "ro_test.db"
    conn_rw = get_rw_connection(db_file)
    init_schema(conn_rw)
    ProjectRepository.create_project(conn_rw, "TestProj", "/test")
    conn_rw.close()

    conn_ro = get_ro_connection(db_file)
    with pytest.raises(sqlite3.OperationalError):
        conn_ro.execute("INSERT INTO projects (name, root_path) VALUES ('X', '/x')")

    with pytest.raises(sqlite3.OperationalError):
        conn_ro.execute("DROP TABLE projects")

    conn_ro.close()


def test_migration_with_active_wal_journal(tmp_path: Path):
    """Migration must checkpoint and preserve data stored in active SQLite WAL files after ungraceful shutdown."""
    import subprocess
    legacy_dir = tmp_path / "legacy_wal"
    canonical_dir = tmp_path / "canonical_wal"
    legacy_dir.mkdir()

    legacy_db = legacy_dir / "dna.db"

    # Simulate an abnormal crash leaving uncheckpointed WAL file on disk
    script = f'''import sqlite3, os
conn = sqlite3.connect(r"{legacy_db.as_posix()}")
conn.execute("PRAGMA journal_mode = WAL;")
conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, msg TEXT);")
conn.execute("INSERT INTO records (msg) VALUES ('committed_in_wal_1');")
conn.execute("INSERT INTO records (msg) VALUES ('committed_in_wal_2');")
conn.commit()
os._exit(0)
'''
    subprocess.run([sys.executable, "-c", script], check=True)

    # Verify WAL file exists on disk
    legacy_wal = legacy_dir / "dna.db-wal"
    assert legacy_wal.exists()
    assert legacy_wal.stat().st_size > 0

    # Execute migration
    adopted_db = check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    assert adopted_db.exists()

    # Verify data from WAL was preserved in canonical DB
    ro_conn = get_ro_connection(adopted_db)
    cur = ro_conn.cursor()
    cur.execute("SELECT msg FROM records ORDER BY id ASC")
    rows = [r[0] for r in cur.fetchall()]
    assert rows == ["committed_in_wal_1", "committed_in_wal_2"]
    ro_conn.close()

    # Verify WAL backup was preserved
    wal_backups = list(legacy_dir.glob("dna.db-wal.backup_*"))
    assert len(wal_backups) >= 1


def test_concurrent_database_migration(tmp_path: Path):
    """Concurrent attempts to migrate/initialize must safely synchronize via FileLock."""
    legacy_dir = tmp_path / "legacy_concurrent"
    canonical_dir = tmp_path / "canonical_concurrent"
    legacy_dir.mkdir()

    legacy_db = legacy_dir / "dna.db"
    conn = get_rw_connection(legacy_db)
    init_schema(conn)
    ProjectRepository.create_project(conn, "ConcurrentProject", "/concurrent")
    conn.close()

    def run_migration():
        return check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    # Run 5 concurrent threads attempting migration
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(run_migration) for _ in range(5)]
        results = [f.result() for f in futures]

    # All threads must report the same canonical DB path
    for res in results:
        assert res == canonical_dir / "dna.db"

    # Exactly one migrated marker and backup should exist
    assert len(list(legacy_dir.glob("dna.db.migrated_*"))) == 1
    assert len(list(legacy_dir.glob("dna.db.backup_*"))) == 1

    # Canonical DB must be fully functional and intact
    verify_database_integrity(canonical_dir / "dna.db")
    ro_conn = get_ro_connection(canonical_dir / "dna.db")
    projects = ProjectRepository.list_projects(ro_conn)
    assert len(projects) == 1
    assert projects[0].name == "ConcurrentProject"
    ro_conn.close()


def test_canonical_populated_and_legacy_empty(tmp_path: Path):
    """If canonical is populated and legacy is empty, canonical is authoritative without error."""
    legacy_dir = tmp_path / "legacy_empty"
    canonical_dir = tmp_path / "canonical_pop"
    legacy_dir.mkdir()
    canonical_dir.mkdir()

    # Canonical is populated
    conn_can = get_rw_connection(canonical_dir / "dna.db")
    init_schema(conn_can)
    ProjectRepository.create_project(conn_can, "CanonicalMaster", "/master")
    conn_can.close()

    # Legacy is empty (0 bytes)
    (legacy_dir / "dna.db").write_bytes(b"")

    res = check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)
    assert res == canonical_dir / "dna.db"

    ro_conn = get_ro_connection(res)
    assert ProjectRepository.list_projects(ro_conn)[0].name == "CanonicalMaster"
    ro_conn.close()


def test_corrupted_legacy_aborts_without_migration(tmp_path: Path):
    """Corrupted legacy database must abort and not create canonical DB."""
    legacy_dir = tmp_path / "legacy_bad"
    canonical_dir = tmp_path / "canonical_uncreated"
    legacy_dir.mkdir()

    (legacy_dir / "dna.db").write_bytes(b"CORRUPTED_NOT_SQLITE")

    with pytest.raises(DatabaseCorruptedError):
        check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    assert not (canonical_dir / "dna.db").exists()


def test_interrupted_staging_migration_recovery(tmp_path: Path):
    """An interrupted migration leaving an incomplete dna.db.tmp must recover cleanly without data loss."""
    legacy_dir = tmp_path / "legacy_interrupted"
    canonical_dir = tmp_path / "canonical_interrupted"
    legacy_dir.mkdir()
    canonical_dir.mkdir()

    # Create populated legacy DB
    conn = get_rw_connection(legacy_dir / "dna.db")
    init_schema(conn)
    ProjectRepository.create_project(conn, "SurvivingProject", "/survive")
    conn.close()

    # Simulate an incomplete/corrupted staging file from an aborted previous run
    staging_file = canonical_dir / "dna.db.tmp"
    staging_file.write_bytes(b"INCOMPLETE_STAGING_BYTES_FROM_CRASH")

    assert not (canonical_dir / "dna.db").exists()
    assert staging_file.exists()

    # Subsequent migration attempt
    adopted_db = check_and_migrate_database(canonical_dir=canonical_dir, legacy_dir=legacy_dir)

    # Staging file was cleaned up and atomically replaced
    assert adopted_db == canonical_dir / "dna.db"
    assert adopted_db.exists()
    assert not staging_file.exists()

    # Records were preserved and verified
    verify_database_integrity(adopted_db)
    ro_conn = get_ro_connection(adopted_db)
    projects = ProjectRepository.list_projects(ro_conn)
    assert len(projects) == 1
    assert projects[0].name == "SurvivingProject"
    ro_conn.close()


def test_subprocess_migration_lock_contention(tmp_path: Path):
    """Multiple concurrent OS processes attempting migration must serialize cleanly via FileLock."""
    import subprocess
    legacy_dir = tmp_path / "legacy_proc_contention"
    canonical_dir = tmp_path / "canonical_proc_contention"
    legacy_dir.mkdir()

    # Create populated legacy DB
    conn = get_rw_connection(legacy_dir / "dna.db")
    init_schema(conn)
    ProjectRepository.create_project(conn, "MultiProcProject", "/proc")
    conn.close()

    script = f'''
import sys
from pathlib import Path
from projectdna.db.migration import check_and_migrate_database

res = check_and_migrate_database(
    canonical_dir=Path(r"{canonical_dir.as_posix()}"),
    legacy_dir=Path(r"{legacy_dir.as_posix()}"),
    lock_timeout=15.0
)
print("SUCCESS")
'''

    # Spawn 3 concurrent Python subprocesses
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]

    for p in procs:
        stdout, stderr = p.communicate(timeout=20)
        assert p.returncode == 0, f"Subprocess failed with stderr: {stderr}"
        assert "SUCCESS" in stdout

    # Verify that only one migration occurred and data is intact
    assert len(list(legacy_dir.glob("dna.db.migrated_*"))) == 1
    assert len(list(legacy_dir.glob("dna.db.backup_*"))) == 1

    canonical_db = canonical_dir / "dna.db"
    assert canonical_db.exists()
    verify_database_integrity(canonical_db)
    ro_conn = get_ro_connection(canonical_db)
    projects = ProjectRepository.list_projects(ro_conn)
    assert len(projects) == 1
    assert projects[0].name == "MultiProcProject"
    ro_conn.close()

