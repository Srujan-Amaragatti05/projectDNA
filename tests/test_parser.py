"""Tests for AST parsing and symbol extraction."""

from projectdna.indexer.parser import parse_python_source, parse_python_file


def test_parse_valid_python_symbols():
    code = '''"""Module docstring."""
import os
from sys import path as sys_path

def top_function(x: int) -> int:
    """A top level function."""
    return x * 2

async def async_worker():
    """An async coroutine."""
    pass

class DataProcessor:
    """Class docstring."""
    
    def __init__(self):
        pass

    async def fetch(self):
        """Method docstring."""
        pass
'''
    parsed = parse_python_source(
        content=code,
        rel_path="processor.py",
        abs_path="/test/processor.py",
        mtime=100.0,
        size=len(code),
        sha256="abc123hash",
    )

    assert not parsed.has_syntax_error
    assert parsed.error_message is None

    # Check symbols
    sym_map = {s.name: s for s in parsed.symbols}
    assert "top_function" in sym_map
    assert sym_map["top_function"].kind == "function"
    assert sym_map["top_function"].docstring == "A top level function."

    assert "async_worker" in sym_map
    assert sym_map["async_worker"].kind == "async_function"

    assert "DataProcessor" in sym_map
    assert sym_map["DataProcessor"].kind == "class"
    assert sym_map["DataProcessor"].docstring == "Class docstring."

    assert "__init__" in sym_map
    assert sym_map["__init__"].kind == "method"
    assert sym_map["__init__"].parent_symbol == "DataProcessor"

    assert "fetch" in sym_map
    assert sym_map["fetch"].kind == "method"
    assert sym_map["fetch"].parent_symbol == "DataProcessor"

    # Check imports
    imports = parsed.imports
    assert any(i.module == "os" and not i.is_from for i in imports)
    assert any(i.module == "sys" and i.name == "path" and i.alias == "sys_path" and i.is_from for i in imports)


def test_parse_syntax_error_resilience():
    code = '''
def broken_syntax(
    # Missing closing parenthesis and colon
    return 42
'''
    parsed = parse_python_source(
        content=code,
        rel_path="broken.py",
        abs_path="/test/broken.py",
        mtime=200.0,
        size=len(code),
        sha256="badhash",
    )

    assert parsed.has_syntax_error
    assert parsed.error_message is not None
    assert "SyntaxError" in parsed.error_message
    # Raw content must still be preserved for search
    assert parsed.content == code
    # No symbols crashed the run
    assert len(parsed.symbols) == 0
