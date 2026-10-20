"""Transactional indexing pipeline coordinating scanner, parser, and database."""

import sqlite3
from pathlib import Path
from typing import Dict, Tuple, Union

from projectdna.db.repository import IndexRepository, ProjectRepository, normalize_path
from projectdna.indexer.exclusions import is_indexable_python_file
from projectdna.indexer.parser import parse_python_file
from projectdna.indexer.scanner import DirectoryScanner, normalize_root_path
from projectdna.models import ScanChangeSet


class IndexingPipeline:
    """Coordinates directory scanning, AST parsing, and atomic database persistence."""

    @staticmethod
    def validate_path_in_root(path: Union[str, Path], root_path: Union[str, Path]) -> Tuple[Path, str]:
        """
        Validate that path is strictly within the project root directory.
        Returns (resolved_abs_path, normalized_rel_path_str).
        """
        resolved_root = normalize_root_path(root_path)
        resolved_path = Path(path).resolve()

        try:
            rel = resolved_path.relative_to(resolved_root)
        except ValueError:
            raise ValueError(f"Path '{resolved_path}' is outside project root '{resolved_root}'")

        return resolved_path, rel.as_posix()

    @classmethod
    def index_project(
        cls,
        conn: sqlite3.Connection,
        project_id: int,
        root_path: Union[str, Path],
    ) -> Dict[str, int]:
        """
        Perform a full reconciliation index of a registered project.
        Detects added, modified, deleted files, parses AST, and commits atomically.
        """
        root = normalize_root_path(root_path)
        known_files = IndexRepository.get_files_by_project(conn, project_id)
        changes = DirectoryScanner.compute_changes(root, known_files)

        # 1. Process deletions
        for rel_path in changes.deleted:
            IndexRepository.delete_file_by_rel_path(conn, project_id, rel_path)

        # 2. Process additions and modifications
        for rel_path in changes.added + changes.modified:
            file_abs = root / rel_path
            if not file_abs.exists() or not is_indexable_python_file(file_abs, root_path=root):
                continue
            parsed = parse_python_file(file_abs, rel_path=rel_path, root_path=root)
            IndexRepository.upsert_indexed_file(conn, project_id, parsed)

        # 3. Update project last_scanned_at
        ProjectRepository.update_project_scanned_at(conn, project_id)

        return {
            "added": len(changes.added),
            "modified": len(changes.modified),
            "deleted": len(changes.deleted),
            "unchanged": len(changes.unchanged),
        }

    @classmethod
    def index_single_file(
        cls,
        conn: sqlite3.Connection,
        project_id: int,
        root_path: Union[str, Path],
        file_path: Union[str, Path],
    ) -> bool:
        """
        Index or delete an individual file event (e.g. from watcher).
        Validates boundaries and exclusion policies.
        """
        root = normalize_root_path(root_path)
        try:
            abs_p, rel_p = cls.validate_path_in_root(file_path, root)
        except ValueError:
            return False

        if not abs_p.exists():
            # File was removed
            return IndexRepository.delete_file_by_rel_path(conn, project_id, rel_p)

        if not is_indexable_python_file(abs_p, root_path=root):
            # If it became excluded (e.g. renamed or matched rule), purge from index
            IndexRepository.delete_file_by_rel_path(conn, project_id, rel_p)
            return False

        parsed = parse_python_file(abs_p, rel_path=rel_p, root_path=root)
        IndexRepository.upsert_indexed_file(conn, project_id, parsed)
        return True
