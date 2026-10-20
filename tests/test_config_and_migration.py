"""Tests for canonical database ownership, virtual environment independence, and safe migration."""

import os
import sys
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
