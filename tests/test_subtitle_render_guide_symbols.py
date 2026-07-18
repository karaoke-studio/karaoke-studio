from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QDialog

from krok_helper.subtitle_render.engine import painter as subtitle_painter
from krok_helper.subtitle_render.frontend import guide_replacement as guide_replacement_module
from krok_helper.subtitle_render.frontend import lyrics_list as lyrics_list_module
from krok_helper.subtitle_render.frontend import main_window as main_window_module
from krok_helper.subtitle_render.engine.painter import _layout_line_uncached, paint_frame
from krok_helper.subtitle_render.engine.timeline import compute_display_lines, find_active_line
from krok_helper.subtitle_render.frontend.guide_replacement import (
    GuidePrefixReplaceDialog,
    detect_guide_prefix_matches,
    guide_marker_options,
    replacement_symbol_for_match,
)
from krok_helper.subtitle_render.frontend.lyrics_list import _CharRoleDialog
from krok_helper.subtitle_render.frontend.lyrics_list import COL_CONTENT, LyricsPanel
from krok_helper.subtitle_render.frontend.main_window import (
    SubtitleRenderWindow,
    _GuideSymbolSettingsDialog,
)
from krok_helper.subtitle_render.guide_symbols import (
    guide_symbol_path,
    import_svg_guide_symbol,
)
from krok_helper.subtitle_render.models import (
    GuideSymbol,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_from_dict,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    guide_symbol_to_dict,
    guide_symbol_with_role_labels,
    line_visible_chars,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.project_store import project_payload


def _symbol(tmp_path, *, duration_ms: int = 1000, count: int = 1) -> GuideSymbol:
    path = tmp_path / "lead.svg"
    path.write_text(
        '<svg viewBox="0 0 100 100">'
        '<g transform="translate(5 0)">'
        '<path d="M5 50 L85 10 L65 50 L85 90 Z"/>'
        "</g></svg>",
        encoding="utf-8",
    )
    return import_svg_guide_symbol(path, duration_ms=duration_ms, count=count)


def test_svg_import_becomes_embedded_glyph_outline(tmp_path):
    symbol = _symbol(tmp_path)
    bounds = guide_symbol_path(symbol).boundingRect()

    assert symbol.name == "lead"
    assert symbol.duration_ms == 1000
    assert symbol.path_commands
    assert 0 < bounds.width() <= symbol.units_per_em
    assert 0 < bounds.height() <= symbol.units_per_em

    restored = guide_symbol_from_dict(guide_symbol_to_dict(symbol))
    assert restored == symbol


def test_guide_symbol_cache_signature_reuses_frozen_outline_model(tmp_path):
    symbol = _symbol(tmp_path)

    signature = subtitle_painter._value_signature(symbol)

    assert signature is symbol


def test_guide_symbol_is_first_timed_inline_glyph(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    style = Style(font_family="Arial", font_size_px=72)

    layout = _layout_line_uncached(track, line, style, 800, 450)

    assert layout is not None
    assert layout.render_line is not None
    assert len(layout.render_line.chars) == 3
    assert layout.render_line.chars[0].vector_glyph == symbol
    assert layout.intervals == [(1000, 2000), (2000, 2500), (2500, 3000)]
    assert layout.text_layout.glyphs[0].left == layout.x0
    assert layout.char_x_ranges[0][1] == layout.char_x_ranges[1][0]
    assert layout.total_w > sum(layout.char_widths[1:])
    assert find_active_line(track, 1500) is line
    display = compute_display_lines(
        track,
        lead_in_ms=0,
        tail_ms=0,
        lane_gap_ms=0,
        max_hold_ms=0,
        continuity_snap_ms=0,
    )
    assert display[0].display_start_ms == 1000


def test_multiple_guides_are_independent_evenly_timed_inline_glyphs(tmp_path):
    symbol = _symbol(tmp_path, duration_ms=1000, count=3)
    line = TimingLine(
        chars=[TimingChar("歌", 5000), TimingChar("詞", 5500)],
        end_ms=6000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])

    layout = _layout_line_uncached(
        track, line, Style(font_family="Arial", font_size_px=72), 900, 450
    )

    assert layout is not None and layout.render_line is not None
    assert len(layout.render_line.chars) == 5
    assert [char.start_ms for char in layout.render_line.chars] == [
        2000,
        3000,
        4000,
        5000,
        5500,
    ]
    assert layout.intervals[:3] == [(2000, 3000), (3000, 4000), (4000, 5000)]
    assert all(char.vector_glyph == symbol for char in layout.render_line.chars[:3])
    assert find_active_line(track, 2500) is line


def test_detects_one_or_more_consecutive_timed_prefix_markers():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("h", 1000), TimingChar("歌", 2000)],
                end_ms=2500,
            ),
            TimingLine(
                chars=[
                    TimingChar("h", 3000),
                    TimingChar("h", 3400),
                    TimingChar("詞", 4100),
                ],
                end_ms=4500,
            ),
            TimingLine(
                chars=[TimingChar("t", 5000), TimingChar("est", 5300)],
                end_ms=5800,
            ),
        ]
    )

    matches = detect_guide_prefix_matches(track, "h")

    assert [match.row for match in matches] == [0, 1]
    assert [match.prefix for match in matches] == [("h",), ("h", "h")]
    assert [match.intervals_ms for match in matches] == [(1000,), (400, 700)]
    assert guide_marker_options(track)[0] == ("h", 2)


def test_detection_can_include_consecutive_markers_inside_a_line():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("h", 1800),
                    TimingChar("詞", 2300),
                    TimingChar("h", 2800),
                ],
                end_ms=3200,
            )
        ]
    )

    assert detect_guide_prefix_matches(track, "h") == []
    matches = detect_guide_prefix_matches(
        track, "h", include_non_prefix=True
    )

    assert [match.start_index for match in matches] == [1, 4]
    assert [match.count for match in matches] == [2, 1]
    assert [match.replacement_text for match in matches] == [
        "歌◆◆詞h",
        "歌hh詞◆",
    ]
    assert matches[0].intervals_ms == (300, 500)
    assert matches[1].intervals_ms == (400,)


def test_prefix_replacement_reuses_original_timing_and_hides_marker(tmp_path):
    line = TimingLine(
        chars=[
            TimingChar("h", 1000, role_label="导唱A"),
            TimingChar("h", 1800, role_label="导唱B"),
            TimingChar("歌", 2500),
            TimingChar("詞", 2900),
        ],
        end_ms=3300,
    )
    match = detect_guide_prefix_matches(TimingTrack(lines=[line]), "h")[0]
    symbol = replacement_symbol_for_match(_symbol(tmp_path), line, match)
    assert symbol is not None
    line.guide_symbol = symbol

    layout = _layout_line_uncached(
        TimingTrack(lines=[line]),
        line,
        Style(font_family="Arial", font_size_px=72),
        900,
        450,
    )

    assert guide_symbol_replacement_count(line) == 2
    assert "".join(char.text for char in line_visible_chars(line)) == "歌詞"
    assert timing_line_start_ms(line) == 1000
    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [
        1000,
        1800,
        2500,
        2900,
    ]
    assert [char.role_label for char in layout.render_line.chars[:2]] == [
        "导唱A",
        "导唱B",
    ]
    assert all(char.vector_glyph == symbol for char in layout.render_line.chars[:2])


def test_multiple_guide_line_hits_layout_and_layer_caches(tmp_path):
    symbol = _symbol(tmp_path, duration_ms=1000, count=3)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 5000), TimingChar("詞", 5500)],
                end_ms=6000,
                guide_symbol=symbol,
            )
        ]
    )
    style = Style(font_family="Arial", font_size_px=72)
    image = QImage(900, 450, QImage.Format.Format_ARGB32_Premultiplied)
    subtitle_painter.clear_before_layer_cache()

    paint_frame(image, track, 2500, style)
    warm_sizes = (
        len(subtitle_painter._LINE_LAYOUT_CACHE),
        len(subtitle_painter._TEXT_RUN_LAYER_CACHE),
    )
    paint_frame(image, track, 2600, style)

    assert warm_sizes[0] == 1
    assert warm_sizes[1] > 0
    assert (
        len(subtitle_painter._LINE_LAYOUT_CACHE),
        len(subtitle_painter._TEXT_RUN_LAYER_CACHE),
    ) == warm_sizes
    subtitle_painter.clear_before_layer_cache()


def test_multiple_guides_keep_independent_role_labels_in_project(tmp_path):
    symbol = guide_symbol_with_role_labels(
        _symbol(tmp_path, count=3), ["角色A", "角色B", None]
    )

    restored = guide_symbol_from_dict(guide_symbol_to_dict(symbol))

    assert restored == symbol
    assert guide_symbol_role_labels(restored) == ("角色A", "角色B", None)
    assert TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 5000)], guide_symbol=restored)]
    ).role_options == ["角色A", "角色B"]


def test_legacy_guide_symbol_payload_defaults_to_one_glyph(tmp_path):
    legacy = guide_symbol_to_dict(_symbol(tmp_path))
    assert legacy is not None
    legacy.pop("count")
    legacy.pop("role_labels")
    legacy["role_label"] = "角色A"

    restored = guide_symbol_from_dict(legacy)

    assert restored is not None
    assert restored.count == 1
    assert guide_symbol_role_labels(restored) == ("角色A",)


def test_guide_symbol_renders_during_its_own_karaoke_interval(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    paint_frame(
        image,
        TimingTrack(lines=[line]),
        1500,
        Style(font_family="Arial", font_size_px=72),
    )

    assert any(image.constBits().asstring(image.sizeInBytes()))


def test_guide_symbol_renders_in_direct_and_vertical_paths(tmp_path, monkeypatch):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 2000), TimingChar("詞", 2500)],
        end_ms=3000,
        guide_symbol=symbol,
    )
    track = TimingTrack(lines=[line])
    for vertical in (False, True):
        monkeypatch.setenv("KROK_SUBTITLE_HORIZONTAL_LAYER", "0")
        image = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(
            image,
            track,
            1500,
            Style(font_family="Arial", font_size_px=72, vertical=vertical),
        )
        assert any(image.constBits().asstring(image.sizeInBytes()))


def test_char_role_dialog_exposes_guide_as_first_selectable_character(tmp_path):
    symbol = _symbol(tmp_path)
    dialog = _CharRoleDialog(
        0,
        ["导", "歌"],
        ["导唱", None],
        ["导唱"],
        Style(),
        vector_symbols=[symbol, None],
    )

    assert dialog.char_labels() == ["导唱", None]
    assert dialog._chips.role_tooltip_text(0) == "导唱符 · 角色方案：导唱"
    dialog.close()


def test_char_role_dialog_replaces_only_selected_source_chars_with_svg(
    tmp_path, monkeypatch
):
    prefix_symbol = _symbol(tmp_path)
    replacement_path = tmp_path / "replacement.svg"
    replacement_path.write_text(
        '<svg viewBox="0 0 20 20"><path d="M2 2 L18 10 L2 18 Z"/></svg>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lyrics_list_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(replacement_path), "SVG 文件 (*.svg)"),
    )
    dialog = _CharRoleDialog(
        0,
        ["导", "歌", "詞"],
        [None, None, None],
        [],
        Style(),
        vector_symbols=[prefix_symbol, None, None],
        protected_prefix_count=1,
    )
    dialog._chips._selected = {0, 2}
    dialog._chips.selectionChanged.emit()

    dialog._replace_selected_with_svg()

    symbols = dialog.char_vector_symbols()
    assert symbols[0] == prefix_symbol
    assert symbols[1] is None
    assert symbols[2] is not None
    assert symbols[2].name == "replacement"
    dialog.close()


def test_inline_svg_replacement_keeps_middle_character_timing_and_layout(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[
            TimingChar("歌", 1000),
            TimingChar("h", 1500, pause_release_ms=1750),
            TimingChar("詞", 2000),
        ],
        end_ms=2500,
        inline_guide_symbols={1: symbol},
    )
    track = TimingTrack(lines=[line])

    layout = _layout_line_uncached(
        track, line, Style(font_family="Arial", font_size_px=72), 900, 450
    )

    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [1000, 1500, 2000]
    assert layout.render_line.chars[1].pause_release_ms == 1750
    assert layout.render_line.chars[1].vector_glyph == symbol
    assert layout.render_line.chars[0].text == "歌"
    assert layout.render_line.chars[2].text == "詞"
    assert layout.char_x_ranges[0][1] == layout.char_x_ranges[1][0]
    assert layout.char_x_ranges[1][1] == layout.char_x_ranges[2][0]


def test_inline_svg_replacement_coexists_with_prefix_marker_replacement(tmp_path):
    prefix_symbol = replace(
        _symbol(tmp_path), count=1, replacement_prefix=("h",), role_labels=(None,)
    )
    inline_symbol = replace(_symbol(tmp_path), name="inline")
    line = TimingLine(
        chars=[TimingChar("h", 1000), TimingChar("歌", 1500), TimingChar("x", 2000)],
        end_ms=2500,
        guide_symbol=prefix_symbol,
        inline_guide_symbols={2: inline_symbol},
    )

    layout = _layout_line_uncached(
        TimingTrack(lines=[line]),
        line,
        Style(font_family="Arial", font_size_px=72),
        900,
        450,
    )

    assert layout is not None and layout.render_line is not None
    assert [char.start_ms for char in layout.render_line.chars] == [1000, 1500, 2000]
    assert layout.render_line.chars[0].vector_glyph == prefix_symbol
    assert layout.render_line.chars[2].vector_glyph == inline_symbol


def test_guide_symbol_settings_dialog_returns_count_and_interval():
    dialog = _GuideSymbolSettingsDialog(count=3, interval_ms=750)

    assert dialog.windowTitle() == "导唱符设置"
    assert dialog.settings() == (3, 750)
    dialog.close()


def test_prefix_replace_dialog_lists_candidates_and_keeps_ambiguous_rows_selectable(
    tmp_path,
):
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("h", 3000), TimingChar("詞", 4100)]),
        ]
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.set_svg_path(tmp_path / "lead.svg")

    assert dialog.windowTitle() == "批量识别导唱标记"
    assert dialog.non_prefix_check.text() == "允许搜索非行首字符"
    assert dialog.marker_edit.width() == 105
    assert "2 行" in dialog.summary_label.text()
    assert [match.row for match in dialog.selected_matches()] == [0, 1]
    dialog._row_checks[1].setCheckState(Qt.CheckState.Unchecked)
    assert [match.row for match in dialog.selected_matches()] == [0]
    dialog.close()


def test_marker_enter_only_refreshes_results_and_table_has_no_row_selection(
    monkeypatch,
):
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)])]
    )
    browse_calls = 0

    def browse(*_args, **_kwargs):
        nonlocal browse_calls
        browse_calls += 1
        return "", ""

    monkeypatch.setattr(
        guide_replacement_module.QFileDialog, "getOpenFileName", browse
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.show()
    QApplication.processEvents()
    dialog.marker_edit.setText("t")
    dialog.marker_edit.setFocus()

    QTest.keyClick(dialog.marker_edit, Qt.Key.Key_Return)
    QApplication.processEvents()

    assert browse_calls == 0
    assert "0 行" in dialog.summary_label.text()
    assert dialog.table.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    assert not dialog.table.selectedItems()
    dialog.close()


def test_dialog_checkbox_enables_non_prefix_detection():
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("詞", 2000),
                ],
                end_ms=2500,
            )
        ]
    )
    dialog = GuidePrefixReplaceDialog(track)
    dialog.marker_edit.setText("h")
    dialog.refresh_matches()
    assert dialog.table.rowCount() == 0

    dialog.non_prefix_check.setChecked(True)
    QApplication.processEvents()

    assert dialog.table.rowCount() == 1
    assert dialog.selected_matches()[0].start_index == 1
    assert "1 处" in dialog.summary_label.text()
    dialog.close()


def test_lyrics_preview_shows_embedded_guide_icon(tmp_path):
    symbol = _symbol(tmp_path)
    panel = LyricsPanel()
    panel.set_track(
        TimingTrack(
            lines=[
                TimingLine(
                    chars=[TimingChar("歌", 2000)],
                    end_ms=2500,
                    guide_symbol=symbol,
                )
            ]
        )
    )

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert not item.icon().isNull()
    assert "行前导唱符" in item.toolTip()
    panel.close()


def test_lyrics_preview_hides_replaced_prefix_and_preserves_source_chars(tmp_path):
    symbol = replace(
        _symbol(tmp_path), count=1, replacement_prefix=("h",), role_labels=(None,)
    )
    line = TimingLine(
        chars=[TimingChar("h", 1000), TimingChar("歌词", 2000)],
        end_ms=2500,
        guide_symbol=symbol,
    )
    panel = LyricsPanel()
    panel.set_track(TimingTrack(lines=[line]))

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert item.text() == "歌词"
    assert "行首标记替换" in item.toolTip()
    assert "".join(char.text for char in line.chars) == "h歌词"
    panel.close()


def test_lyrics_preview_marks_inline_svg_without_changing_source_text(tmp_path):
    symbol = _symbol(tmp_path)
    line = TimingLine(
        chars=[TimingChar("歌", 1000), TimingChar("h", 1500), TimingChar("詞", 2000)],
        end_ms=2500,
        inline_guide_symbols={1: symbol},
    )
    panel = LyricsPanel()
    panel.set_track(TimingTrack(lines=[line]))

    item = panel.table_widget.item(0, COL_CONTENT)
    assert item is not None
    assert item.text() == "歌◆詞"
    assert not item.icon().isNull()
    assert "行内 SVG 导唱符：1 个" in item.toolTip()
    assert "".join(char.text for char in line.chars) == "歌h詞"
    panel.close()


def test_project_reload_skips_replacement_when_source_prefix_changed(tmp_path):
    symbol = replace(_symbol(tmp_path), count=1, replacement_prefix=("h",))
    payload = guide_symbol_to_dict(symbol)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("t", 3000), TimingChar("詞", 4000)]),
        ]
    )

    mismatches = SubtitleRenderWindow._apply_guide_symbol_rows(
        track, [payload, payload]
    )

    assert mismatches == [1]
    assert track.lines[0].guide_symbol == symbol
    assert track.lines[1].guide_symbol is None


def test_batch_prefix_replacement_is_one_undoable_command(tmp_path, monkeypatch):
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("h", 1000), TimingChar("歌", 2000)]),
            TimingLine(chars=[TimingChar("h", 3000), TimingChar("詞", 4100)]),
        ]
    )
    matches = detect_guide_prefix_matches(track, "h")

    class AcceptedDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def svg_path(self):
            return tmp_path / "lead.svg"

        def selected_matches(self):
            return matches

    monkeypatch.setattr(
        main_window_module, "GuidePrefixReplaceDialog", AcceptedDialog
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv(
        "KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings")
    )
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert all(line.guide_symbol is not None for line in track.lines)
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert all(line.guide_symbol is None for line in track.lines)
    window._redo_edit()
    assert all(line.guide_symbol is not None for line in track.lines)
    window.close()


def test_batch_non_prefix_replacement_uses_inline_svg_and_is_undoable(
    tmp_path, monkeypatch
):
    _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("歌", 1000),
                    TimingChar("h", 1500),
                    TimingChar("h", 1800),
                    TimingChar("詞", 2300),
                ],
                end_ms=2800,
            )
        ]
    )
    matches = detect_guide_prefix_matches(
        track, "h", include_non_prefix=True
    )

    class AcceptedDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def svg_path(self):
            return tmp_path / "lead.svg"

        def selected_matches(self):
            return matches

    monkeypatch.setattr(
        main_window_module, "GuidePrefixReplaceDialog", AcceptedDialog
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_guide_prefix_replace_requested()

    assert set(track.lines[0].inline_guide_symbols) == {1, 2}
    assert track.lines[0].guide_symbol is None
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert track.lines[0].inline_guide_symbols == {}
    window._redo_edit()
    assert set(track.lines[0].inline_guide_symbols) == {1, 2}
    window.close()


def test_inline_char_replacement_is_one_undoable_command(tmp_path, monkeypatch):
    symbol = _symbol(tmp_path)
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("歌", 1000), TimingChar("x", 1500)], end_ms=2000
            )
        ]
    )
    monkeypatch.setattr(
        main_window_module.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path / "settings"))
    window = main_window_module.SubtitleRenderWindow(embedded=False)
    window._timing_track = track
    window._lyrics_panel.set_track(track)

    window._on_inline_char_edit_changed(0, None, [None, None], [None, symbol])

    assert track.lines[0].inline_guide_symbols == {1: symbol}
    assert len(window._undo_stack) == 1
    window._undo_edit()
    assert track.lines[0].inline_guide_symbols == {}
    window._redo_edit()
    assert track.lines[0].inline_guide_symbols == {1: symbol}
    window.close()


def test_project_payload_keeps_optional_line_guide_symbols(tmp_path):
    symbol = _symbol(tmp_path)
    row = guide_symbol_to_dict(replace(symbol, role_label="角色A"))

    payload = project_payload(
        subtitle_path=None,
        video_path=None,
        audio_path=None,
        style={},
        screen={},
        selected_scheme_key="default",
        output={},
        line_guide_symbols=[row, None],
    )

    assert guide_symbol_from_dict(payload["line_guide_symbols"][0]) == replace(
        symbol, role_label="角色A"
    )
    assert payload["line_guide_symbols"][1] is None


def test_project_payload_and_reload_keep_inline_guide_symbols(tmp_path):
    symbol = _symbol(tmp_path)
    row = {"1": guide_symbol_to_dict(symbol)}

    payload = project_payload(
        subtitle_path=None,
        video_path=None,
        audio_path=None,
        style={},
        screen={},
        selected_scheme_key="default",
        output={},
        line_inline_guide_symbols=[row, None],
    )
    track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("歌", 1000), TimingChar("h", 1500)]),
            TimingLine(chars=[TimingChar("詞", 2000)]),
        ]
    )

    SubtitleRenderWindow._apply_inline_guide_symbol_rows(
        track, payload["line_inline_guide_symbols"]
    )

    assert track.lines[0].inline_guide_symbols == {1: symbol}
    assert track.lines[1].inline_guide_symbols == {}
