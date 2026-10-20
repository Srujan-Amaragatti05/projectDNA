"""Comprehensive tests for ProjectDNA Phase 4 Read-Only MCP Server."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import pytest

from projectdna.db.connection import get_rw_connection, get_ro_connection
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.db.schema import init_schema
from projectdna.indexer.parser import compute_file_sha256
from projectdna.indexer.pipeline import IndexingPipeline
from projectdna.mcp.server import create_mcp_server


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch):
    """Set up an isolated data directory and a sample registered project."""
    data_dir = tmp_path / "mcp_data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    # Create project source directory
    proj_dir = tmp_path / "sample_project"
    proj_dir.mkdir()

    app_py = proj_dir / "app.py"
    app_py.write_text(
        '''"""Main application module."""
import os
from math import sqrt

class Calculator:
    """A simple calculator class."""
    def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

def calculate_total(x: int) -> int:
    """Calculate total value."""
    calc = Calculator()
    return calc.add(x, 10)
''',
        encoding="utf-8",
    )

    helper_py = proj_dir / "helper.py"
    helper_py.write_text(
        '''"""Helper utilities."""
def format_result(val: int) -> str:
    """Format an integer result."""
    return f"Result: {val}"
''',
        encoding="utf-8",
    )

    # Initialize DB as the background service would
    db_path = data_dir / "dna.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_rw_connection(db_path)
    init_schema(conn)

    proj = ProjectRepository.create_project(conn, "SampleProject", proj_dir.as_posix())
    IndexingPipeline.index_project(conn, proj.id, proj_dir)
    conn.close()

    return {
        "data_dir": data_dir,
        "proj_dir": proj_dir,
        "proj_id": proj.id,
        "app_py": app_py,
        "helper_py": helper_py,
    }


def _extract_dict_from_result(result) -> dict:
    """Helper to parse MCP call_tool result into a dictionary."""
    if hasattr(result, "content") and result.content:
        first_content = result.content[0]
        if hasattr(first_content, "text"):
            return json.loads(first_content.text)
    if isinstance(result, dict):
        return result
    raise ValueError(f"Could not extract dict from MCP result: {result}")


@pytest.mark.anyio
async def test_mcp_tool_registration():
    """MCP server registers all required read-only tools."""
    server = create_mcp_server()
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    expected_tools = {
        "list_projects",
        "get_project_status",
        "search_code",
        "search_symbols",
        "get_file_context",
    }
    assert expected_tools.issubset(tool_names)


@pytest.mark.anyio
async def test_mcp_list_projects(test_env):
    """list_projects discovers registered projects with canonical paths and status."""
    server = create_mcp_server()
    raw = await server.call_tool("list_projects", {})
    data = _extract_dict_from_result(raw)

    assert data["count"] == 1
    proj = data["projects"][0]
    assert proj["name"] == "SampleProject"
    assert proj["root_exists"] is True
    assert proj["id"] == test_env["proj_id"]


@pytest.mark.anyio
async def test_mcp_list_projects_no_database(monkeypatch, tmp_path: Path):
    """list_projects returns clean empty status when database does not exist."""
    empty_dir = tmp_path / "empty_store"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(empty_dir))

    server = create_mcp_server()
    raw = await server.call_tool("list_projects", {})
    data = _extract_dict_from_result(raw)

    assert data["count"] == 0
    assert data["projects"] == []
    assert "No ProjectDNA database found" in data["message"]


@pytest.mark.anyio
async def test_mcp_get_project_status(test_env):
    """get_project_status returns indexing health, file counts, and pending changes."""
    server = create_mcp_server()

    # Query by name
    raw = await server.call_tool("get_project_status", {"project": "SampleProject"})
    data = _extract_dict_from_result(raw)
    assert data["name"] == "SampleProject"
    assert data["total_indexed_files"] == 2
    assert data["syntax_error_count"] == 0
    assert data["pending_changes_count"] == 0

    # Query by ID
    raw_id = await server.call_tool("get_project_status", {"project": str(test_env["proj_id"])})
    data_id = _extract_dict_from_result(raw_id)
    assert data_id["id"] == test_env["proj_id"]

    # Query non-existent project
    raw_missing = await server.call_tool("get_project_status", {"project": "NonExistent"})
    data_missing = _extract_dict_from_result(raw_missing)
    assert data_missing.get("code") == "PROJECT_NOT_FOUND"

    # Query empty string
    raw_empty = await server.call_tool("get_project_status", {"project": "   "})
    data_empty = _extract_dict_from_result(raw_empty)
    assert data_empty.get("code") == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_mcp_search_code(test_env):
    """search_code performs FTS queries and includes freshness tags."""
    server = create_mcp_server()

    # Search for Calculator
    raw = await server.call_tool("search_code", {"query": "Calculator"})
    data = _extract_dict_from_result(raw)
    assert data["count"] >= 1
    match = data["results"][0]
    assert match["rel_path"] == "app.py"
    assert match["is_fresh"] is True
    assert match["freshness"] == "current"
    assert "Calculator" in match["snippet"]

    # Search with project filter
    raw_filtered = await server.call_tool(
        "search_code", {"query": "format_result", "project": "SampleProject"}
    )
    data_filtered = _extract_dict_from_result(raw_filtered)
    assert data_filtered["count"] == 1
    assert data_filtered["results"][0]["rel_path"] == "helper.py"

    # Search query returning no matches
    raw_none = await server.call_tool("search_code", {"query": "non_existent_term_xyz"})
    data_none = _extract_dict_from_result(raw_none)
    assert data_none["count"] == 0
    assert data_none["results"] == []

    # Empty query rejection
    raw_empty = await server.call_tool("search_code", {"query": ""})
    data_empty = _extract_dict_from_result(raw_empty)
    assert data_empty.get("code") == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_mcp_search_symbols(test_env):
    """search_symbols returns classes, functions, line numbers, and docstrings."""
    server = create_mcp_server()

    raw = await server.call_tool("search_symbols", {"query": "add"})
    data = _extract_dict_from_result(raw)
    assert data["count"] >= 1
    sym = data["symbols"][0]
    assert sym["name"] == "add"
    assert sym["kind"] == "method"
    assert sym["parent_symbol"] == "Calculator"
    assert sym["is_fresh"] is True
    assert sym["start_line"] > 0
    assert "Add two numbers" in sym["docstring"]


@pytest.mark.anyio
async def test_mcp_get_file_context(test_env):
    """get_file_context returns source text, symbols, imports, and line slicing."""
    server = create_mcp_server()

    # Retrieve whole file
    raw = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "app.py"},
    )
    data = _extract_dict_from_result(raw)
    assert data["rel_path"] == "app.py"
    assert data["is_fresh"] is True
    assert data["freshness"] == "current"
    assert "class Calculator:" in data["content"]
    assert len(data["symbols"]) >= 2
    assert len(data["imports"]) >= 2

    # Line slicing
    raw_slice = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "app.py", "start_line": 5, "end_line": 9},
    )
    data_slice = _extract_dict_from_result(raw_slice)
    assert data_slice["line_range"] == {"start": 5, "end": 9}
    assert "Calculator" in data_slice["content"]

    # Invalid range (start > end)
    raw_inv = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "app.py", "start_line": 10, "end_line": 5},
    )
    data_inv = _extract_dict_from_result(raw_inv)
    assert data_inv.get("code") == "INVALID_LINE_RANGE"


@pytest.mark.anyio
async def test_mcp_source_freshness_restored_mtime_and_deletion(test_env):
    """MCP tools detect files modified with restored timestamps or deleted from disk."""
    server = create_mcp_server()
    app_py = test_env["app_py"]

    stat_orig = app_py.stat()
    orig_mtime = stat_orig.st_mtime
    content_orig = app_py.read_text(encoding="utf-8")

    # Modify file with EXACT same byte length and restore timestamp
    # Replace "Calculator" (10 chars) with "Calculator" -> replace 'total' with 'grand' (both 5 chars)
    modified_content = content_orig.replace("calculate_total", "calculate_grand")
    assert len(modified_content) == len(content_orig)
    app_py.write_text(modified_content, encoding="utf-8")
    os.utime(app_py, (orig_mtime, orig_mtime))

    assert app_py.stat().st_mtime == orig_mtime
    assert app_py.stat().st_size == stat_orig.st_size

    # 1. get_project_status reports pending changes
    raw_status = await server.call_tool("get_project_status", {"project": "SampleProject"})
    status_data = _extract_dict_from_result(raw_status)
    assert status_data["pending_changes_count"] == 1

    # 2. search_code detects stale / modified file
    raw_search = await server.call_tool("search_code", {"query": "Calculator"})
    search_data = _extract_dict_from_result(raw_search)
    assert search_data["results"][0]["is_fresh"] is False
    assert search_data["results"][0]["freshness"] == "modified_on_disk"

    # 3. get_file_context flags modified_on_disk
    raw_ctx = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "app.py"},
    )
    ctx_data = _extract_dict_from_result(raw_ctx)
    assert ctx_data["is_fresh"] is False
    assert ctx_data["freshness"] == "modified_on_disk"

    # 4. Now delete the file on disk
    app_py.unlink()

    raw_ctx_del = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "app.py"},
    )
    ctx_del = _extract_dict_from_result(raw_ctx_del)
    assert ctx_del["code"] == "FILE_DELETED"
    assert ctx_del["is_fresh"] is False

    raw_search_del = await server.call_tool("search_code", {"query": "Calculator"})
    search_del = _extract_dict_from_result(raw_search_del)
    assert search_del["results"][0]["freshness"] == "deleted_on_disk"


@pytest.mark.anyio
async def test_mcp_security_path_traversal_and_exclusions(test_env):
    """MCP blocks path traversal, excluded patterns, non-python files, and absolute paths."""
    server = create_mcp_server()
    proj_dir = test_env["proj_dir"]

    # 1. Path traversal using ..
    raw1 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "../../secret.py"},
    )
    res1 = _extract_dict_from_result(raw1)
    assert res1.get("code") == "PATH_TRAVERSAL"

    # 2. Absolute path rejection
    raw2 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "/etc/passwd"},
    )
    res2 = _extract_dict_from_result(raw2)
    assert res2.get("code") == "PATH_TRAVERSAL"

    # 3. Excluded file (.env)
    env_file = proj_dir / ".env"
    env_file.write_text("SECRET_KEY=12345\n", encoding="utf-8")
    raw3 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": ".env"},
    )
    res3 = _extract_dict_from_result(raw3)
    assert res3.get("code") == "FILE_EXCLUDED"

    # 4. Excluded directory file (.git/config)
    git_dir = proj_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")
    raw4 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": ".git/config"},
    )
    res4 = _extract_dict_from_result(raw4)
    assert res4.get("code") == "FILE_EXCLUDED"

    # 5. Non-python file
    readme = proj_dir / "README.md"
    readme.write_text("# Readme\n", encoding="utf-8")
    raw5 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "README.md"},
    )
    res5 = _extract_dict_from_result(raw5)
    assert res5.get("code") == "FILE_EXCLUDED"

    # 6. Unindexed python file
    unindexed = proj_dir / "new_unindexed.py"
    unindexed.write_text("z = 10\n", encoding="utf-8")
    raw6 = await server.call_tool(
        "get_file_context",
        {"project": "SampleProject", "rel_path": "new_unindexed.py"},
    )
    res6 = _extract_dict_from_result(raw6)
    assert res6.get("code") == "FILE_NOT_INDEXED"


def test_mcp_strictly_read_only(test_env):
    """MCP connection is strict read-only; write attempts fail at SQLite engine level."""
    from projectdna.mcp.server import get_ro_db

    # Verify writer.lock does not exist
    lock_file = test_env["data_dir"] / "writer.lock"
    assert not lock_file.exists()

    with get_ro_db() as conn:
        cur = conn.cursor()
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            cur.execute("INSERT INTO projects (name, root_path) VALUES ('Bad', 'bad')")
        assert "readonly database" in str(exc_info.value).lower()

    # Lock must still not exist
    assert not lock_file.exists()


def test_mcp_stdio_server_subprocess(monkeypatch, tmp_path: Path):
    """MCP stdio server runs and responds to JSON-RPC 2.0 initialize request."""
    data_dir = tmp_path / "stdio_data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    cmd = [sys.executable, "-m", "projectdna.mcp.server"]
    env = os.environ.copy()
    env["PROJECTDNA_DATA_DIR"] = str(data_dir)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )

    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }

    try:
        proc.stdin.write(json.dumps(init_request) + "\n")
        proc.stdin.flush()

        response_line = proc.stdout.readline()
        assert response_line.strip()

        resp = json.loads(response_line)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert "result" in resp
        assert resp["result"]["serverInfo"]["name"] == "ProjectDNA"
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=3.0)
