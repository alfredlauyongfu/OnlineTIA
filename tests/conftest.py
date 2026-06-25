"""Shared pytest setup: make the project's source dir importable so tests
can do `from excel_to_json import ...` without an editable install.

The source modules live in `src/` (a sibling of `tests/`); only
`run.py` sits at the project root. Adding `src/` to sys.path here
means tests don't need to know about that layout."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
