"""波形对齐导出完成后的交接弹窗（「去下一步」/「留在本页」）。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout, QWidget

# 勾选框与「取消」必须用 qfluentwidgets 的实现 —— ``gui_qt`` 里这两个名字是
# ``CheckBox as QCheckBox`` / ``PushButton as QPushButton`` 的别名。换成
# PyQt 原生控件会和同一行的 ``PrimaryPushButton``（Fluent）高度对不齐，
# 「取消」看着比「确认」矮一截。
from qfluentwidgets import (
    BodyLabel,
    CheckBox as QCheckBox,
    PrimaryPushButton,
    PushButton as QPushButton,
    TitleLabel,
)

__all__ = ["AlignmentHandoffDialog"]


class AlignmentHandoffDialog(QDialog):
    """Choose which completed alignment assets to prefill into later modules."""

    def __init__(
        self,
        *,
        is_video_target: bool,
        output_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        output_label = "对齐视频" if is_video_target else "对齐音频"
        self.setWindowTitle(f"{output_label}导出完成")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = TitleLabel(f"{output_label}导出完成", self)
        layout.addWidget(title)

        summary = BodyLabel(
            f"文件已保存到：\n{output_path}\n\n请选择要自动填入的后续模块素材：",
            self,
        )
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)

        subtitle_text = (
            "将导出的对齐视频作为字幕渲染背景素材"
            if is_video_target
            else "将用于对齐的视频作为字幕渲染背景素材"
        )
        hires_text = (
            "将用于对齐的原唱音源作为 Hi-Res 混流原唱音源"
            if is_video_target
            else "将导出的对齐音频作为 Hi-Res 混流原唱音源"
        )
        self.subtitle_check = QCheckBox(subtitle_text, self)
        self.hires_check = QCheckBox(hires_text, self)
        self.subtitle_check.setChecked(True)
        self.hires_check.setChecked(True)
        layout.addWidget(self.subtitle_check)
        layout.addWidget(self.hires_check)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        self.yesButton = PrimaryPushButton("确认", self)
        self.cancelButton = QPushButton("取消", self)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.clicked.connect(self.reject)
        button_layout.addWidget(self.yesButton, 1)
        button_layout.addWidget(self.cancelButton, 1)
        layout.addLayout(button_layout)
        self.setMinimumWidth(620)

    def selections(self) -> tuple[bool, bool]:
        return self.subtitle_check.isChecked(), self.hires_check.isChecked()
