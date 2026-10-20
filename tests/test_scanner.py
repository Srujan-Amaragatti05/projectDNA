"""Tests for directory scanning and change computation."""

import time
from pathlib import Path
from projectdna.indexer.scanner import DirectoryScanner
from projectdna.models import FileRecord


def test_scan_project_files(tmp_path: Path):
    # Setup test folder structure
    (tmp_path / "app.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "helper.py").write_text("def help(): pass", encoding="utf-8")
    (tmp_path / ".custom.py").write_text("# custom hidden file", encoding="utf-8")

    # Excluded directories
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("# ignored", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "site.py").write_text("# ignored", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "temp.py").write_text("# ignored", encoding="utf-8")

    # Sensitive files
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")

    files = DirectoryScanner.scan_project_files(tmp_path)
    rel_paths = set(files.keys())

    assert "app.py" in rel_paths
    assert "utils/helper.py" in rel_paths
    assert ".custom.py" in rel_paths

    # Ensure excluded directories and files are omitted
    assert ".git/config.py" not in rel_paths
    assert "venv/site.py" not in rel_paths
    assert "build/temp.py" not in rel_paths
    assert ".env" not in rel_paths
    assert "README.md" not in rel_paths


def test_compute_changes(tmp_path: Path):
    from projectdna.indexer.parser import compute_file_sha256

    f1 = tmp_path / "mod1.py"
    f1.write_text("x = 1", encoding="utf-8")
    f2 = tmp_path / "mod2.py"
    f2.write_text("y = 2", encoding="utf-8")

    stat1 = f1.stat()
    stat2 = f2.stat()

    known_files = {
        "mod1.py": FileRecord(
            id=1, project_id=1, rel_path="mod1.py", abs_path=f1.as_posix(),
            mtime=stat1.st_mtime, size=stat1.st_size, sha256=compute_file_sha256(f1.read_bytes())
        ),
        "deleted_file.py": FileRecord(
            id=2, project_id=1, rel_path="deleted_file.py", abs_path="deleted_file.py",
            mtime=1.0, size=10, sha256="hash2"
        ),
    }

    # mod1 is unchanged, mod2 is added, deleted_file.py is deleted
    changes = DirectoryScanner.compute_changes(tmp_path, known_files)

    assert changes.unchanged == ["mod1.py"]
    assert changes.added == ["mod2.py"]
    assert changes.deleted == ["deleted_file.py"]
    assert changes.modified == []

    # Modify mod1
    time.sleep(0.01)
    f1.write_text("x = 1000", encoding="utf-8")
    changes2 = DirectoryScanner.compute_changes(tmp_path, known_files)
    assert "mod1.py" in changes2.modified


def test_compute_changes_restored_mtime_and_size(tmp_path: Path):
    """File content change is detected even if size and mtime are restored to indexed values."""
    import os
    from projectdna.indexer.parser import compute_file_sha256

    f = tmp_path / "app.py"
    initial_content = "val = 100\n"  # 10 bytes
    f.write_text(initial_content, encoding="utf-8")

    stat = f.stat()
    orig_mtime = stat.st_mtime
    orig_size = stat.st_size
    orig_sha256 = compute_file_sha256(f.read_bytes())

    known_files = {
        "app.py": FileRecord(
            id=1,
            project_id=1,
            rel_path="app.py",
            abs_path=f.as_posix(),
            mtime=orig_mtime,
            size=orig_size,
            sha256=orig_sha256,
        )
    }

    # Verify initially unchanged
    changes = DirectoryScanner.compute_changes(tmp_path, known_files)
    assert changes.unchanged == ["app.py"]
    assert changes.modified == []

    # Change content with EXACT same length (10 bytes) and restore mtime
    new_content = "val = 999\n"  # Also 10 bytes
    assert len(new_content) == len(initial_content)
    f.write_text(new_content, encoding="utf-8")
    os.utime(f, (orig_mtime, orig_mtime))

    # Verify stat matches original values
    new_stat = f.stat()
    assert new_stat.st_mtime == orig_mtime
    assert new_stat.st_size == orig_size

    # compute_changes must detect the modification via content hash
    changes_after = DirectoryScanner.compute_changes(tmp_path, known_files)
    assert "app.py" in changes_after.modified
    assert "app.py" not in changes_after.unchanged
