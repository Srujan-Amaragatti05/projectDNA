"""Configuration and path management for ProjectDNA."""

import os
from pathlib import Path

# Base data directory: ~/.projectdna or %USERPROFILE%/.projectdna
DATA_DIR = Path(os.environ.get("PROJECTDNA_DATA_DIR", Path.home() / ".projectdna")).resolve()
DB_PATH = DATA_DIR / "dna.db"
LOCK_PATH = DATA_DIR / "writer.lock"
SERVICE_STATE_PATH = DATA_DIR / "service.json"

# Limits
MAX_INDEXABLE_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB limit for individual Python files

def ensure_data_dir() -> Path:
    """Ensure that the ProjectDNA data directory exists with secure permissions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
