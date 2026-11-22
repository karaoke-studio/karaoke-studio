"""Source-only experimental backend choice, independent of process wrappers."""
import os
import sys


def skia_requested(use_skia_backend: bool | None = None) -> bool:
    """Allow Skia only in an explicitly opted-in source run."""
    return not getattr(sys, 'frozen', False) and (
        use_skia_backend if use_skia_backend is not None
        else os.environ.get('KROK_SUBTITLE_RENDER_BACKEND', '').strip().lower() == 'skia'
    )


def skia_direct_preview_requested() -> bool:
    return skia_requested() and os.environ.get('KROK_SUBTITLE_SKIA_DIRECT_PREVIEW') == '1'
