"""Subtitle backend choice, independent of process wrappers.

``KROK_SUBTITLE_RENDER_BACKEND`` is the session authority: ``skia`` selects
Skia, any other non-empty value (for example ``native``) keeps the legacy
backend. Without it, both packaged builds and source runs default to Skia
whenever a Skia sidecar executable is available locally.
"""
import os
from pathlib import Path
import platform
import sys

RENDER_BACKEND_ENV = 'KROK_SUBTITLE_RENDER_BACKEND'
SKIA_BACKEND = 'skia'
NATIVE_BACKEND = 'native'


def _skia_executable_name() -> str:
    return 'krok_skia_subtitle_renderer' + ('.exe' if sys.platform == 'win32' else '')


def _repository_root() -> Path:
    # krok_helper/subtitle_render/native/backend_selection.py -> repo root
    return Path(__file__).resolve().parents[3]


def bundled_skia_sidecar_present() -> bool:
    if not getattr(sys, 'frozen', False):
        return False
    return (Path(sys.executable).resolve().parent / _skia_executable_name()).is_file()


def skia_sidecar_available() -> bool:
    """Return True when a packaged or locally built Skia sidecar can be found."""
    name = _skia_executable_name()
    override = os.environ.get('KROK_SUBTITLE_SKIA_RENDERER', '').strip()
    if override:
        return Path(override).is_file()
    if getattr(sys, 'frozen', False):
        return (Path(sys.executable).resolve().parent / name).is_file()
    system = 'windows' if sys.platform == 'win32' else 'macos' if sys.platform == 'darwin' else 'linux'
    arch = 'arm64' if platform.machine().lower() in {'arm64', 'aarch64'} else 'x64'
    api = 'metal' if sys.platform == 'darwin' else 'vulkan'
    cache_root = Path.home() / '.cache/krok-helper/skia-deps/native'
    candidates = (
        Path(sys.executable).resolve().parent / name,
        _repository_root() / 'build/skia-deps/native' / f'{system}-{arch}-{api}' / name,
        cache_root / f'{system}-{arch}-{api}' / name,
        cache_root / f'{system}-{arch}-raster' / name,
    )
    return any(candidate.is_file() for candidate in candidates)


def skia_requested(use_skia_backend: bool | None = None) -> bool:
    """Whether the subtitle preview and export use Skia."""
    if use_skia_backend is not None:
        return bool(use_skia_backend)
    choice = os.environ.get(RENDER_BACKEND_ENV, '').strip().lower()
    if choice:
        return choice == SKIA_BACKEND
    return skia_sidecar_available()


def set_session_skia_backend(enabled: bool) -> None:
    """Make a UI choice authoritative for the rest of this session."""
    os.environ[RENDER_BACKEND_ENV] = SKIA_BACKEND if enabled else NATIVE_BACKEND


def skia_direct_preview_requested() -> bool:
    return skia_requested() and os.environ.get('KROK_SUBTITLE_SKIA_DIRECT_PREVIEW') == '1'
