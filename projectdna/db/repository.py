"""Repository for ProjectDNA database operations."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from projectdna.models import (
    ProjectRecord,
    FileRecord,
    SymbolRecord,
    ImportRecord,
    ParsedFile,
)


def normalize_path(path: str) -> str:
    """Normalize path to canonical forward-slash format."""
    return Path(path).resolve().as_posix()


class ProjectRepository:
    """Project management repository."""

    @staticmethod
    def create_project(conn: sqlite3.Connection, name: str, root_path: str) -> ProjectRecord:
        norm_root = normalize_path(root_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO projects (name, root_path, created_at, status)
            VALUES (?, ?, CURRENT_TIMESTAMP, 'active')
            """,
            (name, norm_root),
        )
        project_id = cur.lastrowid
        return ProjectRecord(
            id=project_id,
            name=name,
            root_path=norm_root,
            status="active"
        )

    @staticmethod
    def get_project_by_id(conn: sqlite3.Connection, project_id: int) -> Optional[ProjectRecord]:
        cur = conn.cursor()
        cur.execute("SELECT id, name, root_path, created_at, last_scanned_at, status FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            created_at=row["created_at"],
            last_scanned_at=row["last_scanned_at"],
            status=row["status"],
        )

    @staticmethod
    def get_project_by_root(conn: sqlite3.Connection, root_path: str) -> Optional[ProjectRecord]:
        norm_root = normalize_path(root_path)
        cur = conn.cursor()
        cur.execute("SELECT id, name, root_path, created_at, last_scanned_at, status FROM projects WHERE root_path = ?", (norm_root,))
        row = cur.fetchone()
        if not row:
            return None
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            created_at=row["created_at"],
            last_scanned_at=row["last_scanned_at"],
            status=row["status"],
        )

    @staticmethod
    def list_projects(conn: sqlite3.Connection) -> List[ProjectRecord]:
        cur = conn.cursor()
        cur.execute("SELECT id, name, root_path, created_at, last_scanned_at, status FROM projects ORDER BY name ASC")
        return [
            ProjectRecord(
                id=row["id"],
                name=row["name"],
                root_path=row["root_path"],
                created_at=row["created_at"],
                last_scanned_at=row["last_scanned_at"],
                status=row["status"],
            )
            for row in cur.fetchall()
        ]

    @classmethod
    def resolve_project(cls, conn: sqlite3.Connection, identifier: str) -> Optional[ProjectRecord]:
        """Resolve a project by integer ID, canonical root path, or case-insensitive name."""
        if not identifier or not identifier.strip():
            return None
        clean_id = identifier.strip()
        if clean_id.isdigit():
            p = cls.get_project_by_id(conn, int(clean_id))
            if p:
                return p

        norm_root = normalize_path(clean_id)
        p = cls.get_project_by_root(conn, norm_root)
        if p:
            return p

        all_projects = cls.list_projects(conn)
        matches = [x for x in all_projects if x.name.lower() == clean_id.lower()]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            raise ValueError(
                f"Multiple projects found with name '{clean_id}'. Specify project ID or root path."
            )
        return None

    @staticmethod
    def update_project_scanned_at(conn: sqlite3.Connection, project_id: int) -> None:
        cur = conn.cursor()
        cur.execute("UPDATE projects SET last_scanned_at = CURRENT_TIMESTAMP WHERE id = ?", (project_id,))

    @staticmethod
    def delete_project(conn: sqlite3.Connection, project_id: int) -> bool:
        """
        Delete a project and all associated files, symbols, imports, and FTS entries.
        Explicitly removes FTS rows within the transaction.
        """
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            # First clean up FTS entries for all files in this project
            cur.execute(
                """
                DELETE FROM code_fts
                WHERE file_id IN (SELECT id FROM files WHERE project_id = ?)
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM symbol_fts
                WHERE file_id IN (SELECT id FROM files WHERE project_id = ?)
                """,
                (project_id,),
            )
            # Cascade deletes files, symbols, imports via FOREIGN KEY ON DELETE CASCADE
            cur.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            deleted = cur.rowcount > 0  # Capture before COMMIT resets rowcount
            cur.execute("COMMIT;")
            return deleted
        except Exception:
            cur.execute("ROLLBACK;")
            raise


class IndexRepository:
    """File and symbol indexing repository."""

    @staticmethod
    def get_files_by_project(conn: sqlite3.Connection, project_id: int) -> Dict[str, FileRecord]:
        """Return a mapping of rel_path -> FileRecord for a project."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, project_id, rel_path, abs_path, mtime, size, sha256,
                   last_indexed_at, has_syntax_error, error_message
            FROM files
            WHERE project_id = ?
            """,
            (project_id,),
        )
        result = {}
        for row in cur.fetchall():
            rec = FileRecord(
                id=row["id"],
                project_id=row["project_id"],
                rel_path=row["rel_path"],
                abs_path=row["abs_path"],
                mtime=row["mtime"],
                size=row["size"],
                sha256=row["sha256"],
                last_indexed_at=row["last_indexed_at"],
                has_syntax_error=bool(row["has_syntax_error"]),
                error_message=row["error_message"],
            )
            result[row["rel_path"]] = rec
        return result

    @staticmethod
    def get_file_by_id(conn: sqlite3.Connection, file_id: int) -> Optional[FileRecord]:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, project_id, rel_path, abs_path, mtime, size, sha256,
                   last_indexed_at, has_syntax_error, error_message
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return FileRecord(
            id=row["id"],
            project_id=row["project_id"],
            rel_path=row["rel_path"],
            abs_path=row["abs_path"],
            mtime=row["mtime"],
            size=row["size"],
            sha256=row["sha256"],
            last_indexed_at=row["last_indexed_at"],
            has_syntax_error=bool(row["has_syntax_error"]),
            error_message=row["error_message"],
        )

    @staticmethod
    def get_file_by_rel_path(conn: sqlite3.Connection, project_id: int, rel_path: str) -> Optional[FileRecord]:
        """Return FileRecord for a specific project and relative path."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, project_id, rel_path, abs_path, mtime, size, sha256,
                   last_indexed_at, has_syntax_error, error_message
            FROM files
            WHERE project_id = ? AND rel_path = ?
            """,
            (project_id, rel_path),
        )
        row = cur.fetchone()
        if not row:
            return None
        return FileRecord(
            id=row["id"],
            project_id=row["project_id"],
            rel_path=row["rel_path"],
            abs_path=row["abs_path"],
            mtime=row["mtime"],
            size=row["size"],
            sha256=row["sha256"],
            last_indexed_at=row["last_indexed_at"],
            has_syntax_error=bool(row["has_syntax_error"]),
            error_message=row["error_message"],
        )

    @staticmethod
    def get_symbols_by_file_id(conn: sqlite3.Connection, file_id: int) -> List[Dict[str, Any]]:
        """Return all symbols extracted from a file ordered by line."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, kind, parent_symbol, start_line, end_line, docstring
            FROM symbols
            WHERE file_id = ?
            ORDER BY start_line ASC
            """,
            (file_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def get_imports_by_file_id(conn: sqlite3.Connection, file_id: int) -> List[Dict[str, Any]]:
        """Return all imports extracted from a file."""
        cur = conn.cursor()
        cur.execute(
            """
            SELECT module, name, alias, is_from
            FROM imports
            WHERE file_id = ?
            ORDER BY id ASC
            """,
            (file_id,),
        )
        return [
            {
                "module": row["module"],
                "name": row["name"],
                "alias": row["alias"],
                "is_from": bool(row["is_from"]),
            }
            for row in cur.fetchall()
        ]

    @staticmethod
    def upsert_indexed_file(conn: sqlite3.Connection, project_id: int, parsed: ParsedFile) -> int:
        """
        Atomically insert or update a file, replacing its symbols, imports, and FTS entries.
        Guarantees that all related FTS rows are cleanly deleted and recreated.
        """
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            # Check if file record already exists
            cur.execute(
                "SELECT id FROM files WHERE project_id = ? AND rel_path = ?",
                (project_id, parsed.rel_path),
            )
            row = cur.fetchone()

            if row:
                file_id = row["id"]
                # Clean up existing FTS rows explicitly
                cur.execute("DELETE FROM code_fts WHERE file_id = ?", (file_id,))
                cur.execute("DELETE FROM symbol_fts WHERE file_id = ?", (file_id,))
                
                # Delete existing child symbols and imports
                cur.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
                cur.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))

                # Update file record
                cur.execute(
                    """
                    UPDATE files
                    SET abs_path = ?, mtime = ?, size = ?, sha256 = ?,
                        last_indexed_at = CURRENT_TIMESTAMP,
                        has_syntax_error = ?, error_message = ?
                    WHERE id = ?
                    """,
                    (
                        parsed.abs_path,
                        parsed.mtime,
                        parsed.size,
                        parsed.sha256,
                        1 if parsed.has_syntax_error else 0,
                        parsed.error_message,
                        file_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO files (
                        project_id, rel_path, abs_path, mtime, size, sha256,
                        last_indexed_at, has_syntax_error, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                    """,
                    (
                        project_id,
                        parsed.rel_path,
                        parsed.abs_path,
                        parsed.mtime,
                        parsed.size,
                        parsed.sha256,
                        1 if parsed.has_syntax_error else 0,
                        parsed.error_message,
                    ),
                )
                file_id = cur.lastrowid

            # Insert symbols and their FTS entries
            for sym in parsed.symbols:
                cur.execute(
                    """
                    INSERT INTO symbols (file_id, name, kind, parent_symbol, start_line, end_line, docstring)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        sym.name,
                        sym.kind,
                        sym.parent_symbol,
                        sym.start_line,
                        sym.end_line,
                        sym.docstring,
                    ),
                )
                symbol_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO symbol_fts (symbol_id, file_id, name, kind, docstring)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (symbol_id, file_id, sym.name, sym.kind, sym.docstring or ""),
                )

            # Insert imports
            for imp in parsed.imports:
                cur.execute(
                    """
                    INSERT INTO imports (file_id, module, name, alias, is_from)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (file_id, imp.module, imp.name, imp.alias, 1 if imp.is_from else 0),
                )

            # Insert code into code_fts (even if syntax error, source text is indexed for search)
            cur.execute(
                "INSERT INTO code_fts (file_id, content) VALUES (?, ?)",
                (file_id, parsed.content),
            )

            cur.execute("COMMIT;")
            return file_id
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    @staticmethod
    def delete_file_by_rel_path(conn: sqlite3.Connection, project_id: int, rel_path: str) -> bool:
        """
        Delete a file and explicitly remove its symbols, imports, and FTS entries.
        """
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            cur.execute(
                "SELECT id FROM files WHERE project_id = ? AND rel_path = ?",
                (project_id, rel_path),
            )
            row = cur.fetchone()
            if not row:
                cur.execute("COMMIT;")
                return False

            file_id = row["id"]
            cur.execute("DELETE FROM code_fts WHERE file_id = ?", (file_id,))
            cur.execute("DELETE FROM symbol_fts WHERE file_id = ?", (file_id,))
            cur.execute("DELETE FROM files WHERE id = ?", (file_id,))
            cur.execute("COMMIT;")
            return True
        except Exception:
            cur.execute("ROLLBACK;")
            raise

    @staticmethod
    def search_code(
        conn: sqlite3.Connection, query: str, project_id: Optional[int] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search code_fts and join with files and projects."""
        cur = conn.cursor()
        if project_id is not None:
            sql = """
                SELECT f.id AS file_id, f.rel_path, f.abs_path, f.mtime, f.size, f.sha256, f.has_syntax_error, f.error_message,
                       p.id AS project_id, p.name AS project_name, p.root_path,
                       snippet(code_fts, 1, '[MATCH]', '[/MATCH]', '...', 15) AS snippet
                FROM code_fts
                JOIN files f ON f.id = code_fts.file_id
                JOIN projects p ON p.id = f.project_id
                WHERE code_fts MATCH ? AND f.project_id = ?
                LIMIT ?
            """
            cur.execute(sql, (query, project_id, limit))
        else:
            sql = """
                SELECT f.id AS file_id, f.rel_path, f.abs_path, f.mtime, f.size, f.sha256, f.has_syntax_error, f.error_message,
                       p.id AS project_id, p.name AS project_name, p.root_path,
                       snippet(code_fts, 1, '[MATCH]', '[/MATCH]', '...', 15) AS snippet
                FROM code_fts
                JOIN files f ON f.id = code_fts.file_id
                JOIN projects p ON p.id = f.project_id
                WHERE code_fts MATCH ?
                LIMIT ?
            """
            cur.execute(sql, (query, limit))
        return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def search_symbols(
        conn: sqlite3.Connection, query: str, project_id: Optional[int] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search symbol_fts and join with symbols, files, and projects."""
        cur = conn.cursor()
        if project_id is not None:
            sql = """
                SELECT s.id AS symbol_id, s.name, s.kind, s.parent_symbol, s.start_line, s.end_line, s.docstring,
                       f.id AS file_id, f.rel_path, f.abs_path, f.mtime, f.size, f.sha256,
                       p.id AS project_id, p.name AS project_name, p.root_path
                FROM symbol_fts
                JOIN symbols s ON s.id = symbol_fts.symbol_id
                JOIN files f ON f.id = s.file_id
                JOIN projects p ON p.id = f.project_id
                WHERE symbol_fts MATCH ? AND f.project_id = ?
                LIMIT ?
            """
            cur.execute(sql, (query, project_id, limit))
        else:
            sql = """
                SELECT s.id AS symbol_id, s.name, s.kind, s.parent_symbol, s.start_line, s.end_line, s.docstring,
                       f.id AS file_id, f.rel_path, f.abs_path, f.mtime, f.size, f.sha256,
                       p.id AS project_id, p.name AS project_name, p.root_path
                FROM symbol_fts
                JOIN symbols s ON s.id = symbol_fts.symbol_id
                JOIN files f ON f.id = s.file_id
                JOIN projects p ON p.id = f.project_id
                WHERE symbol_fts MATCH ?
                LIMIT ?
            """
            cur.execute(sql, (query, limit))
        return [dict(row) for row in cur.fetchall()]
