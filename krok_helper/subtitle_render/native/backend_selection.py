"""Subtitle backend choice, independent of process wrappers.

``KROK_SUBTITLE_RENDER_BACKEND`` is the session authority: ``skia`` selects
Skia, any other non-empty value (for example ``native``) keeps the existing
backend. Without it, packaged builds default to Skia when the bundled sidecar
is present, and source runs keep Skia opt-in so development behaviour and the
test suite do not depend on a locally built sidecar.
"""
import os
from pathlib import Path
import sys

RENDER_BACKEND_ENV = 'KROK_SUBTITLE_RENDER_BACKEND'
SKIA_BACKEND = 'skia'
NATIVE_BACKEND = 'native'


def bundled_skia_sidecar_present() -> bool:
    if not getattr(sys, 'frozen', False):
        return False
    name = 'krok_skia_subtitle_renderer' + ('.exe' if sys.platform == 'win32' else '')
    return (Path(sys.executable).resolve().parent / name).is_file()


def skia_requested(use_skia_backend: bool | None = None) -> bool:
    """Whether the subtitle preview and export use Skia."""
    if use_skia_backend is not None:
        return bool(use_skia_backend)
    choice = os.environ.get(RENDER_BACKEND_ENV, '').strip().lower()
    if choice:
        return choice == SKIA_BACKEND
    return bundled_skia_sidecar_present()


def set_session_skia_backend(enabled: bool) -> None:
    """Make a UI choice authoritative for the rest of this session."""
    os.environ[RENDER_BACKEND_ENV] = SKIA_BACKEND if enabled else NATIVE_BACKEND


def skia_direct_preview_requested() -> bool:
    return skia_requested() and os.environ.get('KROK_SUBTITLE_SKIA_DIRECT_PREVIEW') == '1'
