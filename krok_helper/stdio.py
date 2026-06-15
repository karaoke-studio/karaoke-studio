from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Use UTF-8 for console streams on Windows when Python exposes reconfigure()."""

    if sys.platform != "win32":
        return

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
