"""Tests for ProjectDNA CLI commands."""

import os
import sqlite3
import time
from pathlib import Path
from typer.testing import CliRunner

from projectdna.cli import app
from projectdna.db.connection import get_rw_connection
from projectdna.db.schema import init_schema
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.indexer.pipeline import IndexingPipeline
from projectdna.models import ParsedFile, SymbolRecord

runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_cli_list_no_db(monkeypatch, tmp_path: Path):
    """When no database exists, 'dna list' should inform the user without crashing."""
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(tmp_path / "empty_dir"))
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No ProjectDNA database found" in result.stdout


def test_cli_list_with_projects(monkeypatch, tmp_path: Path):
    """When projects are registered, 'dna list' displays them in a table."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    ProjectRepository.create_project(conn, "AlphaApp", (tmp_path / "alpha").as_posix())
    ProjectRepository.create_project(conn, "BetaService", (tmp_path / "beta").as_posix())
    conn.close()

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "AlphaApp" in result.stdout
    assert "BetaService" in result.stdout


def test_cli_status_project_not_found(monkeypatch, tmp_path: Path):
    """Querying status of a nonexistent project reports an error."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    conn.close()

    result = runner.invoke(app, ["status", "UnknownProj"])
    assert result.exit_code == 1
    assert "Project 'UnknownProj' not found" in result.stderr or "Project 'UnknownProj' not found" in result.stdout


def test_cli_status_project_success(monkeypatch, tmp_path: Path):
    """'dna status' displays health, syntax errors, and file modification status."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    proj_dir = tmp_path / "my_project"
    proj_dir.mkdir()
    f1 = proj_dir / "valid.py"
    f1.write_text("def hello(): pass\n", encoding="utf-8")
    f2 = proj_dir / "broken.py"
    f2.write_text("def syntax_err(\n", encoding="utf-8")

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    proj = ProjectRepository.create_project(conn, "MyProj", proj_dir.as_posix())
    IndexingPipeline.index_project(conn, proj.id, proj_dir)
    conn.close()

    result = runner.invoke(app, ["status", str(proj.id)])
    assert result.exit_code == 0
    assert "MyProj" in result.stdout
    assert "Indexed Files" in result.stdout
    assert "Syntax Errors" in result.stdout
    assert "broken.py" in result.stdout

    # Now modify f1 on disk to test pending changes detection
    time.sleep(0.01)
    f1.write_text("def hello_modified(): pass\n", encoding="utf-8")

    result_mod = runner.invoke(app, ["status", "MyProj"])
    assert result_mod.exit_code == 0
    assert "modified on disk since last scan" in result_mod.stdout


def test_cli_search_code_and_symbols(monkeypatch, tmp_path: Path):
    """'dna search' queries code and symbols with freshness verification."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    proj_dir = tmp_path / "search_proj"
    proj_dir.mkdir()
    f1 = proj_dir / "engine.py"
    f1.write_text(
        '''def compute_hash(val: str) -> str:
    """Compute cryptographic hash."""
    return val
''',
        encoding="utf-8",
    )

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    proj = ProjectRepository.create_project(conn, "SearchEngine", proj_dir.as_posix())
    IndexingPipeline.index_project(conn, proj.id, proj_dir)
    conn.close()

    # Search for symbol
    result_sym = runner.invoke(app, ["search", "compute_hash"])
    assert result_sym.exit_code == 0
    assert "compute_hash" in result_sym.stdout
    assert "engine.py" in result_sym.stdout

    # Search for code
    result_code = runner.invoke(app, ["search", "cryptographic"])
    assert result_code.exit_code == 0
    assert "SearchEngine" in result_code.stdout
    assert "engine.py" in result_code.stdout

    # Modify file on disk to verify freshness warning
    time.sleep(0.01)
    f1.write_text("# modified\n", encoding="utf-8")
    result_stale = runner.invoke(app, ["search", "cryptographic"])
    assert result_stale.exit_code == 0
    assert "Modified on disk since indexing" in result_stale.stdout


def test_cli_add_and_scan_and_remove_with_service(monkeypatch, tmp_path: Path):
    """'dna add', 'dna scan', and 'dna remove' communicate with service via IPC."""
    data_dir = tmp_path / "cli_service_data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    from projectdna.service.daemon import ProjectDNAService
    service = ProjectDNAService(data_dir=data_dir)
    service.start(reconcile_on_start=False)

    try:
        # Invalid path
        res_bad = runner.invoke(app, ["add", str(tmp_path / "nonexistent")])
        assert res_bad.exit_code == 1

        # Register valid directory
        proj_dir = tmp_path / "my_cli_proj"
        proj_dir.mkdir()
        src_file = proj_dir / "app.py"
        src_file.write_text("x = 100\n", encoding="utf-8")

        res_add = runner.invoke(app, ["add", str(proj_dir)])
        assert res_add.exit_code == 0
        assert "Project registered successfully" in res_add.stdout
        assert "my_cli_proj" in res_add.stdout

        # Scan project
        src_file.write_text("x = 200\n", encoding="utf-8")
        res_scan = runner.invoke(app, ["scan", "my_cli_proj"])
        assert res_scan.exit_code == 0
        assert "Reconciliation scan completed" in res_scan.stdout

        # Status command confirms indexed data
        res_status = runner.invoke(app, ["status", "my_cli_proj"])
        assert res_status.exit_code == 0
        assert "my_cli_proj" in res_status.stdout

        # Service status command
        res_svc = runner.invoke(app, ["service", "status"])
        assert res_svc.exit_code == 0
        assert "ProjectDNA service is running" in res_svc.stdout

        # Remove project with 'n' (abort)
        res_abort = runner.invoke(app, ["remove", "my_cli_proj"], input="n\n")
        assert res_abort.exit_code == 0
        assert "Removal cancelled" in res_abort.stdout
        assert src_file.exists()

        # Remove project with --yes
        res_rem = runner.invoke(app, ["remove", "my_cli_proj", "--yes"])
        assert res_rem.exit_code == 0
        assert "Project removed from ProjectDNA" in res_rem.stdout
        # Original source files must remain untouched!
        assert src_file.exists()
        assert src_file.read_text(encoding="utf-8") == "x = 200\n"

        # List should now show 0 registered projects
        res_list = runner.invoke(app, ["list"])
        assert "No registered projects found" in res_list.stdout

    finally:
        service.stop()


def test_cli_source_freshness_restored_mtime_and_size(monkeypatch, tmp_path: Path):
    """Retrieval and status detect changed content even when mtime and size are restored."""
    data_dir = tmp_path / "freshness_data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    proj_dir = tmp_path / "freshness_proj"
    proj_dir.mkdir()
    src_file = proj_dir / "app.py"
    initial_content = "secret = 12345\n"  # 15 bytes
    src_file.write_text(initial_content, encoding="utf-8")

    from projectdna.service.daemon import ProjectDNAService
    service = ProjectDNAService(data_dir=data_dir)
    service.start(reconcile_on_start=False)

    try:
        # 1. Register project
        res_add = runner.invoke(app, ["add", str(proj_dir)])
        assert res_add.exit_code == 0

        # Initial search matches without staleness
        res_search_init = runner.invoke(app, ["search", "secret"])
        assert "Modified on disk since indexing" not in res_search_init.stdout

        # Initial status is up to date
        res_status_init = runner.invoke(app, ["status", "freshness_proj"])
        assert "Up to date" in res_status_init.stdout

        # 2. Modify content to new value with same length and restore original mtime
        stat_before = src_file.stat()
        orig_mtime = stat_before.st_mtime

        new_content = "secret = 99999\n"  # 15 bytes
        assert len(new_content) == len(initial_content)
        src_file.write_text(new_content, encoding="utf-8")
        os.utime(src_file, (orig_mtime, orig_mtime))

        assert src_file.stat().st_mtime == orig_mtime
        assert src_file.stat().st_size == stat_before.st_size

        # 3. Retrieval must detect modification and flag it
        res_search_stale = runner.invoke(app, ["search", "secret"])
        assert "Modified on disk since indexing" in res_search_stale.stdout

        # 4. Status must report pending changes
        res_status_stale = runner.invoke(app, ["status", "freshness_proj"])
        assert "1 file(s) modified on disk since last scan" in res_status_stale.stdout

        # 5. Reconciliation scan re-indexes the file
        res_scan = runner.invoke(app, ["scan", "freshness_proj"])
        assert res_scan.exit_code == 0
        assert "Reconciliation scan completed" in res_scan.stdout

        # 6. Now search for 99999 is fresh and status is up to date
        res_search_fresh = runner.invoke(app, ["search", "99999"])
        assert "freshness_proj" in res_search_fresh.stdout
        assert "Modified on disk since indexing" not in res_search_fresh.stdout

        res_status_fresh = runner.invoke(app, ["status", "freshness_proj"])
        assert "Up to date" in res_status_fresh.stdout

    finally:
        service.stop()


def test_cli_serve_phase4_dependency():
    """'dna serve' explains Phase 4 MCP server dependency."""
    res = runner.invoke(app, ["serve"])
    assert res.exit_code == 0
    assert "Phase 4 MCP Dependency" in res.stdout
