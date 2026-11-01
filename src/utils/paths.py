"""Paths used across the project.

Everything is built from the project folder and not from the current
directory, so it works the same no matter where you run it from.
"""
from pathlib import Path

# This file is src/utils/paths.py, so two folders up is the project folder.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def display_path(path: Path) -> str:
    """A path to show to people: relative to the project folder when it's inside it."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
