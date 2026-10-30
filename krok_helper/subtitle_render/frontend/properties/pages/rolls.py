"""前黑/后黑属性页构建（与背景页同款的胶囊式切换 + 每段独立参数组）。

参数编号对应需求：0 总开关、1 背景素材、2 填充风格、3 独立音频、
4 持续时长（0=关闭）、5 自动偏移非标题时间戳（仅前黑）、6/7 与主视频
过渡时长/风格、8/9 自我过渡时长/风格。两段（前黑/后黑）参数完全不联动。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from qfluentwidgets import (
    CaptionLabel,
    ComboBox as FluentComboBox,
    DoubleSpinBox as FluentDoubleSpinBox,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PushButton as FluentPushButton,
    SpinBox as FluentSpinBox,
)

from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_FIT,
    ROLL_FILL_LABELS,
    ROLL_FILL_MODES,
    ROLL_MAIN_TRANSITION_CROSSFADE,
    ROLL_MAIN_TRANSITION_LABELS,
    ROLL_SELF_TRANSITION_LABELS,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed

_SEGMENT_KEYS = ("pre", "post")
_SEGMENT_LABELS = {"pre": "前黑", "post": "后黑"}

_ROLL_KIND_PAGES = (
    ("video", "视频", "选择前黑/后黑视频文件..."),
    ("image_sequence", "图片序列", "选择图片序列首帧..."),
    ("image", "一图流", "选择图片文件（含 GIF / WebP 动图）..."),
    ("solid", "纯色", ""),
)

_TRANSITION_MS_RANGE = 1_800_000
_DURATION_RANGE_S = 36_000.0


class RollsPropertyPageBuilder:
    """构建前黑/后黑两段卡片；状态与文件动作仍由宿主持有。"""

    def __init__(
        self,
        host: Any,
        *,
        segment_pages: Iterable[tuple[str, str, str]] = _ROLL_KIND_PAGES,
        color_button_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._host = host
        self._kind_pages = tuple(segment_pages)
        self._color_button_factory = color_button_factory

    # ------------------------------------------------------------------ sections

    def make_rolls_section(self) -> Any:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout

        from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
            DynamicStackedWidget,
        )
        from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
            PillSelector,
        )

        host = self._host
        section, layout = property_section("前黑 / 后黑")
        hint = CaptionLabel(
            "前黑插入片头、后黑追加片尾，两段参数互不联动；"
            "过渡只作用于背景画面，不影响字幕渲染。"
        )
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary};")
        layout.addWidget(hint)

        host._roll_segment_pill = PillSelector(
            tuple((key, _SEGMENT_LABELS[key]) for key in _SEGMENT_KEYS),
            section,
        )
        host._roll_segment_stack = DynamicStackedWidget(section)
        for key in _SEGMENT_KEYS:
            host._roll_segment_stack.addWidget(
                self._make_segment_page(key, section)
            )
        host._roll_segment_pill.changed.connect(host._on_roll_segment_pill_changed)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        columns.addWidget(host._roll_segment_pill, 0, Qt.AlignmentFlag.AlignTop)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        right.addWidget(host._roll_segment_stack, 1)
        right.addStretch(1)
        columns.addLayout(right, 1)
        layout.addLayout(columns)
        return section

    def _make_segment_page(self, segment: str, section: Any) -> Any:
        from PyQt6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

        from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
            ToggleSwitch,
        )

        host = self._host
        label = _SEGMENT_LABELS[segment]
        page = QWidget(section)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        # 0 总开关：卡片头开关，关闭即该段完全不参与计算。
        seg_section, seg_layout = property_section(
            f"{label}设置", switch=True
        )
        host._roll_enabled_switches[segment] = seg_section.header_switch
        seg_section.header_switch.toggled.connect(
            lambda checked, key=segment: (
                host.rollConfigChanged.emit(key, {"enabled": bool(checked)})
                if not host._syncing
                else None
            )
        )

        # 1 背景素材：类型胶囊 + 详情页（路径 / 纯色）。
        kind_pill_holder = QWidget(seg_section)
        kind_layout = QVBoxLayout(kind_pill_holder)
        kind_layout.setContentsMargins(0, 0, 0, 0)
        kind_layout.setSpacing(6)
        kind_caption = CaptionLabel("背景素材类型", kind_pill_holder)
        themed(kind_caption, lambda: f"color: {palette().text_secondary};")
        kind_layout.addWidget(kind_caption)

        from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
            PillSelector,
        )
        from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
            DynamicStackedWidget,
        )

        kind_pill = PillSelector(
            tuple((kind, text) for kind, text, _ph in self._kind_pages),
            kind_pill_holder,
        )
        kind_pill.changed.connect(
            lambda kind, key=segment: host._on_roll_kind_pill_changed(key, kind)
        )
        host._roll_kind_pills[segment] = kind_pill
        kind_layout.addWidget(kind_pill)

        detail_stack = DynamicStackedWidget(kind_pill_holder)
        host._roll_kind_stacks[segment] = detail_stack
        host._roll_path_edits[segment] = {}
        for kind, _text, placeholder in self._kind_pages:
            detail_stack.addWidget(
                self._make_kind_detail(segment, kind, placeholder, kind_pill_holder)
            )
        kind_layout.addWidget(detail_stack)
        seg_layout.addWidget(kind_pill_holder)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)

        # 2 填充风格。
        fill_combo = FluentComboBox(seg_section)
        for mode in ROLL_FILL_MODES:
            fill_combo.addItem(ROLL_FILL_LABELS[mode], userData=mode)
        fill_combo.setCurrentIndex(max(fill_combo.findData(ROLL_FILL_FIT), 0))
        fill_combo.currentIndexChanged.connect(
            lambda _index, key=segment, combo=fill_combo: (
                host.rollConfigChanged.emit(key, {"fill": str(combo.currentData())})
                if not host._syncing
                else None
            )
        )
        host._roll_fill_combos[segment] = fill_combo
        grid.addWidget(property_field("背景填充风格", fill_combo), 0, 0)

        # 4 持续时长（秒，可填 0 = 关闭）。默认 3s，与用户口径一致。
        duration_spin = FluentDoubleSpinBox(seg_section)
        duration_spin.setRange(0.0, _DURATION_RANGE_S)
        duration_spin.setDecimals(2)
        duration_spin.setValue(3.0)
        duration_spin.setSuffix(" s")
        duration_spin.setToolTip("默认 3 秒；0 = 该段关闭；选视频/图片序列时取素材时长，可手动截断。")
        duration_spin.setKeyboardTracking(False)
        duration_spin.valueChanged.connect(
            lambda value, key=segment: (
                host.rollConfigChanged.emit(key, {"duration_ms": int(round(value * 1000))})
                if not host._syncing
                else None
            )
        )
        host._roll_duration_spins[segment] = duration_spin
        grid.addWidget(property_field("持续时长（0 = 关闭）", duration_spin), 0, 1)

        # 6 与主视频过渡时长（ms；默认 300ms 动画）。
        main_ms = FluentSpinBox(seg_section)
        main_ms.setRange(0, _TRANSITION_MS_RANGE)
        main_ms.setValue(300)
        main_ms.setSuffix(" ms")
        main_ms.setKeyboardTracking(False)
        main_ms.valueChanged.connect(
            lambda value, key=segment: (
                host.rollConfigChanged.emit(key, {"main_transition_ms": int(value)})
                if not host._syncing
                else None
            )
        )
        host._roll_main_transition_spins[segment] = main_ms
        grid.addWidget(property_field("与主视频过渡时长（0 = 不过渡）", main_ms), 1, 0)

        # 7 与主视频过渡风格。默认交叉溶解。
        main_style = FluentComboBox(seg_section)
        for value, text in ROLL_MAIN_TRANSITION_LABELS.items():
            main_style.addItem(text, userData=value)
        main_style.setCurrentIndex(
            max(main_style.findData(ROLL_MAIN_TRANSITION_CROSSFADE), 0)
        )
        main_style.currentIndexChanged.connect(
            lambda _index, key=segment, combo=main_style: (
                host.rollConfigChanged.emit(
                    key, {"main_transition_style": str(combo.currentData())}
                )
                if not host._syncing
                else None
            )
        )
        host._roll_main_transition_combos[segment] = main_style
        grid.addWidget(property_field("与主视频过渡风格", main_style), 1, 1)

        # 8 自我过渡时长（默认 300ms 动画）。
        self_ms = FluentSpinBox(seg_section)
        self_ms.setRange(0, _TRANSITION_MS_RANGE)
        self_ms.setValue(300)
        self_ms.setSuffix(" ms")
        self_ms.setKeyboardTracking(False)
        self_ms.valueChanged.connect(
            lambda value, key=segment: (
                host.rollConfigChanged.emit(key, {"self_transition_ms": int(value)})
                if not host._syncing
                else None
            )
        )
        host._roll_self_transition_spins[segment] = self_ms
        grid.addWidget(property_field("自我过渡时长（0 = 不过渡）", self_ms), 2, 0)

        # 9 自我过渡风格（黑场 / 白场）。
        self_style = FluentComboBox(seg_section)
        for value, text in ROLL_SELF_TRANSITION_LABELS.items():
            self_style.addItem(text, userData=value)
        self_style.currentIndexChanged.connect(
            lambda _index, key=segment, combo=self_style: (
                host.rollConfigChanged.emit(
                    key, {"self_transition_style": str(combo.currentData())}
                )
                if not host._syncing
                else None
            )
        )
        host._roll_self_transition_combos[segment] = self_style
        grid.addWidget(property_field("自我过渡风格", self_style), 2, 1)
        seg_layout.addLayout(grid)

        # 3 独立音频（可留空，填写后取代背景素材声音）。
        audio_widget = self._make_audio_row(segment, seg_section)
        seg_layout.addWidget(audio_widget)

        # 5 自动偏移非标题时间戳（仅前黑）。
        if segment == "pre":
            auto_switch = ToggleSwitch(seg_section)
            auto_switch.setChecked(True)
            auto_switch.toggled.connect(
                lambda checked: (
                    host.rollConfigChanged.emit(
                        "pre", {"auto_offset_subtitles": bool(checked)}
                    )
                    if not host._syncing
                    else None
                )
            )
            host._roll_auto_offset_switch = auto_switch
            seg_layout.addWidget(
                property_field(
                    "自动偏移非标题时间戳（主轴/副轴整体 + 前黑时长，含手调上/离屏）",
                    auto_switch,
                )
            )

        page_layout.addWidget(seg_section)
        page_layout.addStretch(1)
        return page

    def _make_kind_detail(
        self, segment: str, kind: str, placeholder: str, parent: Any
    ) -> Any:
        from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

        host = self._host
        page = QWidget(parent)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if kind == "solid":
            if self._color_button_factory is None:
                raise RuntimeError("color_button_factory is required for solid rolls")
            color_btn = self._color_button_factory("#000000", page)
            color_btn.clicked.connect(
                lambda _checked=False, key=segment: host.rollSolidColorDialogRequested.emit(key)
            )
            color_btn.colorEntered.connect(
                lambda color, key=segment: host.rollConfigChanged.emit(
                    key, {"solid_color": str(color)}
                )
            )
            host._roll_solid_buttons[segment] = color_btn
            row.addWidget(property_field("背景颜色（点击色块输入色值）", color_btn))
            row.addStretch(1)
            page_layout.addLayout(row)
            return page
        path_edit = FluentLineEdit(page)
        path_edit.setPlaceholderText(placeholder)
        path_edit.setReadOnly(True)
        row.addWidget(path_edit, 1)
        browse = FluentPushButton("浏览...", page)
        browse.setMinimumHeight(30)
        browse.setIcon(FIF.FOLDER)
        browse.clicked.connect(
            lambda _checked=False, key=segment, source_kind=kind: (
                host.rollBrowseRequested.emit(key, source_kind)
            )
        )
        row.addWidget(browse)
        page_layout.addLayout(row)
        hint = CaptionLabel("", page)
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary};")
        hint.setVisible(False)
        page_layout.addWidget(hint)
        host._roll_path_edits[segment][kind] = path_edit
        host._roll_duration_hints[segment][kind] = hint
        return page

    def _make_audio_row(self, segment: str, parent: Any) -> Any:
        from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

        host = self._host
        wrapper = QWidget(parent)
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(4)
        audio_label = CaptionLabel("独立音频（填写后取代背景素材的声音）", wrapper)
        themed(audio_label, lambda: f"color: {palette().text_secondary};")
        wrapper_layout.addWidget(audio_label)
        audio_edit = FluentLineEdit(wrapper)
        audio_edit.setPlaceholderText("未设置（点击右侧按钮选择音频）")
        audio_edit.setReadOnly(True)
        audio_browse = FluentPushButton("浏览...", wrapper)
        audio_browse.setMinimumHeight(30)
        audio_browse.setIcon(FIF.MUSIC)
        audio_browse.clicked.connect(
            lambda _checked=False, key=segment: host.rollAudioBrowseRequested.emit(key)
        )
        audio_remove = FluentPushButton("移除", wrapper)
        audio_remove.setMinimumHeight(30)
        audio_remove.clicked.connect(
            lambda _checked=False, key=segment: host.rollAudioClearRequested.emit(key)
        )
        audio_row = QHBoxLayout()
        audio_row.setContentsMargins(0, 0, 0, 0)
        audio_row.setSpacing(8)
        audio_row.addWidget(audio_edit, 1)
        audio_row.addWidget(audio_browse)
        audio_row.addWidget(audio_remove)
        wrapper_layout.addLayout(audio_row)
        host._roll_audio_edits[segment] = audio_edit
        return wrapper
