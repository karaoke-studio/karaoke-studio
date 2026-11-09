"""Bounded write probe for the embedded SUG directory resolver."""

import os
from pathlib import Path
from uuid import uuid4


def is_dir_writable(directory: Path) -> bool:
    """Fail immediately on access denial, without tempfile's Windows retries."""
    probe = directory / f".lin-k-write-probe-{uuid4().hex}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    try:
        try:
            os.close(fd)
        finally:
            probe.unlink()
    except OSError:
        return False
    return True


def install_sug_directory_probe() -> None:
    """Keep SUG's path priorities and fallback while replacing its probe."""
    from strange_uta_game import app_dirs

    app_dirs._is_dir_writable = is_dir_writable
