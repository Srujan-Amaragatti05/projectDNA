"""AST parser for extracting Python symbols, imports, and docstrings."""

import ast
import hashlib
from pathlib import Path
from typing import List, Optional, Tuple, Union

from projectdna.config import MAX_INDEXABLE_FILE_SIZE_BYTES
from projectdna.models import SymbolRecord, ImportRecord, ParsedFile


class SymbolVisitor(ast.NodeVisitor):
    """AST visitor to collect classes, functions, methods, and imports with hierarchy."""

    def __init__(self, file_id: int = 0):
        self.file_id = file_id
        self.symbols: List[SymbolRecord] = []
        self.imports: List[ImportRecord] = []
        self._scope_stack: List[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = ".".join(self._scope_stack) if self._scope_stack else None
        docstring = ast.get_docstring(node)
        end_line = getattr(node, "end_lineno", node.lineno)

        sym = SymbolRecord(
            id=None,
            file_id=self.file_id,
            name=node.name,
            kind="class",
            parent_symbol=parent,
            start_line=node.lineno,
            end_line=end_line,
            docstring=docstring,
        )
        self.symbols.append(sym)

        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        parent = ".".join(self._scope_stack) if self._scope_stack else None
        kind = "method" if self._scope_stack else "function"
        docstring = ast.get_docstring(node)
        end_line = getattr(node, "end_lineno", node.lineno)

        sym = SymbolRecord(
            id=None,
            file_id=self.file_id,
            name=node.name,
            kind=kind,
            parent_symbol=parent,
            start_line=node.lineno,
            end_line=end_line,
            docstring=docstring,
        )
        self.symbols.append(sym)

        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        parent = ".".join(self._scope_stack) if self._scope_stack else None
        kind = "method" if self._scope_stack else "async_function"
        docstring = ast.get_docstring(node)
        end_line = getattr(node, "end_lineno", node.lineno)

        sym = SymbolRecord(
            id=None,
            file_id=self.file_id,
            name=node.name,
            kind=kind,
            parent_symbol=parent,
            start_line=node.lineno,
            end_line=end_line,
            docstring=docstring,
        )
        self.symbols.append(sym)

        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            imp = ImportRecord(
                id=None,
                file_id=self.file_id,
                module=alias.name,
                name=None,
                alias=alias.asname,
                is_from=False,
            )
            self.imports.append(imp)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            imp = ImportRecord(
                id=None,
                file_id=self.file_id,
                module=module,
                name=alias.name,
                alias=alias.asname,
                is_from=True,
            )
            self.imports.append(imp)


def compute_file_sha256(content_bytes: bytes) -> str:
    return hashlib.sha256(content_bytes).hexdigest()


def parse_python_source(
    content: str,
    rel_path: str,
    abs_path: str,
    mtime: float,
    size: int,
    sha256: str,
) -> ParsedFile:
    """
    Parse Python source text into a ParsedFile structure.
    Catches SyntaxError gracefully while preserving raw content for search.
    """
    has_syntax_error = False
    error_message: Optional[str] = None
    symbols: List[SymbolRecord] = []
    imports: List[ImportRecord] = []

    try:
        tree = ast.parse(content, filename=rel_path)
        visitor = SymbolVisitor()
        visitor.visit(tree)
        symbols = visitor.symbols
        imports = visitor.imports
    except SyntaxError as e:
        has_syntax_error = True
        error_message = f"SyntaxError at line {e.lineno}, col {e.offset}: {e.msg}"
    except Exception as e:
        has_syntax_error = True
        error_message = f"Parse error: {str(e)}"

    return ParsedFile(
        rel_path=rel_path,
        abs_path=abs_path,
        mtime=mtime,
        size=size,
        sha256=sha256,
        content=content,
        symbols=symbols,
        imports=imports,
        has_syntax_error=has_syntax_error,
        error_message=error_message,
    )


def parse_python_file(
    abs_path: Union[str, Path],
    rel_path: Optional[str] = None,
    root_path: Optional[Union[str, Path]] = None,
) -> ParsedFile:
    """
    Read and parse a Python file from disk.
    Handles encoding issues, missing files, and oversized files safely.
    """
    path_obj = Path(abs_path).resolve()
    norm_abs = path_obj.as_posix()

    if rel_path is None:
        if root_path is not None:
            norm_rel = path_obj.relative_to(Path(root_path).resolve()).as_posix()
        else:
            norm_rel = path_obj.name
    else:
        norm_rel = Path(rel_path).as_posix()

    stat = path_obj.stat()
    mtime = stat.st_mtime
    size = stat.st_size

    if size > MAX_INDEXABLE_FILE_SIZE_BYTES:
        return ParsedFile(
            rel_path=norm_rel,
            abs_path=norm_abs,
            mtime=mtime,
            size=size,
            sha256="",
            content="",
            symbols=[],
            imports=[],
            has_syntax_error=True,
            error_message=f"File size ({size} bytes) exceeds maximum indexable limit ({MAX_INDEXABLE_FILE_SIZE_BYTES} bytes)",
        )

    content_bytes = path_obj.read_bytes()
    sha256 = compute_file_sha256(content_bytes)

    # Decode with replace to ensure no unhandled decode exceptions
    content = content_bytes.decode("utf-8", errors="replace")

    return parse_python_source(
        content=content,
        rel_path=norm_rel,
        abs_path=norm_abs,
        mtime=mtime,
        size=size,
        sha256=sha256,
    )
