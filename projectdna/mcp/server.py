"""Read-only Model Context Protocol (MCP) server for ProjectDNA."""

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from mcp.server.mcpserver import MCPServer

from projectdna.db.connection import get_ro_connection
from projectdna.db.repository import IndexRepository, ProjectRepository, normalize_path
from projectdna.indexer.exclusions import (
    is_indexable_python_file,
    is_path_excluded,
)
from projectdna.indexer.parser import compute_file_sha256
from projectdna.models import ProjectRecord
from projectdna.service.manager import is_service_running

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50


@contextmanager
def get_ro_db() -> Generator[sqlite3.Connection, None, None]:
    """Provide a strict read-only SQLite database connection context."""
    conn = get_ro_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _verify_result_freshness(
    abs_path_str: str,
    indexed_mtime: float,
    indexed_size: int,
    indexed_sha256: str,
) -> Dict[str, Any]:
    """
    Verify freshness of an individual matching file on disk against indexed records.
    Avoids full-project hashing by evaluating only retrieved results.
    """
    p = Path(abs_path_str)
    if not p.exists():
        return {"is_fresh": False, "freshness": "deleted_on_disk"}

    try:
        stat = p.stat()
        if stat.st_mtime != indexed_mtime or stat.st_size != indexed_size:
            return {"is_fresh": False, "freshness": "modified_on_disk"}

        if indexed_sha256:
            content_bytes = p.read_bytes()
            if compute_file_sha256(content_bytes) != indexed_sha256:
                return {"is_fresh": False, "freshness": "modified_on_disk"}

        return {"is_fresh": True, "freshness": "current"}
    except OSError:
        return {"is_fresh": False, "freshness": "unreadable_on_disk"}


def create_mcp_server() -> MCPServer:
    """Create and configure the read-only ProjectDNA MCP server with tools."""
    server = MCPServer(
        "ProjectDNA",
        description="Local-first Python project intelligence and read-only code retrieval",
    )

    @server.tool(
        description=(
            "List all registered Python projects monitored by ProjectDNA, "
            "including project ID, name, canonical root path, status, and last scan time."
        )
    )
    def list_projects() -> Dict[str, Any]:
        try:
            with get_ro_db() as conn:
                projects = ProjectRepository.list_projects(conn)
                return {
                    "count": len(projects),
                    "service_running": is_service_running(),
                    "projects": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "root_path": p.root_path,
                            "status": p.status,
                            "last_scanned_at": str(p.last_scanned_at) if p.last_scanned_at else None,
                            "root_exists": Path(p.root_path).exists(),
                        }
                        for p in projects
                    ],
                }
        except FileNotFoundError:
            return {
                "count": 0,
                "service_running": is_service_running(),
                "projects": [],
                "message": "No ProjectDNA database found. Register a project with 'dna add <path>' first.",
            }
        except sqlite3.Error as e:
            return {"error": f"Database read error: {e}", "code": "DATABASE_ERROR"}

    @server.tool(
        description=(
            "Get project and indexing health for a registered project by ID, "
            "canonical root path, or display name."
        )
    )
    def get_project_status(project: str) -> Dict[str, Any]:
        if not project or not project.strip():
            return {"error": "Project parameter must not be empty.", "code": "INVALID_ARGUMENT"}

        try:
            with get_ro_db() as conn:
                try:
                    proj = ProjectRepository.resolve_project(conn, project.strip())
                except ValueError as e:
                    return {"error": str(e), "code": "AMBIGUOUS_PROJECT"}

                if not proj:
                    return {"error": f"Project '{project}' not found.", "code": "PROJECT_NOT_FOUND"}

                files_map = IndexRepository.get_files_by_project(conn, proj.id)
                syntax_errors = [
                    {"rel_path": f.rel_path, "error": f.error_message}
                    for f in files_map.values()
                    if f.has_syntax_error
                ]

                root_path = Path(proj.root_path)
                root_exists = root_path.exists() and root_path.is_dir()

                # Verify pending changes using timestamp and content hash verification
                if root_exists:
                    pending_changes = 0
                    for f in files_map.values():
                        p = Path(f.abs_path)
                        try:
                            if not p.exists() or p.stat().st_mtime != f.mtime or p.stat().st_size != f.size:
                                pending_changes += 1
                            elif f.sha256 and compute_file_sha256(p.read_bytes()) != f.sha256:
                                pending_changes += 1
                        except OSError:
                            pending_changes += 1
                else:
                    # Root directory is missing on disk; all indexed files are pending deletion
                    pending_changes = len(files_map)

                return {
                    "id": proj.id,
                    "name": proj.name,
                    "root_path": proj.root_path,
                    "status": proj.status,
                    "last_scanned_at": str(proj.last_scanned_at) if proj.last_scanned_at else None,
                    "root_exists": root_exists,
                    "total_indexed_files": len(files_map),
                    "syntax_error_count": len(syntax_errors),
                    "pending_changes_count": pending_changes,
                    "syntax_errors": syntax_errors,
                }
        except FileNotFoundError:
            return {
                "error": "No ProjectDNA database found. Register a project with 'dna add <path>' first.",
                "code": "DATABASE_NOT_FOUND",
            }
        except sqlite3.Error as e:
            return {"error": f"Database read error: {e}", "code": "DATABASE_ERROR"}

    @server.tool(
        description=(
            "Search indexed Python source code across registered projects using full-text search (SQLite FTS5). "
            "Returns matching excerpts with file location and source freshness verification."
        )
    )
    def search_code(
        query: str,
        project: Optional[str] = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> Dict[str, Any]:
        if not query or not query.strip():
            return {"error": "Query parameter must not be empty.", "code": "INVALID_ARGUMENT"}

        bounded_limit = max(1, min(limit, MAX_SEARCH_LIMIT))

        try:
            with get_ro_db() as conn:
                project_id = None
                if project and project.strip():
                    try:
                        proj = ProjectRepository.resolve_project(conn, project.strip())
                    except ValueError as e:
                        return {"error": str(e), "code": "AMBIGUOUS_PROJECT"}
                    if not proj:
                        return {"error": f"Project '{project}' not found.", "code": "PROJECT_NOT_FOUND"}
                    project_id = proj.id

                try:
                    raw_results = IndexRepository.search_code(
                        conn, query.strip(), project_id=project_id, limit=bounded_limit
                    )
                except sqlite3.OperationalError:
                    # Sanitize special FTS tokens if syntax is malformed
                    safe_terms = " ".join(
                        f'"{term.replace("\"", "")}"'
                        for term in query.strip().split()
                        if term.replace('"', "")
                    )
                    if not safe_terms:
                        return {"query": query, "count": 0, "results": []}
                    try:
                        raw_results = IndexRepository.search_code(
                            conn, safe_terms, project_id=project_id, limit=bounded_limit
                        )
                    except sqlite3.OperationalError as e:
                        return {"error": f"Invalid search query syntax: {e}", "code": "QUERY_SYNTAX_ERROR"}

                results = []
                for r in raw_results:
                    p = Path(r["abs_path"])
                    root = Path(r.get("root_path", ""))
                    # Accidental exposure guard: do not return excluded files
                    if root.exists() and (
                        is_path_excluded(p, root_path=root)
                        or not is_indexable_python_file(p, root_path=root)
                    ):
                        continue

                    freshness_info = _verify_result_freshness(
                        abs_path_str=r["abs_path"],
                        indexed_mtime=r.get("mtime", 0.0),
                        indexed_size=r.get("size", 0),
                        indexed_sha256=r.get("sha256", ""),
                    )

                    result_item: Dict[str, Any] = {
                        "file_id": r["file_id"],
                        "project_id": r["project_id"],
                        "project_name": r["project_name"],
                        "rel_path": r["rel_path"],
                        "abs_path": r["abs_path"],
                        "snippet": r.get("snippet", ""),
                        "is_fresh": freshness_info["is_fresh"],
                        "freshness": freshness_info["freshness"],
                        "has_syntax_error": bool(r.get("has_syntax_error")),
                        "error_message": r.get("error_message"),
                    }
                    if not freshness_info["is_fresh"]:
                        result_item["warning"] = (
                            "Source file is modified or deleted on disk; excerpt may not reflect current state."
                        )

                    results.append(result_item)

                return {
                    "query": query,
                    "count": len(results),
                    "results": results,
                }
        except FileNotFoundError:
            return {
                "error": "No ProjectDNA database found. Register a project with 'dna add <path>' first.",
                "code": "DATABASE_NOT_FOUND",
            }
        except sqlite3.Error as e:
            return {"error": f"Database read error: {e}", "code": "DATABASE_ERROR"}

    @server.tool(
        description=(
            "Search indexed Python symbols (classes, functions, methods) and docstrings. "
            "Returns symbol definitions, line locations, parent containers, and docstrings."
        )
    )
    def search_symbols(
        query: str,
        project: Optional[str] = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> Dict[str, Any]:
        if not query or not query.strip():
            return {"error": "Query parameter must not be empty.", "code": "INVALID_ARGUMENT"}

        bounded_limit = max(1, min(limit, MAX_SEARCH_LIMIT))

        try:
            with get_ro_db() as conn:
                project_id = None
                if project and project.strip():
                    try:
                        proj = ProjectRepository.resolve_project(conn, project.strip())
                    except ValueError as e:
                        return {"error": str(e), "code": "AMBIGUOUS_PROJECT"}
                    if not proj:
                        return {"error": f"Project '{project}' not found.", "code": "PROJECT_NOT_FOUND"}
                    project_id = proj.id

                try:
                    raw_symbols = IndexRepository.search_symbols(
                        conn, query.strip(), project_id=project_id, limit=bounded_limit
                    )
                except sqlite3.OperationalError:
                    safe_terms = " ".join(
                        f'"{term.replace("\"", "")}"'
                        for term in query.strip().split()
                        if term.replace('"', "")
                    )
                    if not safe_terms:
                        return {"query": query, "count": 0, "symbols": []}
                    try:
                        raw_symbols = IndexRepository.search_symbols(
                            conn, safe_terms, project_id=project_id, limit=bounded_limit
                        )
                    except sqlite3.OperationalError as e:
                        return {"error": f"Invalid symbol query syntax: {e}", "code": "QUERY_SYNTAX_ERROR"}

                symbols = []
                for s in raw_symbols:
                    p = Path(s["abs_path"])
                    root = Path(s.get("root_path", ""))
                    # Accidental exposure guard: do not return excluded files
                    if root.exists() and (
                        is_path_excluded(p, root_path=root)
                        or not is_indexable_python_file(p, root_path=root)
                    ):
                        continue

                    freshness_info = _verify_result_freshness(
                        abs_path_str=s["abs_path"],
                        indexed_mtime=s.get("mtime", 0.0),
                        indexed_size=s.get("size", 0),
                        indexed_sha256=s.get("sha256", ""),
                    )

                    sym_item: Dict[str, Any] = {
                        "symbol_id": s["symbol_id"],
                        "name": s["name"],
                        "kind": s["kind"],
                        "parent_symbol": s["parent_symbol"],
                        "project_id": s["project_id"],
                        "project_name": s["project_name"],
                        "rel_path": s["rel_path"],
                        "abs_path": s["abs_path"],
                        "start_line": s["start_line"],
                        "end_line": s["end_line"],
                        "docstring": s["docstring"],
                        "is_fresh": freshness_info["is_fresh"],
                        "freshness": freshness_info["freshness"],
                    }
                    if not freshness_info["is_fresh"]:
                        sym_item["warning"] = (
                            "Source file is modified or deleted on disk; symbol location may be out of date."
                        )

                    symbols.append(sym_item)

                return {
                    "query": query,
                    "count": len(symbols),
                    "symbols": symbols,
                }
        except FileNotFoundError:
            return {
                "error": "No ProjectDNA database found. Register a project with 'dna add <path>' first.",
                "code": "DATABASE_NOT_FOUND",
            }
        except sqlite3.Error as e:
            return {"error": f"Database read error: {e}", "code": "DATABASE_ERROR"}

    @server.tool(
        description=(
            "Retrieve indexed source code, symbol definitions, and imports for a registered Python file. "
            "Supports line-range slicing, security boundary enforcement, and source freshness checks."
        )
    )
    def get_file_context(
        project: str,
        rel_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not project or not project.strip():
            return {"error": "Project parameter must not be empty.", "code": "INVALID_ARGUMENT"}
        if not rel_path or not rel_path.strip():
            return {"error": "rel_path parameter must not be empty.", "code": "INVALID_ARGUMENT"}

        clean_rel = rel_path.strip().replace("\\", "/")
        p_check = Path(rel_path.strip())

        # Enforce strict relative path semantics; reject absolute paths, drive letters, and null bytes
        if (
            p_check.is_absolute()
            or clean_rel.startswith("/")
            or (len(clean_rel) > 1 and clean_rel[1] == ":")
            or clean_rel.startswith("\\")
            or "\0" in clean_rel
        ):
            return {
                "error": "rel_path must be a relative path within the project root, not an absolute path.",
                "code": "PATH_TRAVERSAL",
            }

        try:
            with get_ro_db() as conn:
                try:
                    proj = ProjectRepository.resolve_project(conn, project.strip())
                except ValueError as e:
                    return {"error": str(e), "code": "AMBIGUOUS_PROJECT"}
                if not proj:
                    return {"error": f"Project '{project}' not found.", "code": "PROJECT_NOT_FOUND"}

                project_root = Path(proj.root_path).resolve()
                try:
                    candidate_path = (project_root / clean_rel).resolve()
                except Exception:
                    return {
                        "error": "Invalid path syntax.",
                        "code": "PATH_TRAVERSAL",
                    }

                # Path traversal guard
                try:
                    verified_rel = candidate_path.relative_to(project_root)
                except ValueError:
                    return {
                        "error": "Path traversal detected: target path is outside project root directory.",
                        "code": "PATH_TRAVERSAL",
                    }

                # Exclusion policy guard
                if is_path_excluded(candidate_path, root_path=project_root) or not is_indexable_python_file(
                    candidate_path, root_path=project_root
                ):
                    return {
                        "error": f"Access denied: '{clean_rel}' matches exclusion or non-indexable file rules.",
                        "code": "FILE_EXCLUDED",
                    }

                norm_rel_posix = verified_rel.as_posix()
                file_rec = IndexRepository.get_file_by_rel_path(conn, proj.id, norm_rel_posix)
                if not file_rec:
                    return {
                        "error": f"File '{norm_rel_posix}' is not indexed in project '{proj.name}'.",
                        "code": "FILE_NOT_INDEXED",
                    }

                # Disk existence and freshness validation
                if not candidate_path.exists():
                    return {
                        "error": f"File '{norm_rel_posix}' was deleted from disk.",
                        "code": "FILE_DELETED",
                        "is_fresh": False,
                        "freshness": "deleted_on_disk",
                    }

                try:
                    content_bytes = candidate_path.read_bytes()
                except OSError as e:
                    return {"error": f"Failed to read file from disk: {e}", "code": "IO_ERROR"}

                disk_hash = compute_file_sha256(content_bytes)
                is_fresh = disk_hash == file_rec.sha256
                freshness = "current" if is_fresh else "modified_on_disk"

                source_text = content_bytes.decode("utf-8", errors="replace")
                lines = source_text.splitlines()
                total_lines = len(lines)

                s_line = max(1, start_line) if start_line is not None else 1
                e_line = min(total_lines, end_line) if end_line is not None else total_lines

                if total_lines > 0 and s_line > e_line:
                    return {
                        "error": f"Invalid line range: start_line ({s_line}) exceeds end_line ({e_line}).",
                        "code": "INVALID_LINE_RANGE",
                    }

                selected_lines = lines[s_line - 1 : e_line] if total_lines > 0 else []
                numbered_lines = [
                    f"{s_line + idx:4d} | {line}" for idx, line in enumerate(selected_lines)
                ]

                symbols = IndexRepository.get_symbols_by_file_id(conn, file_rec.id)
                imports = IndexRepository.get_imports_by_file_id(conn, file_rec.id)

                response_data: Dict[str, Any] = {
                    "project_id": proj.id,
                    "project_name": proj.name,
                    "rel_path": norm_rel_posix,
                    "abs_path": candidate_path.as_posix(),
                    "is_fresh": is_fresh,
                    "freshness": freshness,
                    "has_syntax_error": file_rec.has_syntax_error,
                    "error_message": file_rec.error_message,
                    "total_lines": total_lines,
                    "line_range": {"start": s_line, "end": e_line},
                    "content": "\n".join(numbered_lines),
                    "symbols": symbols,
                    "imports": imports,
                }
                if not is_fresh:
                    response_data["warning"] = (
                        "Source file was modified on disk since indexing; content and symbols "
                        "may be out of date. Run 'dna scan' to reconcile."
                    )

                return response_data
        except FileNotFoundError:
            return {
                "error": "No ProjectDNA database found. Register a project with 'dna add <path>' first.",
                "code": "DATABASE_NOT_FOUND",
            }
        except sqlite3.Error as e:
            return {"error": f"Database read error: {e}", "code": "DATABASE_ERROR"}

    return server


def run_stdio_server() -> None:
    """Run the MCP server over stdio for compatible LLM clients."""
    server = create_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    run_stdio_server()
