"""Add the script folders to sys.path so entry points can import each other.

scripts/catalog.py stays at the top. Research, automation, and profile
commands live in subfolders. Call install() before those imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def install() -> None:
    for folder in (SCRIPTS, SCRIPTS / "research", SCRIPTS / "automation", SCRIPTS / "profiles"):
        entry = str(folder)
        if entry not in sys.path:
            sys.path.insert(0, entry)
