"""Source-only experimental backend choice, independent of process wrappers."""
import os
import sys


def skia_requested() -> bool:
    """Allow Skia only in an explicitly opted-in source run."""
    return not getattr(sys, 'frozen', False) and (
        os.environ.get('KROK_SUBTITLE_RENDER_BACKEND', '').strip().lower() == 'skia'
    )
