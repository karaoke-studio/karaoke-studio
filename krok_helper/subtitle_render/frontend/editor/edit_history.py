"""Undo/redo command dispatch for subtitle editor operations."""

from __future__ import annotations

from collections.abc import MutableSequence
from typing import Protocol


class EditRestorePort(Protocol):
    """Operations required to restore one editor history command."""

    def _restore_track_snapshot(self, track_index: int, track: object) -> bool: ...

    def _restore_tracks_snapshot(self, indices: object, tracks: object) -> bool: ...

    def _restore_style_and_tracks(
        self, style: object, indices: object, tracks: object
    ) -> bool: ...

    def _restore_style(self, style: object) -> bool: ...

    def _restore_screen(self, screen: object, style: object) -> bool: ...

    def _restore_char_roles(
        self, track_index: int, row: int, labels: object
    ) -> bool: ...

    def _restore_char_role_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_guide_symbols(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_guide_char_roles(
        self, track_index: int, row: int, value: object
    ) -> bool: ...

    def _restore_inline_char_edit(
        self, track_index: int, row: int, value: object
    ) -> bool: ...

    def _restore_guide_replacement_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_inline_role_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_animation_overrides(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_wipe_reverse_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool: ...

    def _restore_display_override(
        self, track_index: int, line_index: int, values: object
    ) -> bool: ...


def undo_edit(
    undo_stack: MutableSequence[tuple],
    redo_stack: MutableSequence[tuple],
    restorer: EditRestorePort,
) -> None:
    """Apply the newest valid command's old value and move it to redo."""

    while undo_stack:
        command = undo_stack.pop()
        if _restore_command(command, restorer, use_new_value=False):
            redo_stack.append(command)
            return


def redo_edit(
    undo_stack: MutableSequence[tuple],
    redo_stack: MutableSequence[tuple],
    restorer: EditRestorePort,
) -> None:
    """Apply the newest valid redo command and move it back to undo."""

    while redo_stack:
        command = redo_stack.pop()
        if _restore_command(command, restorer, use_new_value=True):
            undo_stack.append(command)
            return


def _restore_command(
    command: tuple,
    restorer: EditRestorePort,
    *,
    use_new_value: bool,
) -> bool:
    kind = command[0]
    if kind == "track_snapshot":
        _kind, track_index, old_track, new_track = command
        return restorer._restore_track_snapshot(
            track_index, new_track if use_new_value else old_track
        )
    if kind == "tracks_snapshot":
        _kind, indices, old_tracks, new_tracks = command
        return restorer._restore_tracks_snapshot(
            indices, new_tracks if use_new_value else old_tracks
        )
    if kind == "style_tracks":
        _kind, old_style, new_style, indices, old_tracks, new_tracks = command
        return restorer._restore_style_and_tracks(
            new_style if use_new_value else old_style,
            indices,
            new_tracks if use_new_value else old_tracks,
        )
    if kind == "style":
        return restorer._restore_style(command[2] if use_new_value else command[1])
    if kind == "screen":
        _kind, old_screen, old_style, new_screen, new_style, _timestamp = command
        return restorer._restore_screen(
            new_screen if use_new_value else old_screen,
            new_style if use_new_value else old_style,
        )
    if kind == "char_roles":
        _kind, track_index, row, old_labels, new_labels = command
        return restorer._restore_char_roles(
            track_index, row, new_labels if use_new_value else old_labels
        )
    if kind == "char_roles_batch":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_char_role_rows(
            track_index, rows, new_values if use_new_value else old_values
        )
    if kind == "guide_symbols":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_guide_symbols(
            track_index, rows, new_values if use_new_value else old_values
        )
    if kind == "guide_char_roles":
        _kind, track_index, row, old_value, new_value = command
        return restorer._restore_guide_char_roles(
            track_index, row, new_value if use_new_value else old_value
        )
    if kind == "inline_char_edit":
        _kind, track_index, row, old_value, new_value = command
        return restorer._restore_inline_char_edit(
            track_index, row, new_value if use_new_value else old_value
        )
    if kind == "guide_replacements":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_guide_replacement_rows(
            track_index, rows, new_values if use_new_value else old_values
        )
    if kind == "inline_roles_batch":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_inline_role_rows(
            track_index, rows, new_values if use_new_value else old_values
        )
    if len(command) == 5 and kind == "animation":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_animation_overrides(
            track_index, rows, new_values if use_new_value else old_values
        )
    if len(command) == 5 and kind == "wipe_reverse":
        _kind, track_index, rows, old_values, new_values = command
        return restorer._restore_wipe_reverse_rows(
            track_index, rows, new_values if use_new_value else old_values
        )
    track_index, line_index, old_values, new_values = command
    return restorer._restore_display_override(
        track_index, line_index, new_values if use_new_value else old_values
    )
