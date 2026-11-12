from copy import deepcopy
from dataclasses import replace

import pytest
from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.domain.models import Style, style_from_dict, style_to_dict
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.ruby.lower import (
    LowerRuby, lower_rubies_for_line, lower_style_snapshot,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    annotated_line_geometry, lower_ruby_reserve,
)
from krok_helper.subtitle_render.engine.ruby.timing import ruby_visual_units_and_intervals
from krok_helper.subtitle_render.engine.painter import _layout_line, _resolve_display_baselines, paint_frame
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine


def _style(**kwargs):
    return Style(font_family="Meiryo", font_size_px=64, ruby_font_size_px=24,
                 lower_ruby_enabled=True,
                 _lower_romaji_options=(False, False, False, False), **kwargs)


def _line(text):
    return TimingLine(chars=[TimingChar(ch, i * 500, explicit_start=True) for i, ch in enumerate(text)],
                      end_ms=len(text) * 500, track_line_index=0)


def test_lower_readings_default_off_but_restore_explicit_project_choice():
    assert Style().lower_ruby_enabled is False
    assert style_from_dict({}).lower_ruby_enabled is False
    assert style_from_dict({}, legacy_project=True).lower_ruby_enabled is False
    saved = style_to_dict(Style(lower_ruby_enabled=True))
    assert style_from_dict(saved).lower_ruby_enabled is True


@pytest.mark.parametrize("text,readings", [
    ("カッター", ["ka", "t", "ta-"]), ("きゃ", ["kya"]),
    ("ユー", ["yu-"]), ("なぐ", ["na", "gu"]), ("漢A", []),
])
def test_lower_kana_groups_keep_original_cell_timing(qapp, text, readings):
    line = _line(text)
    intervals = [(i * 500, (i + 1) * 500) for i in range(len(text))]
    result = lower_rubies_for_line(line, [], intervals, _style())
    assert [ruby.reading for ruby in result] == readings
    for ruby in result:
        assert ruby.pos_start_ms == intervals[ruby.target_char_start][0]
        assert ruby.pos_end_ms == intervals[ruby.target_char_end - 1][1]
    if text == "ユー":
        assert result[0].reading_parts == ["yu", "-"]
        assert result[0].reading_part_ms == [500]


def test_authored_reading_has_priority_and_preserves_pause(qapp):
    line = _line("殴る")
    upper = RubyAnnotation("殴", "なぐ", reading_parts=["な", "", "ぐ"],
                           reading_part_ms=[150, 250], pos_start_ms=0, pos_end_ms=500,
                           target_line_index=0, target_char_start=0, target_char_end=1)
    before = deepcopy((line, upper))
    result = lower_rubies_for_line(line, [upper], [(0, 500), (500, 1000)], _style())
    assert [ruby.reading for ruby in result] == ["nagu", "ru"]
    assert result[0].reading_parts == ["na", "", "gu"]
    assert result[0].reading_part_ms == [150, 250]
    assert (line, upper) == before
    assert not any(start < 250 and end > 150 for _, (start, end) in ruby_visual_units_and_intervals(result[0]))


@pytest.mark.parametrize("semantics", ["legacy", "n3_1074"])
def test_lower_layout_is_below_main_and_in_content_box(qapp, semantics):
    line = _line("カッター")
    track = TimingTrack(lines=[line])
    style = _style(layout_semantics=semantics)
    layout = _layout_line(track, line, style, 960, 540, baseline_y=250)
    assert layout is not None
    assert len(layout.ruby_layouts) == 3
    assert all(isinstance(ruby.ruby, LowerRuby) for ruby in layout.ruby_layouts)
    assert all(ruby.gradient_rect.top() >= layout.baseline_y for ruby in layout.ruby_layouts)
    assert layout.content_bounds.bottom() > layout.line_rect.bottom()
    plain = _layout_line(track, line, replace(style, lower_ruby_enabled=False), 960, 540, baseline_y=250)
    assert plain.ruby_layouts == ()
    assert plain.fill_segments == layout.fill_segments


@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_lower_grid_reserves_height_without_content_dependent_jumps(qapp, position):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    display = DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=1000)
    style = _style(line_y_position=position)
    baseline = _resolve_display_baselines(720, track, [display], style)
    with_upper = replace(track, rubies=[RubyAnnotation("かな", "カナ")])
    assert _resolve_display_baselines(720, with_upper, [display], style) == baseline
    plain = _resolve_display_baselines(720, track, [display], replace(style, lower_ruby_enabled=False))
    assert baseline != plain
    assert lower_ruby_reserve(style) > 0


def test_lower_zero_gap_matches_upper_visual_clearance(qapp):
    from pathlib import Path
    from PyQt6.QtGui import QFontDatabase
    from krok_helper.subtitle_render.engine.painter import _line_main_text_ink_rect
    for filename in ("meiryo.ttc", "comic.ttf"):
        path = Path("C:/Windows/Fonts") / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    line = _line("国")
    upper = RubyAnnotation("国", "に", pos_start_ms=0, pos_end_ms=500,
                           target_line_index=0, target_char_start=0, target_char_end=1)
    style = Style(font_family="Meiryo", font_size_px=100, ruby_font_size_px=45,
        ruby_font_family="Meiryo", ruby_font_family_latin="Comic Sans MS", ruby_font_follow_main=False,
        lower_ruby_enabled=True, ruby_gap_px=0, lower_ruby_gap_px=0,
        stroke_width_px=0, stroke2_enabled=False, ruby_stroke_width_px=0, ruby_stroke2_enabled=False,
        decoration_kind="none")
    layout = _layout_line(TimingTrack(lines=[line], rubies=[upper]), line, style, 960, 540, baseline_y=250)
    above, below = layout.ruby_layouts
    main = _line_main_text_ink_rect(layout)
    upper_clearance = main[1] - above.gradient_rect.bottom()
    lower_clearance = below.gradient_rect.top() - main[3]
    assert abs(lower_clearance - upper_clearance) <= 4


def test_lower_row_box_does_not_reserve_fx_or_unused_role_height(qapp):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    style = _style(decoration_kind="none")
    geometry = annotated_line_geometry(style)
    glow = replace(style, decoration_kind="glow", glow_before_radius_px=70,
                   glow_after_radius_px=70, ruby_glow_before_radius_px=70,
                   ruby_glow_after_radius_px=70)
    assert annotated_line_geometry(glow) == geometry
    shadow = replace(style, decoration_kind="shadow", shadow_offset_y=90,
                     ruby_shadow_offset_y=90)
    assert annotated_line_geometry(shadow) == geometry
    roles = dict(style.custom_style_schemes)
    roles["unused"] = SubtitleStyleScheme(font_size_px=600, ruby_font_size_px=300)
    assert annotated_line_geometry(replace(style, custom_style_schemes=roles)) == geometry


@pytest.mark.parametrize("semantics", ["legacy", "n3_1074"])
def test_upper_and_lower_gap_extend_the_same_row_grid(qapp, semantics):
    style = _style(layout_semantics=semantics, ruby_gap_px=10, lower_ruby_gap_px=10)
    base = annotated_line_geometry(style)
    above = annotated_line_geometry(replace(style, ruby_gap_px=27))
    below = annotated_line_geometry(replace(style, lower_ruby_gap_px=27))
    assert above[0] - base[0] == below[0] - base[0] == 17
    assert above[1] - base[1] == 17
    assert above[2] == base[2]
    assert below[1] == base[1]
    assert below[2] - base[2] == 17


@pytest.mark.parametrize("semantics", ["legacy", "n3_1074"])
def test_lower_zero_line_gap_keeps_visible_readings_adjacent(qapp, semantics):
    line = _line("国")
    upper = RubyAnnotation("国", "あいうえおアイウエオがぎぐげごやゆよ", pos_start_ms=0, pos_end_ms=500,
                           target_line_index=0, target_char_start=0, target_char_end=1)
    track = TimingTrack(lines=[line], rubies=[upper])
    style = _style(line_gap_px=0, layout_semantics=semantics,
                   stroke_width_px=0, stroke2_enabled=False, ruby_stroke_width_px=0,
                   ruby_stroke2_enabled=False, decoration_kind="none")
    display = DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=1000)
    baselines = _resolve_display_baselines(720, track, [display], style)
    first = _layout_line(track, line, style, 960, 720, baseline_y=baselines[0])
    second = _layout_line(track, line, style, 960, 720, baseline_y=baselines[1])
    bottom = max(ruby.gradient_rect.bottom() for ruby in first.ruby_layouts if isinstance(ruby.ruby, LowerRuby))
    top = min(ruby.gradient_rect.top() for ruby in second.ruby_layouts if not isinstance(ruby.ruby, LowerRuby))
    assert -2 <= top - bottom <= 6


def test_lower_style_and_configuration_round_trip(qapp):
    style = _style(lower_ruby_follow_upper=False, lower_ruby_gap_px=13)
    overrides = lower_style_snapshot(replace(style, ruby_latin_font_size_px=48))
    style = replace(style, lower_ruby_style=overrides)
    restored = style_from_dict(style_to_dict(style))
    assert restored.lower_ruby_style == overrides
    line = _line("かな")
    layout = _layout_line(TimingTrack(lines=[line]), line, restored, 960, 540, baseline_y=200)
    assert all(ruby.font.pixelSize() == 48 for ruby in layout.ruby_layouts)


@pytest.mark.parametrize("semantics", ["legacy", "n3_1074"])
def test_lower_readings_do_not_compress_adjacent_row_display_times(qapp, semantics):
    from krok_helper.subtitle_render.domain.timing import TrackPage, TrackPagePlan, TrackSection
    from krok_helper.subtitle_render.engine.painter import display_lines_for_style
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    windows = [(43469, 45101), (45101, 46830), (46830, 49088), (49088, 53032)]
    lines = [TimingLine(chars=[TimingChar("国", start)], end_ms=end, track_line_index=index)
             for index, (start, end) in enumerate(windows)]
    track = TimingTrack(lines=lines, rubies=[RubyAnnotation(
        "国", "に", pos_start_ms=start, pos_end_ms=end, target_line_index=index,
        target_char_start=0, target_char_end=1,
    ) for index, (start, end) in enumerate(windows)],
        page_plan=TrackPagePlan([TrackSection([TrackPage(2), TrackPage(2)])]))
    style = Style(layout_semantics=semantics, font_family="Meiryo", font_size_px=100,
        ruby_font_size_px=45, line_gap_px=10, ruby_gap_px=0, lower_ruby_gap_px=0,
        stroke_width_px=15, decoration_kind="glow", glow_before_radius_px=30,
        glow_after_radius_px=30, ruby_decoration_kind="glow", ruby_glow_before_radius_px=30,
        ruby_glow_after_radius_px=30, line_lead_in_ms=1800, line_tail_ms=1000,
        entry_anim="fade", entry_lead_ms=250, exit_anim="fade", exit_fade_ms=300,
        entry_anim_protect_ms=250, exit_anim_protect_ms=100, line_protect_ms=250,
        sync_entry=True, sync_ending=True, allow_inter_page_line_overlap=False,
        allow_entry_exit_animation_overlap=False, overlap_fallback_mode="displace")
    schedules, native_schedules = [], []
    for enabled in (False, True):
        current = replace(style, lower_ruby_enabled=enabled)
        items = display_lines_for_style(track, current, logical_w=1920, logical_h=1080)
        schedules.append([(item.display_start_ms, item.display_end_ms,
                           item.takeover_end_ms, item.takeover_exit_ms) for item in items])
        ir = build_render_ir(track, current, width=1920, height=1080, fps=60)
        native_schedules.append([(line["display_start_ms"], line["display_end_ms"])
                                 for line in ir["track"]["lines"]])
    assert schedules[1] == schedules[0]
    assert schedules[1][1][1] >= windows[1][1] + 300
    assert schedules[1][1][2:] == (None, None)
    assert native_schedules[1] == native_schedules[0]


def test_disabling_lower_restores_identical_frame(qapp):
    line = _line("かな")
    track = TimingTrack(lines=[line])
    style = replace(_style(), lower_ruby_enabled=False)
    images = []
    for current in (style, replace(style, lower_ruby_enabled=True), style):
        image = QImage(960, 540, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(image, track, 300, current)
        images.append(image)
    assert images[0] == images[2]
    assert images[0] != images[1]


def test_lower_controls_and_independent_editor_keep_upper_intact(qapp, monkeypatch):
    from PyQt6.QtWidgets import QLabel, QPushButton
    from krok_helper.qfluent_compat import ModelessDialog
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel

    panel = PropertyPanel()
    panel.set_style(replace(_style(), lower_ruby_enabled=False), emit=False)
    assert not panel._lower_ruby_style_button.isEnabled()
    panel._lower_ruby_enabled_check.setChecked(True)
    panel._lower_ruby_follow_check.setChecked(False)
    assert panel.subtitle_style.lower_ruby_style
    upper = panel.subtitle_style.ruby_latin_font_size_px
    updates = []
    panel.styleChanged.connect(updates.append)
    import krok_helper.subtitle_render.frontend.properties.property_panel as panel_module
    def unrelated_pages(*args, **kwargs):
        pytest.fail("The lower-style dialog must not construct unrelated property pages")
    monkeypatch.setattr(panel_module, "build_property_pages", unrelated_pages)
    def edit(dialog):
        editor = dialog.findChild(PropertyPanel)
        assert not any(button.text() in {"取消", "应用"} for button in dialog.findChildren(QPushButton))
        assert not any("走字规则始终沿用原读音" in label.text() for label in dialog.findChildren(QLabel))
        assert not hasattr(editor, "_viewport_align_combo")
        assert not hasattr(editor, "_scanline_mode_combo")
        assert not hasattr(editor, "_font_preview_widget")
        editor._ruby_font_latin_size_spin.setValue(56)
        assert panel.subtitle_style.lower_ruby_style["ruby_latin_font_size_px"] == 56
        assert updates[-1] == panel.subtitle_style
        editor._color_tab_panel._buttons[("left", "before")].click()
        editor._fill_mode_pill._buttons["gradient_vertical"].click()
        assert editor.subtitle_style.ruby_karaoke_colors.before.text.mode == "gradient_vertical"
        assert panel.subtitle_style.lower_ruby_style["ruby_karaoke_colors"]["before"]["text"]["mode"] == "gradient_vertical"
        editor._set_ruby_color_controls_visible(True)
        assert editor._ruby_color_actions_row.isHidden()
        # Window close / Escape must keep edits without a final apply step.
        dialog.reject()
        return 0
    monkeypatch.setattr(ModelessDialog, "exec", edit)
    panel._edit_lower_ruby_style()
    assert panel.subtitle_style.lower_ruby_style["ruby_latin_font_size_px"] == 56
    assert panel.subtitle_style.ruby_latin_font_size_px == upper
    panel.close()
    panel.deleteLater()


def test_lower_live_editor_cannot_update_a_replacement_project(qapp, monkeypatch):
    from krok_helper.qfluent_compat import ModelessDialog
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    panel = PropertyPanel()
    panel.set_style(_style(lower_ruby_follow_upper=False), emit=False)
    panel.set_project_generation(1)
    replacement = replace(panel.subtitle_style, lower_ruby_style={"ruby_latin_font_size_px": 31})
    updates = []
    panel.styleChanged.connect(updates.append)
    def edit(dialog):
        editor = dialog.findChild(PropertyPanel)
        panel.set_project_generation(2)
        panel.set_style(replacement, emit=False)
        editor._ruby_font_latin_size_spin.setValue(56)
        return 0
    monkeypatch.setattr(ModelessDialog, "exec", edit)
    panel._edit_lower_ruby_style()
    assert panel.subtitle_style.lower_ruby_style == replacement.lower_ruby_style
    assert updates == []
    panel.close()
    panel.deleteLater()


def test_lower_render_ir_keeps_source_and_main_timing_and_deduplicates_styles(qapp):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    line = _line("なぐなぐ")
    track = TimingTrack(lines=[line])
    before = deepcopy(track)
    style = _style()
    plain = build_render_ir(track, replace(style, lower_ruby_enabled=False), width=960, height=540, fps=60)
    lower = build_render_ir(track, style, width=960, height=540, fps=60)
    assert lower["track"]["lines"][0]["chars"] == plain["track"]["lines"][0]["chars"]
    assert lower["track"]["lines"][0]["resolved_intervals"] == plain["track"]["lines"][0]["resolved_intervals"]
    assert len(lower["track"]["rubies"]) == 4
    assert len(lower["track"]["lower_styles"]) == 1
    assert "lower_styles" not in plain["track"]
    assert "annotation_baseline_y" not in plain["track"]["lines"][0]
    assert before == track


def test_lower_layout_patch_key_invalidates_generated_baselines(qapp):
    from krok_helper.subtitle_render.frontend.preview.preview_async import style_patch_base_key
    line = _line("かな")
    track = TimingTrack(lines=[line])
    def key(style):
        return style_patch_base_key(track, style, [], width=960, height=540, fps=60,
                                    dpr=1, include_layout_signature=False)
    style = _style()
    assert key(style) != key(replace(style, lower_ruby_gap_px=20))
    assert key(style) != key(replace(style, lower_ruby_enabled=False))
    disabled = replace(style, lower_ruby_enabled=False)
    assert key(disabled) == key(replace(disabled, lower_ruby_gap_px=20))


@pytest.mark.parametrize("text,upper_reading,expected", [
    ("っか", "っ", ["k", "ka"]), ("ター", "タ", ["ta", "-"]),
    ("きゃ", "き", ["ky", "a"]),
])
def test_lower_conversion_uses_context_across_authored_reading_boundary(qapp, text, upper_reading, expected):
    line = _line(text)
    upper = RubyAnnotation(text[0], upper_reading, pos_start_ms=0, pos_end_ms=500,
                           target_char_start=0, target_char_end=1, target_line_index=0)
    result = lower_rubies_for_line(line, [upper], [(0, 500), (500, 1000)], _style())
    assert [ruby.reading for ruby in result] == expected
    assert [(ruby.target_char_start, ruby.target_char_end) for ruby in result] == [(0, 1), (1, 2)]


def test_lower_fonts_prepare_on_gui_thread(qapp, monkeypatch):
    from krok_helper.subtitle_render.engine.text import font_preparation
    prepared = []
    monkeypatch.setattr(font_preparation, "_prepare_one", lambda action, label: prepared.append(label))
    line = _line("かな")
    list(font_preparation._iter_prewarm_font_fallback(TimingTrack(lines=[line]), _style(), []))
    assert prepared.count("下层注音样式") == 1


@pytest.mark.parametrize("settings,expected", [
    ({}, ["ka", "t", "ta", "a"]),
    ({"romaji_repeat_long_vowels": False}, ["ka", "t", "ta-"]),
    ({"romaji_link_long_vowels": True}, ["ka", "t", "taa"]),
    ({"romaji_link_sokuon": True}, ["ka", "tta", "a"]),
    ({"romaji_link_long_vowels": True, "romaji_link_sokuon": True,
      "romaji_uppercase": True}, ["KA", "TTAA"]),
])
def test_lower_reads_embedded_sug_auto_check_options(qapp, monkeypatch, settings, expected):
    from types import SimpleNamespace
    from strange_uta_game.frontend.settings import app_settings
    calls = []
    def get(key, default=None):
        calls.append(key)
        assert key == "auto_check"
        return settings
    monkeypatch.setattr(app_settings, "AppSettings", lambda: SimpleNamespace(get=get))
    line = _line("カッター")
    style = replace(_style(), _lower_romaji_options=None)
    before = deepcopy(line)
    result = lower_rubies_for_line(line, [], [(i * 500, (i + 1) * 500) for i in range(4)], style)
    assert [ruby.reading for ruby in result] == expected
    assert calls == ["auto_check"]
    assert line == before


def test_lower_reuses_sug_sentence_particle_readings(qapp):
    line = _line("私は")
    upper = RubyAnnotation("私", "わたし", pos_start_ms=0, pos_end_ms=500,
                           target_line_index=0, target_char_start=0, target_char_end=1)
    before = deepcopy((line, upper))
    result = lower_rubies_for_line(line, [upper], [(0, 500), (500, 1000)], _style())
    assert [ruby.reading for ruby in result] == ["watashi", "wa"]
    assert (line, upper) == before


def test_sug_romaji_snapshot_is_runtime_only_and_survives_worker_pickle(qapp, monkeypatch):
    import pickle
    from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions
    from krok_helper.subtitle_render.engine.ruby import lower
    monkeypatch.setattr(lower, "read_sug_romaji_options", lambda: RomajiOptions(uppercase=True))
    original = replace(_style(), _lower_romaji_options=None)
    frozen = lower.snapshot_lower_romaji_options(original)
    assert original._lower_romaji_options is None
    assert frozen._lower_romaji_options == (True, False, False, True)
    assert "_lower_romaji_options" not in style_to_dict(frozen)
    assert style_from_dict({"_lower_romaji_options": [True] * 4})._lower_romaji_options is None
    worker_style = pickle.loads(pickle.dumps(frozen))
    monkeypatch.setattr(lower, "read_sug_romaji_options", lambda: pytest.fail("worker must use frozen SUG settings"))
    line = _line("かな")
    assert [r.reading for r in lower_rubies_for_line(line, [], [(0, 500), (500, 1000)], worker_style)] == ["KA", "NA"]
    disabled = replace(original, lower_ruby_enabled=False)
    assert lower.snapshot_lower_romaji_options(disabled) is disabled


def test_sug_romaji_options_invalidate_lower_layout_patch_key(qapp, monkeypatch):
    from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions
    from krok_helper.subtitle_render.engine.ruby import lower
    from krok_helper.subtitle_render.frontend.preview.preview_async import style_patch_base_key
    track = TimingTrack(lines=[_line("ユー")])
    style = replace(_style(), _lower_romaji_options=None)
    def key():
        return style_patch_base_key(track, style, [], width=640, height=360, fps=60, dpr=1)
    monkeypatch.setattr(lower, "read_sug_romaji_options", lambda: RomajiOptions())
    original = key()
    monkeypatch.setattr(lower, "read_sug_romaji_options", lambda: RomajiOptions(uppercase=True))
    assert key() != original
