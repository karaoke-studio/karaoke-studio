"""前黑/后黑新建段默认值（app 级偏好）对话框。"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    DoubleSpinBox as FluentDoubleSpinBox,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_LABELS,
    ROLL_FILL_MODES,
    ROLL_MAIN_TRANSITION_LABELS,
    ROLL_MAIN_TRANSITION_STYLES,
    ROLL_SELF_TRANSITION_LABELS,
    ROLL_SELF_TRANSITION_STYLES,
    roll_defaults_from_dict,
)


class RollDefaultsDialog(ModelessDialog):
    """「前黑/后黑默认值…」：只保存新建段默认，不覆盖当前工程段。"""

    def __init__(
        self,
        defaults: object,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("前黑 / 后黑默认值")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(480)
        normalized = roll_defaults_from_dict(defaults)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(StrongBodyLabel("新建前黑 / 后黑的默认参数", self))
        hint = CaptionLabel(
            "只影响之后新建的段；当前工程里已配置的前黑/后黑保持不变。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        duration_row = QHBoxLayout()
        duration_row.addWidget(CaptionLabel("持续时长", self))
        self.duration_spin = FluentDoubleSpinBox(self)
        self.duration_spin.setRange(0.0, 36_000.0)
        self.duration_spin.setDecimals(2)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(normalized["duration_ms"] / 1000.0)
        duration_row.addWidget(self.duration_spin)
        duration_row.addStretch(1)
        layout.addLayout(duration_row)

        fill_row = QHBoxLayout()
        fill_row.addWidget(CaptionLabel("背景填充风格", self))
        self.fill_combo = FluentComboBox(self)
        for value in ROLL_FILL_MODES:
            self.fill_combo.addItem(ROLL_FILL_LABELS[value], userData=value)
        self.fill_combo.setCurrentIndex(
            max(self.fill_combo.findData(normalized["fill"]), 0)
        )
        fill_row.addWidget(self.fill_combo)
        fill_row.addStretch(1)
        layout.addLayout(fill_row)

        main_style_row = QHBoxLayout()
        main_style_row.addWidget(CaptionLabel("与主视频过渡风格", self))
        self.main_style_combo = FluentComboBox(self)
        for value in ROLL_MAIN_TRANSITION_STYLES:
            self.main_style_combo.addItem(
                ROLL_MAIN_TRANSITION_LABELS[value], userData=value
            )
        self.main_style_combo.setCurrentIndex(
            max(self.main_style_combo.findData(normalized["main_transition_style"]), 0)
        )
        main_style_row.addWidget(self.main_style_combo)
        main_style_row.addStretch(1)
        layout.addLayout(main_style_row)

        main_ms_row = QHBoxLayout()
        main_ms_row.addWidget(CaptionLabel("与主视频过渡时长", self))
        self.main_ms_spin = FluentSpinBox(self)
        self.main_ms_spin.setRange(0, 1_800_000)
        self.main_ms_spin.setSuffix(" ms")
        self.main_ms_spin.setValue(normalized["main_transition_ms"])
        main_ms_row.addWidget(self.main_ms_spin)
        main_ms_row.addStretch(1)
        layout.addLayout(main_ms_row)

        self_ms_row = QHBoxLayout()
        self_ms_row.addWidget(CaptionLabel("自我过渡风格", self))
        self.self_style_combo = FluentComboBox(self)
        for value in ROLL_SELF_TRANSITION_STYLES:
            self.self_style_combo.addItem(
                ROLL_SELF_TRANSITION_LABELS[value], userData=value
            )
        self.self_style_combo.setCurrentIndex(
            max(self.self_style_combo.findData(normalized["self_transition_style"]), 0)
        )
        self_ms_row.addWidget(self.self_style_combo)
        self_ms_row.addStretch(1)
        layout.addLayout(self_ms_row)

        self_ms_spin_row = QHBoxLayout()
        self_ms_spin_row.addWidget(CaptionLabel("自我过渡时长", self))
        self.self_ms_spin = FluentSpinBox(self)
        self.self_ms_spin.setRange(0, 1_800_000)
        self.self_ms_spin.setSuffix(" ms")
        self.self_ms_spin.setValue(normalized["self_transition_ms"])
        self_ms_spin_row.addWidget(self.self_ms_spin)
        self_ms_spin_row.addStretch(1)
        layout.addLayout(self_ms_spin_row)

        self.auto_offset_check = CheckBox("自动偏移非标题时间戳（仅前黑）", self)
        self.auto_offset_check.setChecked(bool(normalized["auto_offset_subtitles"]))
        layout.addWidget(self.auto_offset_check)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = FluentPushButton("取消", self)
        ok_button = FluentPrimaryPushButton("保存默认值", self)
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(ok_button)
        layout.addLayout(button_row)

    def selection(self) -> dict:
        return roll_defaults_from_dict(
            {
                "duration_ms": int(round(self.duration_spin.value() * 1000)),
                "fill": self.fill_combo.currentData(),
                "main_transition_style": self.main_style_combo.currentData(),
                "main_transition_ms": int(self.main_ms_spin.value()),
                "self_transition_style": self.self_style_combo.currentData(),
                "self_transition_ms": int(self.self_ms_spin.value()),
                "auto_offset_subtitles": self.auto_offset_check.isChecked(),
            }
        )
