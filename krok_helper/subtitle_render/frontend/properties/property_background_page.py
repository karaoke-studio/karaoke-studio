"""Background-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    ComboBox as FluentComboBox,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PushButton as FluentPushButton,
    RadioButton,
)

from krok_helper.subtitle_render.frontend.properties.property_inputs import (
    DynamicStackedWidget,
    NoWheelSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.property_layout import (
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.frontend.properties.property_widgets import PillSelector
from krok_helper.subtitle_render.screen_settings import SCREEN_FPS_OPTIONS


BACKGROUND_KIND_PAGES = (
    ("video", "视频", "选择背景视频文件..."),
    ("image", "静态图", "选择静态背景图片..."),
    ("image_sequence", "图片序列", "选择图片序列首帧..."),
    ("solid", "纯色", ""),
)


class BackgroundPropertyPageBuilder:
    """Build background controls while state and file actions remain with the host."""

    def __init__(
        self,
        host: Any,
        *,
        fps_options: Iterable[int] = SCREEN_FPS_OPTIONS,
        size_spin_factory: Callable[..., Any] = NoWheelSpinBox,
        fps_combo_factory: Callable[..., Any] = FluentComboBox,
        color_button_factory: Callable[..., Any] | None = None,
        kind_pages: Iterable[tuple[str, str, str]] = BACKGROUND_KIND_PAGES,
    ) -> None:
        self._host = host
        self._fps_options = tuple(int(fps) for fps in fps_options)
        self._size_spin_factory = size_spin_factory
        self._fps_combo_factory = fps_combo_factory
        self._color_button_factory = color_button_factory
        self._kind_pages = tuple(kind_pages)

    def make_source_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("背景素材")
        hint = CaptionLabel(
            "四种背景互斥：左侧选择类型，右侧选择素材；"
            "视频与画面比例不同时等比缩放并自动加黑边。"
        )
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary};")
        layout.addWidget(hint)

        host._background_kind_pill = PillSelector(
            tuple((kind, label) for kind, label, _placeholder in self._kind_pages),
            section,
            vertical=True,
        )
        host._background_kind_pill.changed.connect(
            host._on_background_kind_pill_changed
        )

        host._background_detail_stack = DynamicStackedWidget(section)
        host._background_path_edits: dict[str, FluentLineEdit] = {}
        host._audio_path_edits: list[FluentLineEdit] = []
        for kind, label, placeholder in self._kind_pages:
            host._background_detail_stack.addWidget(
                self.make_detail_page(kind, label, placeholder, section)
            )

        host._image_fit_cover_radio = RadioButton("铺满屏幕", section)
        host._image_fit_contain_radio = RadioButton("加入黑边", section)
        host._image_fit_cover_radio.setToolTip(
            "等比放大铺满画面并居中裁掉超出部分（旧工程默认观感）。"
        )
        host._image_fit_contain_radio.setToolTip(
            "等比缩小完整放入画面，不足处补纯黑边，与视频背景和导出一致。"
        )
        host._image_fit_cover_radio.toggled.connect(
            lambda checked: (
                host.imageFitChanged.emit("cover")
                if checked and not host._syncing
                else None
            )
        )
        host._image_fit_contain_radio.toggled.connect(
            lambda checked: (
                host.imageFitChanged.emit("contain")
                if checked and not host._syncing
                else None
            )
        )
        host._syncing = True
        try:
            host._image_fit_cover_radio.setChecked(True)
        finally:
            host._syncing = False

        fit_row = QHBoxLayout()
        fit_row.setContentsMargins(0, 0, 0, 0)
        fit_row.setSpacing(16)
        fit_row.addWidget(host._image_fit_cover_radio)
        fit_row.addWidget(host._image_fit_contain_radio)
        fit_row.addStretch(1)
        host._image_fit_group = QWidget(section)
        fit_group_layout = QVBoxLayout(host._image_fit_group)
        fit_group_layout.setContentsMargins(0, 0, 0, 0)
        fit_group_layout.setSpacing(4)
        fit_title = CaptionLabel("图片缩放策略", host._image_fit_group)
        themed(fit_title, lambda: f"color: {palette().text_secondary};")
        fit_group_layout.addWidget(fit_title)
        fit_group_layout.addLayout(fit_row)
        host._image_fit_group.setVisible(False)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        columns.addWidget(
            host._background_kind_pill,
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        right.addWidget(host._background_detail_stack, 1)
        right.addWidget(host._image_fit_group)
        right.addStretch(1)
        columns.addLayout(right, 1)
        layout.addLayout(columns)

        clear_row = QHBoxLayout()
        clear_row.setContentsMargins(0, 0, 0, 0)
        clear_row.addStretch(1)
        clear_button = FluentPushButton("清除背景（恢复纯色黑）", section)
        clear_button.setMinimumHeight(30)
        clear_button.clicked.connect(host.backgroundClearRequested.emit)
        clear_row.addWidget(clear_button)
        layout.addLayout(clear_row)
        return section

    def make_detail_page(
        self,
        kind: str,
        label: str,
        placeholder: str,
        section: QWidget,
    ) -> QWidget:
        """Build the material and optional audio controls for one source kind."""
        host = self._host
        page = QWidget(section)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        if kind == "solid":
            if self._color_button_factory is None:
                raise RuntimeError("color_button_factory is required for solid backgrounds")
            host._solid_color_btn = self._color_button_factory("#000000", page)
            host._solid_color_btn.clicked.connect(host._choose_solid_color_dialog)
            host._solid_color_btn.screenPickRequested.connect(
                lambda: host._begin_screen_color_pick(
                    host._solid_color_btn,
                    host._apply_solid_color,
                )
            )
            host._solid_color_btn.colorEntered.connect(host._apply_solid_color)
            page_layout.addWidget(
                property_field(
                    "背景颜色（点击色块输入色值）",
                    host._solid_color_btn,
                )
            )
        else:
            path_row = QHBoxLayout()
            path_row.setContentsMargins(0, 0, 0, 0)
            path_row.setSpacing(8)
            path_edit = FluentLineEdit(page)
            path_edit.setPlaceholderText(placeholder)
            path_edit.setReadOnly(True)
            path_row.addWidget(path_edit, 1)
            browse_button = FluentPushButton("浏览...", page)
            browse_button.setMinimumHeight(30)
            browse_button.setIcon(FIF.FOLDER)
            browse_button.clicked.connect(
                lambda _checked=False, source_kind=kind: (
                    host.backgroundBrowseRequested.emit(source_kind)
                )
            )
            path_row.addWidget(browse_button)
            page_layout.addLayout(path_row)
            host._background_path_edits[kind] = path_edit

        if kind != "video":
            audio_label = CaptionLabel("独立音频（非视频背景的配乐）", page)
            themed(audio_label, lambda: f"color: {palette().text_secondary};")
            page_layout.addWidget(audio_label)
            audio_row = QHBoxLayout()
            audio_row.setContentsMargins(0, 0, 0, 0)
            audio_row.setSpacing(8)
            audio_edit = FluentLineEdit(page)
            audio_edit.setPlaceholderText("未设置（点击右侧按钮选择音频）")
            audio_edit.setReadOnly(True)
            audio_row.addWidget(audio_edit, 1)
            audio_browse = FluentPushButton("浏览...", page)
            audio_browse.setMinimumHeight(30)
            audio_browse.setIcon(FIF.MUSIC)
            audio_browse.clicked.connect(host.audioBrowseRequested.emit)
            audio_row.addWidget(audio_browse)
            audio_remove = FluentPushButton("移除", page)
            audio_remove.setMinimumHeight(30)
            audio_remove.clicked.connect(host.audioClearRequested.emit)
            audio_row.addWidget(audio_remove)
            page_layout.addLayout(audio_row)
            host._audio_path_edits.append(audio_edit)
        return page

    def make_screen_size_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("画面尺寸")
        hint = CaptionLabel("宽度 / 高度 / 帧率与预览页「画面」及导出页设置双向联动。")
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary};")
        layout.addWidget(hint)

        host._screen_size_width_spin = self._size_spin_factory(section)
        host._screen_size_width_spin.setRange(160, 7680)
        host._screen_size_width_spin.setValue(1920)
        host._screen_size_width_spin.setSingleStep(2)
        host._screen_size_width_spin.setKeyboardTracking(False)

        host._screen_size_height_spin = self._size_spin_factory(section)
        host._screen_size_height_spin.setRange(90, 4320)
        host._screen_size_height_spin.setValue(1080)
        host._screen_size_height_spin.setSingleStep(2)
        host._screen_size_height_spin.setKeyboardTracking(False)

        host._screen_size_fps_combo = self._fps_combo_factory(section)
        for fps in self._fps_options:
            host._screen_size_fps_combo.addItem(f"{fps} fps", userData=fps)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        grid.addWidget(property_field("宽度", host._screen_size_width_spin), 0, 0)
        grid.addWidget(property_field("高度", host._screen_size_height_spin), 0, 1)
        grid.addWidget(property_field("帧率", host._screen_size_fps_combo), 1, 0)
        layout.addLayout(grid)

        host._screen_size_width_spin.valueChanged.connect(
            host._on_panel_screen_size_changed
        )
        host._screen_size_height_spin.valueChanged.connect(
            host._on_panel_screen_size_changed
        )
        host._screen_size_fps_combo.currentIndexChanged.connect(
            host._on_panel_screen_size_changed
        )
        return section
