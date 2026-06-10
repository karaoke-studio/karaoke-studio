from __future__ import annotations

import os
import sys


def configure_source_debug_settings_profile() -> None:
    """Keep source debug runs from touching the packaged app's settings."""

    if getattr(sys, "frozen", False):
        return
    if os.environ.get("KARAOKE_STUDIO_SETTINGS_DIR"):
        return
    os.environ.setdefault("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio Dev")
