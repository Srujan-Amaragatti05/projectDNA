"""ProjectDNA background service daemon owning database writes, indexing, and IPC."""

import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from projectdna.config import (
    get_data_dir,
    get_db_path,
    get_lock_path,
    ensure_data_dir,
)
from projectdna.db.connection import get_rw_connection
from projectdna.db.migration import FileLock
from projectdna.db.repository import ProjectRepository, IndexRepository, normalize_path
from projectdna.db.schema import init_schema
from projectdna.indexer.pipeline import IndexingPipeline
from projectdna.models import ProjectRecord
from projectdna.service.ipc import (
    IPCServer,
    write_service_state,
    remove_service_state,
)
from projectdna.service.watcher import ProjectWatcherManager


class ProjectDNAService:
    """
    Background service owning single-writer database access, live watchers, and IPC.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = (data_dir or get_data_dir()).resolve()
        self.lock_path = self.data_dir / "writer.lock"
        self.db_path = self.data_dir / "dna.db"

        self.lock: Optional[FileLock] = None
        self.conn: Optional[sqlite3.Connection] = None
        self.ipc_server: Optional[IPCServer] = None
        self.watcher: Optional[ProjectWatcherManager] = None

        self._running = False
        self._started_at = 0.0
        self._db_lock = threading.Lock()

    def start(self, reconcile_on_start: bool = True) -> None:
        """
        Start the background service.
        1. Acquire exclusive OS file lock on writer.lock.
        2. Perform migration and schema initialization under this existing lock.
        3. Start IPC server and publish service.json.
        4. Reconcile registered projects and attach file watchers.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 1. Acquire OS file lock continuously
        self.lock = FileLock(self.lock_path, timeout=5.0)
        try:
            self.lock.acquire()
        except TimeoutError as e:
            raise RuntimeError(
                f"Another ProjectDNA background service or writer is already running on '{self.data_dir}'."
            ) from e

        try:
            # 2. Initialize database connection and schema under the already-held lock
            self.conn = get_rw_connection(existing_lock=self.lock)
            init_schema(self.conn)

            # 3. Start IPC server
            self.ipc_server = IPCServer(handler=self.dispatch_request)
            self.ipc_server.start()
            write_service_state(
                port=self.ipc_server.port,
                token=self.ipc_server.token,
                pid=os.getpid(),
            )

            # 4. Start file watcher manager
            self.watcher = ProjectWatcherManager(change_callback=self.on_file_changed)
            self.watcher.start()

            # 5. Reconcile active registered projects and attach watchers
            if reconcile_on_start:
                self._startup_reconciliation()

            self._running = True
            self._started_at = time.time()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Gracefully stop the background service and release resources."""
        self._running = False

        if self.watcher:
            try:
                self.watcher.stop()
            except Exception:
                pass
            self.watcher = None

        remove_service_state()

        if self.ipc_server:
            try:
                self.ipc_server.stop()
            except Exception:
                pass
            self.ipc_server = None

        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

        if self.lock:
            try:
                self.lock.release()
            except Exception:
                pass
            self.lock = None

    def run_forever(self) -> None:
        """Run service loop until interrupted."""
        self.start()

        def _handle_signal(signum, frame):
            self.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            pass

        try:
            while self._running:
                time.sleep(0.5)
        finally:
            self.stop()

    def _startup_reconciliation(self) -> None:
        """Reconcile all registered projects and attach watchers."""
        with self._db_lock:
            projects = ProjectRepository.list_projects(self.conn)
            for proj in projects:
                p_root = Path(proj.root_path)
                if p_root.exists() and p_root.is_dir():
                    try:
                        IndexingPipeline.index_project(self.conn, proj.id, p_root)
                    except Exception:
                        pass
                    if self.watcher:
                        self.watcher.watch_project(proj.id, proj.root_path)

    def on_file_changed(self, project_id: int, root_path: Path, file_path: Path) -> None:
        """Handle debounced filesystem event by updating database index."""
        if not self._running or not self.conn:
            return
        with self._db_lock:
            try:
                IndexingPipeline.index_single_file(self.conn, project_id, root_path, file_path)
            except Exception:
                pass

    def _resolve_project(self, identifier: str) -> Optional[ProjectRecord]:
        """Resolve project by ID, canonical root, or display name."""
        if identifier.isdigit():
            p = ProjectRepository.get_project_by_id(self.conn, int(identifier))
            if p:
                return p

        norm_root = normalize_path(identifier)
        p = ProjectRepository.get_project_by_root(self.conn, norm_root)
        if p:
            return p

        all_projects = ProjectRepository.list_projects(self.conn)
        matches = [x for x in all_projects if x.name.lower() == identifier.lower()]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            raise ValueError(
                f"Multiple projects found with name '{identifier}'. Specify project ID or root path."
            )
        return None

    def dispatch_request(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch IPC requests to corresponding service operations."""
        if action == "ping":
            return {
                "message": "pong",
                "pid": os.getpid(),
                "uptime": time.time() - self._started_at,
            }

        elif action == "add_project":
            path_str = payload.get("path")
            if not path_str:
                raise ValueError("Payload missing required 'path' parameter.")

            target_path = Path(path_str).resolve()
            if not target_path.exists():
                raise FileNotFoundError(f"Project directory does not exist: {target_path}")
            if not target_path.is_dir():
                raise NotADirectoryError(f"Path is not a directory: {target_path}")

            norm_root = normalize_path(target_path)
            project_name = payload.get("name") or target_path.name

            with self._db_lock:
                # Check if already registered
                existing = ProjectRepository.get_project_by_root(self.conn, norm_root)
                if existing:
                    proj = existing
                else:
                    proj = ProjectRepository.create_project(self.conn, project_name, norm_root)

                # Initial indexing
                summary = IndexingPipeline.index_project(self.conn, proj.id, target_path)

                # Attach watcher
                if self.watcher:
                    self.watcher.watch_project(proj.id, proj.root_path)

                return {
                    "project_id": proj.id,
                    "name": proj.name,
                    "root_path": proj.root_path,
                    "indexing": summary,
                }

        elif action == "scan_project":
            identifier = payload.get("identifier")
            if not identifier:
                raise ValueError("Payload missing required 'identifier' parameter.")

            with self._db_lock:
                proj = self._resolve_project(identifier)
                if not proj:
                    raise FileNotFoundError(f"Project '{identifier}' not found.")

                root_path = Path(proj.root_path)
                if not root_path.exists():
                    raise FileNotFoundError(f"Project root directory missing on disk: {root_path}")

                summary = IndexingPipeline.index_project(self.conn, proj.id, root_path)
                return {
                    "project_id": proj.id,
                    "name": proj.name,
                    "root_path": proj.root_path,
                    "changes": summary,
                }

        elif action == "remove_project":
            identifier = payload.get("identifier")
            if not identifier:
                raise ValueError("Payload missing required 'identifier' parameter.")

            with self._db_lock:
                proj = self._resolve_project(identifier)
                if not proj:
                    raise FileNotFoundError(f"Project '{identifier}' not found.")

                # Detach watcher
                if self.watcher:
                    self.watcher.unwatch_project(proj.id)

                # Delete from database (never touches original files!)
                success = ProjectRepository.delete_project(self.conn, proj.id)
                return {
                    "project_id": proj.id,
                    "name": proj.name,
                    "root_path": proj.root_path,
                    "removed": success,
                }

        elif action == "status":
            with self._db_lock:
                projects = ProjectRepository.list_projects(self.conn)
                return {
                    "pid": os.getpid(),
                    "uptime": time.time() - self._started_at,
                    "port": self.ipc_server.port if self.ipc_server else None,
                    "projects": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "root_path": p.root_path,
                            "last_scanned_at": str(p.last_scanned_at) if p.last_scanned_at else None,
                        }
                        for p in projects
                    ],
                }

        elif action == "stop":
            # Schedule delayed stop in a thread so this IPC response can be returned cleanly
            def _stop_soon():
                time.sleep(0.1)
                self.stop()

            threading.Thread(target=_stop_soon, daemon=True).start()
            return {"stopping": True}

        else:
            raise ValueError(f"Unknown action: '{action}'")


def main():
    service = ProjectDNAService()
    try:
        service.run_forever()
    except Exception as e:
        print(f"Service error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
