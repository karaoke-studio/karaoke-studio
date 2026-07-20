"""Shared desktop notification helpers."""

from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication


_LOGGER = logging.getLogger(__name__)


def play_completion_sound() -> None:
    """Play an explicit, non-blocking completion notification when possible.

    On Windows, ``MessageBeep`` uses the user's configured information sound.
    Other platforms (and Windows failures) fall back to Qt's platform beep.
    Sound schemes and system mute settings are intentionally respected.
    """

    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except (ImportError, OSError, RuntimeError):
            _LOGGER.debug("Windows completion sound failed; using Qt beep", exc_info=True)

    app = QApplication.instance()
    if app is None:
        return
    try:
        app.beep()
    except RuntimeError:
        _LOGGER.debug("Qt completion sound failed", exc_info=True)
