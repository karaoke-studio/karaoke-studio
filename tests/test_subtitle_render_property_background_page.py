"""Focused construction contracts for the subtitle background-property page."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.properties.pages.background import (
    BackgroundPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.screen_changes = 0

    def _on_panel_screen_size_changed(self, *_args) -> None:
        self.screen_changes += 1


class _SourceHost(QWidget):
    backgroundBrowseRequested = Signal(str)
    backgroundClearRequested = Signal()
    audioBrowseRequested = Signal()
    audioClearRequested = Signal()
    imageFitChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._syncing = False
        self.solid_colors: list[object] = []
        self.picked: list[tuple[object, object]] = []

    def _on_background_kind_pill_changed(self, _kind: str) -> None:
        pass

    def _choose_solid_color_dialog(self) -> None:
        pass

    def _begin_screen_color_pick(self, button, callback) -> None:
        self.picked.append((button, callback))

    def _apply_solid_color(self, color: object) -> None:
        self.solid_colors.append(color)


class _ColorButton(QWidget):
    clicked = Signal()
    screenPickRequested = Signal()
    colorEntered = Signal(object)

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.color = color


def test_background_screen_size_builder_preserves_control_contracts(qapp) -> None:
    host = _Host()
    section = BackgroundPropertyPageBuilder(host).make_screen_size_section()

    assert section.header.text() == "画面尺寸"
    assert host._screen_size_width_spin.minimum() == 160
    assert host._screen_size_width_spin.maximum() == 7680
    assert host._screen_size_width_spin.value() == 1920
    assert host._screen_size_width_spin.singleStep() == 2
    assert not host._screen_size_width_spin.keyboardTracking()
    assert host._screen_size_height_spin.minimum() == 90
    assert host._screen_size_height_spin.maximum() == 4320
    assert host._screen_size_height_spin.value() == 1080
    # 4.4.0 起帧率下拉在 60/120 之前插入「跟随原视频 / 原视频 2×」、之后
    # 追加「自定义」，共 5 项；默认停在 60 fps。
    assert host._screen_size_fps_combo.count() == 5
    assert [
        host._screen_size_fps_combo.itemText(index) for index in range(5)
    ] == ["跟随原视频", "原视频 2×", "60 fps", "120 fps", "自定义"]
    assert [
        host._screen_size_fps_combo.itemData(index) for index in range(5)
    ] == ["source", "source_2x", 60, 120, "custom"]
    assert host._screen_size_fps_combo.currentData() == 60


def test_background_screen_size_builder_routes_each_change_to_host(qapp) -> None:
    host = _Host()
    BackgroundPropertyPageBuilder(host).make_screen_size_section()

    host._screen_size_width_spin.setValue(1280)
    host._screen_size_height_spin.setValue(720)
    host._screen_size_fps_combo.setCurrentIndex(1)

    # 宽、高各通知 1 次；预置下拉跟着宽高自动跳格式（1920×1080 →「自定义」
    # → 1280×720 命中 HDV 720）再通知 2 次；帧率切换 1 次。
    assert host.screen_changes == 5


def test_background_screen_size_preset_combo_fills_values(qapp) -> None:
    host = _Host()
    BackgroundPropertyPageBuilder(host).make_screen_size_section()
    combo = host._screen_size_preset_combo

    assert combo.currentData() == "1080p"
    combo.setCurrentIndex(combo.findData("4k"))

    assert host._screen_size_width_spin.value() == 3840
    assert host._screen_size_height_spin.value() == 2160
    assert combo.currentData() == "4k"
    # 下拉本身 1 次 + 预置回填宽、高各 1 次。
    assert host.screen_changes == 3


def test_background_screen_size_preset_combo_jumps_to_custom_on_edit(qapp) -> None:
    host = _Host()
    BackgroundPropertyPageBuilder(host).make_screen_size_section()
    combo = host._screen_size_preset_combo

    host._screen_size_width_spin.setValue(1234)

    assert combo.currentData() == "custom"
    # 选回「自定义」不应改动宽高。
    host._screen_size_height_spin.setValue(567)
    combo.setCurrentIndex(combo.findData("custom"))
    assert (host._screen_size_width_spin.value(), host._screen_size_height_spin.value()) == (
        1234,
        567,
    )


def test_background_source_builder_preserves_material_and_audio_contracts(qapp) -> None:
    host = _SourceHost()
    builder = BackgroundPropertyPageBuilder(
        host,
        color_button_factory=_ColorButton,
    )

    section = builder.make_source_section()

    assert section.header.text() == "背景素材"
    assert host._background_kind_pill.current() == "video"
    assert host._background_detail_stack.count() == 4
    assert set(host._background_path_edits) == {"video", "image", "image_sequence"}
    assert len(host._audio_path_edits) == 3
    assert host._image_fit_cover_radio.isChecked()
    assert host._image_fit_group.isHidden()
    assert host._background_path_edits["image"].placeholderText() == (
        "选择静态背景图片..."
    )


def test_background_source_builder_routes_fit_and_solid_color_signals(qapp) -> None:
    host = _SourceHost()
    fit_changes: list[str] = []
    host.imageFitChanged.connect(fit_changes.append)
    builder = BackgroundPropertyPageBuilder(
        host,
        color_button_factory=_ColorButton,
    )
    builder.make_source_section()

    host._image_fit_contain_radio.setChecked(True)
    host._solid_color_btn.colorEntered.emit("#305070")
    host._solid_color_btn.screenPickRequested.emit()

    assert fit_changes == ["contain"]
    assert host.solid_colors == ["#305070"]
    assert host.picked == [(host._solid_color_btn, host._apply_solid_color)]
