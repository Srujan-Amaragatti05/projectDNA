"""Exclusion and sensitive file filtering policy for ProjectDNA."""

import fnmatch
from pathlib import Path
from typing import Set, Tuple, Union

# Directories that must never be traversed or indexed
EXCLUDED_DIRECTORIES: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "env",
    ".env_dir",
    ".tox",
    ".nox",
    "site-packages",
    "build",
    "dist",
    "node_modules",
}

# Glob patterns for sensitive files and secret keys
SENSITIVE_FILE_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.cer",
    "*.crt",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "*credentials*.json",
    "*secret*.json",
    "*secret*.yaml",
    "*secret*.yml",
)

# Common non-source binary/artifact extensions
IGNORED_EXTENSIONS: Set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".db",
    ".sqlite",
    ".sqlite3",
}


def is_directory_excluded(dir_name: str) -> bool:
    """Check if a directory name matches excluded directory rules."""
    norm = dir_name.lower().rstrip("/\\")
    if norm in EXCLUDED_DIRECTORIES:
        return True
    if norm.endswith(".egg-info"):
        return True
    return False


def is_path_excluded(rel_or_abs_path: Union[str, Path], root_path: Union[str, Path, None] = None) -> bool:
    """
    Check if a file or directory path is excluded by directory exclusion rules
    or sensitive file pattern rules.

    Avoids blanket hidden-file exclusions so legitimate hidden Python source files
    (e.g., .test_script.py or within non-excluded folders) are preserved.
    """
    p = Path(rel_or_abs_path)
    
    # If root_path is provided, check relative parts
    if root_path is not None:
        try:
            p_rel = p.resolve().relative_to(Path(root_path).resolve())
            parts = p_rel.parts
        except ValueError:
            parts = p.parts
    else:
        parts = p.parts

    # Check each directory segment along the path
    for part in parts[:-1]:
        if is_directory_excluded(part):
            return True

    # Check the final component (file or directory name)
    name = parts[-1] if parts else p.name
    name_lower = name.lower()

    if is_directory_excluded(name_lower):
        return True

    # Check sensitive file patterns
    for pattern in SENSITIVE_FILE_PATTERNS:
        if fnmatch.fnmatch(name_lower, pattern.lower()):
            return True

    # Check ignored extensions
    suffix = p.suffix.lower()
    if suffix in IGNORED_EXTENSIONS:
        return True

    return False


def is_indexable_python_file(path: Union[str, Path], root_path: Union[str, Path, None] = None) -> bool:
    """
    Determine if a file is a candidate for Python source indexing:
    - Must be a file with .py or .pyw extension
    - Must not match any exclusion rule
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".py", ".pyw"):
        return False
    return not is_path_excluded(p, root_path=root_path)
