"""User-owned runtime state locations, separate from the application checkout."""
from __future__ import annotations

import os
from pathlib import Path


USER_DATA_DIR = Path(os.environ.get("KORELAB_DATA_HOME", "~/.korelab")).expanduser()
RUNS_DIR = USER_DATA_DIR / "runs"
KNOWLEDGE_DIR = USER_DATA_DIR / "knowledge"
TECHNICAL_DIR = USER_DATA_DIR / "technical"
