"""Tests for SQLite database schema, constraints, and repository operations."""

import sqlite3
from pathlib import Path
from projectdna.db.connection import get_rw_connection, get_ro_connection
from projectdna.db.schema import init_schema
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.models import ParsedFile, SymbolRecord, ImportRecord


def test_schema_and_project_constraints(tmp_path: Path):
    db_file = tmp_path / "test.db"
    conn = get_rw_connection(db_file)
    init_schema(conn)

    # 1. Verify duplicate project display names are allowed if roots differ
    p1 = ProjectRepository.create_project(conn, name="my_app", root_path=(tmp_path / "root1").as_posix())
    p2 = ProjectRepository.create_project(conn, name="my_app", root_path=(tmp_path / "root2").as_posix())
    assert p1.id != p2.id
    assert p1.name == p2.name

    # 2. Verify root_path must be unique
    try:
        ProjectRepository.create_project(conn, name="another_name", root_path=(tmp_path / "root1").as_posix())
        assert False, "Expected sqlite3.IntegrityError for duplicate root_path"
    except sqlite3.IntegrityError:
        pass

    conn.close()


def test_file_identity_unique_per_project(tmp_path: Path):
    db_file = tmp_path / "test.db"
    conn = get_rw_connection(db_file)
    init_schema(conn)

    p1 = ProjectRepository.create_project(conn, name="p1", root_path=(tmp_path / "r1").as_posix())
    p2 = ProjectRepository.create_project(conn, name="p2", root_path=(tmp_path / "r2").as_posix())

    # Two different projects can have files with the same rel_path
    parsed1 = ParsedFile(
        rel_path="app.py", abs_path="/r1/app.py", mtime=1.0, size=10, sha256="h1", content="code1"
    )
    parsed2 = ParsedFile(
        rel_path="app.py", abs_path="/r2/app.py", mtime=1.0, size=10, sha256="h2", content="code2"
    )

    f1_id = IndexRepository.upsert_indexed_file(conn, p1.id, parsed1)
    f2_id = IndexRepository.upsert_indexed_file(conn, p2.id, parsed2)

    assert f1_id != f2_id

    # Within the same project, rel_path updates the existing record
    parsed1_updated = ParsedFile(
        rel_path="app.py", abs_path="/r1/app.py", mtime=2.0, size=20, sha256="h1_mod", content="code1_mod"
    )
    f1_updated_id = IndexRepository.upsert_indexed_file(conn, p1.id, parsed1_updated)
    assert f1_id == f1_updated_id

    conn.close()


def test_transactional_fts_cleanup_on_update_and_delete(tmp_path: Path):
    db_file = tmp_path / "test.db"
    conn = get_rw_connection(db_file)
    init_schema(conn)

    p = ProjectRepository.create_project(conn, name="test_fts", root_path=(tmp_path / "proj").as_posix())

    # Initial file with symbol and content
    sym1 = SymbolRecord(id=None, file_id=0, name="AlphaClass", kind="class", start_line=1, end_line=5)
    parsed = ParsedFile(
        rel_path="mod.py",
        abs_path="/proj/mod.py",
        mtime=1.0,
        size=50,
        sha256="h1",
        content="class AlphaClass:\n    pass\n",
        symbols=[sym1],
    )
    file_id = IndexRepository.upsert_indexed_file(conn, p.id, parsed)

    # Verify searchable in FTS
    results = IndexRepository.search_code(conn, "AlphaClass")
    assert len(results) == 1
    sym_results = IndexRepository.search_symbols(conn, "AlphaClass")
    assert len(sym_results) == 1

    # Update file: replace AlphaClass with BetaClass
    sym2 = SymbolRecord(id=None, file_id=0, name="BetaClass", kind="class", start_line=1, end_line=5)
    parsed_updated = ParsedFile(
        rel_path="mod.py",
        abs_path="/proj/mod.py",
        mtime=2.0,
        size=50,
        sha256="h2",
        content="class BetaClass:\n    pass\n",
        symbols=[sym2],
    )
    IndexRepository.upsert_indexed_file(conn, p.id, parsed_updated)

    # Old symbol and code must no longer match FTS
    assert len(IndexRepository.search_code(conn, "AlphaClass")) == 0
    assert len(IndexRepository.search_symbols(conn, "AlphaClass")) == 0

    # New symbol and code must match
    assert len(IndexRepository.search_code(conn, "BetaClass")) == 1
    assert len(IndexRepository.search_symbols(conn, "BetaClass")) == 1

    # Now delete file: verify FTS rows are cleanly purged
    IndexRepository.delete_file_by_rel_path(conn, p.id, "mod.py")
    assert len(IndexRepository.search_code(conn, "BetaClass")) == 0
    assert len(IndexRepository.search_symbols(conn, "BetaClass")) == 0

    conn.close()


def test_read_only_connection(tmp_path: Path):
    db_file = tmp_path / "ro_test.db"
    conn_rw = get_rw_connection(db_file)
    init_schema(conn_rw)
    ProjectRepository.create_project(conn_rw, "app", (tmp_path / "r").as_posix())
    conn_rw.close()

    # Open read-only connection
    conn_ro = get_ro_connection(db_file)
    projects = ProjectRepository.list_projects(conn_ro)
    assert len(projects) == 1

    # Verify write attempt on RO connection fails
    try:
        conn_ro.execute("INSERT INTO projects (name, root_path) VALUES ('x', 'y')")
        assert False, "Write operation should fail on read-only connection"
    except sqlite3.OperationalError:
        pass

    conn_ro.close()
