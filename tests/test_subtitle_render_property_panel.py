"""Tests for A5/A6 subtitle style controls."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QContextMenuEvent,
    QFont,
    QFocusEvent,
    QImage,
    QMouseEvent,
    QWheelEvent,
)
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QWidget,
)
from qfluentwidgets import (  # noqa: E402
    CheckBox,
    ComboBox,
    Dialog,
    EditableComboBox,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SpinBox,
    SubtitleLabel,
    TransparentToolButton,
)

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend import property_panel as pp  # noqa: E402
from krok_helper.subtitle_render.frontend.fluent_dialogs import (  # noqa: E402
    FluentIntInputDialog,
    FluentMessageDialog,
    FluentTextInputDialog,
)
from krok_helper.subtitle_render.frontend.property_panel import (  # noqa: E402
    _ColorDialog,
    _LayoutSchematic,
    _SchematicBoard,
    _StylePresetDetailsDialog,
    ColorButton,
    PropertyPanel,
    ScreenColorPicker,
    StylePresetManagerDialog,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    LyricsLayout,
    PaintFill,
    StylePreset,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TimingChar,
    TimingLine,
    TimingTrack,
    TitleOverlay,
    effective_karaoke_animation,
    paint_fill_from_dict,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.n3_template_import import (  # noqa: E402
    N3TemplateBatchResult,
    N3TemplateLoadResult,
)
from krok_helper.subtitle_render.n3_font_catalog import N3FontCatalog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # This module creates many parentless panels/dialogs.  Leaving their Qt
    # objects alive until a later module starts pooled preview rendering can
    # make Windows destroy them from an unsafe teardown context.
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_property_panel_uses_fluent_checkboxes(qapp):
    panel = PropertyPanel()

    checkboxes = (
        panel._italic_check,
        panel._ruby_anchor_check,
        panel._ruby_colors_follow_main_check,
        panel._ruby_horizontal_gradient_with_main_check,
        panel._allow_biting_check,
        panel._lit_shadow_check,
        panel._vertical_check,
        panel._rtl_check,
        panel._sync_entry_check,
        panel._sync_ending_check,
        panel._ruby_main_reading_units_check,
    )

    assert all(isinstance(checkbox, CheckBox) for checkbox in checkboxes)
    assert not panel._ruby_main_reading_units_check.isChecked()


def test_property_panel_uses_fluent_form_controls(qapp):
    panel = PropertyPanel()

    assert isinstance(panel._font_combo, ComboBox)
    assert isinstance(panel._font_latin_combo, ComboBox)
    assert isinstance(panel._title_layout_combo, ComboBox)
    assert isinstance(panel._font_weight_combo, ComboBox)
    assert isinstance(panel._font_latin_weight_combo, ComboBox)
    assert isinstance(panel._font_size_spin, SpinBox)
    assert isinstance(panel._font_latin_size_spin, SpinBox)
    assert isinstance(panel._paint_image_path_edit, LineEdit)
    assert isinstance(panel._title_text_edit, PlainTextEdit)
    assert isinstance(panel._pages[0], ScrollArea)
    # 角色卡片的操作收成单行紧凑图标按钮（下拉框吸收剩余宽度）
    assert isinstance(panel._add_scheme_button, TransparentToolButton)
    assert isinstance(panel._rename_role_button, TransparentToolButton)
    assert isinstance(panel._delete_role_button, TransparentToolButton)
    assert isinstance(panel._manage_presets_button, TransparentToolButton)
    assert isinstance(panel._save_scheme_button, TransparentToolButton)
    # 布局方案的管理操作同款收成紧凑图标按钮
    assert isinstance(panel._add_layout_btn, TransparentToolButton)
    assert isinstance(panel._rename_layout_btn, TransparentToolButton)
    assert isinstance(panel._delete_layout_btn, TransparentToolButton)
    assert isinstance(panel._save_layout_btn, TransparentToolButton)


@pytest.mark.parametrize(
    ("factory", "typed", "expected"),
    [
        (lambda: pp._spin(0, 10_000), "1234", 1234),
        (lambda: pp._double_spin(0.0, 100.0), "12.5", 12.5),
    ],
)
def test_numeric_property_typing_is_debounced(qapp, factory, typed, expected):
    spin = factory()
    spin.show()
    spin.lineEdit().setFocus()
    spin.lineEdit().selectAll()
    emitted = []
    spin.valueChanged.connect(emitted.append)

    QTest.keyClicks(spin.lineEdit(), typed)
    qapp.processEvents()

    assert emitted == []

    QTest.qWait(180)
    qapp.processEvents()

    assert spin.value() == expected
    assert emitted == [expected]
    spin.close()


def test_cleared_numeric_field_is_not_refilled_by_debounce(qapp):
    """清空输入框后停顿一下，旧值不能自己长回来。"""
    spin = pp._spin(0, 10_000, suffix="px")
    spin.setValue(120)
    spin.show()
    spin.lineEdit().setFocus()
    spin.lineEdit().selectAll()

    QTest.keyClick(spin.lineEdit(), Qt.Key.Key_Delete)
    QTest.qWait(220)
    qapp.processEvents()

    assert spin.lineEdit().text() == "px"

    QTest.keyClicks(spin.lineEdit(), "8")
    QTest.qWait(220)
    qapp.processEvents()

    assert spin.lineEdit().text() == "8px"
    assert spin.value() == 8
    spin.close()


def test_below_minimum_typing_is_not_rewritten_mid_edit(qapp):
    """两位数删成一位数（暂时低于下限）时不能把文本改写成下限。"""
    spin = pp._spin(8, 200)
    spin.setValue(24)
    spin.show()
    spin.lineEdit().setFocus()
    spin.lineEdit().selectAll()

    QTest.keyClicks(spin.lineEdit(), "1")
    QTest.qWait(220)
    qapp.processEvents()

    assert spin.lineEdit().text() == "1"
    assert spin.value() == 24  # 越界文本不提交，旧值仍在但不回写输入框

    QTest.keyClicks(spin.lineEdit(), "2")
    QTest.qWait(220)
    qapp.processEvents()

    assert spin.lineEdit().text() == "12"
    assert spin.value() == 12
    spin.close()


@pytest.mark.parametrize(
    ("factory", "typed", "expected"),
    [
        (lambda: pp._spin(0, 10_000), "007", 7),
        (lambda: pp._double_spin(0.0, 100.0), "12.5", 12.5),
    ],
)
def test_debounced_commit_keeps_typed_text_and_caret(qapp, factory, typed, expected):
    """提交数值不能顺手规范化文本，否则光标会在输入途中跳走。"""
    spin = factory()
    spin.show()
    spin.lineEdit().setFocus()
    spin.lineEdit().selectAll()

    QTest.keyClicks(spin.lineEdit(), typed)
    QTest.qWait(220)
    qapp.processEvents()

    assert spin.value() == expected
    assert spin.lineEdit().text() == typed
    assert spin.lineEdit().cursorPosition() == len(typed)
    spin.close()


def test_numeric_field_normalises_text_once_editing_finishes(qapp):
    """失焦后照常收敛：半成品文本按 Qt 惯例回到上一个有效值。"""
    spin = pp._spin(8, 200)
    spin.setValue(24)
    spin.show()
    spin.lineEdit().setFocus()
    spin.lineEdit().selectAll()

    QTest.keyClicks(spin.lineEdit(), "3")
    QTest.qWait(220)
    qapp.processEvents()
    assert spin.lineEdit().text() == "3"

    spin.clearFocus()
    qapp.processEvents()

    assert spin.value() == 24
    assert spin.lineEdit().text() == "24"
    spin.close()


def test_font_weight_menu_only_shows_selected_font_weights(
    monkeypatch, qapp
):
    families = ("Demo Sans", "MyEmoji5", "Variable Font")
    available = {
        "Demo Sans": (400, 700),
        "MyEmoji5": (400,),
        "Variable Font": (100, 400, 700, 900),
    }
    monkeypatch.setattr(pp, "n3_font_families", lambda: families)
    monkeypatch.setattr(
        pp,
        "canonicalize_n3_font_family",
        lambda family: family if family in families else None,
    )
    monkeypatch.setattr(
        pp,
        "_available_font_weights",
        lambda family: available.get(family, pp._DEFAULT_FONT_WEIGHTS),
    )
    monkeypatch.setattr(
        pp,
        "_supports_synthetic_bold",
        lambda family, _weights: family == "MyEmoji5",
    )
    panel = PropertyPanel()
    panel.set_style(Style(font_family="Variable Font", font_weight=900))

    assert [
        panel._font_weight_combo.itemData(index)
        for index in range(panel._font_weight_combo.count())
    ] == [100, 400, 700, 900]
    assert panel._font_weight_combo.itemText(0) == "极细 100"

    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._font_combo.setCurrentFont(QFont("MyEmoji5"))

    assert [
        panel._font_weight_combo.itemData(index)
        for index in range(panel._font_weight_combo.count())
    ] == [400, 700]
    assert panel._font_weight_combo.currentText() == "粗体 700（合成）"
    assert panel.subtitle_style.font_family == "MyEmoji5"
    assert panel.subtitle_style.font_weight == 700
    assert emitted[-1].font_weight == 700


def test_inherited_font_weight_menu_tracks_effective_parent_font(
    monkeypatch, qapp
):
    families = ("Demo Sans", "MyEmoji5")
    monkeypatch.setattr(pp, "n3_font_families", lambda: families)
    monkeypatch.setattr(
        pp,
        "canonicalize_n3_font_family",
        lambda family: family if family in families else None,
    )
    monkeypatch.setattr(
        pp,
        "_available_font_weights",
        lambda family: (400,) if family == "MyEmoji5" else (400, 700),
    )
    monkeypatch.setattr(
        pp,
        "_supports_synthetic_bold",
        lambda family, _weights: family == "MyEmoji5",
    )
    panel = PropertyPanel()
    panel.set_style(
        Style(
            font_family="Demo Sans",
            font_weight=700,
            font_family_latin=None,
            latin_font_weight=None,
        )
    )

    assert [
        panel._font_latin_weight_combo.itemData(index)
        for index in range(panel._font_latin_weight_combo.count())
    ] == [0, 400, 700]
    assert panel._font_latin_weight_combo.currentData() == 0

    panel._font_combo.setCurrentFont(QFont("MyEmoji5"))

    assert [
        panel._font_latin_weight_combo.itemData(index)
        for index in range(panel._font_latin_weight_combo.count())
    ] == [0, 400, 700]
    assert (
        panel._font_latin_weight_combo.itemText(
            panel._font_latin_weight_combo.findData(700)
        )
        == "粗体 700（合成）"
    )
    assert panel._font_latin_weight_combo.currentData() == 0
    assert panel.subtitle_style.latin_font_weight is None


@pytest.mark.parametrize(
    ("factory", "value", "suffix", "expected"),
    (
        (lambda: pp._spin(0, 200, suffix=" px"), 75, "px", "75"),
        (
            lambda: pp._double_spin(0, 100, decimals=3, suffix=" %"),
            12.5,
            "%",
            "12.500",
        ),
    ),
)
def test_unit_spin_boxes_select_only_the_numeric_value(
    qapp, factory, value, suffix, expected
):
    spin = factory()
    spin.setValue(value)
    spin.show()
    editor = spin.lineEdit()
    editor.setFocus()

    QTest.keyClick(
        editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier
    )
    qapp.processEvents()

    assert editor.selectedText() == expected
    assert suffix not in editor.selectedText()


def test_unit_spin_box_keeps_mouse_selection_and_cursor_out_of_suffix(qapp):
    spin = pp._spin(0, 200, suffix=" px")
    spin.setValue(75)
    editor = spin.lineEdit()
    value_end = editor.text().index(" px")

    editor.setSelection(0, len(editor.text()))
    qapp.processEvents()
    assert editor.selectedText() == "75"

    editor.setSelection(value_end, len(" px"))
    qapp.processEvents()
    assert not editor.hasSelectedText()
    assert editor.cursorPosition() == value_end

    editor.setCursorPosition(len(editor.text()))
    qapp.processEvents()
    assert editor.cursorPosition() == value_end


def test_font_combo_uses_n3_catalog_and_never_appends_unknown(
    monkeypatch, qapp
):
    monkeypatch.setattr(
        pp, "n3_font_families", lambda: ("游明朝", "Comic Sans MS"), raising=False
    )
    monkeypatch.setattr(
        pp, "canonicalize_n3_font_family", lambda _name: None, raising=False
    )
    combo = pp._WheelFocusedFontComboBox()
    before = [combo.itemText(index) for index in range(combo.count())]

    combo.setCurrentFont(QFont("Missing Font"))

    assert [combo.itemText(index) for index in range(combo.count())] == before
    assert "Missing Font" not in before


def test_font_combo_selects_n3_canonical_name_for_saved_alias(monkeypatch, qapp):
    monkeypatch.setattr(
        pp, "n3_font_families", lambda: ("UD デジタル 教科書体 N-B",), raising=False
    )
    monkeypatch.setattr(
        pp,
        "canonicalize_n3_font_family",
        lambda name: (
            "UD デジタル 教科書体 N-B"
            if name == "UD Digi Kyokasho N-B"
            else name
        ),
        raising=False,
    )
    combo = pp._WheelFocusedFontComboBox()

    combo.setCurrentFont(QFont("UD Digi Kyokasho N-B"))

    assert combo.currentText() == "UD デジタル 教科書体 N-B"


def test_font_combo_retains_chinese_inheritance_entry(monkeypatch, qapp):
    monkeypatch.setattr(pp, "n3_font_families", lambda: ("游明朝",), raising=False)
    combo = pp._WheelFocusedFontComboBox()

    combo.enable_inheritance("跟随主文字（0）")

    assert combo.itemText(0) == "跟随主文字（0）"
    assert combo.itemText(1) == "游明朝"


class _FontMigrationSettingsProvider:
    def __init__(self, data: dict):
        self.data = dict(data)

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)


def test_live_scheme_edits_do_not_auto_save_as_app_defaults(qapp):
    initial_style = Style(
        fill_color="#111111",
        custom_style_schemes={
            TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#222222")
        },
    )
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(initial_style)}
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    win._apply_style(
        Style(
            fill_color="#333333",
            custom_style_schemes={
                TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#444444"),
                "初音": SubtitleStyleScheme(fill_color="#555555"),
            },
        )
    )
    win._selected_scheme_key = "custom:初音"
    win._save_persisted_state()

    saved = style_from_dict(provider.data["style"])
    assert saved.fill_color == "#111111"
    assert saved.custom_style_schemes[TITLE_SCHEME_NAME].fill_color == "#222222"
    assert "初音" not in saved.custom_style_schemes
    assert provider.data["selected_scheme_key"] == "global"


def test_title_enabled_and_layout_are_remembered_without_leaking_project_text(qapp):
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(Style())}
    )

    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    current = Style(
        layouts=[LyricsLayout(name="用户标题布局")],
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="当前项目标题",
            layout_index=1,
        ),
    )
    win._apply_style(current)

    assert "title_overlay" not in provider.data["style"]
    assert provider.data["new_project_defaults"] == {
        "title_enabled": True,
        "title_layout_name": "用户标题布局",
    }
    reloaded = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    assert reloaded._style.title_overlay is not None
    assert reloaded._style.title_overlay.enabled is True
    assert reloaded._style.title_overlay.text_template == "{title} / {artist}"
    assert reloaded._style.title_overlay.layout_index == 1
    assert reloaded._style.layouts[0].name == "用户标题布局"
    assert {
        layout.layout_id for layout in reloaded._style.layouts[1:]
    } >= {f"builtin-{rows}" for rows in (1, 3, 4, 5, 6, 7, 8)}
    project_style = style_from_dict(win._current_project_data()["style"])
    assert project_style.title_overlay is not None
    assert project_style.title_overlay.enabled is True
    assert project_style.title_overlay.text_template == "当前项目标题"

    # Opening a project uses its own state and does not overwrite the user's
    # new-project preference merely because the project was loaded.
    reloaded._apply_project_data(
        {
            "style": style_to_dict(
                Style(title_overlay=TitleOverlay(enabled=False, text_template="项目值"))
            )
        }
    )
    assert reloaded._style.title_overlay is not None
    assert reloaded._style.title_overlay.enabled is False
    reloaded._project_dirty = False
    reloaded._new_project()
    assert reloaded._style.title_overlay is not None
    assert reloaded._style.title_overlay.enabled is True
    assert reloaded._style.title_overlay.text_template == "{title} / {artist}"

    reloaded._apply_style(
        replace(
            reloaded._style,
            title_overlay=replace(reloaded._style.title_overlay, enabled=False),
        )
    )
    disabled_reload = mw.SubtitleRenderWindow(
        embedded=True, settings_provider=provider
    )
    assert disabled_reload._style.title_overlay is not None
    assert disabled_reload._style.title_overlay.enabled is False


def test_builtin_scheme_defaults_are_saved_only_for_requested_target(qapp):
    initial_style = Style(
        fill_color="#111111",
        line_gap_px=21,
        layouts=[LyricsLayout(name="保留布局", line_gap_px=22)],
        custom_style_schemes={
            TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#222222")
        },
    )
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(initial_style)}
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    win._apply_style(
        Style(
            fill_color="#333333",
            line_gap_px=99,
            layouts=[LyricsLayout(name="项目布局", line_gap_px=98)],
            custom_style_schemes={
                TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#444444"),
                "初音": SubtitleStyleScheme(fill_color="#555555"),
            },
        )
    )

    win._save_builtin_scheme_default("global")
    saved = style_from_dict(provider.data["style"])
    assert saved.fill_color == "#333333"
    # Layout preferences are remembered automatically; the explicit font/color
    # save action still updates only its requested scheme target.
    assert saved.line_gap_px == 99
    assert (saved.layouts[0].name, saved.layouts[0].line_gap_px) == (
        "项目布局",
        98,
    )
    assert {
        layout.layout_id for layout in saved.layouts[1:]
    } >= {f"builtin-{rows}" for rows in (1, 3, 4, 5, 6, 7, 8)}
    assert saved.custom_style_schemes[TITLE_SCHEME_NAME].fill_color == "#222222"
    assert "初音" not in saved.custom_style_schemes

    win._save_builtin_scheme_default(f"custom:{TITLE_SCHEME_NAME}")
    saved = style_from_dict(provider.data["style"])
    assert saved.fill_color == "#333333"
    assert saved.custom_style_schemes[TITLE_SCHEME_NAME].fill_color == "#444444"
    assert "初音" not in saved.custom_style_schemes

    win._apply_style(
        replace(
            win._style,
            fill_color="#666666",
            custom_style_schemes={
                TITLE_SCHEME_NAME: SubtitleStyleScheme(fill_color="#777777"),
                "镜音": SubtitleStyleScheme(fill_color="#888888"),
            },
        )
    )
    win._project_dirty = False
    win._new_project()
    assert win._style.fill_color == "#333333"
    assert win._style.custom_style_schemes[TITLE_SCHEME_NAME].fill_color == "#444444"
    assert "镜音" not in win._style.custom_style_schemes
    assert win._style.line_gap_px == 99
    assert win._style.layouts[0].name == "项目布局"


def test_builtin_font_defaults_are_normalized_to_app_reference_height(qapp, monkeypatch):
    initial_style = Style(
        font_size_px=100,
        stroke_width_px=15,
        font_reference_height=1080,
        custom_style_schemes={
            TITLE_SCHEME_NAME: SubtitleStyleScheme(
                font_size_px=40,
                stroke_width_px=5,
            )
        },
    )
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(initial_style)}
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    monkeypatch.setattr(mw.InfoBar, "success", lambda *args, **kwargs: None)
    win._apply_style(
        replace(
            win._style,
            font_size_px=200,
            stroke_width_px=30,
            font_reference_height=2160,
            custom_style_schemes={
                TITLE_SCHEME_NAME: SubtitleStyleScheme(
                    font_size_px=80,
                    stroke_width_px=10,
                )
            },
        )
    )

    win._save_builtin_scheme_default("global")
    win._save_builtin_scheme_default(f"custom:{TITLE_SCHEME_NAME}")

    saved = style_from_dict(provider.data["style"])
    assert saved.font_reference_height == 1080
    assert saved.font_size_px == 100
    assert saved.stroke_width_px == 15
    assert saved.custom_style_schemes[TITLE_SCHEME_NAME].font_size_px == 40
    assert saved.custom_style_schemes[TITLE_SCHEME_NAME].stroke_width_px == 5


def test_current_style_is_resolved_for_persisted_output_height(qapp):
    app_default = Style(
        font_size_px=100,
        line_gap_px=90,
        font_reference_height=1080,
        layout_reference_height=1080,
    )
    provider = _FontMigrationSettingsProvider(
        {
            "style": style_to_dict(app_default),
            "screen": {
                "preset_key": "uhd_4k",
                "par": "1:1",
                "width": 3840,
                "height": 2160,
                "fps": 60,
            },
        }
    )

    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._app_default_style.font_size_px == 100
    assert win._app_default_style.font_reference_height == 1080
    assert win._style.font_size_px == 200
    assert win._style.font_reference_height == 2160
    assert win._style.line_gap_px == 180
    assert win._style.layout_reference_height == 2160


def test_layout_defaults_follow_live_user_edits_at_app_reference_height(
    qapp, monkeypatch
):
    initial_style = Style(
        line_gap_px=21,
        horizontal_margin_px=31,
        layouts=[
            LyricsLayout(name="副歌布局", line_y_margin_px=41),
            LyricsLayout(name="保留布局", line_y_margin_px=51),
        ],
        layout_reference_height=1080,
    )
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(initial_style)}
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    monkeypatch.setattr(mw.InfoBar, "success", lambda *args, **kwargs: None)
    project_style = replace(
        win._style,
        line_gap_px=70,
        horizontal_margin_px=80,
        upper_line_left_margin_px=80,
        lower_line_right_margin_px=80,
        layouts=[
            LyricsLayout(name="副歌布局", line_y_margin_px=72),
            LyricsLayout(name="项目新增", line_y_margin_px=60),
        ],
        layout_reference_height=720,
    )

    # User layout edits automatically become the next new project's defaults,
    # normalized from the current 720p project to the app's 1080p reference.
    win._apply_style(project_style)
    saved = style_from_dict(provider.data["style"])
    assert saved.line_gap_px == 105
    assert saved.horizontal_margin_px == 120
    assert [layout.name for layout in saved.layouts[:2]] == ["副歌布局", "项目新增"]
    assert saved.layouts[0].line_y_margin_px == 108
    assert saved.layouts[1].line_y_margin_px == 90

    win._project_dirty = False
    win._new_project()
    assert win._style.line_gap_px == 105
    assert [layout.name for layout in win._style.layouts[:2]] == [
        "副歌布局",
        "项目新增",
    ]


def test_batch_layout_assignment_is_remembered_for_new_subtitle_sources(qapp):
    provider = _FontMigrationSettingsProvider(
        {
            "style": style_to_dict(
                Style(layouts=[LyricsLayout(name="常用布局")])
            )
        }
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    first_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("一", 0)], end_ms=500),
            TimingLine(chars=[TimingChar("二", 500)], end_ms=1000),
        ]
    )
    win._timing_track = first_track

    win._on_layout_assign_all(1)

    assert [line.layout_index for line in first_track.lines] == [1, 1]
    assert provider.data["new_project_defaults"]["layout_assignment"] == {
        "mode": "all",
        "layout_name": "常用布局",
    }

    reloaded = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    next_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("三", 0)], end_ms=500),
            TimingLine(chars=[TimingChar("四", 500)], end_ms=1000),
        ]
    )
    reloaded._apply_timing_track(next_track, None)
    assert [line.layout_index for line in next_track.lines] == [1, 1]

    # Project loading bypasses the app preference; its saved row assignments
    # remain authoritative and are applied later by the project loader.
    project_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("五", 0)], end_ms=500, layout_index=0)]
    )
    reloaded._loading_project = True
    try:
        reloaded._apply_timing_track(project_track, None)
    finally:
        reloaded._loading_project = False
    assert project_track.lines[0].layout_index == 0


def _font_migration_catalog() -> N3FontCatalog:
    return N3FontCatalog(
        families=("游明朝", "标准名称"),
        aliases={"游明朝": "游明朝", "old alias": "标准名称", "标准名称": "标准名称"},
        authoritative=True,
    )


def test_startup_rewrites_persisted_font_alias(monkeypatch, qapp):
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(Style(font_family="Old Alias"))}
    )
    monkeypatch.setattr(
        mw, "get_n3_font_catalog", _font_migration_catalog, raising=False
    )

    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._style.font_family == "标准名称"
    assert provider.data["style"]["font_family"] == "标准名称"


def test_startup_normalizes_saved_style_presets(monkeypatch, qapp):
    provider = _FontMigrationSettingsProvider(
        {
            "style": style_to_dict(Style(font_family="游明朝")),
            "style_presets": {
                "旧预设": {
                    "group": "",
                    "scheme": {"font_family": "Old Alias"},
                    "source_type": "",
                    "source_data": {},
                }
            },
        }
    )
    monkeypatch.setattr(
        mw, "get_n3_font_catalog", _font_migration_catalog, raising=False
    )

    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._style_presets["旧预设"].scheme.font_family == "标准名称"
    assert provider.data["style_presets"][0]["name"] == "旧预设"
    assert provider.data["style_presets"][0]["scheme"]["font_family"] == "标准名称"


def test_project_load_normalizes_font_alias_in_memory(monkeypatch, qapp):
    provider = _FontMigrationSettingsProvider(
        {"style": style_to_dict(Style(font_family="游明朝"))}
    )
    monkeypatch.setattr(
        mw, "get_n3_font_catalog", _font_migration_catalog, raising=False
    )
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    win._apply_project_data(
        {"style": style_to_dict(Style(font_family="Old Alias"))}
    )

    assert win._style.font_family == "标准名称"


def test_delete_layout_uses_fluent_confirmation(qapp, monkeypatch):
    panel = PropertyPanel()
    panel.set_style(Style(layouts=[LyricsLayout(name="副歌布局")]))
    panel._layout_combo.setCurrentIndex(panel._layout_combo.findData(1))
    captured: dict[str, object] = {}

    def reject(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return False

    monkeypatch.setattr(pp, "fluent_question", reject)
    panel._on_delete_layout()

    assert [layout.name for layout in panel.subtitle_style.layouts] == ["副歌布局"]
    assert captured["args"][1:3] == (
        "删除布局",
        "确定要删除布局“副歌布局”吗？\n使用它的歌词行（和标题）会回到默认布局。",
    )
    assert captured["kwargs"] == {
        "yes_text": "删除",
        "no_text": "取消",
        "default_cancel": True,
    }

    monkeypatch.setattr(pp, "fluent_question", lambda *args, **kwargs: True)
    panel._on_delete_layout()
    assert panel.subtitle_style.layouts == []


def test_property_panel_set_style_populates_controls(qapp):
    panel = PropertyPanel()
    style = Style(
        font_family="Microsoft YaHei UI",
        font_family_latin="Arial",
        font_size_px=72,
        latin_font_size_px=64,
        latin_font_weight=600,
        letter_spacing_px=6,
        space_width_percent=35,
        allow_biting=True,
        font_weight=900,
        italic=True,
        affects_ruby_anchor=False,
        base_color="#102030",
        fill_color="#405060",
        fill_gradient_enabled=True,
        fill_gradient_start_color="#111111",
        fill_gradient_end_color="#EEEEEE",
        fill_gradient_angle_deg=45,
        stroke_color="#708090",
        stroke_width_px=8,
        shadow_color="#A0B0C0",
        shadow_offset_x=3,
        shadow_offset_y=4,
        viewport_align="bottom_right",
        viewport_offset_x=-120,
        viewport_offset_y=60,
        viewport_scale_pct=150,
        viewport_rotation_deg=-30,
        line_y_position="top",
        line_y_margin_px=120,
        dual_line_layout=False,
        right_to_left=True,
        vertical=True,
        line_horizontal_layout="per_row",
        line_gap_px=66,
        horizontal_margin_px=77,
        row1_align="center",
        row1_offset_x=11,
        row1_offset_y=-22,
        row2_align="left",
        row2_offset_x=33,
        row2_offset_y=44,
        line_lead_in_ms=900,
        line_tail_ms=1100,
        timing_offset_ms=-120,
        ruby_main_progress_mode="reading_units",
        section_gap_ms=5000,
        sync_entry=True,
        sync_ending=True,
        section_ending_mode="clear",
        line_lane_gap_ms=250,
        line_max_hold_ms=9000,
        entry_anim="utopia",
        entry_lead_ms=450,
        exit_anim="char_fade",
        exit_fade_ms=650,
        karaoke_anim="utopia",
        lit_enabled=True,
        lit_style="rounded",
        lit_number=2,
        lit_size=36,
        lit_offset_x=90,
        lit_offset_y=70,
        lit_tracking=14,
        lit_fill_color="#333333",
        lit1_fill_color="#112233",
        lit2_fill_color="#445566",
        lit3_fill_color="#778899",
        lit_stroke_color="#AABBCC",
        lit_stroke_width=5,
        lit_stroke_soften=3,
        lit_opacity_pct=80,
        lit_edge_brightness_pct=45,
        lit_shadow=True,
        lit_time_offset_ms=-300,
        lit_waiting_time_ms=200,
        lit_transition_mode="slide",
        lit_transition_ratio_pct=30,
        lit_transition_angle_deg=45,
        lit_transition_distance=24,
        signals_duration_ms=1800,
        volume_size=64,
        volume_offset_x=12,
        volume_offset_y=-8,
        volume_column_width=10,
        volume_column_count=5,
        volume_column_spacing=3,
        volume_align=2,
        volume_ratio=4.0,
        volume_fill_color="#010203",
        volume_stroke_color="#040506",
        volume_overlay_fill_color="#070809",
        volume_overlay_stroke_color="#0A0B0C",
        volume_flash_times=6,
        volume_flash_duration_ratio=0.75,
        volume_transition_ratio_pct=55,
        ruby_font_size_px=30,
        ruby_color="#223344",
        ruby_gap_px=9,
    )

    panel.set_style(style)

    assert panel.subtitle_style == style
    assert panel._font_size_spin.value() == 72
    assert panel._font_latin_combo.currentFont().family() == "Arial"
    assert panel._font_latin_size_spin.value() == 64
    assert panel._font_latin_weight_combo.currentData() == 600
    assert panel._letter_spacing_spin.value() == 6
    assert panel._space_width_spin.value() == 35
    assert panel._font_weight_combo.currentData() == 900
    assert panel._italic_check.isChecked()
    assert not panel._ruby_anchor_check.isChecked()
    assert panel._allow_biting_check.isChecked()
    assert panel._color_state_combo.currentData() == "after"
    assert panel._color_layer_combo.currentData() == "text"
    assert panel._fill_mode_combo.currentData() == "gradient_horizontal"
    assert panel._paint_gradient_start_btn.color == "#111111"
    assert panel._paint_gradient_end_btn.color == "#EEEEEE"
    panel._color_state_combo.setCurrentIndex(panel._color_state_combo.findData("before"))
    assert panel._paint_solid_btn.color == "#102030"
    assert panel._stroke_width_spin.value() == 8
    assert panel._shadow_x_spin.value() == 3
    assert panel._shadow_y_spin.value() == 4
    assert panel._viewport_align_combo.currentData() == "bottom_right"
    assert panel._viewport_x_spin.value() == -120
    assert panel._viewport_y_spin.value() == 60
    assert panel._viewport_scale_spin.value() == 150
    assert panel._viewport_rotation_spin.value() == -30
    assert panel._line_position_seg.value() == "top"
    assert panel._line_margin_spin.value() == 120
    assert panel._rtl_check.isChecked()
    assert panel._vertical_check.isChecked()
    assert panel._line_gap_spin.value() == 66
    assert panel._horizontal_margin_spin.value() == 77
    # 旧项目的「单行 + 逐行独立」投影为右侧唯一的行布局列表。
    assert panel._current_layout_alignments() == ["center"]
    assert not hasattr(panel, "_dual_line_check")
    assert not hasattr(panel, "_horizontal_layout_combo")
    assert panel._line_lead_spin.value() == 900
    assert panel._line_tail_spin.value() == 1100
    assert panel._line_offset_spin.value() == -120
    assert panel._ruby_main_reading_units_check.isChecked()
    assert panel._section_gap_spin.value() == 5000
    assert panel._sync_entry_check.isChecked()
    assert panel._sync_ending_check.isChecked()
    assert panel._section_ending_combo.currentData() == "clear"
    assert panel._entry_anim_combo.currentData() == "utopia"
    assert panel._entry_lead_spin.value() == 450
    assert panel._exit_anim_combo.currentData() == "char_fade"
    assert panel._exit_fade_spin.value() == 650
    assert panel._karaoke_anim_combo.currentData() == "utopia"
    assert panel._lit_enabled_switch.isChecked()
    assert panel._lit_style_combo.currentData() == "rounded"
    assert panel._lit_number_spin.value() == 2
    assert panel._lit_size_spin.value() == 36
    assert panel._lit_x_spin.value() == 90
    assert panel._lit_y_spin.value() == 70
    assert panel._lit_tracking_spin.value() == 14
    assert panel._lit_fill_btn.color == "#333333"
    assert panel._lit_stroke_btn.color == "#AABBCC"
    assert panel._lit_stroke_width_spin.value() == 5
    assert panel._lit_stroke_soften_spin.value() == 3
    assert panel._lit_opacity_spin.value() == 80
    assert panel._lit_edge_brightness_spin.value() == 45
    assert panel._lit_shadow_check.isChecked()
    assert panel._lit_waiting_time_spin.value() == 200
    assert panel._lit_transition_mode_combo.currentData() == "slide"
    assert panel._lit_transition_ratio_spin.value() == 30
    assert panel._lit_transition_angle_spin.value() == 45
    assert panel._lit_transition_distance_spin.value() == 24
    assert panel._lit_duration_spin.value() == 1800
    assert panel._volume_size_spin.value() == 64
    assert panel._volume_x_spin.value() == 12
    assert panel._volume_y_spin.value() == -8
    assert panel._volume_column_width_spin.value() == 10
    assert panel._volume_column_count_spin.value() == 5
    assert panel._volume_column_spacing_spin.value() == 3
    assert panel._volume_ratio_spin.value() == 4
    assert panel._volume_align_combo.currentData() == 2
    assert panel._volume_flash_times_spin.value() == 6
    assert panel._volume_flash_duration_spin.value() == 75
    assert panel._volume_transition_ratio_spin.value() == 55
    assert panel._volume_fill_btn.color == "#010203"
    assert panel._volume_stroke_btn.color == "#040506"
    assert panel._volume_overlay_fill_btn.color == "#070809"
    assert panel._volume_overlay_stroke_btn.color == "#0A0B0C"
    assert panel._ruby_font_size_spin.value() == 30
    assert panel._ruby_gap_spin.value() == 9


def test_lit_section_shows_only_active_style_controls(qapp):
    panel = PropertyPanel()

    # Volume style: the volume.* groups apply, the shape lit.* groups do not.
    panel.set_style(Style(lit_enabled=True, lit_style="volume"))
    assert all(not w.isHidden() for w in panel._lit_volume_groups)
    assert all(w.isHidden() for w in panel._lit_shape_groups)

    # Shape style: the opposite — shape groups apply, volume groups do not.
    panel.set_style(Style(lit_enabled=True, lit_style="circle"))
    assert all(not w.isHidden() for w in panel._lit_shape_groups)
    assert all(w.isHidden() for w in panel._lit_volume_groups)

    # Switching the style live (via the combo) flips visibility too.
    panel._lit_style_combo.setCurrentIndex(panel._lit_style_combo.findData("volume"))
    assert panel._style.lit_style == "volume"
    assert all(not w.isHidden() for w in panel._lit_volume_groups)
    assert all(w.isHidden() for w in panel._lit_shape_groups)


def test_property_panel_does_not_shadow_qwidget_style(qapp):
    panel = PropertyPanel()

    qt_style = panel.style()
    qt_style.unpolish(panel)
    qt_style.polish(panel)

    assert panel.subtitle_style == panel._style


def test_style_defaults_match_nicokara_layout_baseline():
    style = Style()

    assert style.font_family == "UD デジタル 教科書体 N-B"
    assert style.font_family_latin is None
    assert style.font_size_px == 100
    assert style.latin_font_size_px is None
    assert style.latin_font_weight is None
    assert style.latin_stroke_width_px is None
    assert style.latin_stroke2_enabled is None
    assert style.latin_stroke2_width_px is None
    assert style.letter_spacing_px == 0
    assert style.space_width_percent == 20
    assert style.allow_biting is False
    assert style.font_weight == 400
    assert style.fill_gradient_enabled is False
    assert style.fill_gradient_start_color == "#FF5A6F"
    assert style.fill_gradient_end_color == "#0055FF"
    assert style.fill_gradient_angle_deg == 0
    assert style.ruby_font_size_px == 45
    assert style.ruby_font_family is None
    assert style.ruby_font_weight is None
    assert style.ruby_font_family_latin is None
    assert style.ruby_latin_font_size_px is None
    assert style.ruby_latin_font_weight is None
    assert style.ruby_latin_stroke_width_px is None
    assert style.ruby_latin_stroke2_enabled is None
    assert style.ruby_latin_stroke2_width_px is None
    assert style.ruby_gap_px == 0
    assert style.ruby_stroke_width_px == 10
    assert style.ruby_stroke2_width_px == 3
    assert style.viewport_align == "center"
    assert style.viewport_offset_x == 0
    assert style.viewport_offset_y == 0
    assert style.viewport_scale_pct == 100
    assert style.viewport_rotation_deg == 0
    assert style.line_y_position == "bottom"
    assert style.line_y_margin_px == 80
    assert style.dual_line_layout is True
    assert style.line_horizontal_layout == "asymmetric"
    assert style.right_to_left is False
    assert style.vertical is False
    assert style.row1_align == "left"
    assert style.row1_offset_x == 50
    assert style.row1_offset_y == 0
    assert style.row2_align == "right"
    assert style.row2_offset_x == -50
    assert style.row2_offset_y == 0
    assert style.line_gap_px == 90
    assert style.stroke_width_px == 15
    assert style.stroke2_width_px == 5
    assert style.decoration_kind == "shadow"
    assert style.glow_radius_px == 10
    # N3 阴影偏移固定 = DecorSize（双轴同值），新建默认 10（CreateLyricsFont）。
    assert style.shadow_offset_x == 10
    assert style.shadow_offset_y == 10
    assert style.horizontal_margin_px == 50
    assert style.line_alignments == ["left", "right"]
    assert style.line_lead_in_ms == 1800
    assert style.line_tail_ms == 1000
    assert style.timing_offset_ms == 0
    assert style.section_gap_ms == 4000
    assert style.sync_entry is False
    assert style.sync_ending is False
    assert style.section_ending_mode == "hold"
    assert style.line_lane_gap_ms == 300
    assert style.line_continuity_snap_ms == 800
    assert style.line_pair_second_delay_ms == 3000
    assert style.line_max_hold_ms == 12_000
    assert style.entry_anim == "none"
    assert style.entry_lead_ms == 300
    assert style.exit_anim == "none"
    assert style.exit_fade_ms == 300
    assert style.lit_enabled is False
    assert style.lit_style == "volume"
    assert style.lit_number == 4
    assert style.lit_size == 32
    assert style.lit_offset_x == 0
    assert style.lit_offset_y == -24
    assert style.lit_tracking == 0
    assert style.lit_fill_color == "#0000FF"
    assert style.lit1_fill_color == "#FF0000"
    assert style.lit2_fill_color == "#FFFF00"
    assert style.lit3_fill_color == "#00FF00"
    assert style.lit_stroke_color == "#FFFFFF"
    assert style.lit_stroke_width == 2
    assert style.lit_stroke_soften == 0
    assert style.lit_opacity_pct == 100
    assert style.lit_edge_brightness_pct == 60
    assert style.lit_shadow is True
    assert style.lit_time_offset_ms == 0
    assert style.lit_waiting_time_ms == 0
    assert style.lit_transition_mode == "fade"
    assert style.lit_transition_ratio_pct == 67
    assert style.lit_transition_angle_deg == 0
    assert style.lit_transition_distance == 0
    assert style.signals_duration_ms == 4000
    assert style.volume_size == 48
    assert style.volume_offset_x == 0
    assert style.volume_offset_y == 0
    assert style.volume_column_width == 12
    assert style.volume_column_count == 4
    assert style.volume_column_spacing == 0
    assert style.volume_align == 1
    assert style.volume_ratio == 3.0
    assert style.volume_fill_color == "#FFFFFF"
    assert style.volume_stroke_color == "#0000FF"
    assert style.volume_overlay_fill_color == "#0000FF"
    assert style.volume_overlay_stroke_color == "#FFFFFF"
    assert style.volume_flash_times == 3
    assert style.volume_flash_duration_ratio == 1.0
    assert style.volume_transition_ratio_pct == 67


def test_property_panel_subtitle_page_has_no_horizontal_scroll(qapp):
    panel = PropertyPanel()
    subtitle_page = panel.widget(0)
    layout_page = panel.widget(1)

    assert panel.minimumWidth() == 320
    assert subtitle_page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert layout_page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    panel.setCurrentIndex(1)
    panel.show()
    panel.resize(320, 800)
    qapp.processEvents()
    assert layout_page.widget().width() <= layout_page.viewport().width()
    assert panel._font_combo.minimumWidth() == 0
    # Numeric controls keep the current localized value readable; the compact
    # arrow hides before it can overlap instead of forcing a zero minimum width.
    assert panel._font_size_spin.minimumWidth() >= (
        panel._font_size_spin.lineEdit().fontMetrics().horizontalAdvance(
            panel._font_size_spin.lineEdit().text()
        )
    )
    assert panel._line_margin_spin.parentWidget() is not panel._font_size_spin.parentWidget()
    assert panel._singer_combo.parentWidget() is not panel._line_margin_spin.parentWidget()
    subtitle_layout = subtitle_page.widget().layout()
    first_section = subtitle_layout.itemAt(0).widget()
    assert first_section.objectName() == "SubtitlePropertyCard"
    assert not hasattr(first_section, "header")
    assert panel._role_section is first_section
    assert panel._font_color_section is first_section


def test_property_panel_sections_are_collapsible(qapp):
    panel = PropertyPanel()
    effects_page = panel.widget(3)
    effects_layout = effects_page.widget().layout()
    first_section = effects_layout.itemAt(0).widget()
    header = first_section.header
    content = first_section.layout().itemAt(1).widget()

    assert header.text() == "入退场动画"
    assert not content.isHidden()
    assert header.arrowType() == Qt.ArrowType.DownArrow

    header.click()
    assert content.isHidden()
    assert header.arrowType() == Qt.ArrowType.RightArrow

    header.click()
    assert not content.isHidden()
    assert header.arrowType() == Qt.ArrowType.DownArrow


def test_effects_page_uses_compact_responsive_groups(qapp):
    panel = PropertyPanel()
    panel.resize(1050, 820)
    panel.show()
    panel.setCurrentIndex(3)
    qapp.processEvents()

    assert not panel._lit_section.header.isChecked()
    assert not panel._lit_section.is_expanded()
    assert panel._lit_section.header.arrowType() == Qt.ArrowType.RightArrow

    panel._lit_section.set_expanded(True)
    qapp.processEvents()
    assert panel._animation_grid._columns == 2
    assert panel._entry_anim_combo.parentWidget() is panel._entry_animation_row
    assert panel._entry_lead_spin.parentWidget() is panel._entry_animation_row
    assert panel._exit_anim_combo.parentWidget() is panel._exit_animation_row
    assert panel._exit_fade_spin.parentWidget() is panel._exit_animation_row
    assert panel._lit_group_grids["通用"]._columns >= 4
    assert panel._lit_group_grids["音量柱 · 布局"]._columns == 4

    subgroup_titles = [
        label.text()
        for label in panel.widget(3).findChildren(QLabel, "SubtitlePropertySubheading")
    ]
    assert "音量柱 · 尺寸" not in subgroup_titles
    assert "音量柱 · 位置" not in subgroup_titles
    assert "形状灯 · 尺寸" not in subgroup_titles
    assert "形状灯 · 位置" not in subgroup_titles
    assert "音量柱 · 布局" in subgroup_titles
    assert "形状灯 · 布局" in subgroup_titles

    panel.resize(360, 820)
    qapp.processEvents()
    assert panel._animation_grid._columns == 1


def test_title_page_uses_compact_responsive_appearance_and_timing(qapp):
    panel = PropertyPanel()
    panel.resize(1050, 820)
    panel.show()
    panel.setCurrentIndex(4)
    qapp.processEvents()

    assert panel._title_appearance_grid._columns == 2
    assert panel._title_time_grid._columns == 1
    assert panel._title_head_grid._columns == 4
    assert panel._title_tail_row.isHidden()

    subgroup_titles = [
        label.text()
        for label in panel.widget(4).findChildren(QLabel, "SubtitlePropertySubheading")
    ]
    assert "时间" not in subgroup_titles

    panel.resize(360, 820)
    qapp.processEvents()
    assert panel._title_appearance_grid._columns == 1
    assert panel._title_time_grid._columns == 1
    # 秒 + 毫秒复合框放不下两列，窄面板下退成单列而不是把 "999 ms" 截断。
    assert panel._title_head_grid._columns == 1
    title_page = panel.widget(4)
    assert title_page.horizontalScrollBar().maximum() == 0


def test_title_head_tail_mode_has_independent_timing_rows(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            title_overlay=TitleOverlay(
                enabled=True,
                show_mode="head_tail",
                head_offset_ms=100,
                duration_ms=2_000,
                fade_in_ms=300,
                fade_out_ms=400,
                tail_offset_ms=500,
                tail_duration_ms=3_000,
                tail_fade_in_ms=600,
                tail_fade_out_ms=700,
            )
        )
    )

    assert not panel._title_head_row.isHidden()
    assert not panel._title_tail_row.isHidden()
    assert panel._title_head_row_label.text() == "开头"
    assert panel._title_tail_duration_spin.value() == 3_000
    assert panel._title_tail_fade_in_spin.value() == 600
    assert panel._title_tail_fade_out_spin.value() == 700

    panel._title_tail_duration_spin.setValue(4_000)
    panel._title_tail_fade_in_spin.setValue(800)
    panel._title_tail_fade_out_spin.setValue(900)
    title = panel._style.title_overlay
    assert title is not None
    assert title.duration_ms == 2_000
    assert title.fade_in_ms == 300
    assert title.fade_out_ms == 400
    assert title.tail_duration_ms == 4_000
    assert title.tail_fade_in_ms == 800
    assert title.tail_fade_out_ms == 900


def test_title_timing_fields_split_seconds_and_millis(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            title_overlay=TitleOverlay(
                enabled=True,
                # N3 的时间标签是 10ms 粒度，导入值本来就不是整秒。
                head_offset_ms=1_230,
                duration_ms=10_000,
                fade_in_ms=300,
            )
        )
    )

    assert panel._title_head_spin.seconds_spin.value() == 1
    assert panel._title_head_spin.millis_spin.value() == 230
    assert panel._title_head_spin.value() == 1_230
    assert panel._title_duration_spin.seconds_spin.value() == 10
    assert panel._title_duration_spin.millis_spin.value() == 0
    assert panel._title_fade_in_spin.seconds_spin.value() == 0
    assert panel._title_fade_in_spin.millis_spin.value() == 300

    # 两个分量分别编辑，写回模型的仍是整数毫秒。
    panel._title_duration_spin.seconds_spin.setValue(12)
    panel._title_duration_spin.millis_spin.setValue(45)
    title = panel._style.title_overlay
    assert title is not None
    assert title.duration_ms == 12_045


def test_title_timing_millis_step_carries_into_seconds(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(title_overlay=TitleOverlay(enabled=True, duration_ms=1_999))
    )

    spin = panel._title_duration_spin
    spin.millis_spin.stepBy(1)  # 999 → 进位
    assert spin.value() == 2_000
    assert (spin.seconds_spin.value(), spin.millis_spin.value()) == (2, 0)

    spin.millis_spin.stepBy(-1)  # 0 → 借位
    assert spin.value() == 1_999
    assert (spin.seconds_spin.value(), spin.millis_spin.value()) == (1, 999)

    title = panel._style.title_overlay
    assert title is not None
    assert title.duration_ms == 1_999


def test_title_timing_millis_range_follows_seconds_limit(qapp):
    panel = PropertyPanel()
    panel.set_style(Style(title_overlay=TitleOverlay(enabled=True)))

    spin = panel._title_fade_in_spin  # 上限 10 000ms
    assert spin.millis_spin.maximum() == 999
    spin.seconds_spin.setValue(10)
    # 秒位顶到上限后毫秒位只能是 0，越界组合根本敲不出来。
    assert spin.millis_spin.maximum() == 0
    assert spin.value() == 10_000

    spin.seconds_spin.setValue(9)
    assert spin.millis_spin.maximum() == 999

    spin.setValue(99_999)
    assert spin.value() == 10_000
    assert (spin.seconds_spin.value(), spin.millis_spin.value()) == (10, 0)

    # 借位不能突破下限。
    spin.setValue(0)
    spin.millis_spin.stepBy(-1)
    assert spin.value() == 0


def test_title_timing_millis_typing_survives_panel_round_trip(qapp):
    """毫秒位打字提交后，样式回流不能把用户正在敲的文本改写掉。"""
    panel = PropertyPanel()
    panel.set_style(
        Style(title_overlay=TitleOverlay(enabled=True, duration_ms=2_500))
    )
    panel.show()
    panel.setCurrentIndex(4)
    qapp.processEvents()

    editor = panel._title_duration_spin.millis_spin.lineEdit()
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, "045")
    QTest.qWait(220)
    qapp.processEvents()

    assert panel._title_duration_spin.value() == 2_045
    title = panel._style.title_overlay
    assert title is not None
    assert title.duration_ms == 2_045
    # 提交走的是 _update_title → _sync_title_controls → setValue 这条回路，
    # 途中不能把 "045" 规范化成 "45"。
    assert editor.text() == "045 ms"
    panel.close()


def test_title_timing_unit_split_does_not_touch_undo_or_dirty_state(qapp, tmp_path):
    """拆成两个框只是显示方式：同一个毫秒值不该产生多余的样式变更。"""
    panel = PropertyPanel()
    panel.set_style(
        Style(title_overlay=TitleOverlay(enabled=True, duration_ms=2_500))
    )

    emitted: list[int] = []
    panel.styleChanged.connect(lambda style: emitted.append(
        style.title_overlay.duration_ms if style.title_overlay else -1
    ))

    spin = panel._title_duration_spin
    spin.setValue(2_500)  # 值没变
    assert emitted == []

    spin.seconds_spin.setValue(3)
    assert emitted == [3_500]


def test_property_panel_font_and_color_sections_are_side_by_side(qapp):
    panel = PropertyPanel()
    panel.resize(900, 800)
    panel.show()
    qapp.processEvents()

    subtitle_layout = panel.widget(0).widget().layout()
    assert subtitle_layout.count() == 2  # 一张组合卡片 + 底部 stretch
    assert panel._font_color_section.isAncestorOf(panel._role_navigation)
    assert panel._scheme_section is panel._font_color_section
    assert panel._role_navigation.geometry().bottom() < panel._font_color_row.geometry().top()
    assert panel._font_section.parentWidget() is panel._font_color_row
    assert panel._color_section.parentWidget() is panel._font_color_row
    assert panel._font_section.geometry().top() == panel._color_section.geometry().top()
    # 颜色在左、字体在右（与 nicokara maker3 的编辑顺序一致）。
    assert panel._color_section.geometry().right() < panel._font_section.geometry().left()
    assert abs(panel._font_section.width() - panel._color_section.width()) <= 1
    # 描边尺寸进入字体页后字体卡片更高；两张卡片仍只做顶部对齐。
    assert panel._font_section.height() > panel._color_section.height()
    assert (
        panel._font_tab_panel.mapTo(panel._font_section, QPoint()).y()
        == panel._color_tab_panel.mapTo(panel._color_section, QPoint()).y()
    )
    font_header_top = panel._font_section.mapTo(panel._font_color_row, QPoint()).y()
    color_header_top = panel._color_section.mapTo(panel._font_color_row, QPoint()).y()
    assert font_header_top == color_header_top
    # 注音排版是排版语义，卡片挪到了「布局」页；字体列只留字体本体
    assert panel._ruby_section.header.text() == "注音"
    assert panel._pages[1].isAncestorOf(panel._ruby_section)
    assert not panel._font_section.isAncestorOf(panel._ruby_section)
    subgroup_titles = [
        label.text()
        for label in panel._font_section.findChildren(
            QLabel, "SubtitlePropertySubheading"
        )
    ]
    assert subgroup_titles == ["字体"]


def test_property_panel_font_and_color_sections_stack_in_narrow_viewport(qapp):
    panel = PropertyPanel()
    panel.resize(520, 800)
    panel.show()
    qapp.processEvents()

    row = panel._font_color_row
    assert row.is_stacked()
    assert panel._color_section.geometry().bottom() < panel._font_section.geometry().top()
    assert panel._color_section.geometry().right() <= row.rect().right()
    assert panel._font_section.geometry().right() <= row.rect().right()

    subtitle_page = panel.widget(0)
    assert subtitle_page.widget().width() <= subtitle_page.viewport().width()


def test_role_navigation_prioritizes_combo_width_at_minimum_panel_width(qapp):
    panel = PropertyPanel()
    panel.resize(320, 800)
    panel.show()
    qapp.processEvents()

    assert panel.currentIndex() == 0
    assert panel._singer_combo.width() == 120
    assert panel._role_navigation.width() < panel._font_color_section.width()


def test_subtitle_preview_frame_keeps_child_at_16_9(qapp):
    child = QWidget()
    frame = mw._AspectRatioBox(child)

    frame.show()
    frame.resize(1000, 700)
    qapp.processEvents()

    geometry = child.geometry()
    assert geometry.width() == 1000
    assert geometry.height() == pytest.approx(562, abs=1)
    assert geometry.width() / geometry.height() == pytest.approx(16 / 9, rel=0.003)


def test_layout_schematic_tracks_output_resolution_and_aspect_ratio(qapp):
    schematic = _LayoutSchematic()

    assert (schematic._virtual_width, schematic._virtual_height) == (1920, 1080)

    # Same aspect ratio changes pixel mapping even though the widget shape is stable.
    schematic.set_output_size(3840, 2160)
    assert (schematic._virtual_width, schematic._virtual_height) == (3840, 2160)
    assert schematic.width() / schematic.height() == pytest.approx(16 / 9, rel=0.01)

    # A non-16:9 output also changes the visible curtain shape.
    schematic.set_output_size(1024, 768)
    assert (schematic._virtual_width, schematic._virtual_height) == (1024, 768)
    assert schematic.width() / schematic.height() == pytest.approx(4 / 3, rel=0.01)


def test_property_panel_forwards_output_size_to_layout_schematic(qapp):
    panel = PropertyPanel()

    panel.set_output_size(3840, 2160)

    assert panel._n3_template_target_height == 2160
    assert panel._layout_schematic._virtual_width == 3840
    assert panel._layout_schematic._virtual_height == 2160


def test_schematic_board_places_margin_controls_beside_and_below_screen(qapp):
    left = QWidget()
    top_left = QWidget()
    top_center = QWidget()
    bottom_left = QWidget()
    bottom_right = QWidget()
    center = QWidget()
    bottom = QWidget()
    right = QWidget()
    left.setFixedSize(100, 60)
    top_left.setFixedSize(100, 40)
    top_center.setFixedSize(120, 40)
    bottom_left.setFixedSize(120, 40)
    bottom_right.setFixedSize(120, 24)
    center.setFixedSize(320, 180)
    bottom.setFixedSize(180, 32)
    right.setFixedSize(100, 60)
    board = _SchematicBoard(
        left,
        center,
        bottom,
        right,
        top_left=top_left,
        top_center=top_center,
        bottom_left=bottom_left,
        bottom_right=bottom_right,
    )

    board.resize(700, 240)
    board.show()
    qapp.processEvents()

    assert left.geometry().right() < center.geometry().left()
    assert center.geometry().left() - left.geometry().right() <= 13
    assert top_left.geometry().right() < center.geometry().left()
    assert top_left.geometry().top() == center.geometry().top()
    assert top_center.geometry().bottom() < center.geometry().top()
    assert top_center.geometry().center().x() == pytest.approx(
        center.geometry().center().x(), abs=1
    )
    assert bottom_left.geometry().bottom() == bottom.geometry().bottom()
    assert bottom_right.geometry().center().y() == bottom.geometry().center().y()
    assert bottom_right.geometry().right() == board.contentsRect().right()
    assert bottom_left.geometry().right() < center.geometry().left()
    assert left.geometry().center().y() == pytest.approx(
        center.geometry().center().y(), abs=1
    )
    assert bottom.geometry().top() > center.geometry().bottom()
    assert bottom.geometry().center().x() == pytest.approx(
        center.geometry().center().x(), abs=1
    )


def test_layout_schematic_preserves_columns_until_controls_would_collide(qapp):
    panel = PropertyPanel()
    panel.setCurrentIndex(1)
    panel.resize(680, 800)
    panel.show()
    qapp.processEvents()

    assert panel._schematic_board._wide is True
    assert panel._layout_navigation.geometry().top() == (
        panel._layout_assignment_actions.geometry().top()
    )
    assert panel._smart_horizontal_field.mapTo(
        panel._schematic_board, QPoint()
    ).x() < panel._layout_schematic.geometry().left()
    assert panel._layout_schematic.geometry().right() < (
        panel._line_alignments_box.geometry().left()
    )


def test_layout_schematic_stacks_cleanly_after_collision_breakpoint(qapp):
    panel = PropertyPanel()
    panel.setCurrentIndex(1)
    panel.resize(660, 1000)
    panel.show()
    qapp.processEvents()

    board = panel._schematic_board
    assert board.width() < board._wide_width_hint()
    assert board._wide is False
    ordered = [
        panel._layout_navigation,
        panel._layout_assignment_actions,
        panel._line_position_field,
        panel._layout_schematic,
        panel._vertical_margin_field,
        panel._left_layout_controls,
        panel._character_layout_group,
        panel._line_alignments_box,
        panel._allow_biting_check,
    ]
    tops = [widget.mapTo(board, QPoint()).y() for widget in ordered]
    assert tops == sorted(tops)
    for widget in ordered:
        top_left = widget.mapTo(board, QPoint())
        assert top_left.x() >= 0
        assert top_left.x() + widget.width() <= board.width()


def test_vertical_margin_label_follows_line_anchor(qapp):
    panel = PropertyPanel()

    assert panel._line_position_seg.value() == "bottom"
    assert panel._vertical_margin_label.text() == "下余白"
    assert not panel._vertical_margin_field.isHidden()

    panel._line_position_seg.setValue("top")
    assert panel._vertical_margin_label.text() == "上余白"
    assert not panel._vertical_margin_field.isHidden()

    panel._line_position_seg.setValue("center")
    assert panel._vertical_margin_field.isHidden()


def test_property_panel_basic_page_has_no_screen_section(qapp):
    panel = PropertyPanel()
    basic_layout = panel.widget(1).widget().layout()
    assert basic_layout.itemAt(0).widget() is panel._layout_section
    assert panel._layout_section.objectName() == "SubtitlePropertyCard"
    pair = basic_layout.itemAt(1).widget()
    assert [
        pair.layout().itemAt(index).widget().header.text()
        for index in range(pair.layout().count())
    ] == ["注音", "垂直与方向"]
    assert not hasattr(panel, "_screen_preset_combo")
    # 视图是低频的整体变换，默认折叠
    viewport_section = basic_layout.itemAt(basic_layout.count() - 2).widget()
    assert viewport_section.header.text() == "视图"
    assert viewport_section.is_expanded() is False

    timing_layout = panel.widget(2).widget().layout()
    timing_titles = [
        timing_layout.itemAt(index).widget().header.text()
        for index in range(timing_layout.count() - 1)
    ]
    assert timing_titles == ["时间"]


def test_layout_navigation_is_merged_above_row_structure(qapp):
    panel = PropertyPanel()
    panel.setCurrentIndex(1)
    panel.resize(1000, 800)
    panel.show()
    qapp.processEvents()

    basic_layout = panel.widget(1).widget().layout()
    assert basic_layout.itemAt(0).widget() is panel._layout_section
    assert panel._layout_section.height() < 360
    assert panel._layout_section.isAncestorOf(panel._layout_navigation)
    assert panel._layout_navigation.geometry().right() < (
        panel._layout_assignment_actions.geometry().left()
    )
    assert panel._layout_navigation.geometry().top() == (
        panel._layout_assignment_actions.geometry().top()
    )
    assert panel._layout_navigation.geometry().right() < (
        panel._line_position_field.geometry().left()
    )
    assert panel._line_position_field.geometry().right() < (
        panel._layout_assignment_actions.geometry().left()
    )
    assert panel._layout_navigation.width() < panel._layout_section.width()
    assert not panel._layout_navigation.isAncestorOf(panel._smart_horizontal_field)
    smart_top_left = panel._smart_horizontal_field.mapTo(
        panel._schematic_board, QPoint()
    )
    assert smart_top_left.x() + panel._smart_horizontal_field.width() < (
        panel._layout_schematic.geometry().left()
    )
    assert smart_top_left.y() == panel._layout_schematic.geometry().top()
    horizontal_margin_top_left = panel._horizontal_margin_field.mapTo(
        panel._schematic_board, QPoint()
    )
    assert horizontal_margin_top_left.y() > (
        smart_top_left.y() + panel._smart_horizontal_field.height()
    )
    assert panel._line_position_field.geometry().bottom() < (
        panel._layout_schematic.geometry().top()
    )
    assert panel._line_position_field.geometry().center().x() == pytest.approx(
        panel._layout_schematic.geometry().center().x(), abs=1
    )
    character_top_left = panel._character_layout_group.mapTo(
        panel._schematic_board, QPoint()
    )
    assert character_top_left.y() > (
        horizontal_margin_top_left.y() + panel._horizontal_margin_field.height()
    )
    assert character_top_left.x() + panel._character_layout_group.width() < (
        panel._layout_schematic.geometry().left()
    )
    assert 105 <= panel._letter_spacing_spin.width() <= 120
    assert 105 <= panel._space_width_spin.width() <= 120
    horizontal_gap = panel._layout_schematic.geometry().left() - (
        horizontal_margin_top_left.x() + panel._horizontal_margin_field.width()
    )
    line_margin_top_left = panel._line_margin_spin.mapTo(
        panel._schematic_board, QPoint()
    )
    vertical_gap = line_margin_top_left.y() - (
        panel._layout_schematic.geometry().top()
        + panel._layout_schematic.height()
    )
    assert horizontal_gap == pytest.approx(vertical_gap, abs=1)
    assert panel._letter_spacing_spin.height() == panel._line_margin_spin.height()
    assert panel._space_width_spin.height() == panel._line_margin_spin.height()
    letter_top_left = panel._letter_spacing_spin.mapTo(
        panel._schematic_board, QPoint()
    )
    space_top_left = panel._space_width_spin.mapTo(
        panel._schematic_board, QPoint()
    )
    line_margin_bottom = line_margin_top_left.y() + panel._line_margin_spin.height()
    assert letter_top_left.y() + panel._letter_spacing_spin.height() == (
        line_margin_bottom
    )
    assert space_top_left.y() + panel._space_width_spin.height() == (
        line_margin_bottom
    )
    biting_top_left = panel._allow_biting_check.mapTo(
        panel._schematic_board, QPoint()
    )
    assert biting_top_left.y() + panel._allow_biting_check.height() / 2 == pytest.approx(
        line_margin_top_left.y() + panel._line_margin_spin.height() / 2,
        abs=1,
    )
    assert biting_top_left.x() + panel._allow_biting_check.width() - 1 == (
        panel._schematic_board.contentsRect().right()
    )


def test_property_panel_uses_horizontal_text_tabs_in_expected_order(qapp):
    panel = PropertyPanel()

    assert isinstance(panel._navigation, SegmentedWidget)
    assert panel.count() == 5
    assert [spec[1] for spec in panel._PAGE_SPECS] == ["角色", "布局", "时间", "特效", "标题"]
    for route_key, label in panel._PAGE_SPECS:
        item = panel._navigation.widget(route_key)
        assert item.text() == label
        assert item.accessibleName() == label

    panel.setCurrentIndex(3)
    assert panel.currentIndex() == 3
    assert panel._navigation.currentRouteKey() == "effects"

    for expected_index, (route_key, _label) in enumerate(panel._PAGE_SPECS):
        panel._navigation.widget(route_key).click()
        qapp.processEvents()
        assert panel.currentIndex() == expected_index
        assert panel._navigation.currentRouteKey() == route_key


def test_property_panel_font_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._font_size_spin.setValue(88)
    panel._font_latin_size_spin.setValue(76)
    panel._font_latin_weight_combo.setCurrentIndex(
        panel._font_latin_weight_combo.findData(600)
    )
    panel._letter_spacing_spin.setValue(7)
    panel._space_width_spin.setValue(30)
    panel._font_weight_combo.setCurrentIndex(panel._font_weight_combo.findData(500))
    panel._italic_check.setChecked(True)
    panel._ruby_anchor_check.setChecked(False)
    panel._allow_biting_check.setChecked(True)

    assert emitted[-1].font_size_px == 88
    assert emitted[-1].latin_font_size_px == 76
    assert emitted[-1].latin_font_weight == 600
    assert emitted[-1].letter_spacing_px == 7
    assert emitted[-1].space_width_percent == 30
    assert emitted[-1].font_weight == 500
    assert emitted[-1].italic is True
    assert emitted[-1].affects_ruby_anchor is False
    assert emitted[-1].allow_biting is True


def test_font_tabs_own_four_independent_stroke_groups(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            stroke_width_px=12,
            stroke2_enabled=True,
            stroke2_width_px=5,
            latin_stroke_width_px=9,
            latin_stroke2_enabled=False,
            latin_stroke2_width_px=4,
            ruby_stroke_width_px=6,
            ruby_stroke2_enabled=True,
            ruby_stroke2_width_px=3,
            ruby_latin_stroke_width_px=2,
            ruby_latin_stroke2_enabled=True,
            ruby_latin_stroke2_width_px=1,
        )
    )

    assert panel._stroke_width_spin.value() == 12
    assert panel._latin_stroke_width_spin.value() == 9
    assert panel._ruby_stroke_width_spin.value() == 6
    assert panel._ruby_latin_stroke_width_spin.value() == 2
    assert panel._latin_stroke2_enabled_check.isChecked() is False
    assert panel._latin_stroke2_width_spin.value() == 4
    assert panel._latin_stroke2_width_spin.isEnabled() is False
    for prefix in ("", "latin_", "ruby_", "ruby_latin_"):
        enabled_check = getattr(panel, f"_{prefix}stroke2_enabled_check")
        enabled_field = getattr(panel, f"_{prefix}stroke2_enabled_field")
        width_field = getattr(panel, f"_{prefix}stroke2_width_field")
        assert enabled_check.text() == ""
        assert enabled_field is width_field
    for field in (
        panel._stroke_width_field,
        panel._latin_stroke_width_field,
        panel._ruby_stroke_width_field,
        panel._ruby_latin_stroke_width_field,
    ):
        assert panel._font_section.isAncestorOf(field)
        assert not panel._color_section.isAncestorOf(field)

    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._latin_stroke2_enabled_check.setChecked(True)
    panel._ruby_latin_stroke_width_spin.setValue(7)

    assert emitted[-1].latin_stroke2_enabled is True
    assert emitted[-1].latin_stroke2_width_px == 4
    assert emitted[-1].ruby_latin_stroke_width_px == 7


def test_stroke2_checkbox_sits_tightly_beside_width_input(qapp):
    panel = PropertyPanel()
    panel.resize(900, 800)
    panel.show()
    qapp.processEvents()

    checkbox = panel._stroke2_enabled_check
    width_spin = panel._stroke2_width_spin
    assert checkbox.width() <= 30
    assert checkbox.parentWidget() is width_spin.parentWidget()
    gap = width_spin.geometry().left() - checkbox.geometry().right() - 1
    assert 0 <= gap <= 2


def test_font_scripts_are_tabs_and_spacing_lives_on_layout_page(qapp):
    panel = PropertyPanel()

    assert not hasattr(panel, "_font_latin_check")
    assert panel._font_tab_stack.count() == 4
    assert panel._font_tab_panel._buttons[("left", "japanese")].text() == "日文"
    assert panel._font_tab_panel._buttons[("left", "latin")].text() == "英数"
    assert panel._font_tab_panel._buttons[("right", "main")].text() == "主文字"
    assert panel._font_tab_panel._buttons[("right", "ruby")].text() == "注音"
    assert panel._font_tab_stack.currentIndex() == 0

    panel._font_tab_panel._buttons[("left", "latin")].click()
    qapp.processEvents()
    assert panel._font_tab_stack.currentIndex() == 1
    assert panel._font_latin_combo.isEnabled()

    panel._font_tab_panel._buttons[("right", "ruby")].click()
    qapp.processEvents()
    assert panel._font_tab_stack.currentIndex() == 3
    assert panel._ruby_font_latin_combo.isEnabled()

    subgroup_titles = [
        label.text()
        for label in panel._character_layout_section.findChildren(
            QLabel, "SubtitlePropertySubheading"
        )
    ]
    assert subgroup_titles == ["字符排版"]
    assert panel._layout_section.isAncestorOf(panel._character_layout_section)
    assert not panel._layout_section.isAncestorOf(panel._ruby_font_size_spin)
    assert not panel._font_tab_panel.isAncestorOf(panel._letter_spacing_spin)
    assert not panel._font_tab_panel.isAncestorOf(panel._space_width_spin)
    assert not panel._character_layout_section.isAncestorOf(panel._allow_biting_check)
    assert panel._schematic_board.isAncestorOf(panel._allow_biting_check)

    panel.set_style(
        Style(font_family="Yu Gothic UI", font_size_px=70, font_weight=800)
    )
    assert panel._font_latin_combo.is_inherited()
    assert panel._font_latin_size_spin.value() == 0
    assert panel._font_latin_weight_combo.currentData() == 0


def test_latin_font_overrides_round_trip_and_legacy_values_inherit():
    style = Style(
        font_family="Yu Gothic UI",
        font_family_latin="Arial",
        font_size_px=72,
        font_weight=700,
        latin_font_size_px=60,
        latin_font_weight=500,
        latin_stroke_width_px=8,
        latin_stroke2_enabled=False,
        latin_stroke2_width_px=3,
        ruby_latin_stroke_width_px=4,
        ruby_latin_stroke2_enabled=True,
        ruby_latin_stroke2_width_px=2,
    )

    restored = style_from_dict(style_to_dict(style))
    assert restored.font_family_latin == "Arial"
    assert restored.latin_font_size_px == 60
    assert restored.latin_font_weight == 500
    assert restored.latin_stroke_width_px == 8
    assert restored.latin_stroke2_enabled is False
    assert restored.latin_stroke2_width_px == 3
    assert restored.ruby_latin_stroke_width_px == 4
    assert restored.ruby_latin_stroke2_enabled is True
    assert restored.ruby_latin_stroke2_width_px == 2

    legacy = style_from_dict(
        {"font_family": "Yu Gothic UI", "font_size_px": 72, "font_weight": 700}
    )
    assert legacy.font_family_latin is None
    assert legacy.latin_font_size_px is None
    assert legacy.latin_font_weight is None
    assert legacy.latin_stroke_width_px is None
    assert legacy.latin_stroke2_enabled is None
    assert legacy.latin_stroke2_width_px is None

    zero_slots = style_from_dict(
        {
            "font_family_latin": 0,
            "latin_font_size_px": 0,
            "latin_font_weight": 0,
            "latin_stroke_width_px": 0,
            "latin_stroke2_width_px": 0,
            "ruby_font_family": 0,
            "ruby_font_weight": 0,
            "ruby_font_family_latin": 0,
            "ruby_latin_font_size_px": 0,
            "ruby_latin_font_weight": 0,
            "ruby_latin_stroke_width_px": 0,
            "ruby_latin_stroke2_width_px": 0,
        }
    )
    assert zero_slots.font_family_latin is None
    assert zero_slots.latin_font_size_px is None
    assert zero_slots.latin_font_weight is None
    assert zero_slots.latin_stroke_width_px is None
    assert zero_slots.latin_stroke2_width_px is None
    assert zero_slots.ruby_font_family is None
    assert zero_slots.ruby_font_weight is None
    assert zero_slots.ruby_font_family_latin is None
    assert zero_slots.ruby_latin_font_size_px is None
    assert zero_slots.ruby_latin_font_weight is None
    assert zero_slots.ruby_latin_stroke_width_px is None
    assert zero_slots.ruby_latin_stroke2_width_px is None


def test_ruby_font_defaults_show_zero_inheritance_but_keep_own_size(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            font_family="Arial",
            font_family_latin="Courier New",
            font_size_px=72,
            latin_font_size_px=64,
            font_weight=700,
            latin_font_weight=600,
        )
    )

    assert panel._ruby_font_combo.is_inherited()
    assert panel._ruby_font_size_spin.value() == 45
    assert panel._ruby_font_weight_combo.currentData() == 0
    assert panel._ruby_font_latin_combo.is_inherited()
    assert panel._ruby_font_latin_size_spin.value() == 0
    assert panel._ruby_font_latin_weight_combo.currentData() == 0

    panel._font_size_spin.setValue(80)
    assert panel.subtitle_style.ruby_font_follow_main is True
    assert panel._ruby_font_size_spin.value() == 45

    panel._ruby_font_size_spin.setValue(34)
    assert panel.subtitle_style.ruby_font_follow_main is False
    panel._font_size_spin.setValue(90)
    assert panel._ruby_font_size_spin.value() == 34


def test_default_latin_controls_keep_zero_until_user_adds_override(qapp):
    panel = PropertyPanel()
    panel.set_style(Style(font_family="Arial", font_weight=700, stroke_width_px=12))

    assert panel._font_latin_combo.is_inherited()
    assert panel._font_latin_size_spin.value() == 0
    assert panel._font_latin_weight_combo.currentData() == 0
    assert panel._latin_stroke_width_spin.value() == 0
    assert (
        panel._latin_stroke2_enabled_check.checkState()
        == Qt.CheckState.PartiallyChecked
    )
    assert panel._latin_stroke2_width_spin.value() == 0
    assert panel._ruby_font_combo.is_inherited()
    assert panel._ruby_font_weight_combo.currentData() == 0
    assert panel._ruby_font_latin_combo.is_inherited()
    assert panel._ruby_font_latin_size_spin.value() == 0
    assert panel._ruby_font_latin_weight_combo.currentData() == 0
    assert panel._ruby_latin_stroke_width_spin.value() == 0
    assert (
        panel._ruby_latin_stroke2_enabled_check.checkState()
        == Qt.CheckState.PartiallyChecked
    )
    assert panel._ruby_latin_stroke2_width_spin.value() == 0

    panel._font_latin_size_spin.setValue(64)
    panel._font_latin_weight_combo.setCurrentIndex(
        panel._font_latin_weight_combo.findData(600)
    )
    panel._latin_stroke_width_spin.setValue(8)
    assert panel.subtitle_style.latin_font_size_px == 64
    assert panel.subtitle_style.latin_font_weight == 600
    assert panel.subtitle_style.latin_stroke_width_px == 8

    panel._font_latin_size_spin.setValue(0)
    panel._font_latin_weight_combo.setCurrentIndex(
        panel._font_latin_weight_combo.findData(0)
    )
    panel._latin_stroke_width_spin.setValue(0)
    assert panel.subtitle_style.latin_font_size_px is None
    assert panel.subtitle_style.latin_font_weight is None
    assert panel.subtitle_style.latin_stroke_width_px is None

    panel._latin_stroke2_enabled_check.setCheckState(Qt.CheckState.Checked)
    assert panel.subtitle_style.latin_stroke2_enabled is True
    panel._latin_stroke2_enabled_check.setCheckState(Qt.CheckState.PartiallyChecked)
    assert panel.subtitle_style.latin_stroke2_enabled is None


def test_new_project_global_font_sizes_are_main_100_and_ruby_45(qapp):
    panel = PropertyPanel()

    panel.set_style(Style())

    assert panel.subtitle_style.font_size_px == 100
    assert panel.subtitle_style.ruby_font_size_px == 45
    assert panel._font_size_spin.value() == 100
    assert panel._ruby_font_size_spin.value() == 45


def test_layout_ruby_and_direction_controls_are_single_row(qapp):
    panel = PropertyPanel()
    panel.setCurrentIndex(1)
    panel.resize(1000, 800)
    panel.show()
    qapp.processEvents()

    assert not panel._ruby_section.isAncestorOf(panel._ruby_font_size_spin)
    ruby_controls = [
        panel._ruby_gap_spin,
        panel._ruby_interval_spin,
        panel._ruby_alignment_combo,
    ]
    assert len({control.geometry().top() for control in ruby_controls}) == 1
    direction_controls = [
        panel._line_gap_spin,
        panel._vertical_check,
        panel._rtl_check,
    ]
    assert len({control.geometry().bottom() for control in direction_controls}) == 1
    assert not any(
        label.text() == "书写方向"
        for label in panel._layout_section.parentWidget().findChildren(QLabel)
    )


def test_property_panel_color_controls_emit_normalized_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._set_color("fill_color", "#123abc")
    panel._set_color("stroke_color", "not-a-color")

    assert emitted[-1].fill_color == "#123ABC"
    assert emitted[-1].stroke_color == "#222222"
    assert emitted[-1].karaoke_colors.after.text.color == "#123ABC"
    assert emitted[-1].karaoke_colors.after.stroke.color == "#222222"
    assert panel._paint_solid_btn.color == "#123ABC"


def test_property_panel_color_controls_preserve_alpha(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._set_color("fill_color", "#80123abc")

    assert emitted[-1].fill_color == "#80123ABC"
    assert emitted[-1].karaoke_colors.after.text.color == "#80123ABC"
    assert panel._paint_solid_btn.color == "#80123ABC"


def test_property_panel_swaps_complete_before_after_color_states(qapp):
    main_colors = KaraokeColors(
        before=KaraokeColorState(text=PaintFill(color="#112233")),
        after=KaraokeColorState(text=PaintFill(color="#AABBCC")),
    )
    ruby_colors = KaraokeColors(
        before=KaraokeColorState(text=PaintFill(color="#334455")),
        after=KaraokeColorState(text=PaintFill(color="#DDEEFF")),
    )
    panel = PropertyPanel()
    panel.set_style(
        Style(
            karaoke_colors=main_colors,
            ruby_colors_follow_main=False,
            ruby_karaoke_colors=ruby_colors,
        )
    )
    panel.resize(520, 900)
    panel.show()
    qapp.processEvents()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    tabs = panel._color_tab_panel
    button = panel._color_state_swap_button
    assert not button.icon().isNull()
    assert button.toolTip() == "交换走字前后配色"
    after_tab = tabs._buttons[("left", "after")]
    before_tab = tabs._buttons[("left", "before")]
    after_origin = after_tab.mapTo(panel._color_section, QPoint(0, 0))
    before_origin = before_tab.mapTo(panel._color_section, QPoint(0, 0))
    assert button.geometry().bottom() < after_origin.y()
    seam_x = (after_origin.x() + after_tab.width() - 1 + before_origin.x()) / 2
    assert button.geometry().center().x() == pytest.approx(seam_x, abs=1)

    panel.resize(900, 900)
    qapp.processEvents()
    after_origin = after_tab.mapTo(panel._color_section, QPoint(0, 0))
    before_origin = before_tab.mapTo(panel._color_section, QPoint(0, 0))
    seam_x = (after_origin.x() + after_tab.width() - 1 + before_origin.x()) / 2
    assert button.geometry().bottom() < after_origin.y()
    assert button.geometry().center().x() == pytest.approx(seam_x, abs=1)

    button.click()

    assert emitted[-1].karaoke_colors == KaraokeColors(
        before=main_colors.after,
        after=main_colors.before,
    )
    assert emitted[-1].ruby_karaoke_colors == ruby_colors
    assert panel._color_state_combo.currentData() == "after"
    assert panel._paint_solid_btn.color == "#112233"

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    button.click()

    assert emitted[-1].ruby_karaoke_colors == KaraokeColors(
        before=ruby_colors.after,
        after=ruby_colors.before,
    )
    assert emitted[-1].karaoke_colors == KaraokeColors(
        before=main_colors.after,
        after=main_colors.before,
    )
    panel.close()


def test_color_tabs_put_karaoke_state_left_and_subject_right(qapp):
    panel = PropertyPanel()
    panel.resize(520, 900)
    panel.show()
    qapp.processEvents()

    tabs = panel._color_tab_panel
    assert tabs._buttons[("left", "after")].text() == "走字后"
    assert tabs._buttons[("left", "before")].text() == "走字前"
    assert tabs._buttons[("right", "main")].text() == "主文字"
    assert tabs._buttons[("right", "ruby")].text() == "注音"
    assert tabs.current_left() == "after"
    assert tabs.current_right() == "main"

    tabs._buttons[("left", "before")].click()
    tabs._buttons[("right", "ruby")].click()
    qapp.processEvents()

    assert panel._color_state_combo.currentData() == "before"
    assert panel._color_subject_combo.currentData() == "ruby"
    panel.close()


def test_color_dialog_alpha_slider_is_visible_and_updates_color(qapp):
    dialog = _ColorDialog(QColor("#804093E9"))
    dialog.show()
    qapp.processEvents()

    slider = dialog._alpha_slider
    assert slider.isVisible()
    assert slider.alpha == 128
    assert slider.geometry().height() >= 100

    slider.alphaChanged.emit(64)
    assert dialog.currentColor().name(QColor.NameFormat.HexArgb).upper() == "#404093E9"

    dialog.setCurrentColor(QColor("#20ABCDEF"))
    assert slider.alpha == 32
    dialog.close()


def test_property_panel_fill_modes_use_compact_svg_icon_buttons(qapp):
    panel = PropertyPanel()
    expected_labels = {
        "solid": "全色",
        "gradient_horizontal": "横渐变",
        "gradient_vertical": "纵渐变",
        "split_vertical": "拼色",
        "image": "图像",
    }

    for key, label in expected_labels.items():
        button = panel._fill_mode_pill._buttons[key]
        assert button.text() == ""
        assert not button.icon().isNull()
        assert not button.icon().pixmap(button.iconSize()).isNull()
        assert button.toolTip() == label
        assert button.accessibleName() == label
        assert button.width() == button.height()
        assert button.iconSize().width() < button.width()

    panel._fill_mode_pill._buttons["gradient_vertical"].click()
    assert panel._fill_mode_combo.currentData() == "gradient_vertical"
    assert panel._fill_mode_pill.current() == "gradient_vertical"


def test_property_panel_gradient_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    panel._update_current_fill(start_color="#00AAEE")
    panel._update_current_fill(end_color="#FFCC00")

    fill = emitted[-1].karaoke_colors.after.text
    assert fill.mode == "gradient_vertical"
    assert fill.start_color == "#00AAEE"
    assert fill.end_color == "#FFCC00"
    assert panel._paint_gradient_start_btn.color == "#00AAEE"
    assert panel._paint_gradient_end_btn.color == "#FFCC00"


def test_horizontal_gradient_shared_ruby_progress_switch_defaults_on(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    assert not panel._ruby_horizontal_gradient_with_main_check.isHidden()
    assert panel._ruby_horizontal_gradient_with_main_check.isChecked()

    panel._ruby_horizontal_gradient_with_main_check.setChecked(False)
    assert emitted[-1].ruby_horizontal_gradient_with_main is False
    restored = style_from_dict(style_to_dict(emitted[-1]))
    assert restored.ruby_horizontal_gradient_with_main is False

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    assert panel._ruby_horizontal_gradient_with_main_check.isHidden()


def test_property_panel_gradient_stop_editor_emits_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    panel._gradient_editor.add_stop(50, "#808080")
    panel._gradient_stop_position_spin.setValue(60.125)
    panel._gradient_editor.set_selected_color("#336699")

    fill = emitted[-1].karaoke_colors.after.text
    assert fill.mode == "gradient_horizontal"
    assert (60.125, "#336699") in fill.gradient_stops
    assert fill.start_color == "#FF5A6F"
    assert fill.end_color == "#FF5A6F"


def test_gradient_stop_json_round_trip_preserves_positions_and_colors():
    stops = [
        (0, "#112233"),
        (40.125, "#804093E9"),
        (40.125, "#AABBCC"),
        (100, "#DDEEFF"),
    ]

    text = pp._gradient_stops_to_json(stops)
    payload = json.loads(text)

    assert payload["format"] == "karaoke-studio/gradient-stops"
    assert payload["version"] == 1
    assert "mode" not in payload
    assert pp._gradient_stops_from_json(text) == stops


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"format": "other", "version": 1, "stops": []}, "无法识别"),
        (
            {
                "format": "karaoke-studio/gradient-stops",
                "version": 2,
                "stops": [],
            },
            "不支持",
        ),
        (
            {
                "format": "karaoke-studio/gradient-stops",
                "version": 1,
                "stops": [
                    {"position": 0, "color": "#FFFFFF"},
                    {"position": 101, "color": "#000000"},
                ],
            },
            "0 到 100",
        ),
        (
            {
                "format": "karaoke-studio/gradient-stops",
                "version": 1,
                "stops": [
                    {"position": 0, "color": "#FFFFFF"},
                    {"position": 100, "color": "not-a-color"},
                ],
            },
            "色号无效",
        ),
    ],
)
def test_gradient_stop_json_rejects_invalid_payload(payload, message):
    with pytest.raises(ValueError, match=message):
        pp._gradient_stops_from_json(json.dumps(payload))


def test_gradient_stop_copy_and_paste_applies_to_current_layer(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    source_stops = [
        (0, "#112233"),
        (37.5, "#804093E9"),
        (100, "#DDEEFF"),
    ]
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    panel._gradient_editor.set_stops(source_stops)
    copied = panel._gradient_editor.copy_gradient_info()

    assert QApplication.clipboard().text() == copied

    panel._color_layer_combo.setCurrentIndex(
        panel._color_layer_combo.findData("stroke2")
    )
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    monkeypatch.setattr(
        pp._GradientStopsPasteDialog,
        "exec",
        lambda _self: QDialog.DialogCode.Accepted,
    )

    assert panel._gradient_editor.paste_gradient_info()

    fill = emitted[-1].karaoke_colors.after.stroke2
    assert fill.mode == "gradient_horizontal"
    assert fill.gradient_stops == source_stops
    assert fill.start_color == "#112233"
    assert fill.end_color == "#DDEEFF"


def test_gradient_bar_context_menu_exposes_copy_and_paste(qapp, monkeypatch):
    editor = pp.GradientStopsEditor()
    action_texts: list[str] = []

    def fake_exec(menu, _pos):
        action_texts.extend(action.text() for action in menu.actions())

    monkeypatch.setattr(pp.RoundMenu, "exec", fake_exec)

    class FakeContextMenuEvent:
        accepted = False

        @staticmethod
        def globalPos():  # noqa: N802 - Qt API shape
            return QPoint(10, 10)

        def accept(self):
            self.accepted = True

    event = FakeContextMenuEvent()
    editor.contextMenuEvent(event)

    assert action_texts == ["复制渐变信息", "粘贴渐变信息…"]
    assert event.accepted


def test_property_panel_gradient_bar_click_adds_stop(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )

    editor = panel._gradient_editor
    editor.resize(240, editor.sizeHint().height())
    point = editor._bar_rect().center()  # noqa: SLF001
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    editor.mousePressEvent(event)

    fill = emitted[-1].karaoke_colors.after.text
    assert any(position == 50 for position, _color in fill.gradient_stops)


def test_property_panel_gradient_endpoint_stops_cannot_be_deleted(qapp):
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    editor = panel._gradient_editor

    editor._selected = 0  # noqa: SLF001
    editor.delete_selected_stop()
    editor._selected = len(editor._stops) - 1  # noqa: SLF001
    editor.delete_selected_stop()

    assert editor._stops[0][0] == 0  # noqa: SLF001
    assert editor._stops[-1][0] == 100  # noqa: SLF001


def _render_vertical_gradient_stop_editor(qapp):
    editor = pp.GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.resize(editor.sizeHint())
    editor.set_stops(
        [
            (0, "#F8E599"),
            (25, "#F8E599"),
            (50, "#F8E599"),
            (100, "#EDBD46"),
        ]
    )
    editor._selected = 2  # noqa: SLF001
    editor.show()
    editor.update()
    qapp.processEvents()
    return editor, editor.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)


def test_gradient_stop_markers_are_external_pointers(qapp):
    editor, image = _render_vertical_gradient_stop_editor(qapp)
    del image
    bar = editor._bar_rect()  # noqa: SLF001
    tip = editor._marker_tip(25)  # noqa: SLF001
    marker = editor._marker_polygon(25, selected=False)  # noqa: SLF001

    assert tip.x() > bar.right()
    assert tip.y() == pytest.approx(bar.top() + bar.height() * 0.25)
    assert all(point.x() > bar.right() for point in marker)


def test_selected_gradient_stop_pointer_keeps_size_and_uses_blue_fill(qapp):
    editor, image = _render_vertical_gradient_stop_editor(qapp)
    regular = editor._marker_polygon(50, selected=False)  # noqa: SLF001
    selected = editor._marker_polygon(50, selected=True)  # noqa: SLF001
    center = editor._marker_center(50)  # noqa: SLF001

    fill = image.pixelColor(round(center.x()), round(center.y()))

    assert selected.boundingRect() == regular.boundingRect()
    assert fill == QColor(editor._POINTER_BLUE)  # noqa: SLF001


def test_gradient_stop_pointer_uses_compact_geometry(qapp):
    editor, image = _render_vertical_gradient_stop_editor(qapp)
    del image
    marker = editor._marker_polygon(25, selected=False)  # noqa: SLF001

    assert marker.boundingRect().width() == pytest.approx(24)
    assert marker.boundingRect().height() == pytest.approx(10)


def test_unselected_gradient_stop_pointer_uses_neutral_fill(qapp):
    editor, image = _render_vertical_gradient_stop_editor(qapp)
    center = editor._marker_center(25)  # noqa: SLF001
    fill = image.pixelColor(round(center.x()), round(center.y()))

    assert fill == QColor(pp.palette().input_bg)


def test_gradient_editor_does_not_draw_a_secondary_rail(qapp):
    editor, image = _render_vertical_gradient_stop_editor(qapp)
    old_rail_point = QPoint(round(editor._bar_rect().right() + 19), 95)  # noqa: SLF001
    background_point = QPoint(editor.width() - 2, 95)

    assert image.pixelColor(old_rail_point) == image.pixelColor(background_point)


def test_gradient_and_split_bars_share_pointer_geometry(qapp):
    editor = pp.GradientStopsEditor()
    editor.resize(editor.sizeHint())
    editor.set_orientation("gradient_vertical")
    gradient_pointer = list(editor._marker_polygon(50, selected=True))  # noqa: SLF001
    editor.set_orientation("split_vertical")
    split_pointer = list(editor._marker_polygon(50, selected=True))  # noqa: SLF001

    assert split_pointer == gradient_pointer


def test_horizontal_gradient_pointer_sits_below_bar_and_points_up(qapp):
    editor = pp.GradientStopsEditor()
    editor.resize(editor.sizeHint())
    bar = editor._bar_rect()  # noqa: SLF001
    tip = editor._marker_tip(50)  # noqa: SLF001
    marker = editor._marker_polygon(50, selected=False)  # noqa: SLF001

    assert tip.y() > bar.bottom()
    assert tip.x() == pytest.approx(bar.center().x())
    assert all(point.y() > bar.bottom() for point in marker)


def test_gradient_pointer_tip_selects_existing_stop(qapp):
    editor = pp.GradientStopsEditor()
    editor.resize(editor.sizeHint())
    editor.set_stops([(0, "#FFFFFF"), (50, "#808080"), (100, "#000000")])
    editor._selected = 0  # noqa: SLF001

    editor.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            editor._marker_tip(50),  # noqa: SLF001
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert editor.selected_stop[0] == 50
    assert len(editor._stops) == 3  # noqa: SLF001


def test_selected_gradient_endpoint_outline_is_not_clipped(qapp):
    editor = pp.GradientStopsEditor()
    editor.set_orientation("gradient_vertical")
    editor.resize(editor.sizeHint())
    editor.show()
    editor.update()
    qapp.processEvents()
    image = editor.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)

    blue_rows = [
        y
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) == QColor(editor._POINTER_BLUE)  # noqa: SLF001
    ]

    assert blue_rows
    assert min(blue_rows) > 0


def test_property_panel_dragging_endpoint_creates_mergeable_stop(qapp):
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    editor = panel._gradient_editor
    editor.resize(240, editor.sizeHint().height())

    start = editor._marker_center(0)  # noqa: SLF001
    middle = editor._bar_rect().center()  # noqa: SLF001
    editor.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    editor.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            middle,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert [position for position, _color in editor._stops] == [0, 50, 100]  # noqa: SLF001

    editor.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            start,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert [position for position, _color in editor._stops] == [0, 100]  # noqa: SLF001


def test_property_panel_fill_editor_height_follows_current_page(qapp):
    panel = PropertyPanel()

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    gradient_height = panel._fill_editor_stack.sizeHint().height()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("solid"))
    solid_height = panel._fill_editor_stack.sizeHint().height()

    assert solid_height < gradient_height


def test_vertical_gradient_and_split_use_compact_matching_bar_layout(qapp):
    panel = PropertyPanel()
    panel.resize(900, 800)
    panel.show()
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    qapp.processEvents()
    layout = panel._gradient_editor_layout

    bar_position = layout.getItemPosition(layout.indexOf(panel._gradient_bar_field))
    color_position = layout.getItemPosition(layout.indexOf(panel._gradient_color_field))
    stop_position = layout.getItemPosition(layout.indexOf(panel._gradient_position_field))
    assert bar_position == (0, 0, 2, 1)
    assert color_position == (0, 1, 1, 1)
    assert stop_position == (1, 1, 1, 1)
    assert panel._gradient_editor.sizeHint().width() == panel._split_editor.sizeHint().width()
    assert panel._gradient_editor._bar_rect().width() == panel._split_editor._bar_rect().width()

    def label_control_gap(field):
        field_layout = field.layout()
        label = field_layout.itemAt(0).widget()
        control = field_layout.itemAt(1).widget()
        return control.geometry().top() - label.geometry().bottom() - 1

    assert label_control_gap(panel._gradient_color_field) == label_control_gap(
        panel._stroke_width_field
    )

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    bar_position = layout.getItemPosition(layout.indexOf(panel._gradient_bar_field))
    assert bar_position == (0, 0, 1, 2)


def test_gradient_and_split_editors_do_not_show_redundant_titles(qapp):
    panel = PropertyPanel()

    assert panel._gradient_bar_field is panel._gradient_editor
    assert panel._split_bar_field is panel._split_editor


def test_solid_fill_editor_does_not_show_color_title(qapp):
    panel = PropertyPanel()

    assert panel._paint_solid_btn.parent().objectName() != "SubtitlePropertyField"


def test_property_panel_split_and_image_fill_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert panel._paint_image_scale_spin.minimum() == 1
    assert panel._paint_image_scale_spin.maximum() == 1000

    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("split_vertical"))
    panel._update_split_stops(
        [(0, "#FFFFFF"), (30, "#FF0000"), (65, "#888888"), (100, "#888888")]
    )

    split = emitted[-1].karaoke_colors.after.text
    assert split.mode == "split_vertical"
    assert split.split_stops == [
        (0, "#FFFFFF"),
        (30, "#FF0000"),
        (65, "#888888"),
        (100, "#888888"),
    ]
    assert split.split_top_color == "#FFFFFF"
    assert split.split_bottom_color == "#888888"
    assert split.split_position_pct == 30

    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("image"))
    panel._paint_image_path_edit.setText(r"D:\cover.png")
    panel._paint_image_path_edit.editingFinished.emit()
    panel._paint_image_scale_spin.setValue(150)

    image = emitted[-1].karaoke_colors.after.text
    assert image.mode == "image"
    assert image.image_path == r"D:\cover.png"
    assert image.image_scale_pct == 150


def test_split_fill_editor_supports_multiple_hard_color_bands(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("split_vertical")
    )

    editor = panel._split_editor
    assert editor._orientation == "vertical"
    assert editor._hard_edges is True

    panel._update_split_stops(
        [(0, "#FFFFFF"), (30, "#FF0000"), (65, "#888888"), (100, "#888888")]
    )
    editor.add_stop(80, "#123456")

    assert emitted[-1].karaoke_colors.after.text.split_stops == [
        (0, "#FFFFFF"),
        (30, "#FF0000"),
        (65, "#888888"),
        (80, "#123456"),
        (100, "#888888"),
    ]


def test_spin_box_uses_direct_input_without_step_button_overlay(qapp):
    panel = PropertyPanel()
    spin = panel._font_size_spin
    spin.setParent(None)
    spin.setValue(spin.maximum())
    spin.resize(1, spin.height())
    spin.show()
    qapp.processEvents()

    text_width = spin.lineEdit().fontMetrics().horizontalAdvance(
        spin.lineEdit().text()
    )
    assert spin.lineEdit().width() >= text_width
    assert spin.upButton.isHidden()
    assert spin.downButton.isHidden()
    assert not hasattr(spin, "spinFlyout")
    spin.close()
    spin.deleteLater()


def test_style_serialization_preserves_complex_fills_and_schemes(tmp_path):
    image_path = str(tmp_path / "texture.png")
    fill = PaintFill(
        mode="image",
        color="#112233",
        start_color="#112233",
        end_color="#445566",
        gradient_stops=[(0, "#112233"), (40, "#778899"), (100, "#445566")],
        split_top_color="#112233",
        split_bottom_color="#445566",
        split_position_pct=35,
        split_stops=[
            (0, "#112233"),
            (35, "#778899"),
            (70, "#445566"),
            (100, "#445566"),
        ],
        image_path=image_path,
        image_scale_pct=175,
    )
    scheme = SubtitleStyleScheme(
        font_size_px=88,
        letter_spacing_px=5,
        space_width_percent=40,
        allow_biting=True,
        fill_color="#112233",
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=fill)),
    )
    style = Style(
        letter_spacing_px=3,
        space_width_percent=25,
        allow_biting=True,
        entry_anim="utopia",
        entry_lead_ms=500,
        exit_anim="char_fade",
        exit_fade_ms=700,
        line_protect_ms=450,
        lit_enabled=True,
        lit_style="square",
        lit_number=2,
        lit_size=40,
        lit_offset_x=80,
        lit_offset_y=70,
        lit_tracking=12,
        lit_fill_color="#222222",
        lit1_fill_color="#FF0000",
        lit2_fill_color="#00FF00",
        lit3_fill_color="#0000FF",
        lit_stroke_color="#FFFFFF",
        lit_stroke_width=4,
        lit_stroke_soften=2,
        lit_opacity_pct=75,
        lit_edge_brightness_pct=60,
        lit_shadow=True,
        lit_time_offset_ms=-250,
        lit_waiting_time_ms=100,
        lit_transition_mode="fade",
        lit_transition_ratio_pct=25,
        lit_transition_angle_deg=-30,
        lit_transition_distance=16,
        signals_duration_ms=1500,
        volume_size=54,
        volume_offset_x=8,
        volume_offset_y=-6,
        volume_column_width=9,
        volume_column_count=6,
        volume_column_spacing=2,
        volume_align=2,
        volume_ratio=5.0,
        volume_fill_color="#101112",
        volume_stroke_color="#131415",
        volume_overlay_fill_color="#161718",
        volume_overlay_stroke_color="#191A1B",
        volume_flash_times=4,
        volume_flash_duration_ratio=0.5,
        volume_transition_ratio_pct=44,
        singer_style_overrides={2: scheme},
        custom_style_schemes={"图像方案": scheme},
    )

    restored = style_from_dict(style_to_dict(style))

    assert restored.letter_spacing_px == 3
    assert restored.space_width_percent == 25
    assert restored.allow_biting is True
    assert restored.singer_style_overrides[2].letter_spacing_px == 5
    assert restored.singer_style_overrides[2].space_width_percent == 40
    assert restored.singer_style_overrides[2].allow_biting is True
    assert restored.entry_anim == "utopia"
    assert restored.exit_anim == "char_fade"
    assert restored.line_protect_ms == 450
    assert restored.lit_enabled is True
    assert restored.lit_style == "square"
    assert restored.lit_number == 2
    assert restored.lit_size == 40
    assert restored.lit_offset_x == 80
    assert restored.lit_offset_y == 70
    assert restored.lit_tracking == 12
    assert restored.lit_fill_color == "#222222"
    assert restored.lit1_fill_color == "#FF0000"
    assert restored.lit2_fill_color == "#00FF00"
    assert restored.lit3_fill_color == "#0000FF"
    assert restored.lit_stroke_color == "#FFFFFF"
    assert restored.lit_stroke_width == 4
    assert restored.lit_stroke_soften == 2
    assert restored.lit_opacity_pct == 75
    assert restored.lit_edge_brightness_pct == 60
    assert restored.lit_shadow is True
    assert restored.lit_time_offset_ms == -250
    assert restored.lit_waiting_time_ms == 100
    assert restored.lit_transition_mode == "fade"
    assert restored.lit_transition_ratio_pct == 25
    assert restored.lit_transition_angle_deg == -30
    assert restored.lit_transition_distance == 16
    assert restored.signals_duration_ms == 1500
    assert restored.volume_size == 54
    assert restored.volume_offset_x == 8
    assert restored.volume_offset_y == -6
    assert restored.volume_column_width == 9
    assert restored.volume_column_count == 6
    assert restored.volume_column_spacing == 2
    assert restored.volume_align == 2
    assert restored.volume_ratio == 5.0
    assert restored.volume_fill_color == "#101112"
    assert restored.volume_stroke_color == "#131415"
    assert restored.volume_overlay_fill_color == "#161718"
    assert restored.volume_overlay_stroke_color == "#191A1B"
    assert restored.volume_flash_times == 4
    assert restored.volume_flash_duration_ratio == 0.5
    assert restored.volume_transition_ratio_pct == 44
    assert restored.singer_style_overrides[2].karaoke_colors.after.text.image_path == image_path
    assert restored.singer_style_overrides[2].karaoke_colors.after.text.image_scale_pct == 175
    assert restored.singer_style_overrides[2].karaoke_colors.after.text.split_stops == [
        (0, "#112233"),
        (35, "#778899"),
        (70, "#445566"),
        (100, "#445566"),
    ]
    assert restored.custom_style_schemes["图像方案"].karaoke_colors.after.text.mode == "image"


def test_legacy_two_color_split_fill_is_upgraded_to_hard_stops():
    fill = paint_fill_from_dict(
        {
            "mode": "split_vertical",
            "split_top_color": "#FFFFFF",
            "split_bottom_color": "#777777",
            "split_position_pct": 35,
        }
    )

    assert fill.split_stops == [
        (0, "#FFFFFF"),
        (35, "#777777"),
        (100, "#777777"),
    ]


def test_paint_fill_preserves_fractional_and_duplicate_gradient_stops():
    fill = paint_fill_from_dict(
        {
            "mode": "gradient_vertical",
            "gradient_stops": [
                (0, "#FFFFFF"),
                (33.3333, "#FF0000"),
                (33.3333, "#0000FF"),
                (100, "#000000"),
            ],
        }
    )

    assert fill.gradient_stops == [
        (0, "#FFFFFF"),
        (33.3333, "#FF0000"),
        (33.3333, "#0000FF"),
        (100, "#000000"),
    ]


def test_property_panel_decoration_controls_visibility_and_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert panel._decoration_type_field.isHidden()
    assert not panel._stroke_width_field.isHidden()
    assert not panel._stroke2_width_field.isHidden()
    assert panel._font_section.isAncestorOf(panel._stroke_width_field)
    assert panel._font_section.isAncestorOf(panel._stroke2_width_field)

    panel._color_layer_combo.setCurrentIndex(panel._color_layer_combo.findData("shadow"))
    assert not panel._decoration_type_field.isHidden()
    assert not panel._stroke_width_field.isHidden()
    assert not panel._stroke2_width_field.isHidden()
    assert not panel._shadow_x_field.isHidden()
    assert not panel._shadow_y_field.isHidden()

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("glow")
    )
    assert emitted[-1].decoration_kind == "glow"
    assert panel._shadow_x_field.isHidden()
    assert panel._shadow_y_field.isHidden()
    assert not panel._glow_radius_field.isHidden()
    assert not panel._glow_after_radius_field.isHidden()
    assert not panel._glow_controls_row.isHidden()
    glow_layout = panel._glow_controls_row.layout()
    assert [glow_layout.itemAt(index).widget() for index in range(3)] == [
        panel._glow_radius_field,
        panel._glow_after_radius_field,
        panel._glow_concentration_field,
    ]

    panel._glow_radius_spin.setValue(28)
    assert emitted[-1].glow_radius_px == 10
    assert emitted[-1].glow_before_radius_px == 28
    assert emitted[-1].glow_after_radius_px == 10

    panel._glow_after_radius_spin.setValue(16)
    assert emitted[-1].glow_before_radius_px == 28
    assert emitted[-1].glow_after_radius_px == 16

    panel._glow_radius_spin.setValue(0)
    assert emitted[-1].glow_before_radius_px == 0
    assert emitted[-1].glow_after_radius_px == 16

    panel._glow_after_radius_spin.setValue(0)
    assert emitted[-1].glow_before_radius_px == 0
    assert emitted[-1].glow_after_radius_px == 0

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("none")
    )
    assert emitted[-1].decoration_kind == "none"
    assert panel._shadow_x_field.isHidden()
    assert panel._shadow_y_field.isHidden()
    assert panel._glow_radius_field.isHidden()
    assert panel._glow_after_radius_field.isHidden()
    assert panel._glow_controls_row.isHidden()

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("shadow")
    )
    assert emitted[-1].decoration_kind == "shadow"
    assert not panel._shadow_x_field.isHidden()
    assert not panel._shadow_y_field.isHidden()
    assert panel._glow_radius_field.isHidden()
    assert panel._glow_after_radius_field.isHidden()
    assert panel._glow_controls_row.isHidden()


def test_property_panel_glow_concentration_is_shared_by_main_and_ruby(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            ruby_decoration_kind="shadow",
            ruby_glow_concentration_level=2,
        )
    )
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._color_layer_combo.setCurrentIndex(
        panel._color_layer_combo.findData("shadow")
    )
    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("glow")
    )
    assert emitted[-1].ruby_decoration_kind is None

    assert not panel._glow_concentration_field.isHidden()
    assert panel._glow_concentration_combo.currentData() == 0

    panel._glow_concentration_combo.setCurrentIndex(
        panel._glow_concentration_combo.findData(-1)
    )
    assert emitted[-1].glow_concentration_level == -1
    assert emitted[-1].glow_before_radius_px == 10
    assert emitted[-1].glow_after_radius_px == 10
    assert emitted[-1].ruby_glow_concentration_level is None

    panel._glow_concentration_combo.setCurrentIndex(
        panel._glow_concentration_combo.findData(2)
    )
    assert emitted[-1].glow_concentration_level == 2
    assert emitted[-1].ruby_glow_concentration_level is None

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    assert panel._glow_concentration_combo.currentData() == 2

    panel._glow_concentration_combo.setCurrentIndex(
        panel._glow_concentration_combo.findData(1)
    )
    assert emitted[-1].ruby_glow_concentration_level is None
    assert emitted[-1].glow_concentration_level == 1

    panel._apply_main_colors_to_ruby()
    assert emitted[-1].ruby_glow_concentration_level is None

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("shadow")
    )
    assert panel._glow_concentration_field.isHidden()


def test_property_panel_ruby_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._ruby_font_size_spin.setValue(34)
    panel._ruby_gap_spin.setValue(11)

    assert emitted[-1].ruby_font_size_px == 34
    assert emitted[-1].ruby_font_follow_main is False
    assert emitted[-1].ruby_gap_px == 11


def test_property_panel_ruby_section_has_no_color_controls(qapp):
    panel = PropertyPanel()

    assert not hasattr(panel, "_ruby_color_btn")
    assert not hasattr(panel, "_ruby_color_hint")


def test_property_panel_layout_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._line_margin_spin.setValue(123)
    panel._line_gap_spin.setValue(70)
    panel._horizontal_margin_spin.setValue(31)

    panel._rtl_check.setChecked(True)
    panel._vertical_check.setChecked(True)

    assert emitted[-1].dual_line_layout is True
    assert emitted[-1].right_to_left is True
    assert emitted[-1].vertical is True
    assert emitted[-1].line_horizontal_layout == "asymmetric"
    assert emitted[-1].line_y_margin_px == 123
    assert emitted[-1].line_gap_px == 70
    assert emitted[-1].horizontal_margin_px == 31
    # 旧字段镜像跟随（native 后端序列化兼容）
    assert emitted[-1].upper_line_left_margin_px == 31
    assert emitted[-1].lower_line_right_margin_px == 31


def test_property_panel_preserves_high_resolution_n3_layout_values(qapp):
    panel = PropertyPanel()
    style = Style(
        line_y_margin_px=1_010,
        line_gap_px=-620,
        horizontal_margin_px=1_234,
        letter_spacing_px=-240,
        ruby_interval_px=300,
        ruby_gap_px=-180,
    )

    panel.set_style(style)

    assert panel._line_margin_spin.value() == 1_010
    assert panel._line_gap_spin.value() == -620
    assert panel._horizontal_margin_spin.value() == 1_234
    assert panel._letter_spacing_spin.value() == -240
    assert panel._ruby_interval_spin.value() == 300
    assert panel._ruby_gap_spin.value() == -180


def test_property_panel_line_layout_directly_controls_rows(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert panel._current_layout_alignments() == ["left", "right"]
    panel._on_line_alignment_changed(0, "center")

    assert emitted[-1].line_alignments == ["center", "right"]
    assert emitted[-1].dual_line_layout is True
    assert emitted[-1].line_horizontal_layout == "asymmetric"

    panel._on_add_line_alignment()
    assert len(emitted[-1].line_alignments) == 3
    panel._on_remove_line_alignment(0)
    assert len(emitted[-1].line_alignments) == 2


def test_property_panel_legacy_layout_modes_project_into_line_list(qapp):
    """旧模式只用于导入投影；用户一旦编辑行列表就归一化为新逻辑。"""
    panel = PropertyPanel()
    panel.set_style(
        Style(
            dual_line_layout=False,
            line_horizontal_layout="per_row",
            row1_align="center",
        )
    )
    assert panel._current_layout_alignments() == ["center"]
    assert not panel._line_alignments_box.isHidden()

    panel._on_line_alignment_changed(0, "right")
    assert panel.subtitle_style.line_alignments == ["right"]
    assert panel.subtitle_style.dual_line_layout is True
    assert panel.subtitle_style.line_horizontal_layout == "asymmetric"


def test_property_panel_viewport_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._viewport_align_combo.setCurrentIndex(
        panel._viewport_align_combo.findData("top_left")
    )
    panel._viewport_x_spin.setValue(-80)
    panel._viewport_y_spin.setValue(45)
    panel._viewport_scale_spin.setValue(120)
    panel._viewport_rotation_spin.setValue(15)

    assert emitted[-1].viewport_align == "top_left"
    assert emitted[-1].viewport_offset_x == -80
    assert emitted[-1].viewport_offset_y == 45
    assert emitted[-1].viewport_scale_pct == 120
    assert emitted[-1].viewport_rotation_deg == 15


def test_property_panel_timing_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._line_lead_spin.setValue(1500)
    panel._line_tail_spin.setValue(1200)
    panel._line_offset_spin.setValue(-250)
    panel._section_gap_spin.setValue(6000)
    panel._section_ending_combo.setCurrentIndex(
        panel._section_ending_combo.findData("clear")
    )
    panel._sync_entry_check.setChecked(True)
    panel._sync_ending_check.setChecked(True)
    panel._ruby_main_reading_units_check.setChecked(True)

    assert emitted[-1].line_lead_in_ms == 1500
    assert emitted[-1].line_tail_ms == 1200
    assert emitted[-1].timing_offset_ms == -250
    assert emitted[-1].section_gap_ms == 6000
    assert emitted[-1].section_ending_mode == "clear"
    assert emitted[-1].sync_entry is True
    assert emitted[-1].sync_ending is True
    assert emitted[-1].ruby_main_progress_mode == "reading_units"


def test_ruby_main_progress_mode_round_trips_and_rejects_unknown_values():
    restored = style_from_dict(
        style_to_dict(Style(ruby_main_progress_mode="reading_units"))
    )

    assert restored.ruby_main_progress_mode == "reading_units"
    assert (
        style_from_dict({"ruby_main_progress_mode": "unknown"}).ruby_main_progress_mode
        == "checkpoint_segments"
    )
    assert (
        style_from_dict({"ruby_main_progress_mode": []}).ruby_main_progress_mode
        == "checkpoint_segments"
    )


def test_karaoke_animation_round_trips_and_rejects_unknown_values():
    assert (
        style_from_dict(style_to_dict(Style(karaoke_anim="utopia"))).karaoke_anim
        == "utopia"
    )
    assert style_from_dict({}).karaoke_anim == "inherit"
    assert style_from_dict({"karaoke_anim": "unknown"}).karaoke_anim == "inherit"


def test_property_panel_animation_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert [
        panel._karaoke_anim_combo.itemText(index)
        for index in range(panel._karaoke_anim_combo.count())
    ] == ["无", "utopia"]

    panel._entry_anim_combo.setCurrentIndex(
        panel._entry_anim_combo.findData("char_fade")
    )
    panel._entry_lead_spin.setValue(700)
    panel._exit_anim_combo.setCurrentIndex(panel._exit_anim_combo.findData("utopia"))
    panel._exit_fade_spin.setValue(900)
    panel._karaoke_anim_combo.setCurrentIndex(
        panel._karaoke_anim_combo.findData("utopia")
    )

    assert emitted[-1].entry_anim == "char_fade"
    assert emitted[-1].entry_lead_ms == 700
    assert emitted[-1].exit_anim == "utopia"
    assert emitted[-1].exit_fade_ms == 900
    assert effective_karaoke_animation(emitted[-1]) == "utopia"


def test_property_panel_shows_legacy_utopia_as_utopia_karaoke_effect(qapp):
    panel = PropertyPanel()

    panel._entry_anim_combo.setCurrentIndex(
        panel._entry_anim_combo.findData("utopia")
    )
    assert panel.subtitle_style.karaoke_anim == "inherit"
    assert panel._karaoke_anim_combo.currentData() == "utopia"

    panel.set_style(Style(entry_anim="utopia"), emit=False)
    assert panel.subtitle_style.karaoke_anim == "inherit"
    assert panel._karaoke_anim_combo.currentData() == "utopia"

    panel._karaoke_anim_combo.setCurrentIndex(
        panel._karaoke_anim_combo.findData("none")
    )
    assert panel.subtitle_style.karaoke_anim == "none"


def test_property_panel_exposes_n3_char_drip_animation(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._entry_anim_combo.setCurrentIndex(
        panel._entry_anim_combo.findData("char_drip")
    )
    panel._exit_anim_combo.setCurrentIndex(
        panel._exit_anim_combo.findData("char_drip")
    )

    assert emitted[-1].entry_anim == "char_drip"
    assert emitted[-1].exit_anim == "char_drip"


def test_property_panel_lit_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._lit_enabled_switch.setChecked(True)
    panel._lit_style_combo.setCurrentIndex(panel._lit_style_combo.findData("square"))
    panel._lit_number_spin.setValue(2)
    panel._lit_size_spin.setValue(44)
    panel._lit_x_spin.setValue(120)
    panel._lit_y_spin.setValue(64)
    panel._lit_tracking_spin.setValue(18)
    panel._lit_duration_spin.setValue(1700)
    panel._lit_stroke_width_spin.setValue(6)
    panel._lit_stroke_soften_spin.setValue(4)
    panel._lit_opacity_spin.setValue(70)
    panel._lit_edge_brightness_spin.setValue(50)
    panel._lit_shadow_check.setChecked(True)
    panel._lit_waiting_time_spin.setValue(250)
    panel._lit_transition_mode_combo.setCurrentIndex(
        panel._lit_transition_mode_combo.findData("fade")
    )
    panel._lit_transition_ratio_spin.setValue(20)
    panel._lit_transition_angle_spin.setValue(90)
    panel._lit_transition_distance_spin.setValue(30)
    panel._volume_size_spin.setValue(72)
    panel._volume_x_spin.setValue(24)
    panel._volume_y_spin.setValue(-12)
    panel._volume_column_width_spin.setValue(11)
    panel._volume_column_count_spin.setValue(5)
    panel._volume_column_spacing_spin.setValue(4)
    panel._volume_ratio_spin.setValue(6)
    panel._volume_align_combo.setCurrentIndex(panel._volume_align_combo.findData(2))
    panel._volume_flash_times_spin.setValue(5)
    panel._volume_flash_duration_spin.setValue(40)
    panel._volume_transition_ratio_spin.setValue(58)
    panel._set_color("lit_fill_color", "#111111")
    panel._set_color("lit_stroke_color", "#eeeeee")
    panel._set_color("volume_fill_color", "#112244")
    panel._set_color("volume_stroke_color", "#223355")
    panel._set_color("volume_overlay_fill_color", "#334466")
    panel._set_color("volume_overlay_stroke_color", "#445577")

    assert emitted[-1].lit_enabled is True
    assert emitted[-1].lit_style == "square"
    assert emitted[-1].lit_number == 2
    assert emitted[-1].lit_size == 44
    assert emitted[-1].lit_offset_x == 120
    assert emitted[-1].lit_offset_y == 64
    assert emitted[-1].lit_tracking == 18
    assert emitted[-1].signals_duration_ms == 1700
    assert emitted[-1].lit_stroke_width == 6
    assert emitted[-1].lit_stroke_soften == 4
    assert emitted[-1].lit_opacity_pct == 70
    assert emitted[-1].lit_edge_brightness_pct == 50
    assert emitted[-1].lit_shadow is True
    assert emitted[-1].lit_waiting_time_ms == 250
    assert emitted[-1].lit_transition_mode == "fade"
    assert emitted[-1].lit_transition_ratio_pct == 20
    assert emitted[-1].lit_transition_angle_deg == 90
    assert emitted[-1].lit_transition_distance == 30
    assert emitted[-1].lit_fill_color == "#111111"
    assert emitted[-1].lit_stroke_color == "#EEEEEE"
    assert emitted[-1].volume_size == 72
    assert emitted[-1].volume_offset_x == 24
    assert emitted[-1].volume_offset_y == -12
    assert emitted[-1].volume_column_width == 11
    assert emitted[-1].volume_column_count == 5
    assert emitted[-1].volume_column_spacing == 4
    assert emitted[-1].volume_align == 2
    assert emitted[-1].volume_ratio == 6.0
    assert emitted[-1].volume_flash_times == 5
    assert emitted[-1].volume_flash_duration_ratio == 0.4
    assert emitted[-1].volume_transition_ratio_pct == 58
    assert emitted[-1].volume_fill_color == "#112244"
    assert emitted[-1].volume_stroke_color == "#223355"
    assert emitted[-1].volume_overlay_fill_color == "#334466"
    assert emitted[-1].volume_overlay_stroke_color == "#445577"


def test_property_panel_role_scheme_controls_emit_style(qapp):
    panel = PropertyPanel()
    panel.set_roles(["A", "B"])  # 角色名来自字幕标签
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:B"))
    panel._font_size_spin.setValue(88)
    panel._font_latin_size_spin.setValue(74)
    panel._letter_spacing_spin.setValue(9)
    panel._set_color("fill_color", "#00aaee")
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    panel._update_current_fill(start_color="#00AAEE")
    panel._update_current_fill(end_color="#FFCC00")
    panel._set_color("base_color", "#112233")
    panel._ruby_gap_spin.setValue(8)

    # 编辑进角色 B（按名字存进 custom_style_schemes）
    scheme = emitted[-1].custom_style_schemes["B"]
    assert scheme.font_size_px == 88
    assert scheme.latin_font_size_px == 74
    assert emitted[-1].letter_spacing_px == 9
    assert scheme.letter_spacing_px != 9
    assert scheme.fill_color == "#00AAEE"
    assert scheme.karaoke_colors.after.text.mode == "gradient_horizontal"
    assert scheme.karaoke_colors.after.text.start_color == "#00AAEE"
    assert scheme.karaoke_colors.after.text.end_color == "#FFCC00"
    assert scheme.base_color == "#112233"
    assert scheme.karaoke_colors.before.text.color == "#112233"
    assert emitted[-1].ruby_gap_px == 8
    assert scheme.ruby_gap_px != 8
    assert panel._paint_gradient_start_btn.color == "#00AAEE"


def test_property_panel_role_ruby_color_matrix_edits_go_into_custom_scheme(qapp):
    panel = PropertyPanel()
    panel.set_roles(["B"])
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:B"))
    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    panel._color_state_combo.setCurrentIndex(panel._color_state_combo.findData("after"))
    panel._color_layer_combo.setCurrentIndex(panel._color_layer_combo.findData("text"))
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    panel._update_current_fill(start_color="#11AAFF")
    panel._update_current_fill(end_color="#FFAA11")

    scheme = emitted[-1].custom_style_schemes["B"]
    assert scheme.ruby_karaoke_colors is not None
    fill = scheme.ruby_karaoke_colors.after.text
    assert fill.mode == "gradient_horizontal"
    assert fill.start_color == "#11AAFF"
    assert fill.end_color == "#FFAA11"
    assert (
        scheme.karaoke_colors is None
        or scheme.karaoke_colors.after.text.start_color != "#11AAFF"
    )


def test_property_panel_apply_main_colors_button_only_shows_for_ruby_colors(qapp):
    panel = PropertyPanel()

    assert panel._color_subject_combo.currentData() == "main"
    assert panel._ruby_apply_main_btn.isHidden()
    assert panel._ruby_colors_follow_main_check.isHidden()

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )

    assert not panel._ruby_apply_main_btn.isHidden()
    assert not panel._ruby_colors_follow_main_check.isHidden()
    assert panel._ruby_colors_follow_main_check.isChecked()
    assert not panel._ruby_apply_main_btn.isEnabled()


def test_ruby_colors_follow_every_main_color_layer_and_fill_setting(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    for index, layer in enumerate(("text", "stroke", "stroke2", "shadow")):
        panel._color_layer_combo.setCurrentIndex(
            panel._color_layer_combo.findData(layer)
        )
        panel._update_current_fill(
            mode="gradient_horizontal",
            color=f"#FF{index + 1:02X}2233",
            start_color=f"#FF{index + 1:02X}4455",
            end_color=f"#FF{index + 1:02X}6677",
            gradient_stops=[
                (0, f"#FF{index + 1:02X}4455"),
                (100, f"#FF{index + 1:02X}6677"),
            ],
        )

        assert emitted[-1].ruby_karaoke_colors is None
        assert emitted[-1].ruby_colors_follow_main is True
        assert panel._current_ruby_karaoke_colors() == emitted[-1].karaoke_colors


def test_ruby_color_follow_toggle_detaches_and_restores_live_inheritance(qapp):
    panel = PropertyPanel()
    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._ruby_colors_follow_main_check.setChecked(False)
    detached = emitted[-1].ruby_karaoke_colors
    assert detached is not None
    assert emitted[-1].ruby_colors_follow_main is False
    assert detached == panel._current_karaoke_colors()
    assert panel._ruby_apply_main_btn.isEnabled()

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("main")
    )
    panel._update_current_fill(color="#FF123456")
    assert emitted[-1].ruby_karaoke_colors == detached
    assert emitted[-1].ruby_karaoke_colors != emitted[-1].karaoke_colors

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    panel._ruby_colors_follow_main_check.setChecked(True)
    assert emitted[-1].ruby_karaoke_colors is None
    assert emitted[-1].ruby_colors_follow_main is True
    assert panel._current_ruby_karaoke_colors() == emitted[-1].karaoke_colors
    assert not panel._ruby_apply_main_btn.isEnabled()


def test_ruby_color_follow_state_is_saved_per_role_scheme(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            custom_style_schemes={
                "主唱": SubtitleStyleScheme(
                    ruby_colors_follow_main=False,
                    ruby_karaoke_colors=KaraokeColors(),
                )
            }
        )
    )
    panel.set_roles(["主唱"])
    panel.set_current_scheme_key("custom:主唱")
    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("ruby")
    )
    assert not panel._ruby_colors_follow_main_check.isChecked()

    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._ruby_colors_follow_main_check.setChecked(True)

    scheme = emitted[-1].custom_style_schemes["主唱"]
    assert scheme.ruby_colors_follow_main is True
    assert scheme.ruby_karaoke_colors is None
    restored = style_from_dict(style_to_dict(emitted[-1]))
    assert restored.custom_style_schemes["主唱"].ruby_colors_follow_main is True


def test_property_panel_ruby_font_tab_controls_emit_ruby_strokes(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            stroke_width_px=10,
            stroke2_width_px=4,
            decoration_kind="shadow",
            shadow_offset_y=3,
            ruby_stroke_width_px=None,
            ruby_stroke2_width_px=None,
        )
    )
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert panel._ruby_stroke_width_spin.value() == 4
    assert panel._ruby_stroke2_width_spin.value() == 2

    panel._ruby_stroke_width_spin.setValue(6)
    assert emitted[-1].ruby_stroke_width_px == 6
    assert emitted[-1].stroke_width_px == 10

    panel._ruby_stroke2_width_spin.setValue(3)
    assert emitted[-1].ruby_stroke2_width_px == 3
    assert emitted[-1].stroke2_width_px == 4

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("glow")
    )
    assert emitted[-1].ruby_decoration_kind is None
    assert emitted[-1].decoration_kind == "glow"

    panel._glow_radius_spin.setValue(9)
    assert emitted[-1].ruby_glow_radius_px is None
    assert emitted[-1].ruby_glow_before_radius_px is None
    assert emitted[-1].glow_before_radius_px == 9

    panel._color_subject_combo.setCurrentIndex(
        panel._color_subject_combo.findData("main")
    )
    assert panel._stroke_width_spin.value() == 10
    assert panel._decoration_type_combo.currentData() == "glow"


def test_property_panel_role_ruby_subject_controls_go_into_custom_scheme(qapp):
    panel = PropertyPanel()
    panel.set_roles(["B"])
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:B"))
    panel._ruby_stroke_width_spin.setValue(7)
    panel._shadow_y_spin.setValue(5)

    scheme = emitted[-1].custom_style_schemes["B"]
    assert scheme.ruby_stroke_width_px == 7
    assert scheme.shadow_offset_y == 5
    assert scheme.ruby_shadow_offset_y is None
    assert scheme.stroke_width_px != 7
    assert emitted[-1].ruby_stroke_width_px == 10


def test_property_panel_role_scheme_switches_subtitle_controls(qapp):
    panel = PropertyPanel()
    panel.set_roles(["A"])
    style = Style(
        custom_style_schemes={
            "A": SubtitleStyleScheme(
                font_size_px=72,
                letter_spacing_px=11,
                font_weight=700,
                fill_color="#0088ff",
                fill_gradient_enabled=True,
                fill_gradient_start_color="#0088ff",
                fill_gradient_end_color="#ffcc00",
                fill_gradient_angle_deg=270,
                ruby_gap_px=12,
            )
        }
    )

    panel.set_style(style)
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:A"))

    assert panel._font_size_spin.value() == 72
    assert panel._letter_spacing_spin.value() == style.letter_spacing_px
    assert panel._font_weight_combo.currentData() == 700
    assert panel._fill_mode_combo.currentData() == "gradient_vertical"
    assert panel._paint_gradient_start_btn.color == "#0088FF"
    assert panel._paint_gradient_end_btn.color == "#FFCC00"
    assert panel._ruby_gap_spin.value() == style.ruby_gap_px

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("global"))
    assert panel._font_size_spin.value() == style.font_size_px
    assert panel._letter_spacing_spin.value() == style.letter_spacing_px
    assert panel._paint_solid_btn.color == style.fill_color


def test_property_panel_hides_presets_when_subtitle_has_no_roles(qapp):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {
            "蓝色方案": StylePreset(
                name="蓝色方案",
                group="常用",
                scheme=SubtitleStyleScheme(fill_color="#123456"),
            )
        }
    )

    panel.set_roles([])

    # 全局默认 + 内置「标题」方案（恒在），预设不自动进入角色下拉
    assert panel._singer_combo.count() == 2
    assert panel._singer_combo.currentData() == "global"
    assert panel._singer_combo.findData("custom:标题") >= 0
    assert panel._singer_combo.findData("custom:蓝色方案") == -1
    assert panel.preset_schemes["蓝色方案"].scheme.fill_color == "#123456"


def test_property_panel_does_not_guess_between_cross_group_same_name_presets(qapp):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {
            "preset-a": StylePreset(
                name="A",
                group="作品一",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            ),
            "preset-b": StylePreset(
                name="A",
                group="作品二",
                scheme=SubtitleStyleScheme(fill_color="#222222"),
            ),
        }
    )

    panel.set_roles(["A"])

    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#FF5A6F"


def test_property_panel_prompts_each_ambiguous_imported_role_for_group(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {
            "miku-common": StylePreset(
                name="初音", group="常用", scheme=SubtitleStyleScheme(fill_color="#111111")
            ),
            "miku-sekai": StylePreset(
                name="初音",
                group="Project SEKAI",
                scheme=SubtitleStyleScheme(fill_color="#222222"),
            ),
            "rin-common": StylePreset(
                name="镜音", group="常用", scheme=SubtitleStyleScheme(fill_color="#333333")
            ),
            "rin-sekai": StylePreset(
                name="镜音",
                group="Project SEKAI",
                scheme=SubtitleStyleScheme(fill_color="#444444"),
            ),
            "luka-only": StylePreset(
                name="巡音", group="常用", scheme=SubtitleStyleScheme(fill_color="#555555")
            ),
        }
    )
    captured: dict[str, list[tuple[str, str]]] = {}

    class FakeDialog:
        def __init__(self, candidates, parent=None):
            captured.update(
                {
                    role: [(preset.preset_id, preset.group) for preset in presets]
                    for role, presets in candidates.items()
                }
            )

        def exec(self):
            return pp.QDialog.DialogCode.Accepted

        def selected_preset_ids(self):
            return {"初音": "miku-sekai", "镜音": "rin-common"}

    monkeypatch.setattr(pp, "_RolePresetGroupDialog", FakeDialog)

    selected = panel.choose_role_presets_for_import(["初音", "镜音", "巡音"])

    assert captured == {
        "初音": [("miku-common", "常用"), ("miku-sekai", "Project SEKAI")],
        "镜音": [("rin-common", "常用"), ("rin-sekai", "Project SEKAI")],
    }
    assert selected["初音"].fill_color == "#222222"
    assert selected["镜音"].fill_color == "#333333"
    assert "巡音" not in selected


def test_role_preset_group_dialog_requires_each_role_dropdown_selection(qapp):
    dialog = pp._RolePresetGroupDialog(
        {
            "初音": [
                StylePreset(name="初音", group="常用", preset_id="miku-common"),
                StylePreset(name="初音", group="Project SEKAI", preset_id="miku-sekai"),
            ],
            "镜音": [
                StylePreset(name="镜音", group="常用", preset_id="rin-common"),
                StylePreset(name="镜音", group="Project SEKAI", preset_id="rin-sekai"),
            ],
        }
    )

    assert list(dialog._combos) == ["初音", "镜音"]
    assert [dialog._combos["初音"].itemText(index) for index in range(3)] == [
        "请选择分组",
        "常用",
        "Project SEKAI",
    ]
    assert not dialog.apply_button.isEnabled()

    dialog._combos["初音"].setCurrentIndex(2)
    assert not dialog.apply_button.isEnabled()
    dialog._combos["镜音"].setCurrentIndex(1)

    assert dialog.apply_button.isEnabled()
    assert dialog.selected_preset_ids() == {
        "初音": "miku-sekai",
        "镜音": "rin-common",
    }
    dialog.close()
    dialog.deleteLater()
    qapp.processEvents()


def test_property_panel_delete_role_keeps_same_named_preset(qapp):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {
            "fhana": StylePreset(
                name="fhana", scheme=SubtitleStyleScheme(fill_color="#123456")
            )
        }
    )
    panel.set_roles(["fhana"])
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:fhana"))

    assert panel.subtitle_style.custom_style_schemes["fhana"].fill_color == "#123456"

    panel._delete_current_role()

    assert panel._singer_combo.findData("custom:fhana") == -1
    assert "fhana" not in panel.subtitle_style.custom_style_schemes
    assert panel.preset_schemes["fhana"].scheme.fill_color == "#123456"


def test_property_panel_auto_created_role_scheme_is_not_saved_as_preset(qapp):
    panel = PropertyPanel()

    panel.set_roles(["新分色"])

    assert "新分色" in panel.subtitle_style.custom_style_schemes
    assert "新分色" not in panel.preset_schemes


def test_property_panel_existing_project_role_is_not_backfilled_to_presets(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            custom_style_schemes={
                "fhana": SubtitleStyleScheme(fill_color="#123456"),
            }
        )
    )

    panel.set_roles(["fhana"])

    assert panel.subtitle_style.custom_style_schemes["fhana"].fill_color == "#123456"
    assert "fhana" not in panel.preset_schemes


def test_property_panel_can_apply_preset_to_global_and_role(qapp):
    panel = PropertyPanel()
    preset = SubtitleStyleScheme(fill_color="#12ABCD", font_size_px=88)

    panel._apply_preset_to_current_target(preset)

    assert panel.subtitle_style.fill_color == "#12ABCD"
    assert panel.subtitle_style.font_size_px == 88

    panel.set_roles(["A"])
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:A"))
    panel._apply_preset_to_current_target(SubtitleStyleScheme(fill_color="#FFCC00"))

    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#FFCC00"


def test_property_panel_ruby_anchor_toggle_is_saved_per_role(qapp):
    panel = PropertyPanel()
    panel.set_roles(["导唱符"])
    panel.set_current_scheme_key("custom:导唱符")

    assert panel._ruby_anchor_check.isChecked()

    panel._ruby_anchor_check.setChecked(False)

    scheme = panel.subtitle_style.custom_style_schemes["导唱符"]
    assert scheme.affects_ruby_anchor is False
    assert panel.subtitle_style.affects_ruby_anchor is True

    panel.set_current_scheme_key("global")
    assert panel._ruby_anchor_check.isChecked()


def test_style_preset_manager_dialog_saves_current_scheme(qapp):
    dialog = StylePresetManagerDialog(
        presets={},
        current_scheme=SubtitleStyleScheme(fill_color="#123456", font_size_px=77),
        target_label="全局默认",
    )

    assert dialog.add_preset("蓝色方案", "常用")

    presets = dialog.preset_schemes()
    assert presets["蓝色方案"].group == "常用"
    assert presets["蓝色方案"].scheme.fill_color == "#123456"
    assert presets["蓝色方案"].scheme.font_size_px == 77
    assert dialog._preset_list.count() == 1


def test_style_preset_library_forms_use_fluent_controls(qapp):
    details = _StylePresetDetailsDialog(
        name="A", group="作品一", groups=["作品一", "作品二"]
    )
    manager = StylePresetManagerDialog(
        presets={"A": StylePreset(name="A", group="作品一")},
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )
    confirmation = FluentMessageDialog("确认", "确认内容", manager)

    assert isinstance(details.name_edit, LineEdit)
    assert isinstance(details.group_combo, EditableComboBox)
    assert isinstance(details.ok_button, PrimaryPushButton)
    assert isinstance(manager._import_n3_btn, PushButton)
    assert manager._import_n3_btn.text() == "从 N3 导入"
    assert manager.findChildren(SubtitleLabel)
    assert isinstance(confirmation, Dialog)
    assert isinstance(confirmation.yesButton, PrimaryPushButton)
    confirmation.close()
    details.close()
    manager.close()
    confirmation.deleteLater()
    details.deleteLater()
    manager.deleteLater()
    qapp.processEvents()


def test_common_input_dialogs_use_qfluentwidgets_controls(qapp):
    text_dialog = FluentTextInputDialog("新建角色", "角色名称", text="主唱")
    int_dialog = FluentIntInputDialog(
        "图片序列帧率",
        "源帧率（每秒图片数）",
        value=60,
        minimum=1,
        maximum=240,
    )

    assert isinstance(text_dialog.control, LineEdit)
    assert text_dialog.value() == "主唱"
    assert isinstance(int_dialog.control, SpinBox)
    assert int_dialog.value() == 60
    assert int_dialog.control.minimum() == 1
    assert int_dialog.control.maximum() == 240
    assert isinstance(text_dialog.ok_button, PrimaryPushButton)
    assert isinstance(int_dialog.ok_button, PrimaryPushButton)
    assert not text_dialog.findChildren(SubtitleLabel)
    assert not int_dialog.findChildren(SubtitleLabel)

    text_dialog.close()
    int_dialog.close()
    text_dialog.deleteLater()
    int_dialog.deleteLater()
    qapp.processEvents()


def test_style_preset_manager_requires_explicit_exact_pair_overwrite(qapp):
    dialog = StylePresetManagerDialog(
        presets={
            "蓝色方案": StylePreset(
                name="蓝色方案",
                group="旧分组",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            )
        },
        current_scheme=SubtitleStyleScheme(fill_color="#222222"),
        target_label="全局默认",
    )

    assert not dialog.add_preset("蓝色方案", "旧分组")
    assert dialog.preset_schemes()["蓝色方案"].scheme.fill_color == "#111111"

    assert dialog.add_preset("蓝色方案", "旧分组", overwrite=True)
    preset = dialog.preset_schemes()["蓝色方案"]
    assert preset.group == "旧分组"
    assert preset.scheme.fill_color == "#222222"


def test_style_preset_manager_allows_same_name_in_different_groups(qapp):
    dialog = StylePresetManagerDialog(
        presets={
            "preset-a": StylePreset(
                name="蓝色方案",
                group="作品一",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            )
        },
        current_scheme=SubtitleStyleScheme(fill_color="#222222"),
        target_label="角色 A",
    )

    assert dialog.add_preset("蓝色方案", "作品二")

    matches = [
        preset
        for preset in dialog.preset_schemes().values()
        if preset.name == "蓝色方案"
    ]
    assert {(preset.group, preset.scheme.fill_color) for preset in matches} == {
        ("作品一", "#111111"),
        ("作品二", "#222222"),
    }


def test_style_preset_manager_overwrites_only_exact_name_and_group(qapp):
    dialog = StylePresetManagerDialog(
        presets={
            "preset-a": StylePreset(
                name="蓝色方案",
                group="作品一",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            ),
            "preset-b": StylePreset(
                name="蓝色方案",
                group="作品二",
                scheme=SubtitleStyleScheme(fill_color="#222222"),
            ),
        },
        current_scheme=SubtitleStyleScheme(fill_color="#333333"),
        target_label="角色 A",
    )

    assert not dialog.add_preset("蓝色方案", "作品二")
    assert dialog.add_preset("蓝色方案", "作品二", overwrite=True)

    matches = [
        preset
        for preset in dialog.preset_schemes().values()
        if preset.name == "蓝色方案"
    ]
    assert {(preset.group, preset.scheme.fill_color) for preset in matches} == {
        ("作品一", "#111111"),
        ("作品二", "#333333"),
    }


def test_style_preset_manager_same_name_save_respects_confirmation(qapp, monkeypatch):
    dialog = StylePresetManagerDialog(
        presets={
            "蓝色方案": StylePreset(
                name="蓝色方案",
                group="旧分组",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            )
        },
        current_scheme=SubtitleStyleScheme(fill_color="#222222"),
        target_label="角色 A",
    )
    monkeypatch.setattr(
        dialog, "_prompt_preset_details", lambda *_args: ("蓝色方案", "旧分组")
    )
    monkeypatch.setattr(dialog, "_confirm_overwrite", lambda _name: "cancel")

    dialog._on_save_current()
    assert dialog.preset_schemes()["蓝色方案"].scheme.fill_color == "#111111"

    monkeypatch.setattr(dialog, "_confirm_overwrite", lambda _name: "overwrite")
    dialog._on_save_current()
    preset = dialog.preset_schemes()["蓝色方案"]
    assert preset.group == "旧分组"
    assert preset.scheme.fill_color == "#222222"


def test_style_preset_settings_migrate_legacy_entries_and_roundtrip_groups():
    loaded = mw._style_presets_from_dict(
        {
            "旧预设": {"fill_color": "#123456"},
            "新预设": {
                "group": "作品一",
                "scheme": {"fill_color": "#ABCDEF", "font_size_px": 72},
                "source_type": "n3_font_template",
                "source_data": {"guid": "demo", "payload": {"SettingsName": "新预设"}},
            },
        }
    )

    assert loaded["旧预设"].group == ""
    assert loaded["旧预设"].scheme.fill_color == "#123456"
    assert loaded["新预设"].group == "作品一"
    assert loaded["新预设"].scheme.font_size_px == 72
    assert loaded["新预设"].source_type == "n3_font_template"
    assert loaded["新预设"].source_data["guid"] == "demo"

    payload = {
        item["name"]: item for item in mw._style_presets_to_dict(loaded)
    }
    assert payload["旧预设"]["group"] == ""
    assert payload["旧预设"]["scheme"]["fill_color"] == "#123456"
    assert payload["新预设"]["group"] == "作品一"
    assert payload["新预设"]["source_type"] == "n3_font_template"
    assert payload["新预设"]["source_data"]["guid"] == "demo"


def test_style_preset_settings_roundtrip_cross_group_duplicate_names():
    loaded = mw._style_presets_from_dict(
        [
            {
                "id": "preset-a",
                "name": "蓝色方案",
                "group": "作品一",
                "scheme": {"fill_color": "#111111"},
            },
            {
                "id": "preset-b",
                "name": "蓝色方案",
                "group": "作品二",
                "scheme": {"fill_color": "#222222"},
            },
        ]
    )

    assert set(loaded) == {"preset-a", "preset-b"}
    assert {preset.name for preset in loaded.values()} == {"蓝色方案"}
    assert {preset.group for preset in loaded.values()} == {"作品一", "作品二"}

    payload = mw._style_presets_to_dict(loaded)
    assert isinstance(payload, list)
    assert {(item["id"], item["name"], item["group"]) for item in payload} == {
        ("preset-a", "蓝色方案", "作品一"),
        ("preset-b", "蓝色方案", "作品二"),
    }


def test_style_preset_manager_dialog_imports_multiple_selected_schemes(qapp):
    dialog = StylePresetManagerDialog(
        presets={
            "A": StylePreset(
                name="A", group="作品一", scheme=SubtitleStyleScheme(fill_color="#111111")
            ),
            "B": StylePreset(
                name="B", group="作品二", scheme=SubtitleStyleScheme(fill_color="#222222")
            ),
        },
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )

    dialog._preset_list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog._preset_list.item(1).setCheckState(Qt.CheckState.Checked)
    dialog._on_import_selected()

    imported = dialog.imported_schemes()
    assert set(imported) == {"A", "B"}
    assert imported["A"].scheme.fill_color == "#111111"
    assert imported["B"].scheme.fill_color == "#222222"


def test_style_preset_manager_requires_one_choice_for_cross_group_same_name(
    qapp, monkeypatch
):
    dialog = StylePresetManagerDialog(
        presets={
            "preset-a": StylePreset(name="A", group="作品一"),
            "preset-b": StylePreset(name="A", group="作品二"),
        },
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )
    for index in range(dialog._preset_list.count()):
        dialog._preset_list.item(index).setCheckState(Qt.CheckState.Checked)
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pp.InfoBar,
        "warning",
        lambda **kwargs: warnings.append((kwargs["title"], kwargs["content"])),
    )

    dialog._on_import_selected()

    assert dialog.imported_schemes() == {}
    assert warnings == [
        ("存在同名预设", "同名预设不能同时导入为项目角色，请只选择其中一个：A")
    ]


def test_style_preset_manager_leaves_new_n3_templates_unchecked_after_import(
    qapp, monkeypatch
):
    imported_templates = tuple(
        N3TemplateLoadResult(
            path=Path(f"{name}.tpl"),
            guid=f"guid-{name}",
            name=name,
            preset=StylePreset(
                name=name,
                scheme=SubtitleStyleScheme(fill_color=color),
                source_type="n3_font_template",
            ),
        )
        for name, color in (("N3 角色 A", "#111111"), ("N3 角色 B", "#222222"))
    )
    monkeypatch.setattr(
        pp,
        "find_n3_template_files",
        lambda: [item.path for item in imported_templates],
    )
    monkeypatch.setattr(pp, "fluent_choice", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        pp,
        "load_n3_font_templates",
        lambda *_args, **_kwargs: N3TemplateBatchResult(
            templates=imported_templates,
            skipped=(),
            failed=(),
        ),
    )
    monkeypatch.setattr(pp.InfoBar, "success", lambda **_kwargs: None)
    dialog = StylePresetManagerDialog(
        presets={"旧预设": StylePreset(name="旧预设")},
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )

    dialog._on_import_n3()

    assert dialog._checked_names() == []
    assert not dialog._import_btn.isEnabled()
    dialog._on_import_selected()
    assert dialog.imported_schemes() == {}


def test_style_preset_manager_publishes_n3_import_before_dialog_closes(
    qapp, monkeypatch
):
    imported = N3TemplateLoadResult(
        path=Path("n3-template.tpl"),
        guid="guid-n3-template",
        name="N3 模板",
        preset=StylePreset(
            name="N3 模板",
            group="N3",
            scheme=SubtitleStyleScheme(fill_color="#123456"),
            source_type="n3_font_template",
            source_data={"payload": {"SettingsName": "N3 模板"}},
        ),
    )
    monkeypatch.setattr(pp, "find_n3_template_files", lambda: [imported.path])
    monkeypatch.setattr(pp, "fluent_choice", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        pp,
        "load_n3_font_templates",
        lambda *_args, **_kwargs: N3TemplateBatchResult(
            templates=(imported,), skipped=(), failed=()
        ),
    )
    monkeypatch.setattr(pp.InfoBar, "success", lambda **_kwargs: None)
    dialog = StylePresetManagerDialog(
        presets={},
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )
    published: list[dict[str, StylePreset]] = []
    dialog.presetLibraryChanged.connect(published.append)

    dialog._on_import_n3()

    assert len(published) == 1
    saved = next(iter(published[0].values()))
    assert saved.name == "N3 模板"
    assert saved.source_type == "n3_font_template"
    assert dialog.result() == 0


def test_style_preset_manager_filters_groups_without_losing_checks(qapp):
    dialog = StylePresetManagerDialog(
        presets={
            "A": StylePreset(name="A", group="作品一"),
            "B": StylePreset(name="B", group="作品二"),
            "C": StylePreset(name="C", group=""),
        },
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )
    item_a = next(
        dialog._preset_list.item(index)
        for index in range(dialog._preset_list.count())
        if dialog._preset_list.item(index).data(Qt.ItemDataRole.UserRole) == "A"
    )
    item_a.setCheckState(Qt.CheckState.Checked)

    dialog._group_filter.setCurrentIndex(dialog._group_filter.findData("作品二"))
    qapp.processEvents()

    assert item_a.isHidden()
    assert item_a.checkState() == Qt.CheckState.Checked
    assert dialog._checked_names() == ["A"]


def test_style_preset_manager_sets_group_for_checked_items(qapp, monkeypatch):
    dialog = StylePresetManagerDialog(
        presets={
            "A": StylePreset(name="A", group="旧分组"),
            "B": StylePreset(name="B", group=""),
        },
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )
    for index in range(dialog._preset_list.count()):
        dialog._preset_list.item(index).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.property_panel.fluent_get_editable_choice",
        lambda *args, **kwargs: ("新分组", True),
    )

    dialog._on_set_group()

    assert {preset.group for preset in dialog.preset_schemes().values()} == {"新分组"}
    assert dialog._group_filter.findData("新分组") >= 0


def test_property_panel_imports_selected_presets_as_role_schemes(qapp):
    panel = PropertyPanel()

    panel._import_preset_schemes(
        {
            "A": StylePreset(name="A", scheme=SubtitleStyleScheme(fill_color="#111111")),
            "B": StylePreset(name="B", scheme=SubtitleStyleScheme(fill_color="#222222")),
        }
    )

    assert panel._singer_combo.findData("custom:A") >= 0
    assert panel._singer_combo.findData("custom:B") >= 0
    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#111111"
    assert panel.subtitle_style.custom_style_schemes["B"].fill_color == "#222222"


def test_property_panel_imports_only_one_cross_group_same_name_role(qapp):
    panel = PropertyPanel()

    panel._import_preset_schemes(
        {
            "preset-a": StylePreset(
                name="A",
                group="作品一",
                scheme=SubtitleStyleScheme(fill_color="#111111"),
            ),
            "preset-b": StylePreset(
                name="A",
                group="作品二",
                scheme=SubtitleStyleScheme(fill_color="#222222"),
            ),
        }
    )

    assert panel.role_names == ["A"]
    assert set(panel.subtitle_style.custom_style_schemes) >= {"A"}
    assert "preset-a" not in panel.subtitle_style.custom_style_schemes
    assert "preset-b" not in panel.subtitle_style.custom_style_schemes


def test_property_panel_batch_import_does_not_overwrite_existing_project_role(qapp):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            custom_style_schemes={
                "A": SubtitleStyleScheme(fill_color="#AAAAAA"),
            }
        )
    )
    panel.set_roles(["A"])

    panel._import_preset_schemes(
        {
            "A": StylePreset(name="A", scheme=SubtitleStyleScheme(fill_color="#111111")),
            "B": StylePreset(name="B", scheme=SubtitleStyleScheme(fill_color="#222222")),
        }
    )

    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#AAAAAA"
    assert panel.subtitle_style.custom_style_schemes["B"].fill_color == "#222222"


def test_property_panel_deleting_preset_keeps_same_named_project_role(qapp):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {"A": StylePreset(name="A", scheme=SubtitleStyleScheme(fill_color="#111111"))}
    )
    panel.set_roles(["A"])

    panel._set_preset_schemes_from_dialog({})

    assert "A" not in panel.preset_schemes
    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#111111"
    assert panel._singer_combo.findData("custom:A") >= 0


def test_style_preset_manager_dialog_deletes_only_library_entry(qapp, monkeypatch):
    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.property_panel.fluent_question",
        lambda *args, **kwargs: True,
    )
    dialog = StylePresetManagerDialog(
        presets={
            "A": StylePreset(name="A", scheme=SubtitleStyleScheme(fill_color="#111111"))
        },
        current_scheme=SubtitleStyleScheme(),
        target_label="全局默认",
    )

    dialog._preset_list.item(0).setCheckState(Qt.CheckState.Checked)
    dialog._on_delete()

    assert "A" not in dialog.preset_schemes()


def test_property_panel_dialog_library_changes_do_not_remove_project_roles(qapp, monkeypatch):
    panel = PropertyPanel()
    panel.set_preset_schemes(
        {"A": StylePreset(name="A", scheme=SubtitleStyleScheme(fill_color="#111111"))}
    )
    panel.set_roles(["A"])

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            self.presetLibraryChanged = FakeSignal()

        def exec(self):
            return 0

        def preset_schemes(self):
            return {}

        def imported_schemes(self):
            return {}

        def applied_scheme(self):
            return None

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.property_panel.StylePresetManagerDialog",
        FakeDialog,
    )

    panel._open_preset_manager()

    assert "A" not in panel.preset_schemes
    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#111111"
    assert panel._singer_combo.findData("custom:A") >= 0


def test_n3_preset_import_is_persisted_before_manager_dialog_closes(qapp, monkeypatch):
    provider = _FontMigrationSettingsProvider({})
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    imported = StylePreset(
        name="N3 模板",
        group="N3",
        scheme=SubtitleStyleScheme(fill_color="#123456"),
        preset_id="n3-template",
        source_type="n3_font_template",
        source_data={"payload": {"SettingsName": "N3 模板"}},
    )

    class FakeSignal:
        def connect(self, callback):
            self._callback = callback

        def emit(self, payload):
            self._callback(payload)

    class FakeDialog:
        def __init__(self, *args, **kwargs):
            self.presetLibraryChanged = FakeSignal()

        def exec(self):
            self.presetLibraryChanged.emit({"n3-template": imported})
            saved = provider.data["style_presets"]
            assert [(item["id"], item["source_type"]) for item in saved] == [
                ("n3-template", "n3_font_template")
            ]
            return 0

        def preset_schemes(self):
            return {"n3-template": imported}

        def imported_schemes(self):
            return {}

        def applied_scheme(self):
            return None

    monkeypatch.setattr(pp, "StylePresetManagerDialog", FakeDialog)

    win._property_panel._open_preset_manager()

    reloaded = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    assert reloaded._style_presets["n3-template"].name == "N3 模板"
    assert reloaded._style_presets["n3-template"].source_data == imported.source_data


def test_property_panel_can_add_custom_scheme(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._set_color("fill_color", "#123456")
    panel._add_custom_scheme("蓝色方案")

    assert "蓝色方案" in panel.subtitle_style.custom_style_schemes
    assert emitted[-1].custom_style_schemes["蓝色方案"].fill_color == "#123456"
    assert panel._singer_combo.currentData() == "custom:蓝色方案"

    panel._font_size_spin.setValue(77)
    assert emitted[-1].custom_style_schemes["蓝色方案"].font_size_px == 77


def test_property_panel_rejects_renaming_role_to_existing_project_name(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    panel.set_style(
        Style(
            custom_style_schemes={
                "A": SubtitleStyleScheme(fill_color="#111111"),
                "B": SubtitleStyleScheme(fill_color="#222222"),
            }
        )
    )
    panel.set_roles(["A", "B"])
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:A"))
    monkeypatch.setattr(pp, "fluent_get_text", lambda *_args, **_kwargs: ("B", True))
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pp.InfoBar,
        "warning",
        lambda **kwargs: warnings.append((kwargs["title"], kwargs["content"])),
    )

    panel._rename_current_role()

    assert panel.role_names == ["A", "B"]
    assert panel.subtitle_style.custom_style_schemes["A"].fill_color == "#111111"
    assert panel.subtitle_style.custom_style_schemes["B"].fill_color == "#222222"
    assert warnings == [("名称已存在", "项目中已经存在角色“B”。")]


def test_property_panel_scheme_selection_emits_current_key(qapp):
    panel = PropertyPanel()
    panel._add_custom_scheme("图像方案")
    emitted: list[str] = []
    panel.schemeSelectionChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("global"))
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:图像方案"))

    assert emitted[-1] == "custom:图像方案"


def test_property_panel_add_scheme_button_ignores_clicked_checked_arg(qapp, monkeypatch):
    panel = PropertyPanel()
    monkeypatch.setattr(
        pp,
        "fluent_get_text",
        lambda *args, **kwargs: ("按钮方案", True),
    )

    panel._add_scheme_button.clicked.emit(False)

    assert "按钮方案" in panel.subtitle_style.custom_style_schemes
    assert panel._singer_combo.currentData() == "custom:按钮方案"


def test_wheel_changes_spinbox_only_when_focused(qapp):
    panel = PropertyPanel()
    panel.show()
    spin = panel._font_size_spin
    assert spin.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert spin.lineEdit().focusPolicy() == Qt.FocusPolicy.StrongFocus
    spin.setValue(100)
    panel.setFocus()
    spin.clearFocus()
    qapp.processEvents()

    unfocused_event = _wheel_event(spin)
    spin.wheelEvent(unfocused_event)
    assert spin.value() == 100
    assert not unfocused_event.isAccepted()

    spin.setFocus(Qt.FocusReason.MouseFocusReason)
    qapp.processEvents()
    focused_event = _wheel_event(spin)
    spin.wheelEvent(focused_event)
    assert spin.value() != 100


def test_unfocused_wheel_does_not_change_combo(qapp):
    panel = PropertyPanel()
    panel.show()
    combo = panel._font_weight_combo
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    combo.setCurrentIndex(combo.findData(400))
    panel.setFocus()
    combo.clearFocus()
    qapp.processEvents()

    unfocused_event = _wheel_event(combo)
    QApplication.sendEvent(combo, unfocused_event)
    assert combo.currentData() == 400
    assert not unfocused_event.isAccepted()


def test_color_button_updates_text_and_color(qapp):
    button = ColorButton("#abcdef")
    assert button.color == "#ABCDEF"
    assert button.text() == "#ABCDEF"
    button.set_color("#010203")
    assert button.color == "#010203"
    assert button.text() == "#010203"


def test_color_button_hover_keeps_neutral_border(qapp):
    button = ColorButton("#39C5BB")
    stylesheet = button._swatch.styleSheet()

    hover_rule = stylesheet.split("QPushButton:hover", 1)[1]
    assert f"border-color: {pp.palette().card_border}" in hover_rule
    assert pp.palette().accent_primary not in hover_rule


def test_color_button_click_edits_hex_without_opening_dialog(qapp):
    button = ColorButton("#4093E9")
    button.show()
    entered: list[str] = []
    dialog_requests: list[bool] = []
    button.colorEntered.connect(entered.append)
    button.clicked.connect(lambda: dialog_requests.append(True))

    button.click()
    assert button._swatch_stack.currentWidget() is button._color_edit
    button._color_edit.setText("123456")
    QTest.keyClick(button._color_edit, Qt.Key.Key_Return)
    assert button.color == "#123456"

    button.click()
    button._color_edit.setText("#ABCDEF")
    QTest.keyClick(button._color_edit, Qt.Key.Key_Return)

    assert button.color == "#ABCDEF"
    assert entered == ["#123456", "#ABCDEF"]
    assert dialog_requests == []


def _send_context_menu_event(widget: QWidget) -> None:
    pos = QPoint(5, 5)
    QApplication.sendEvent(
        widget,
        QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse,
            pos,
            widget.mapToGlobal(pos),
        ),
    )


def test_color_button_right_click_pastes_without_entering_edit_mode(
    qapp, monkeypatch
):
    button = ColorButton("#4093E9")
    button.show()
    qapp.processEvents()
    entered: list[str] = []
    menu_labels: list[str] = []
    button.colorEntered.connect(entered.append)
    QApplication.clipboard().setText("ABCDEF")

    def trigger_paste(menu, _pos):
        actions = menu.actions()
        menu_labels.extend(action.text() for action in actions)
        next(action for action in actions if action.text() == "粘贴色号").trigger()

    monkeypatch.setattr(pp.RoundMenu, "exec", trigger_paste)

    _send_context_menu_event(button._swatch)

    assert menu_labels == ["复制色号", "粘贴色号"]
    assert button.color == "#ABCDEF"
    assert entered == ["#ABCDEF"]
    assert button._swatch_stack.currentWidget() is button._swatch


def test_color_button_custom_menu_copies_color_and_has_no_edit_actions(
    qapp, monkeypatch
):
    button = ColorButton("#39C5BB")
    button.show()
    qapp.processEvents()
    QApplication.clipboard().clear()

    def trigger_copy(menu, _pos):
        actions = menu.actions()
        assert [action.text() for action in actions] == ["复制色号", "粘贴色号"]
        next(action for action in actions if action.text() == "复制色号").trigger()

    monkeypatch.setattr(pp.RoundMenu, "exec", trigger_copy)

    _send_context_menu_event(button._swatch)

    assert QApplication.clipboard().text() == "#39C5BB"


def test_color_button_right_click_paste_works_while_hex_editor_is_active(
    qapp, monkeypatch
):
    button = ColorButton("#4093E9")
    button.show()
    qapp.processEvents()
    QApplication.clipboard().setText("#123456")
    button.click()

    def trigger_paste(menu, _pos):
        assert button._color_edit._context_menu_active
        QApplication.sendEvent(
            button._color_edit,
            QFocusEvent(
                QEvent.Type.FocusOut,
                Qt.FocusReason.PopupFocusReason,
            ),
        )
        assert button._swatch_stack.currentWidget() is button._color_edit
        next(
            action for action in menu.actions() if action.text() == "粘贴色号"
        ).trigger()

    monkeypatch.setattr(pp.RoundMenu, "exec", trigger_paste)

    _send_context_menu_event(button._color_edit)

    assert button.color == "#123456"
    assert button._swatch_stack.currentWidget() is button._swatch


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("abc", "#AABBCC"),
        ("123456", "#123456"),
        ("80123456", "#80123456"),
    ],
)
def test_color_button_applies_valid_hex_without_enter(qapp, typed, expected):
    button = ColorButton("#4093E9")
    button.show()
    entered: list[str] = []
    button.colorEntered.connect(entered.append)

    button.click()
    QTest.keyClicks(button._color_edit, typed)
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)

    assert button.color == expected
    assert entered == [expected]
    assert button._swatch_stack.currentWidget() is button._color_edit


def test_color_button_invalid_live_text_keeps_last_valid_color(qapp):
    button = ColorButton("#4093E9")
    button.show()
    entered: list[str] = []
    button.colorEntered.connect(entered.append)

    button.click()
    QTest.keyClicks(button._color_edit, "not-a-color")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)

    assert button.color == "#4093E9"
    assert entered == []
    assert button._swatch_stack.currentWidget() is button._color_edit


def test_color_button_focus_out_keeps_last_live_color(qapp):
    button = ColorButton("#4093E9")
    button.show()
    button.click()
    QTest.keyClicks(button._color_edit, "123456")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)

    button._color_edit.finishRequested.emit()

    assert button.color == "#123456"
    assert button._swatch_stack.currentWidget() is button._swatch


def test_color_button_escape_reverts_entire_live_edit(qapp):
    button = ColorButton("#4093E9")
    button.show()
    entered: list[str] = []
    button.colorEntered.connect(entered.append)
    button.click()
    QTest.keyClicks(button._color_edit, "123456")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)
    assert button.color == "#123456"

    QTest.keyClick(button._color_edit, Qt.Key.Key_Escape)

    assert button.color == "#4093E9"
    assert entered == ["#123456", "#4093E9"]
    assert button._swatch_stack.currentWidget() is button._swatch


def test_color_button_invalid_hex_or_escape_does_not_apply(qapp):
    button = ColorButton("#4093E9")
    button.show()
    entered: list[str] = []
    button.colorEntered.connect(entered.append)

    button.click()
    button._color_edit.setText("not-a-color")
    QTest.keyClick(button._color_edit, Qt.Key.Key_Return)
    assert button._swatch_stack.currentWidget() is button._color_edit
    assert entered == []

    QTest.keyClick(button._color_edit, Qt.Key.Key_Escape)
    assert button._swatch_stack.currentWidget() is button._swatch
    assert button.color == "#4093E9"
    assert entered == []


def test_color_button_shortens_swatch_and_exposes_two_icon_actions(qapp):
    button = ColorButton("#4093e9")
    button.resize(220, 30)
    button.show()
    qapp.processEvents()

    dialog_requests: list[bool] = []
    screen_requests: list[bool] = []
    button.clicked.connect(lambda: dialog_requests.append(True))
    button.screenPickRequested.connect(lambda: screen_requests.append(True))
    button.palette_button.click()
    button.screen_picker_button.click()

    assert button._swatch.width() < button.width() - 60
    assert button.palette_button.accessibleName() == "打开颜色选择窗口"
    assert button.screen_picker_button.accessibleName() == "从屏幕取色"
    assert dialog_requests == [True]
    assert screen_requests == [True]


def test_direct_screen_picker_maps_virtual_desktop_coordinates(qapp):
    picker = ScreenColorPicker()
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    image.setPixelColor(1, 1, QColor("#ABCDEF"))
    picker._screens = [(QRect(-10, 5, 20, 20), image)]

    assert picker.cursor().shape() == Qt.CursorShape.CrossCursor
    assert picker.color_at(QPoint(5, 20)).name().upper() == "#ABCDEF"

    picker.cancel()
    qapp.processEvents()


def test_color_control_screen_action_requests_direct_screen_pick(qapp, monkeypatch):
    requests = []

    def fail_if_dialog_opens(*args, **kwargs):
        raise AssertionError("direct screen picking must not open QColorDialog")

    monkeypatch.setattr(
        "krok_helper.subtitle_render.frontend.property_panel._select_color",
        fail_if_dialog_opens,
    )
    panel = PropertyPanel()
    monkeypatch.setattr(
        panel,
        "_begin_screen_color_pick",
        lambda button, callback: requests.append((button, callback)),
    )

    panel._paint_solid_btn.screen_picker_button.click()
    assert len(requests) == 1

    preview_button, callback = requests[0]
    assert preview_button is panel._paint_solid_btn
    callback(QColor("#123456"))
    assert panel._paint_solid_btn.color == "#123456"


def test_screen_picker_hover_previews_but_cancel_restores(qapp, monkeypatch):
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    panel = PropertyPanel()
    button = panel._paint_solid_btn
    original = button.color
    applied: list[str] = []

    panel._begin_screen_color_pick(
        button,
        lambda color: applied.append(color.name().upper()),
    )
    picker = panel._screen_color_picker
    assert picker is not None

    picker.colorHovered.emit(QColor("#123456"))
    assert button.color == "#123456"
    assert applied == []

    picker.cancel()
    assert button.color == original
    assert applied == []


def test_screen_picker_left_click_preview_is_applied(qapp, monkeypatch):
    monkeypatch.setattr(ScreenColorPicker, "start", lambda self: None)
    panel = PropertyPanel()
    button = panel._paint_solid_btn
    applied: list[str] = []

    panel._begin_screen_color_pick(
        button,
        lambda color: applied.append(color.name().upper()),
    )
    picker = panel._screen_color_picker
    assert picker is not None

    picker.colorHovered.emit(QColor("#123456"))
    picker.colorPicked.emit(QColor("#ABCDEF"))
    picker.cancel()

    assert button.color == "#ABCDEF"
    assert applied == ["#ABCDEF"]


def test_main_window_style_panel_updates_preview(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    win = mw.SubtitleRenderWindow(embedded=False)

    win._property_panel._font_size_spin.setValue(96)
    win._property_panel._set_color("fill_color", "#00aaee")
    win._property_panel._ruby_font_size_spin.setValue(28)
    win._property_panel._line_gap_spin.setValue(77)
    win._property_panel.set_roles(["A"])
    win._property_panel._singer_combo.setCurrentIndex(
        win._property_panel._singer_combo.findData("custom:A")
    )
    win._property_panel._set_color("fill_color", "#ffcc00")

    assert win._style.font_size_px == 96
    assert win._style.fill_color == "#00AAEE"
    assert win._style.ruby_font_size_px == 28
    assert win._style.line_gap_px == 77
    assert win._style.custom_style_schemes["A"].fill_color == "#FFCC00"
    assert win._preview_panel.canvas._style.font_size_px == 96
    assert win._preview_panel.canvas._style.fill_color == "#00AAEE"
    assert win._preview_panel.canvas._style.ruby_font_size_px == 28
    assert win._preview_panel.canvas._style.line_gap_px == 77
    assert win._preview_panel.canvas._style.custom_style_schemes["A"].fill_color == "#FFCC00"


def test_live_color_updates_merge_into_one_style_undo_step(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    win = mw.SubtitleRenderWindow(embedded=False)
    button = win._property_panel._paint_solid_btn
    original = button.color

    button.click()
    QTest.keyClicks(button._color_edit, "123456")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)
    button._color_edit.selectAll()
    QTest.keyClicks(button._color_edit, "ABCDEF")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)

    assert button.color == "#ABCDEF"
    assert len(win._undo_stack) == 1

    win._undo_edit()
    assert button.color == original
    win.close()


def test_escape_removes_noop_live_color_undo_step(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    win = mw.SubtitleRenderWindow(embedded=False)
    button = win._property_panel._paint_solid_btn
    original = button.color

    button.click()
    QTest.keyClicks(button._color_edit, "123456")
    QTest.qWait(button._LIVE_APPLY_DELAY_MS + 30)
    assert len(win._undo_stack) == 1

    QTest.keyClick(button._color_edit, Qt.Key.Key_Escape)

    assert button.color == original
    assert win._undo_stack == []
    win.close()


def test_main_window_preview_tab_uses_two_top_regions_and_bottom_timeline(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    win.resize(1600, 900)
    win.show()
    qapp.processEvents()

    left_width, right_width = win._preview_splitter.sizes()
    navigation_center = win._bottom_navigation.mapTo(
        win._project_bar,
        win._bottom_navigation.rect().center(),
    ).x()

    assert win.layout().count() == 2
    assert win.layout().itemAt(0).widget() is win._project_bar
    assert win.layout().itemAt(1).widget() is win._stack
    assert win._project_bar.isAncestorOf(win._bottom_navigation)
    assert abs(navigation_center - win._project_bar.rect().center().x()) <= 1
    assert win._preview_body_splitter.orientation() == Qt.Orientation.Vertical
    assert win._preview_body_splitter.widget(0) is win._preview_splitter
    assert win._preview_body_splitter.widget(1) is win._tracks_view
    assert win._preview_splitter.orientation() == Qt.Orientation.Horizontal
    assert win._preview_splitter.count() == 2
    assert win._preview_splitter.widget(0) is win._lyrics_panel
    assert win._preview_splitter.widget(1) is win._video_settings_panel
    assert win._lyrics_panel.geometry().right() < win._video_settings_panel.geometry().left()
    assert left_width >= 320
    assert right_width >= win._property_panel.minimumWidth()
    assert win._transport_bar.parentWidget() is win._preview_window


def test_preview_player_window_keeps_full_16_9_canvas_below_title_bar(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    win.resize(1600, 900)
    win.move(120, 80)
    win.show()
    qapp.processEvents()

    preview = win._preview_window
    preview.show()
    qapp.processEvents()
    geometry = preview.geometry()

    assert geometry.size() == QSize(800, 492)
    assert geometry.topLeft() == win.mapToGlobal(QPoint(0, 0))
    assert preview._top_controls.geometry() == QRect(0, 0, 800, 42)
    assert preview._preview_frame.geometry() == QRect(0, 42, 800, 450)
    assert preview.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert preview.findChild(mw.TransportBar) is win._transport_bar
    assert preview._transport_bar._preview_quality_label.parentWidget() is preview._top_controls
    assert preview._transport_bar._preview_quality_combo.parentWidget() is preview._top_controls
    assert preview._transport_bar.layout().indexOf(
        preview._transport_bar._preview_quality_combo
    ) == -1
    assert (
        preview._transport_bar._preview_quality_combo.geometry().right()
        < preview._minimize_button.geometry().left()
    )
    assert preview._transport_bar._preview_quality_combo.objectName() == (
        "PreviewQualityCombo"
    )
    combo = preview._transport_bar._preview_quality_combo
    assert combo.fontMetrics().horizontalAdvance("清晰（1/1）") + 26 <= combo.width()


def test_preview_player_maximize_button_restores_from_fullscreen_state(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    win.resize(1600, 900)
    win.move(120, 80)
    win.show()
    qapp.processEvents()

    preview = win._preview_window
    preview.showFullScreen()
    qapp.processEvents()
    assert preview._is_expanded() is True

    preview._maximize_button.click()
    qapp.processEvents()

    assert preview._is_expanded() is False
    assert preview.geometry().size() == QSize(800, 492)
    assert preview.geometry().topLeft() == win.mapToGlobal(QPoint(0, 0))


def test_preview_player_collapses_to_labeled_bar_inside_workspace(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    win.resize(1600, 900)
    win.move(120, 80)
    win.show()
    qapp.processEvents()

    preview = win._preview_window
    preview.set_media_title(Path("demo.mp4"))
    preview.show()
    preview._minimize_button.click()
    qapp.processEvents()

    owner_top_left = win.mapToGlobal(QPoint(0, 0))
    expected_left = owner_top_left.x() + (
        win.width() - preview._COLLAPSED_SIZE.width()
    ) // 2
    expected_top = (
        owner_top_left.y()
        + round(win.height() * preview._COLLAPSED_CENTER_Y_RATIO)
        - preview._COLLAPSED_SIZE.height() // 2
    )
    assert preview.is_collapsed() is True
    assert preview.isMinimized() is False
    assert preview.geometry() == QRect(
        expected_left,
        expected_top,
        preview._COLLAPSED_SIZE.width(),
        preview._COLLAPSED_SIZE.height(),
    )
    assert preview._title_label.text() == "预览窗口"
    assert preview._top_controls.isVisible() is True
    assert preview._bottom_controls.isVisible() is False
    assert preview._transport_bar._preview_quality_label.isVisible() is False
    assert preview._transport_bar._preview_quality_combo.isVisible() is False

    preview.hide_controls(force=True)
    assert preview._title_label.text() == "预览窗口"
    assert preview._top_controls.isVisible() is True

    win._show_preview_window()
    qapp.processEvents()
    assert preview.is_collapsed() is False
    assert preview.geometry().size() == QSize(800, 492)
    assert preview._title_label.text() == "demo.mp4"
    assert preview._transport_bar._preview_quality_label.isVisible() is True
    assert preview._transport_bar._preview_quality_combo.isVisible() is True


def test_preview_player_controls_auto_hide_and_restore(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    preview = win._preview_window
    preview.show()
    qapp.processEvents()

    preview.show_controls()
    assert preview._top_controls.isVisible() is True
    assert preview._bottom_controls.isVisible() is True

    preview.hide_controls(force=True)
    qapp.processEvents()
    assert preview._top_controls.isVisible() is True
    assert preview._bottom_controls.isVisible() is False

    preview.show_controls()
    assert preview._top_controls.isVisible() is True
    assert preview._bottom_controls.isVisible() is True


def test_preview_player_idle_timeout_keeps_controls_while_mouse_inside(qapp, monkeypatch):
    win = mw.SubtitleRenderWindow(embedded=False)
    preview = win._preview_window
    preview.show()
    qapp.processEvents()
    preview.show_controls()
    monkeypatch.setattr(preview, "underMouse", lambda: True)

    preview._on_controls_idle_timeout()

    assert preview._top_controls.isVisible() is True
    assert preview._bottom_controls.isVisible() is True
    assert preview._hide_controls_timer.isActive() is True


def test_preview_player_transport_bar_uses_overlay_style(qapp):
    win = mw.SubtitleRenderWindow(embedded=False)
    bar = win._transport_bar

    assert "background: transparent" in bar._play_btn.styleSheet()
    assert "border: none" in bar._play_btn.styleSheet()
    assert bar._slider.__class__.__name__ == "PlayerProgressSlider"
    assert "rgba(0, 0, 0, 0)" in bar.styleSheet()


def test_preview_player_close_stops_playback(qapp, monkeypatch):
    win = mw.SubtitleRenderWindow(embedded=False)
    preview = win._preview_window
    stopped: list[bool] = []
    monkeypatch.setattr(preview.transport_bar, "stop", lambda: stopped.append(True))
    preview.show()
    qapp.processEvents()

    preview._close_button.click()
    qapp.processEvents()

    assert stopped == [True]
    assert preview.isVisible() is False


def test_preview_player_keyboard_shortcuts_control_transport(qapp, monkeypatch):
    win = mw.SubtitleRenderWindow(embedded=False)
    preview = win._preview_window
    toggled: list[bool] = []
    seeks: list[int] = []
    monkeypatch.setattr(
        preview.transport_bar,
        "toggle_play",
        lambda: toggled.append(True),
    )
    monkeypatch.setattr(preview.transport_bar, "seek_relative", seeks.append)

    preview.show()
    preview.activateWindow()
    preview.setFocus()
    qapp.processEvents()
    QTest.keyClick(preview, Qt.Key.Key_Space)
    QTest.keyClick(preview, Qt.Key.Key_Z)
    QTest.keyClick(preview, Qt.Key.Key_X)
    qapp.processEvents()

    assert toggled == [True]
    assert seeks == [-5_000, 5_000]
    assert preview._space_shortcut.context() == Qt.ShortcutContext.WindowShortcut


def test_video_drop_region_becomes_property_panel_after_video_load(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(mw, "unified_player_enabled", lambda: False)
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_probe",
        lambda self, path, label: mw.MediaInfo(
            path=path,
            duration=10.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
            video_width=1920,
            video_height=1080,
            video_fps=60.0,
        ),
    )
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"fake")
    win = mw.SubtitleRenderWindow(embedded=False)

    assert win._video_settings_panel.is_populated() is False

    info = win.load_video(path)

    assert info is not None
    assert win._video_settings_panel.is_populated() is True
    assert win._video_settings_panel._content_layout.itemAt(0).widget() is win._property_panel
    assert win._preview_panel.canvas.has_video_source is True


def test_video_import_syncs_output_size_and_rescales_style(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(mw, "unified_player_enabled", lambda: False)
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    dimensions = {
        "4k.mp4": (3840, 2160),
        "720p.mp4": (1280, 720),
    }

    def fake_probe(self, path, label):
        width, height = dimensions[path.name]
        return mw.MediaInfo(
            path=path,
            duration=10.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
            video_width=width,
            video_height=height,
            video_fps=24.0,
        )

    monkeypatch.setattr(mw.SubtitleRenderWindow, "_probe", fake_probe)
    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    win._set_export_fps_value(120)

    video_4k = tmp_path / "4k.mp4"
    video_720p = tmp_path / "720p.mp4"
    video_4k.write_bytes(b"fake")
    video_720p.write_bytes(b"fake")

    win.load_video(video_4k)

    assert (win._screen_settings.width, win._screen_settings.height) == (3840, 2160)
    assert win._export_fps_value() == 120
    assert win._style.font_reference_height == 2160
    assert win._style.font_size_px == 200
    assert win._style.stroke_width_px == 30
    assert win._style.layout_reference_height == 2160
    assert win._style.line_gap_px == 180
    assert win._preview_panel.canvas._output_width == 3840
    assert win._preview_panel.canvas._output_height == 2160
    assert win._property_panel._layout_schematic._virtual_width == 3840
    assert win._property_panel._layout_schematic._virtual_height == 2160

    win.load_video(video_720p)

    assert (win._screen_settings.width, win._screen_settings.height) == (1280, 720)
    assert win._export_fps_value() == 120
    assert win._style.font_reference_height == 720
    assert win._style.font_size_px == 66
    assert win._style.stroke_width_px == 10
    assert win._style.layout_reference_height == 720
    assert win._style.line_gap_px == 60
    assert win._property_panel._layout_schematic._virtual_width == 1280
    assert win._property_panel._layout_schematic._virtual_height == 720
    assert provider.data["screen"]["width"] == 1280
    assert provider.data["screen"]["height"] == 720

    # Reopening a project must keep its explicitly saved output dimensions.
    win._export_width_spin.setValue(1920)
    win._export_height_spin.setValue(1080)
    win._loading_project = True
    try:
        win.load_video(video_4k)
    finally:
        win._loading_project = False
    assert (win._screen_settings.width, win._screen_settings.height) == (1920, 1080)


def test_legacy_project_font_sizes_use_saved_screen_as_reference(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    win = mw.SubtitleRenderWindow(
        embedded=True,
        settings_provider=FakeSettingsProvider(),
    )
    win._apply_project_data(
        {
            "style": {"font_size_px": 200, "stroke_width_px": 30},
            "screen": {"width": 3840, "height": 2160, "fps": 60, "par": "1:1"},
        }
    )

    assert win._style.font_reference_height == 2160
    win._export_height_spin.setValue(1080)
    assert win._style.font_reference_height == 1080
    assert win._style.font_size_px == 100
    assert win._style.stroke_width_px == 15


def test_main_window_export_screen_controls_update_and_persist(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    win._export_width_spin.setValue(3840)
    win._export_height_spin.setValue(2160)

    assert win._export_width_spin.value() == 3840
    assert win._export_height_spin.value() == 2160
    assert win._export_fps_combo.currentData() == 60
    assert win._preview_panel.canvas._output_width == 3840
    assert win._preview_panel.canvas._output_height == 2160
    assert win._property_panel._layout_schematic._virtual_width == 3840
    assert win._property_panel._layout_schematic._virtual_height == 2160
    assert provider.data["screen"] == {
        "preset_key": "uhd_4k",
        "par": "1:1",
        "width": 3840,
        "height": 2160,
        "fps": 60,
    }

    win._export_width_spin.setValue(4000)
    assert provider.data["screen"]["preset_key"] == "custom"
    assert provider.data["screen"]["width"] == 4000

    win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(120))
    assert win._transport_bar._tick_timer.interval() == 8
    assert win._transport_bar._position_poll_timer.interval() == 8
    assert provider.data["screen"]["fps"] == 120


def test_main_window_native_export_is_hard_disabled(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {"output": {"native_export_enabled": True}}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert isinstance(win._export_native_check, CheckBox)
    assert win._export_native_check.isChecked() is False
    assert win._export_native_check.isEnabled() is False
    assert win._export_native_check.isHidden() is True
    win._save_persisted_state()
    assert provider.data["output"]["native_export_enabled"] is False
    assert provider.data["output"]["gpu_preview_enabled"] is False
    assert provider.data["output"]["gpu_preview_default_version"] == 2
    assert provider.data["output"]["gpu_export_enabled"] is (
        mw.sys.platform == "win32"
    )
    assert provider.data["output"]["gpu_export_default_version"] == 1


def test_main_window_gpu_preferences_are_local_and_persisted(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {"output": {}}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    assert win._gpu_preview_check.text() == "使用 GPU 渲染字幕预览"
    assert win._gpu_export_check.text() == "使用 GPU 渲染字幕导出"
    calls = []
    monkeypatch.setattr(
        win._preview_panel,
        "set_gpu_preview_enabled",
        lambda enabled: calls.append(bool(enabled)) or True,
    )

    win._gpu_preview_check.setChecked(True)
    win._gpu_export_check.setChecked(True)

    assert calls == [True]
    assert provider.data["output"]["gpu_preview_enabled"] is True
    assert provider.data["output"]["gpu_preview_default_version"] == 2
    assert provider.data["output"]["gpu_export_enabled"] is True
    assert provider.data["output"]["gpu_export_default_version"] == 1
    assert win._project_dirty is False


def test_main_window_preview_quality_is_local_and_persisted(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {"output": {"preview_quality": "medium"}}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    calls: list[str] = []
    monkeypatch.setattr(
        win._preview_panel,
        "set_preview_quality",
        lambda quality: calls.append(str(quality)),
    )

    assert win._transport_bar._preview_quality_label.text() == "预览质量"
    assert win._transport_bar.preview_quality() == "medium"

    win._transport_bar._preview_quality_combo.setCurrentIndex(
        win._transport_bar._preview_quality_combo.findData("low")
    )

    assert calls == ["low"]
    assert provider.data["output"]["preview_quality"] == "low"
    assert win._project_dirty is False

    win._loading_project = True
    try:
        win._apply_output_settings({"preview_quality": "high"})
    finally:
        win._loading_project = False

    assert win._transport_bar.preview_quality() == "low"
    assert calls == ["low"]


def test_main_window_migrates_to_g5_default_once(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(mw, "gpu_preview_enabled", lambda: True)
    monkeypatch.setattr(
        mw.PreviewPanel,
        "set_gpu_preview_enabled",
        lambda self, enabled: True,
    )

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {
                "output": {
                    "gpu_preview_enabled": False,
                    "gpu_preview_default_version": 1,
                    "gpu_export_enabled": False,
                }
            }

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._gpu_preview_check.isChecked() is True
    assert win._gpu_export_check.isChecked() is (mw.sys.platform == "win32")
    win._save_persisted_state()
    assert provider.data["output"]["gpu_preview_default_version"] == 2
    assert provider.data["output"]["gpu_export_default_version"] == 1

    win._gpu_preview_check.setChecked(False)
    win._gpu_export_check.setChecked(False)
    assert provider.data["output"]["gpu_preview_enabled"] is False
    assert provider.data["output"]["gpu_export_enabled"] is False
    reopened = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    assert reopened._gpu_preview_check.isChecked() is False
    assert reopened._gpu_export_check.isChecked() is False


def test_render_worker_choice_is_local_and_not_saved_in_project(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {"output": {"render_workers": 16}}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert [
        win._export_render_workers_combo.itemData(index)
        for index in range(win._export_render_workers_combo.count())
    ] == [0, 4, 8, 12, 16]
    assert win._export_render_workers_combo.currentData() == 16
    assert win._project_dirty is False

    win._export_render_workers_combo.setCurrentIndex(
        win._export_render_workers_combo.findData(12)
    )

    assert provider.data["output"]["render_workers"] == 12
    assert win._project_dirty is False
    assert "render_workers" not in win._current_project_data()["output"]


def test_export_encoding_choices_persist_as_local_new_project_defaults(
    qapp, monkeypatch
):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {"output": {}}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    win._export_encoder_combo.setCurrentIndex(
        win._export_encoder_combo.findData(mw.ENCODER_NVENC)
    )
    win._export_codec_combo.setCurrentIndex(
        win._export_codec_combo.findData(mw.CODEC_HEVC)
    )
    win._export_preset_combo.setCurrentIndex(
        win._export_preset_combo.findData("slow")
    )
    win._export_crf_spin.setValue(24)
    win._export_render_workers_combo.setCurrentIndex(
        win._export_render_workers_combo.findData(12)
    )

    assert provider.data["output"]["encoder_mode"] == mw.ENCODER_NVENC
    assert provider.data["output"]["codec"] == mw.CODEC_HEVC
    assert provider.data["output"]["preset"] == "slow"
    assert provider.data["output"]["crf"] == 24
    assert provider.data["output"]["render_workers"] == 12

    reopened = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    assert reopened._export_encoder_combo.currentData() == mw.ENCODER_NVENC
    assert reopened._export_codec_combo.currentData() == mw.CODEC_HEVC
    assert reopened._export_preset_combo.currentData() == "slow"
    assert reopened._export_crf_spin.value() == 24
    assert reopened._export_render_workers_combo.currentData() == 12

    # Existing projects retain their own encoding settings without replacing
    # the last local choices used to seed a future new project.
    win._apply_project_data(
        {
            "output": {
                "encoder_mode": mw.ENCODER_CPU,
                "codec": mw.CODEC_H264,
                "preset": "medium",
                "crf": 18,
            }
        }
    )
    assert win._export_encoder_combo.currentData() == mw.ENCODER_CPU
    assert provider.data["output"]["encoder_mode"] == mw.ENCODER_NVENC
    win._save_persisted_state()
    assert provider.data["output"]["encoder_mode"] == mw.ENCODER_NVENC

    win._set_project_dirty(False)
    win._new_project()
    assert win._export_encoder_combo.currentData() == mw.ENCODER_NVENC
    assert win._export_codec_combo.currentData() == mw.CODEC_HEVC
    assert win._export_preset_combo.currentData() == "slow"
    assert win._export_crf_spin.value() == 24
    assert win._export_render_workers_combo.currentData() == 12


def test_main_window_drops_leaked_project_roles_and_falls_back_to_global_selection(
    qapp, monkeypatch
):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    initial_style = Style(
        custom_style_schemes={
            "图像方案": SubtitleStyleScheme(
                karaoke_colors=KaraokeColors(
                    after=KaraokeColorState(
                        text=PaintFill(
                            mode="image",
                            image_path=r"D:\cover.png",
                            image_scale_pct=150,
                        )
                    )
                )
            )
        }
    )

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {
                "style": style_to_dict(initial_style),
                "selected_scheme_key": "custom:图像方案",
            }

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._property_panel.current_scheme_key() == "global"
    assert "图像方案" not in win._style.custom_style_schemes

    win._save_persisted_state()

    saved_style = style_from_dict(provider.data["style"])
    assert "图像方案" not in saved_style.custom_style_schemes
    assert TITLE_SCHEME_NAME in saved_style.custom_style_schemes
    assert provider.data["selected_scheme_key"] == "global"


def _wheel_event(widget, delta: int = 120) -> QWheelEvent:
    center = QPointF(widget.rect().center())
    global_center = QPointF(widget.mapToGlobal(widget.rect().center()))
    return QWheelEvent(
        center,
        global_center,
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_layout_save_button_is_after_delete_and_confirms_selected_layout(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    panel.set_style(Style(layouts=[LyricsLayout(name="副歌布局")]))
    panel._layout_combo.setCurrentIndex(panel._layout_combo.findData(1))
    requested: list[int] = []
    panel.defaultLayoutSaveRequested.connect(requested.append)
    captured: dict[str, object] = {}

    def confirm(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(pp, "fluent_question", confirm)
    panel._save_layout_btn.click()

    button_layout = panel._save_layout_btn.parentWidget().layout()
    assert button_layout.indexOf(panel._save_layout_btn) == (
        button_layout.indexOf(panel._delete_layout_btn) + 1
    )
    assert requested == [1]
    assert captured["args"][1:3] == (
        "保存布局",
        "是否将当前改动保存到布局“副歌布局”？\n"
        "保存后，新建项目将使用此布局参数。",
    )
    assert captured["kwargs"] == {
        "yes_text": "保存",
        "no_text": "取消",
        "default_cancel": True,
    }


def test_layout_save_cancel_does_not_request_persistence(qapp, monkeypatch):
    panel = PropertyPanel()
    requested: list[int] = []
    panel.defaultLayoutSaveRequested.connect(requested.append)
    monkeypatch.setattr(pp, "fluent_question", lambda *args, **kwargs: False)

    panel._save_current_layout_default()

    assert requested == []


def test_property_panel_layout_selector_edits_selected_layout(qapp):
    panel = PropertyPanel()
    emitted = []
    panel.styleChanged.connect(emitted.append)

    base_count = len(Style().layouts)  # 内置タイトル左上
    panel._on_add_layout()
    assert len(panel.subtitle_style.layouts) == base_count + 1
    assert panel._current_layout_index() == base_count + 1

    # 编辑写入选中的布局，不动默认布局字段
    panel._line_gap_spin.setValue(33)
    panel._letter_spacing_spin.setValue(-6)
    panel._allow_biting_check.setChecked(True)
    panel._ruby_interval_spin.setValue(3)
    panel._ruby_alignment_combo.setCurrentIndex(
        panel._ruby_alignment_combo.findData("center")
    )
    panel._ruby_gap_spin.setValue(-2)
    assert emitted[-1].layouts[base_count].line_gap_px == 33
    assert emitted[-1].layouts[base_count].letter_spacing_px == -6
    assert emitted[-1].layouts[base_count].allow_biting is True
    assert emitted[-1].layouts[base_count].ruby_interval_px == 3
    assert emitted[-1].layouts[base_count].ruby_alignment == "center"
    assert emitted[-1].layouts[base_count].ruby_gap_px == -2
    assert emitted[-1].line_gap_px == Style().line_gap_px
    assert emitted[-1].letter_spacing_px == Style().letter_spacing_px

    # 切回默认布局 → 编辑写回 Style 自身
    panel._layout_combo.setCurrentIndex(0)
    panel._line_gap_spin.setValue(44)
    panel._letter_spacing_spin.setValue(8)
    assert emitted[-1].line_gap_px == 44
    assert emitted[-1].letter_spacing_px == 8
    assert emitted[-1].layouts[base_count].line_gap_px == 33
    assert emitted[-1].layouts[base_count].letter_spacing_px == -6


def test_property_panel_layout_rename_uses_fluent_input(qapp, monkeypatch):
    panel = PropertyPanel()
    panel._on_add_layout()
    current = panel._current_layout_index()
    captured: dict[str, object] = {}

    def get_text(parent, title, label, *, text="", placeholder=""):
        captured.update(
            parent=parent,
            title=title,
            label=label,
            text=text,
            placeholder=placeholder,
        )
        return "主歌词布局", True

    monkeypatch.setattr(pp, "fluent_get_text", get_text)
    panel._on_rename_layout()

    assert captured == {
        "parent": panel,
        "title": "重命名布局",
        "label": "布局名称",
        "text": f"布局 {current}",
        "placeholder": "",
    }
    assert panel.subtitle_style.layouts[current - 1].name == "主歌词布局"


def test_ruby_font_tab_edits_write_ruby_fields(qapp):
    """注音字体页的描边尺寸写入注音字段（全局路径）。"""
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._ruby_stroke_width_spin.setValue(21)
    panel._ruby_stroke2_width_spin.setValue(7)

    assert emitted[-1].ruby_stroke_width_px == 21
    assert emitted[-1].ruby_stroke2_width_px == 7
    # 主文字宽度不受影响
    assert emitted[-1].stroke_width_px == Style().stroke_width_px


def test_apply_main_colors_to_ruby_preserves_font_and_decoration_parameters(qapp):
    """应用主文字配色只复制颜色，不修改字体页描边或方案装饰参数。"""
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel.set_style(
        Style(
            stroke_width_px=20,
            stroke2_width_px=8,
            ruby_stroke_width_px=11,
            ruby_stroke2_width_px=4,
            shadow_offset_x=6,
        )
    )

    panel._apply_main_colors_to_ruby()

    style = emitted[-1]
    assert style.ruby_karaoke_colors is not None
    assert style.ruby_stroke_width_px == 11
    assert style.ruby_stroke2_width_px == 4
    assert style.shadow_offset_x == 6
    assert style.ruby_shadow_offset_x is None


def test_role_navigation_selects_target_without_card_header(qapp):
    """当前角色只由紧凑导航条显示，外层卡片没有重复标题。"""
    panel = PropertyPanel()
    panel.set_roles(["主唱"])
    for i in range(panel._singer_combo.count()):
        if "主唱" in str(panel._singer_combo.itemText(i)):
            panel._singer_combo.setCurrentIndex(i)
            break
    assert "主唱" in panel._singer_combo.currentText()
    assert panel._font_color_section.objectName() == "SubtitlePropertyCard"
    assert not hasattr(panel._font_color_section, "header")

    panel._singer_combo.setCurrentIndex(0)  # 回全局默认
    assert panel._singer_combo.currentText() == "全局默认"


def test_save_button_confirms_builtin_defaults_without_touching_preset_library(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    requested: list[str] = []
    emitted_presets: list[dict] = []
    panel.defaultSchemeSaveRequested.connect(requested.append)
    panel.presetSchemesChanged.connect(emitted_presets.append)
    monkeypatch.setattr(pp, "fluent_question", lambda *args, **kwargs: True)

    panel._save_current_scheme()
    panel.set_current_scheme_key(f"custom:{TITLE_SCHEME_NAME}")
    panel._save_current_scheme()

    assert requested == ["global", f"custom:{TITLE_SCHEME_NAME}"]
    assert emitted_presets == []
    assert panel.preset_schemes == {}


def test_save_button_writes_project_role_to_software_preset_library(
    qapp, monkeypatch
):
    panel = PropertyPanel()
    panel.set_roles(["初音"])
    panel.set_current_scheme_key("custom:初音")
    panel.set_style(
        Style(
            custom_style_schemes={
                TITLE_SCHEME_NAME: SubtitleStyleScheme(),
                "初音": SubtitleStyleScheme(fill_color="#39C5BB"),
            }
        )
    )
    panel.set_preset_schemes(
        {
            "existing": StylePreset(
                name="初音",
                group="VOCALOID",
                scheme=SubtitleStyleScheme(fill_color="#000000"),
                preset_id="existing",
            )
        }
    )
    emitted: list[dict] = []
    panel.presetSchemesChanged.connect(emitted.append)

    class FakeDetailsDialog:
        def __init__(self, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def details(self):
            return "初音", "VOCALOID"

    monkeypatch.setattr(pp, "_StylePresetDetailsDialog", FakeDetailsDialog)
    monkeypatch.setattr(pp.InfoBar, "success", lambda *args, **kwargs: None)

    panel._save_current_scheme()

    assert len(emitted) == 1
    assert list(panel.preset_schemes) == ["existing"]
    saved = next(iter(panel.preset_schemes.values()))
    assert saved.name == "初音"
    assert saved.group == "VOCALOID"
    assert saved.scheme.fill_color == "#39C5BB"


def test_role_combo_width_tracks_longest_option_with_cap(qapp):
    """角色下拉框跟随内容自然宽度，但超长名称不能撑满整张卡片。"""
    panel = PropertyPanel()
    panel.show()
    qapp.processEvents()

    assert panel._singer_combo.width() == 120
    panel.set_roles(["这是一个非常非常长的角色名称用于测试"])
    assert 120 < panel._singer_combo.width() <= 280
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_preview_splitter_defaults_to_4_6_and_remembers_dragged_ratio(qapp, monkeypatch):
    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    win.resize(1600, 900)
    win.show()
    qapp.processEvents()

    left, right = win._preview_splitter.sizes()
    assert left / (left + right) == pytest.approx(0.4, abs=0.03)

    # 模拟拖动 splitter 到 55%，比例应被记忆并在下次构建时恢复
    total = left + right
    win._preview_splitter.setSizes([round(total * 0.55), round(total * 0.45)])
    win._on_preview_splitter_moved(0, 1)
    win._save_persisted_state()
    assert provider.data["preview_splitter_ratio"] == pytest.approx(0.55, abs=0.02)

    win2 = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)
    win2.resize(1600, 900)
    win2.show()
    qapp.processEvents()
    left2, right2 = win2._preview_splitter.sizes()
    assert left2 / (left2 + right2) == pytest.approx(0.55, abs=0.03)
    win.close()
    win2.close()
    win.deleteLater()
    win2.deleteLater()
    qapp.processEvents()
