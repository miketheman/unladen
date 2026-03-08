"""Utility module with no external imports."""

import os
from pathlib import Path


def get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/tmp/data"))
