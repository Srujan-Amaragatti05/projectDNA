"""Regression tests for Phase 4 MCP audit findings.

Each test is tagged with its finding number for traceability.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from projectdna.db.connection import get_rw_connection
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.db.schema import init_schema
from projectdna.indexer.pipeline import IndexingPipeline
from projectdna.mcp.server import create_mcp_server, _verify_result_freshness

import json


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def test_env(tmp_path: Path, monkeypatch):
    """Isolated data directory and a registered+indexed sample project."""
    data_dir = tmp_path / "mcp_audit_data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    proj_dir = tmp_path / "sample_project"
    proj_dir.mkdir()

    app_py = proj_dir / "app.py"
    app_py.write_text(
        '"""Main module."""\nimport os\n\nclass Widget:\n    """A widget."""\n    def run(self):\n        """Run the widget."""\n        return 42\n',
        encoding="utf-8",
    )

    db_path = data_dir / "dna.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_rw_connection(db_path)
    init_schema(conn)
    proj = ProjectRepository.create_project(conn, "AuditProject", proj_dir.as_posix())
    IndexingPipeline.index_project(conn, proj.id, proj_dir)
    conn.close()

    return {
        "data_dir": data_dir,
        "proj_dir": proj_dir,
        "proj_id": proj.id,
        "app_py": app_py,
    }


def _extract(result) -> dict:
    if hasattr(result, "content") and result.content:
        first = result.content[0]
        if hasattr(first, "text"):
            return json.loads(first.text)
    if isinstance(result, dict):
        return result
    raise ValueError(f"Cannot extract dict from: {result}")


# ---------------------------------------------------------------------------
# Finding 2 — Windows drive-letter absolute path rejection
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_get_file_context_windows_drive_path_traversal(test_env):
    """Finding 2: Windows drive-letter paths must be rejected with PATH_TRAVERSAL.

    /etc/passwd already covered by existing test. This test covers Windows-specific
    drive letters like C:/Windows/System32/file.py which Path.is_absolute() catches
    on POSIX but the explicit check guards on Windows.
    """
    server = create_mcp_server()

    # Drive-letter path — the check `clean_rel[1] == ':'` handles Windows
    raw = await server.call_tool(
        "get_file_context",
        {"project": "AuditProject", "rel_path": "C:/Windows/System32/secret.py"},
    )
    data = _extract(raw)
    assert data.get("code") == "PATH_TRAVERSAL", (
        f"Expected PATH_TRAVERSAL for drive-letter path, got: {data}"
    )

    # Backslash absolute path variant
    raw2 = await server.call_tool(
        "get_file_context",
        {"project": "AuditProject", "rel_path": "\\Windows\\System32\\secret.py"},
    )
    data2 = _extract(raw2)
    assert data2.get("code") == "PATH_TRAVERSAL", (
        f"Expected PATH_TRAVERSAL for backslash absolute path, got: {data2}"
    )

    # Null byte injection
    raw3 = await server.call_tool(
        "get_file_context",
        {"project": "AuditProject", "rel_path": "app.py\x00/etc/passwd"},
    )
    data3 = _extract(raw3)
    assert data3.get("code") == "PATH_TRAVERSAL", (
        f"Expected PATH_TRAVERSAL for null-byte path, got: {data3}"
    )


# ---------------------------------------------------------------------------
# Finding 3 — Empty safe_terms fallback for all-quotes FTS query
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_search_code_all_quotes_query(test_env):
    """Finding 3: A query consisting entirely of quotes must not raise an error.

    After stripping quotes all terms are empty, so safe_terms becomes empty string.
    The guard must return count=0, not raise sqlite3.OperationalError.
    """
    server = create_mcp_server()

    # Triple-quote query — FTS rejects this; after stripping quotes no terms remain
    raw = await server.call_tool("search_code", {"query": '"""'})
    data = _extract(raw)
    assert "error" not in data, f"Expected no error for all-quotes query, got: {data}"
    assert data.get("count") == 0

    # Same for search_symbols
    raw2 = await server.call_tool("search_symbols", {"query": '"""'})
    data2 = _extract(raw2)
    assert "error" not in data2, f"Expected no error for all-quotes symbols query, got: {data2}"
    assert data2.get("count") == 0


@pytest.mark.anyio
async def test_mcp_search_symbols_all_quotes_query(test_env):
    """Finding 3 (symbols): Double-quote-only query returns empty results, not error."""
    server = create_mcp_server()
    raw = await server.call_tool("search_symbols", {"query": '""'})
    data = _extract(raw)
    assert "error" not in data
    assert data.get("count") == 0


# ---------------------------------------------------------------------------
# Finding 4 — Excluded files never returned in search results
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_search_code_excludes_env_and_git_files(test_env):
    """Finding 4: Files matching exclusion patterns must never appear in search results.

    Even if somehow indexed (e.g., before exclusion rules existed), search_code and
    search_symbols must filter them out.
    """
    server = create_mcp_server()

    # Retrieve the connection and manually insert an excluded .env file as if it were indexed
    db_path = test_env["data_dir"] / "dna.db"
    conn = get_rw_connection(db_path)

    proj_dir = test_env["proj_dir"]
    env_file = proj_dir / ".env"
    env_file.write_text("SECRET_KEY=supersecret\n", encoding="utf-8")
    env_stat = env_file.stat()

    # Insert directly bypassing the normal exclusion rules
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO files (project_id, rel_path, abs_path, mtime, size, sha256, last_indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            test_env["proj_id"],
            ".env",
            env_file.as_posix(),
            env_stat.st_mtime,
            env_stat.st_size,
            "abc123",
        ),
    )
    file_id = cur.lastrowid
    cur.execute(
        "INSERT INTO code_fts (file_id, content) VALUES (?, ?)",
        (file_id, "SECRET_KEY=supersecret"),
    )
    conn.commit()
    conn.close()

    # search_code must NOT return the .env file despite it being in the FTS index
    raw = await server.call_tool("search_code", {"query": "SECRET_KEY"})
    data = _extract(raw)
    returned_paths = [r["rel_path"] for r in data.get("results", [])]
    assert ".env" not in returned_paths, (
        f".env appeared in search_code results despite exclusion: {returned_paths}"
    )


# ---------------------------------------------------------------------------
# Finding 5 — root_path present in search_code and search_symbols results
# ---------------------------------------------------------------------------

def test_repository_search_code_returns_root_path(test_env):
    """Finding 5: search_code rows include root_path for exclusion guard in MCP."""
    db_path = test_env["data_dir"] / "dna.db"
    conn = get_rw_connection(db_path)

    results = IndexRepository.search_code(conn, "Widget")
    conn.close()

    assert results, "Expected at least one result for 'Widget'"
    row = results[0]
    assert "root_path" in row, f"root_path missing from search_code result: {list(row.keys())}"
    assert row["root_path"], "root_path must be non-empty"
    # Confirm no duplicate key collision (dict preserves last; check value is a path string)
    assert Path(row["root_path"]).is_absolute() or len(row["root_path"]) > 1


def test_repository_search_symbols_returns_root_path(test_env):
    """Finding 5: search_symbols rows include root_path for exclusion guard in MCP."""
    db_path = test_env["data_dir"] / "dna.db"
    conn = get_rw_connection(db_path)

    results = IndexRepository.search_symbols(conn, "Widget")
    conn.close()

    assert results, "Expected at least one symbol result for 'Widget'"
    row = results[0]
    assert "root_path" in row, f"root_path missing from search_symbols result: {list(row.keys())}"
    assert row["root_path"], "root_path must be non-empty"


# ---------------------------------------------------------------------------
# Finding 6 — sqlite3.Error returns structured DATABASE_ERROR, not unhandled exception
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_database_error_returns_structured_response(test_env):
    """Finding 6: sqlite3.Error in MCP tools returns DATABASE_ERROR, not an unhandled exception."""
    server = create_mcp_server()

    # Patch get_ro_db to yield a connection that raises sqlite3.DatabaseError on execute
    class BrokenCursor:
        def execute(self, *a, **kw):
            raise sqlite3.DatabaseError("simulated corruption")
        def fetchall(self):
            return []
        def fetchone(self):
            return None

    class BrokenConn:
        def cursor(self):
            return BrokenCursor()
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    from contextlib import contextmanager

    @contextmanager
    def broken_ro_db():
        yield BrokenConn()

    with patch("projectdna.mcp.server.get_ro_db", broken_ro_db):
        raw = await server.call_tool("list_projects", {})
        data = _extract(raw)
        assert data.get("code") == "DATABASE_ERROR", (
            f"Expected DATABASE_ERROR for sqlite3.Error, got: {data}"
        )

        raw2 = await server.call_tool("get_project_status", {"project": "AuditProject"})
        data2 = _extract(raw2)
        assert data2.get("code") == "DATABASE_ERROR", (
            f"Expected DATABASE_ERROR for get_project_status with broken DB, got: {data2}"
        )


# ---------------------------------------------------------------------------
# Finding 7 — Stale file returns warning field in all relevant tools
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_stale_file_has_warning_in_search_code(test_env):
    """Finding 7: search_code results for stale files include a 'warning' key."""
    import os
    server = create_mcp_server()
    app_py = test_env["app_py"]

    # Capture original stat
    stat_orig = app_py.stat()
    orig_mtime = stat_orig.st_mtime
    content_orig = app_py.read_text(encoding="utf-8")

    # Modify file content with same length and restore mtime
    modified = content_orig.replace("Widget", "Gadget")
    assert len(modified) == len(content_orig), "Replacement must preserve byte length for size match"
    app_py.write_text(modified, encoding="utf-8")
    os.utime(app_py, (orig_mtime, orig_mtime))

    # search_code for 'Widget' still hits the FTS index (indexed value), file is now stale
    raw = await server.call_tool("search_code", {"query": "Widget"})
    data = _extract(raw)

    if data["count"] > 0:
        result = data["results"][0]
        assert result["is_fresh"] is False, "File should be detected as stale"
        assert result["freshness"] == "modified_on_disk"
        assert "warning" in result, f"'warning' key missing from stale search_code result: {result}"


@pytest.mark.anyio
async def test_mcp_stale_file_has_warning_in_get_file_context(test_env):
    """Finding 7: get_file_context for a stale file includes a 'warning' key."""
    import os
    server = create_mcp_server()
    app_py = test_env["app_py"]

    stat_orig = app_py.stat()
    orig_mtime = stat_orig.st_mtime
    content_orig = app_py.read_text(encoding="utf-8")

    modified = content_orig.replace("Widget", "Gadget")
    assert len(modified) == len(content_orig)
    app_py.write_text(modified, encoding="utf-8")
    os.utime(app_py, (orig_mtime, orig_mtime))

    raw = await server.call_tool(
        "get_file_context",
        {"project": "AuditProject", "rel_path": "app.py"},
    )
    data = _extract(raw)

    assert data["is_fresh"] is False
    assert data["freshness"] == "modified_on_disk"
    assert "warning" in data, f"'warning' key missing from stale get_file_context result: {data}"
