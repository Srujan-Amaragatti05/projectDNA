"""Data models and transfer objects for ProjectDNA."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


@dataclass
class ProjectRecord:
    id: Optional[int]
    name: str
    root_path: str
    created_at: Optional[datetime] = None
    last_scanned_at: Optional[datetime] = None
    status: str = "active"


@dataclass
class FileRecord:
    id: Optional[int]
    project_id: int
    rel_path: str
    abs_path: str
    mtime: float
    size: int
    sha256: str
    last_indexed_at: Optional[datetime] = None
    has_syntax_error: bool = False
    error_message: Optional[str] = None


@dataclass
class SymbolRecord:
    id: Optional[int]
    file_id: int
    name: str
    kind: str  # 'class', 'function', 'async_function', 'method'
    parent_symbol: Optional[str] = None
    start_line: int = 0
    end_line: int = 0
    docstring: Optional[str] = None


@dataclass
class ImportRecord:
    id: Optional[int]
    file_id: int
    module: str
    name: Optional[str] = None
    alias: Optional[str] = None
    is_from: bool = False


@dataclass
class ParsedFile:
    rel_path: str
    abs_path: str
    mtime: float
    size: int
    sha256: str
    content: str
    symbols: List[SymbolRecord] = field(default_factory=list)
    imports: List[ImportRecord] = field(default_factory=list)
    has_syntax_error: bool = False
    error_message: Optional[str] = None


@dataclass
class ScanChangeSet:
    added: List[str] = field(default_factory=list)      # relative paths
    modified: List[str] = field(default_factory=list)   # relative paths
    deleted: List[str] = field(default_factory=list)    # relative paths
    unchanged: List[str] = field(default_factory=list)  # relative paths
