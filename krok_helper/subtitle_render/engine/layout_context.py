"""Thread-local lifetime for one immutable subtitle layout pass."""

from __future__ import annotations

from contextlib import contextmanager
from threading import local as thread_local


_LAYOUT_PASS = thread_local()


@contextmanager
def layout_pass():
    """Mark a re-entrant interval where track and style inputs stay immutable.

    Painter's measurement helpers share scratch maps during this interval.  The
    outermost exit releases every owner reference, while nested calls reuse the
    same per-thread maps.
    """

    depth = getattr(_LAYOUT_PASS, "depth", 0)
    if depth == 0:
        _LAYOUT_PASS.page_maps = {}
        _LAYOUT_PASS.line_styles = {}
        _LAYOUT_PASS.line_indices = {}
        _LAYOUT_PASS.active_rubies = {}
        _LAYOUT_PASS.ruby_gaps = {}
        _LAYOUT_PASS.char_advances = {}
        _LAYOUT_PASS.ink_rects = {}
        _LAYOUT_PASS.sayatoo_layouts = {}
        _LAYOUT_PASS.signal_heads = {}
        _LAYOUT_PASS.tracks = []
        _LAYOUT_PASS.styles = []
        _LAYOUT_PASS.lines = []
        _LAYOUT_PASS.ruby_lists = []
        _LAYOUT_PASS.metrics = []
    _LAYOUT_PASS.depth = depth + 1
    try:
        yield
    finally:
        _LAYOUT_PASS.depth = depth
        if depth == 0:
            _LAYOUT_PASS.page_maps = None
            _LAYOUT_PASS.line_styles = None
            _LAYOUT_PASS.line_indices = None
            _LAYOUT_PASS.active_rubies = None
            _LAYOUT_PASS.ruby_gaps = None
            _LAYOUT_PASS.char_advances = None
            _LAYOUT_PASS.ink_rects = None
            _LAYOUT_PASS.sayatoo_layouts = None
            _LAYOUT_PASS.signal_heads = None
            _LAYOUT_PASS.tracks = []
            _LAYOUT_PASS.styles = []
            _LAYOUT_PASS.lines = []
            _LAYOUT_PASS.ruby_lists = []
            _LAYOUT_PASS.metrics = []


__all__ = ["layout_pass"]
