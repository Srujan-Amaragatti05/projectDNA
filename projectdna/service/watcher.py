"""Watchdog-based project file monitoring with debouncing."""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from projectdna.indexer.exclusions import (
    is_directory_excluded,
    is_indexable_python_file,
    is_path_excluded,
)

DEBOUNCE_DELAY_SECONDS = 0.5


class DebouncedChangeHandler(FileSystemEventHandler):
    """
    Filesystem event handler that filters excluded files and debounces burst events.
    """

    def __init__(
        self,
        project_id: int,
        root_path: Path,
        change_callback: Callable[[int, Path, Path], None],
        debounce_delay: float = DEBOUNCE_DELAY_SECONDS,
    ):
        super().__init__()
        self.project_id = project_id
        self.root_path = root_path.resolve()
        self.change_callback = change_callback
        self.debounce_delay = debounce_delay

        self._lock = threading.Lock()
        self._timers: Dict[str, threading.Timer] = {}

    def _should_handle(self, path_str: str) -> bool:
        p = Path(path_str).resolve()
        # Fast directory exclusion check
        if is_path_excluded(p, root_path=self.root_path):
            return False

        # If it's a file, it must be python source or deleted python source
        suffix = p.suffix.lower()
        if suffix not in (".py", ".pyw"):
            return False
        return True

    def _enqueue_change(self, file_path_str: str) -> None:
        if not self._should_handle(file_path_str):
            return

        norm_path = Path(file_path_str).resolve()

        with self._lock:
            key = norm_path.as_posix()
            if key in self._timers:
                self._timers[key].cancel()

            timer = threading.Timer(
                self.debounce_delay,
                self._fire_change,
                args=(norm_path,),
            )
            self._timers[key] = timer
            timer.start()

    def _fire_change(self, file_path: Path) -> None:
        with self._lock:
            self._timers.pop(file_path.as_posix(), None)

        try:
            self.change_callback(self.project_id, self.root_path, file_path)
        except Exception:
            pass

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue_change(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue_change(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue_change(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue_change(event.src_path)
            if hasattr(event, "dest_path"):
                self._enqueue_change(event.dest_path)

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class ProjectWatcherManager:
    """Manages active watchdog observers across all registered projects."""

    def __init__(self, change_callback: Callable[[int, Path, Path], None]):
        self.change_callback = change_callback
        self._observer: Optional[Observer] = None
        self._watches: Dict[int, Any] = {}  # project_id -> watchdog watch object
        self._handlers: Dict[int, DebouncedChangeHandler] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._observer is None:
                self._observer = Observer()
                self._observer.daemon = True
                self._observer.start()

    def stop(self) -> None:
        with self._lock:
            for handler in self._handlers.values():
                handler.cancel_all()
            self._handlers.clear()

            if self._observer:
                try:
                    self._observer.stop()
                    self._observer.join(timeout=2.0)
                except Exception:
                    pass
                self._observer = None
                self._watches.clear()

    def watch_project(self, project_id: int, root_path: str) -> bool:
        p = Path(root_path).resolve()
        if not p.exists() or not p.is_dir():
            return False

        with self._lock:
            if self._observer is None:
                self.start()

            # If already watching this project, unwatch first to refresh
            if project_id in self._watches:
                self.unwatch_project(project_id)

            handler = DebouncedChangeHandler(
                project_id=project_id,
                root_path=p,
                change_callback=self.change_callback,
            )
            try:
                watch = self._observer.schedule(handler, str(p), recursive=True)
                self._watches[project_id] = watch
                self._handlers[project_id] = handler
                return True
            except Exception:
                return False

    def unwatch_project(self, project_id: int) -> None:
        with self._lock:
            handler = self._handlers.pop(project_id, None)
            if handler:
                handler.cancel_all()

            watch = self._watches.pop(project_id, None)
            if watch and self._observer:
                try:
                    self._observer.unschedule(watch)
                except Exception:
                    pass
