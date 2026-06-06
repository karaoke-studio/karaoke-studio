"""Karaoke Studio package."""

from __future__ import annotations

import sys
from pathlib import Path


SUG_ROOT = Path(__file__).resolve().parent / "lyrics_timing"
SUG_SRC = SUG_ROOT / "src"


def _prepend_sys_path(path: Path) -> Path:
    resolved = path.resolve()
    path_text = str(resolved)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
    return resolved


def ensure_sug_src_path() -> Path:
    """Make the bundled StrangeUtaGame src layout importable."""

    return _prepend_sys_path(SUG_SRC)


def ensure_sug_root_path() -> Path:
    """Make StrangeUtaGame root-level helper modules importable."""

    return _prepend_sys_path(SUG_ROOT)


ensure_sug_src_path()
