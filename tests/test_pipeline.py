"""Integration tests for transactional indexing pipeline."""

import time
from pathlib import Path
from projectdna.db.connection import get_rw_connection
from projectdna.db.schema import init_schema
from projectdna.db.repository import ProjectRepository, IndexRepository
from projectdna.indexer.pipeline import IndexingPipeline


def test_full_pipeline_lifecycle(tmp_path: Path):
    db_file = tmp_path / "pipeline.db"
    conn = get_rw_connection(db_file)
    init_schema(conn)

    # 1. Setup a project folder
    proj_dir = tmp_path / "sample_project"
    proj_dir.mkdir()

    # Valid Python file
    f1 = proj_dir / "calculator.py"
    f1.write_text(
        '''"""Calculator module."""

def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

class MathEngine:
    """Engine for math operations."""
    def multiply(self, x: int, y: int) -> int:
        return x * y
''',
        encoding="utf-8",
    )

    # Syntax-invalid Python file (should still be indexed and searchable with warning)
    f2 = proj_dir / "broken.py"
    f2.write_text(
        '''def incomplete_func(
    # missing closing paren and body
''',
        encoding="utf-8",
    )

    # Excluded file (.env)
    (proj_dir / ".env").write_text("API_KEY=secret_value", encoding="utf-8")

    # Excluded folder (venv)
    venv_dir = proj_dir / "venv"
    venv_dir.mkdir()
    (venv_dir / "lib.py").write_text("x = 1", encoding="utf-8")

    # 2. Register project
    project = ProjectRepository.create_project(conn, name="sample", root_path=proj_dir.as_posix())

    # 3. Initial indexing pass
    summary = IndexingPipeline.index_project(conn, project.id, proj_dir)
    assert summary["added"] == 2
    assert summary["modified"] == 0
    assert summary["deleted"] == 0
    assert summary["unchanged"] == 0

    # Verify calculator symbols were extracted
    files = IndexRepository.get_files_by_project(conn, project.id)
    assert "calculator.py" in files
    assert "broken.py" in files
    assert ".env" not in files
    assert "venv/lib.py" not in files

    calc_file = files["calculator.py"]
    assert not calc_file.has_syntax_error

    broken_file = files["broken.py"]
    assert broken_file.has_syntax_error
    assert broken_file.error_message is not None

    # Verify search on valid file
    calc_search = IndexRepository.search_code(conn, "MathEngine", project_id=project.id)
    assert len(calc_search) == 1
    assert calc_search[0]["rel_path"] == "calculator.py"

    sym_search = IndexRepository.search_symbols(conn, "multiply", project_id=project.id)
    assert len(sym_search) == 1
    assert sym_search[0]["name"] == "multiply"

    # Verify search on syntax-invalid file (readable syntax-invalid source is searchable with warning)
    broken_search = IndexRepository.search_code(conn, "incomplete_func", project_id=project.id)
    assert len(broken_search) == 1
    assert broken_search[0]["rel_path"] == "broken.py"
    assert broken_search[0]["has_syntax_error"] == 1
    assert "SyntaxError" in broken_search[0]["error_message"]

    # 4. Incremental pass without changes
    summary_noop = IndexingPipeline.index_project(conn, project.id, proj_dir)
    assert summary_noop["added"] == 0
    assert summary_noop["modified"] == 0
    assert summary_noop["deleted"] == 0
    assert summary_noop["unchanged"] == 2

    # 5. Modify a file
    time.sleep(0.01)
    f1.write_text(
        '''def subtract(a: int, b: int) -> int:
    return a - b
''',
        encoding="utf-8",
    )
    summary_mod = IndexingPipeline.index_project(conn, project.id, proj_dir)
    assert summary_mod["added"] == 0
    assert summary_mod["modified"] == 1
    assert summary_mod["unchanged"] == 1

    # Old symbol MathEngine should now be gone from FTS
    assert len(IndexRepository.search_symbols(conn, "MathEngine", project_id=project.id)) == 0
    assert len(IndexRepository.search_symbols(conn, "subtract", project_id=project.id)) == 1

    # 6. Delete a file
    f2.unlink()
    summary_del = IndexingPipeline.index_project(conn, project.id, proj_dir)
    assert summary_del["deleted"] == 1
    assert len(IndexRepository.search_code(conn, "incomplete_func", project_id=project.id)) == 0

    # 7. Test single-file boundary validation
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("pass", encoding="utf-8")
    assert not IndexingPipeline.index_single_file(conn, project.id, proj_dir, outside_file)

    conn.close()
