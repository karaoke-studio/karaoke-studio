"""Render IR v1 helpers for the native subtitle renderer sidecar.

C1 keeps the native boundary intentionally boring: Python owns project parsing
and UI state, then sends a JSON-serializable Render IR snapshot to the sidecar.
The first native renderer only uses a small subset of fields for smoke output,
but the IR already carries the full ``style_to_dict`` payload so future C2/C3
work can migrate painter features without changing the process protocol shape.
"""

from __future__ import annotations

from typing import Any

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
    style_to_dict,
)

RENDER_IR_SCHEMA = 1


def timing_char_to_ir(ch: TimingChar) -> dict[str, Any]:
    return {
        "text": ch.text,
        "start_ms": int(ch.start_ms),
        "pause_release_ms": (
            int(ch.pause_release_ms) if ch.pause_release_ms is not None else None
        ),
        "role_label": ch.role_label,
    }


def timing_line_to_ir(line: TimingLine) -> dict[str, Any]:
    return {
        "chars": [timing_char_to_ir(ch) for ch in line.chars],
        "end_ms": int(line.end_ms) if line.end_ms is not None else None,
        "singer_label": line.singer_label,
        "singer_id": line.singer_id,
        "is_blank": bool(line.is_blank),
    }


def ruby_to_ir(ruby: RubyAnnotation) -> dict[str, Any]:
    return {
        "kanji": ruby.kanji,
        "reading": ruby.reading,
        "reading_part_ms": [int(item) for item in ruby.reading_part_ms],
        "pos_start_ms": int(ruby.pos_start_ms),
        "pos_end_ms": int(ruby.pos_end_ms),
    }


def track_to_ir(track: TimingTrack) -> dict[str, Any]:
    return {
        "meta": {
            "title": track.meta.title,
            "artist": track.meta.artist,
            "album": track.meta.album,
            "tagging_by": track.meta.tagging_by,
            "silence_ms": int(track.meta.silence_ms),
            "offset_ms": int(track.meta.offset_ms),
            "custom": list(track.meta.custom),
        },
        "lines": [timing_line_to_ir(line) for line in track.lines],
        "rubies": [ruby_to_ir(ruby) for ruby in track.rubies],
    }


def build_render_ir(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
) -> dict[str, Any]:
    """Build a JSON-friendly Render IR v1 snapshot for the native sidecar."""
    return {
        "schema": RENDER_IR_SCHEMA,
        "screen": {
            "width": max(int(width), 1),
            "height": max(int(height), 1),
            "fps": max(int(fps), 1),
        },
        "style": style_to_dict(style),
        "track": track_to_ir(track),
    }
