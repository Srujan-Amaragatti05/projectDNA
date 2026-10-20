"""Database schema definition and initialization for ProjectDNA."""

import sqlite3

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        root_path TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_scanned_at TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'active'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        rel_path TEXT NOT NULL,
        abs_path TEXT NOT NULL,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        last_indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        has_syntax_error INTEGER DEFAULT 0,
        error_message TEXT,
        CONSTRAINT uq_project_rel_path UNIQUE (project_id, rel_path)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        parent_symbol TEXT,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        docstring TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        module TEXT NOT NULL,
        name TEXT,
        alias TEXT,
        is_from INTEGER DEFAULT 0
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
        file_id UNINDEXED,
        content,
        tokenize = 'porter unicode61'
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(
        symbol_id UNINDEXED,
        file_id UNINDEXED,
        name,
        kind,
        docstring,
        tokenize = 'porter unicode61'
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
    """,
]


def init_schema(conn: sqlite3.Connection) -> None:
    """
    Initialize database schema and indices.
    Must only be called by the background service writer.
    """
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    try:
        for stmt in SCHEMA_STATEMENTS:
            cursor.execute(stmt)
        cursor.execute("COMMIT;")
    except Exception:
        cursor.execute("ROLLBACK;")
        raise
