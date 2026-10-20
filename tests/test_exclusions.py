"""Tests for exclusion rules and sensitive file detection."""

from pathlib import Path
from projectdna.indexer.exclusions import (
    is_directory_excluded,
    is_path_excluded,
    is_indexable_python_file,
)


def test_directory_exclusions():
    assert is_directory_excluded(".git")
    assert is_directory_excluded("__pycache__")
    assert is_directory_excluded("venv")
    assert is_directory_excluded(".venv")
    assert is_directory_excluded("node_modules")
    assert is_directory_excluded("dist")
    assert is_directory_excluded("build")
    assert is_directory_excluded("my_pkg.egg-info")

    # Legitimate project folders should not be excluded
    assert not is_directory_excluded("src")
    assert not is_directory_excluded("projectdna")
    assert not is_directory_excluded("tests")


def test_sensitive_file_patterns():
    root = "D:/myproject"
    assert is_path_excluded(f"{root}/.env", root_path=root)
    assert is_path_excluded(f"{root}/.env.local", root_path=root)
    assert is_path_excluded(f"{root}/secrets.json", root_path=root)
    assert is_path_excluded(f"{root}/id_rsa", root_path=root)
    assert is_path_excluded(f"{root}/server.key", root_path=root)
    assert is_path_excluded(f"{root}/cert.pem", root_path=root)
    assert is_path_excluded(f"{root}/compiled.pyc", root_path=root)


def test_indexable_python_file():
    root = "D:/myproject"
    # Valid Python files
    assert is_indexable_python_file(f"{root}/main.py", root_path=root)
    assert is_indexable_python_file(f"{root}/pkg/sub/module.py", root_path=root)
    assert is_indexable_python_file(f"{root}/script.pyw", root_path=root)

    # Legitimate hidden Python file should NOT be blindly excluded
    assert is_indexable_python_file(f"{root}/.custom_test.py", root_path=root)

    # Non-Python files
    assert not is_indexable_python_file(f"{root}/README.md", root_path=root)
    assert not is_indexable_python_file(f"{root}/data.json", root_path=root)

    # Files inside excluded folders
    assert not is_indexable_python_file(f"{root}/.git/hooks/pre-commit.py", root_path=root)
    assert not is_indexable_python_file(f"{root}/venv/lib/site.py", root_path=root)
    assert not is_indexable_python_file(f"{root}/__pycache__/mod.cpython-312.pyc", root_path=root)
