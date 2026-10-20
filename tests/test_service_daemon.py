"""Tests for ProjectDNA background service daemon, continuous single-writer lock, and live watcher."""

import os
import sqlite3
import time
from pathlib import Path
import pytest

from projectdna.db.connection import get_ro_connection
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.service.daemon import ProjectDNAService
from projectdna.service.ipc import send_ipc_request, remove_service_state


def test_service_single_writer_lock_mutual_exclusion(monkeypatch, tmp_path: Path):
    """A running service holds writer.lock continuously; competing writers cannot start."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    service1 = ProjectDNAService(data_dir=data_dir)
    service1.start(reconcile_on_start=False)

    try:
        service2 = ProjectDNAService(data_dir=data_dir)
        with pytest.raises(RuntimeError) as exc_info:
            service2.start(reconcile_on_start=False)
        assert "already running" in str(exc_info.value)
    finally:
        service1.stop()


def test_service_ipc_mutations_and_source_safety(monkeypatch, tmp_path: Path):
    """add_project, scan_project, and remove_project execute via IPC without touching source files."""
    data_dir = tmp_path / "data_mutations"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    # Setup a sample project
    proj_dir = tmp_path / "sample_app"
    proj_dir.mkdir()
    source_file = proj_dir / "main.py"
    source_file.write_text("def run(): pass\n", encoding="utf-8")

    service = ProjectDNAService(data_dir=data_dir)
    service.start(reconcile_on_start=False)

    try:
        # 1. Register project via IPC
        add_res = send_ipc_request("add_project", {"path": str(proj_dir)})
        proj_id = add_res["project_id"]
        assert add_res["name"] == "sample_app"
        assert add_res["indexing"]["added"] == 1

        # Verify in read-only DB
        conn = get_ro_connection(data_dir / "dna.db")
        projects = ProjectRepository.list_projects(conn)
        assert len(projects) == 1
        files = IndexRepository.get_files_by_project(conn, proj_id)
        assert "main.py" in files
        conn.close()

        # 2. Modify source file and request manual scan via IPC
        time.sleep(0.01)
        source_file.write_text("def run(): pass\ndef new_func(): pass\n", encoding="utf-8")
        scan_res = send_ipc_request("scan_project", {"identifier": str(proj_id)})
        assert scan_res["changes"]["modified"] == 1

        # 3. Remove project via IPC
        rem_res = send_ipc_request("remove_project", {"identifier": str(proj_id)})
        assert rem_res["removed"] is True

        # Verify removed from database
        conn = get_ro_connection(data_dir / "dna.db")
        assert len(ProjectRepository.list_projects(conn)) == 0
        conn.close()

        # Critical constraint: Original source files must NOT be deleted!
        assert source_file.exists()
        assert "new_func" in source_file.read_text(encoding="utf-8")
    finally:
        service.stop()


def test_service_live_watchdog_monitoring(monkeypatch, tmp_path: Path):
    """Watchdog observer automatically detects and indexes new/modified/deleted files with debouncing."""
    data_dir = tmp_path / "data_watch"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    proj_dir = tmp_path / "watched_proj"
    proj_dir.mkdir()

    service = ProjectDNAService(data_dir=data_dir)
    service.start(reconcile_on_start=False)

    try:
        # Register project
        add_res = send_ipc_request("add_project", {"path": str(proj_dir)})
        proj_id = add_res["project_id"]

        # Create a new file in watched directory
        new_file = proj_dir / "worker.py"
        new_file.write_text(
            '''def perform_task(x: int) -> int:
    """Worker task."""
    return x * 10
''',
            encoding="utf-8",
        )

        # Wait for debounce timer (0.5s) to fire and index
        deadline = time.time() + 3.0
        indexed = False
        while time.time() < deadline:
            conn = get_ro_connection(data_dir / "dna.db")
            files = IndexRepository.get_files_by_project(conn, proj_id)
            conn.close()
            if "worker.py" in files:
                indexed = True
                break
            time.sleep(0.2)

        assert indexed, "worker.py was not indexed automatically by watchdog"

        # Check symbol was extracted
        conn = get_ro_connection(data_dir / "dna.db")
        syms = IndexRepository.search_symbols(conn, "perform_task", project_id=proj_id)
        conn.close()
        assert len(syms) == 1
        assert syms[0]["name"] == "perform_task"

        # Now delete the file and verify automatic removal from index
        new_file.unlink()
        deadline = time.time() + 3.0
        deleted_from_index = False
        while time.time() < deadline:
            conn = get_ro_connection(data_dir / "dna.db")
            files = IndexRepository.get_files_by_project(conn, proj_id)
            conn.close()
            if "worker.py" not in files:
                deleted_from_index = True
                break
            time.sleep(0.2)

        assert deleted_from_index, "worker.py was not removed from index after deletion"

    finally:
        service.stop()
