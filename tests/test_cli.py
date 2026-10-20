"""Tests for ProjectDNA CLI commands."""

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


def test_cli_add_validation_and_service_dependency(tmp_path: Path):
    """'dna add' validates path and reports Phase 3 service dependency."""
    # Invalid path
    res_bad = runner.invoke(app, ["add", str(tmp_path / "nonexistent")])
    assert res_bad.exit_code == 1

    # Valid directory
    valid_dir = tmp_path / "valid_project"
    valid_dir.mkdir()
    res_valid = runner.invoke(app, ["add", str(valid_dir)])
    assert res_valid.exit_code == 0
    assert "Project path validated" in res_valid.stdout
    assert "Phase 3 Service Dependency" in res_valid.stdout


def test_cli_scan_service_dependency(monkeypatch, tmp_path: Path):
    """'dna scan' reports Phase 3 service dependency."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    ProjectRepository.create_project(conn, "TargetProj", (tmp_path / "proj").as_posix())
    conn.close()

    res = runner.invoke(app, ["scan", "TargetProj"])
    assert res.exit_code == 0
    assert "Scan requested for project: TargetProj" in res.stdout
    assert "Phase 3 Service Dependency" in res.stdout


def test_cli_remove_confirmation_and_preserves_files(monkeypatch, tmp_path: Path):
    """'dna remove' requires confirmation and never touches source files."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    src_dir = tmp_path / "preserved_project"
    src_dir.mkdir()
    src_file = src_dir / "app.py"
    src_file.write_text("x = 1\n", encoding="utf-8")

    db_path = data_dir / "dna.db"
    conn = get_rw_connection(db_path)
    init_schema(conn)
    ProjectRepository.create_project(conn, "PreserveMe", src_dir.as_posix())
    conn.close()

    # 1. Abort confirmation ('n')
    res_abort = runner.invoke(app, ["remove", "PreserveMe"], input="n\n")
    assert res_abort.exit_code == 0
    assert "Removal cancelled" in res_abort.stdout
    assert src_file.exists()

    # 2. Confirm removal ('y')
    res_confirm = runner.invoke(app, ["remove", "PreserveMe"], input="y\n")
    assert res_confirm.exit_code == 0
    assert "Removal confirmed for project: PreserveMe" in res_confirm.stdout
    assert "Original source files" in res_confirm.stdout
    assert "remain untouched" in res_confirm.stdout
    assert "Phase 3 Service Dependency" in res_confirm.stdout
    # Source file must remain intact
    assert src_file.exists()
    assert src_file.read_text(encoding="utf-8") == "x = 1\n"

    # 3. Using --yes flag
    res_yes = runner.invoke(app, ["remove", "PreserveMe", "--yes"])
    assert res_yes.exit_code == 0
    assert "Removal confirmed for project: PreserveMe" in res_yes.stdout
    assert src_file.exists()


def test_cli_serve_phase4_dependency():
    """'dna serve' explains Phase 4 MCP server dependency."""
    res = runner.invoke(app, ["serve"])
    assert res.exit_code == 0
    assert "Phase 4 MCP Dependency" in res.stdout
