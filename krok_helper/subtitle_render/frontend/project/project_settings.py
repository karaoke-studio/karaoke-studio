"""Project save and backup settings dialog."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
)

from krok_helper.qfluent_compat import ModelessDialog


class AutoSaveSettingsDialog(ModelessDialog):
    """Project auto-save and history-backup settings."""

    def __init__(
        self,
        enabled: bool,
        interval_minutes: int,
        backup_count: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("项目保存与备份")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(StrongBodyLabel("项目保存与备份", self))
        self.enabled_check = CheckBox("启用字幕项目自动保存", self)
        self.enabled_check.setChecked(enabled)
        layout.addWidget(self.enabled_check)

        interval_row = QHBoxLayout()
        interval_row.addWidget(CaptionLabel("周期保存间隔", self))
        self.interval_spin = FluentSpinBox(self)
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(max(1, min(60, int(interval_minutes))))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)
        layout.addWidget(CaptionLabel("编辑停止 2 秒后会先写一次恢复快照。", self))

        backup_row = QHBoxLayout()
        backup_row.addWidget(CaptionLabel("手动保存历史备份", self))
        self.backup_count_spin = FluentSpinBox(self)
        self.backup_count_spin.setRange(1, 20)
        self.backup_count_spin.setSuffix(" 份")
        self.backup_count_spin.setValue(max(1, min(20, int(backup_count))))
        backup_row.addWidget(self.backup_count_spin)
        backup_row.addStretch(1)
        layout.addLayout(backup_row)
        layout.addWidget(CaptionLabel("放弃未保存修改时，另保留 7 天紧急备份。", self))

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = FluentPushButton("取消", self)
        ok_button = FluentPrimaryPushButton("保存设置", self)
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(ok_button)
        layout.addLayout(button_row)

        self.enabled_check.toggled.connect(self.interval_spin.setEnabled)
        self.interval_spin.setEnabled(enabled)

    def selection(self) -> tuple[bool, int, int]:
        return (
            self.enabled_check.isChecked(),
            self.interval_spin.value(),
            self.backup_count_spin.value(),
        )
