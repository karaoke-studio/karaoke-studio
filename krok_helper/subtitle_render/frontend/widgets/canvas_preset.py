"""Bidirectional controller linking a canvas-size combo to width/height fields.

预览页「画面尺寸」与导出页「画面与编码」共用：下拉选常用格式时自动填充
宽高；用户手动改宽高（或程序同步宽高）时下拉自动跳到匹配格式或「自定义」。
"""

from __future__ import annotations

from typing import Protocol

from PyQt6.QtWidgets import QComboBox

from krok_helper.subtitle_render.settings.screen import (
    CANVAS_SIZE_CUSTOM_KEY,
    CANVAS_SIZE_PRESETS,
    canvas_size_for_key,
    match_canvas_size_key,
)


class _ValueField(Protocol):
    def value(self) -> int: ...

    def setValue(self, value: int) -> None: ...

    valueChanged: object


class CanvasSizePresetController:
    """Wire a preset combo with width/height fields; both sides stay in sync."""

    def __init__(
        self,
        combo: QComboBox,
        width_field: _ValueField,
        height_field: _ValueField,
        *,
        source_modes: bool = False,
    ) -> None:
        self._combo = combo
        self._width = width_field
        self._height = height_field
        self._applying_preset = False
        self._matching_combo = False

        if source_modes:
            combo.addItem("跟随原视频", userData="source")
            combo.addItem("原视频 2×", userData="source_2x")
        for key, label, width, height in CANVAS_SIZE_PRESETS:
            combo.addItem(label, userData=key)
        combo.addItem("自定义", userData=CANVAS_SIZE_CUSTOM_KEY)
        combo.setToolTip(
            "常用画布格式：选择后自动填充宽高；手动改宽高则变为「自定义」。\n"
            + "；".join(
                f"{label} {width}×{height}"
                for _key, label, width, height in CANVAS_SIZE_PRESETS
            )
        )
        if source_modes:
            combo.setToolTip(
                "跟随原视频使用背景视频宽高；原视频 2× 将宽高各乘 2。\n"
                "奇数尺寸向下对齐为偶数，以满足视频编码要求。\n"
                + combo.toolTip()
            )
        self._select_key(match_canvas_size_key(width_field.value(), height_field.value()))

        combo.currentIndexChanged.connect(self._on_combo_changed)
        width_field.valueChanged.connect(self._sync_combo_from_values)
        height_field.valueChanged.connect(self._sync_combo_from_values)

    def _on_combo_changed(self, _index: int) -> None:
        if self._matching_combo:
            return
        size = canvas_size_for_key(str(self._combo.currentData() or ""))
        if size is None:
            return
        width, height = size
        if width == self._width.value() and height == self._height.value():
            return
        self._applying_preset = True
        try:
            self._width.setValue(width)
            self._height.setValue(height)
        finally:
            self._applying_preset = False

    @property
    def applying_preset(self) -> bool:
        return self._applying_preset

    def set_mode(self, key: str) -> None:
        """Restore a saved selection without reapplying its dimensions."""
        self._select_key(key)

    def _sync_combo_from_values(self, *_args) -> None:
        if self._applying_preset:
            return
        self._select_key(
            match_canvas_size_key(self._width.value(), self._height.value())
        )

    def _select_key(self, key: str) -> None:
        index = self._combo.findData(key)
        if index < 0 or index == self._combo.currentIndex():
            return
        self._matching_combo = True
        try:
            self._combo.setCurrentIndex(index)
        finally:
            self._matching_combo = False
