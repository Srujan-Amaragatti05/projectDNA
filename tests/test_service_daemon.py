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


def test_service_simultaneous_ipc_database_operations(monkeypatch, tmp_path: Path):
    """Multiple concurrent IPC client threads performing database operations run safely without conflicts."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    data_dir = tmp_path / "data_concurrency"
    monkeypatch.setenv("PROJECTDNA_DATA_DIR", str(data_dir))

    # Setup projects
    p1 = tmp_path / "proj1"
    p1.mkdir()
    (p1 / "mod1.py").write_text("def f1(): return 1\n", encoding="utf-8")

    p2 = tmp_path / "proj2"
    p2.mkdir()
    (p2 / "mod2.py").write_text("def f2(): return 2\n", encoding="utf-8")

    p3 = tmp_path / "proj3"
    p3.mkdir()
    (p3 / "mod3.py").write_text("def f3(): return 3\n", encoding="utf-8")

    service = ProjectDNAService(data_dir=data_dir)
    service.start(reconcile_on_start=False)

    try:
        # Register p1 and p2 initially
        r1 = send_ipc_request("add_project", {"path": str(p1)})
        r2 = send_ipc_request("add_project", {"path": str(p2)})
        assert r1["project_id"] is not None
        assert r2["project_id"] is not None

        # Build list of concurrent tasks
        def _task(action: str, payload: dict):
            return send_ipc_request(action, payload)

        tasks = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            # Dispatch multiple scan_project, status, and add_project calls simultaneously
            futures = [
                executor.submit(_task, "status", {}),
                executor.submit(_task, "scan_project", {"identifier": "proj1"}),
                executor.submit(_task, "status", {}),
                executor.submit(_task, "scan_project", {"identifier": "proj2"}),
                executor.submit(_task, "add_project", {"path": str(p3)}),
                executor.submit(_task, "status", {}),
                executor.submit(_task, "scan_project", {"identifier": "proj1"}),
                executor.submit(_task, "scan_project", {"identifier": "proj2"}),
                executor.submit(_task, "status", {}),
                executor.submit(_task, "scan_project", {"identifier": "proj3"}),
            ]

            results = []
            for f in as_completed(futures):
                res = f.result()
                results.append(res)

        assert len(results) == len(futures)

        # Verify DB integrity and project state via read-only connection
        conn = get_ro_connection(data_dir / "dna.db")
        projects = ProjectRepository.list_projects(conn)
        assert len(projects) == 3
        proj_names = {p.name for p in projects}
        assert proj_names == {"proj1", "proj2", "proj3"}

        # Verify search works across the concurrently indexed projects
        syms1 = IndexRepository.search_symbols(conn, "f1")
        syms2 = IndexRepository.search_symbols(conn, "f2")
        syms3 = IndexRepository.search_symbols(conn, "f3")
        conn.close()

        assert len(syms1) == 1
        assert len(syms2) == 1
        assert len(syms3) == 1

    finally:
        service.stop()
