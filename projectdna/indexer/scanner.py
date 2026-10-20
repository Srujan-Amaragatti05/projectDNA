"""Directory scanner and change detector for ProjectDNA."""

import os
from pathlib import Path
from typing import Dict, List, Set, Union

from projectdna.indexer.exclusions import (
    is_directory_excluded,
    is_indexable_python_file,
    is_path_excluded,
)
from projectdna.models import FileRecord, ScanChangeSet


def normalize_root_path(root_path: Union[str, Path]) -> Path:
    """Normalize and resolve root path to a strict canonical Path object."""
    p = Path(root_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Project root does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Project root is not a directory: {p}")
    return p


class DirectoryScanner:
    """Scans project directories and computes change sets against indexed records."""

    @staticmethod
    def scan_project_files(root_path: Union[str, Path]) -> Dict[str, Path]:
        """
        Walk project directory tree, prune excluded directories,
        and return mapping of canonical rel_path (forward-slash) -> absolute Path.
        """
        root = normalize_root_path(root_path)
        found_files: Dict[str, Path] = {}

        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # Prune excluded directories in-place so os.walk does not descend into them
            dirnames[:] = [
                d for d in dirnames
                if not is_directory_excluded(d) and not is_path_excluded(Path(dirpath) / d, root_path=root)
            ]

            for fname in filenames:
                file_path = Path(dirpath) / fname
                if is_indexable_python_file(file_path, root_path=root):
                    rel_p = file_path.relative_to(root).as_posix()
                    found_files[rel_p] = file_path

        return found_files

    @classmethod
    def compute_changes(
        cls,
        root_path: Union[str, Path],
        known_files: Dict[str, FileRecord],
    ) -> ScanChangeSet:
        """
        Compare current on-disk Python files with indexed database records.
        Returns a ScanChangeSet with added, modified, deleted, and unchanged lists.
        """
        current_disk_files = cls.scan_project_files(root_path)
        disk_keys: Set[str] = set(current_disk_files.keys())
        known_keys: Set[str] = set(known_files.keys())

        added: List[str] = []
        modified: List[str] = []
        unchanged: List[str] = []
        deleted: List[str] = sorted(list(known_keys - disk_keys))

        for rel_path in sorted(disk_keys):
            disk_path = current_disk_files[rel_path]
            if rel_path not in known_keys:
                added.append(rel_path)
            else:
                known = known_files[rel_path]
                try:
                    stat = disk_path.stat()
                    # Check mtime and size first for high performance
                    if stat.st_mtime != known.mtime or stat.st_size != known.size:
                        modified.append(rel_path)
                    else:
                        unchanged.append(rel_path)
                except OSError:
                    # In case of file read error during stat, treat as modified/retryable
                    modified.append(rel_path)

        return ScanChangeSet(
            added=added,
            modified=modified,
            deleted=deleted,
            unchanged=unchanged,
        )
