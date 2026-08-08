"""分离产物转交给后续模块。

音频分离跑完一整批以后，伴奏类的产物（伴奏 / 和声伴奏）正好是第 6 步 Hi-Res 混流
要的素材。这里只负责问用户「要不要送过去、送哪几条」，真正落地由工作流上下文
（``gui_qt`` 那边的 ``accept_separated_accompaniment``）完成——分离包不反向依赖
主窗口。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, PushButton, SubtitleLabel

from krok_helper.audio_processing.separation.states import TaskType

#: 哪些任务的产物算「伴奏」，以及在对话框里怎么称呼。
ACCOMPANIMENT_TASKS: dict[TaskType, str] = {
    TaskType.INSTRUMENTAL: "伴奏",
    TaskType.HARMONY: "和声伴奏",
}


def collect_accompaniments(results) -> list[tuple[str, Path]]:
    """从一批任务结果里挑出伴奏类产物，返回 ``(名称, 路径)``。

    失败的任务没有产物；文件可能已被用户移走，落地前再确认一次存在。
    """
    picked: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for result in results:
        label = ACCOMPANIMENT_TASKS.get(result.task)
        if label is None or result.failed:
            continue
        for item in result.files:
            path = Path(item.path)
            key = str(path).lower()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            picked.append((item.label or label, path))
    return picked


class AccompanimentHandoffDialog(QDialog):
    """问用户把哪几条伴奏放进 Hi-Res 混流。

    即使只有一条也用勾选框：两条时必须能选，一条时保持同一套交互，用户不用重新
    认一遍界面。
    """

    def __init__(
        self,
        candidates: list[tuple[str, Path]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._checks: list[tuple[QCheckBox, Path]] = []

        self.setWindowTitle("音频分离完成")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(SubtitleLabel("音频分离完成", self))

        summary = BodyLabel(
            "已生成伴奏音频。勾选要放进第 6 步 Hi-Res 混流的伴奏："
            if len(candidates) > 1
            else "已生成伴奏音频。是否放进第 6 步 Hi-Res 混流？",
            self,
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        for label, path in candidates:
            check = QCheckBox(f"{label}：{path.name}", self)
            check.setChecked(True)
            check.setToolTip(str(path))
            layout.addWidget(check)
            hint = CaptionLabel(str(path), self)
            hint.setWordWrap(True)
            hint.setIndent(24)
            layout.addWidget(hint)
            self._checks.append((check, path))

        if len(candidates) > 1:
            layout.addWidget(
                CaptionLabel("放入多条伴奏时，Hi-Res 会为每条各生成一个混流视频。", self)
            )

        layout.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        self.cancelButton = PushButton("暂不放入", self)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton = PrimaryPushButton("放入并前往 Hi-Res 混流", self)
        self.yesButton.clicked.connect(self.accept)
        self.yesButton.setDefault(True)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.yesButton)
        layout.addLayout(buttons)

        for check, _path in self._checks:
            check.toggled.connect(self._sync_confirm_enabled)
        self._sync_confirm_enabled()
        self.setMinimumWidth(560)

    def _sync_confirm_enabled(self) -> None:
        # 一条都没勾还点确认没有意义。
        self.yesButton.setEnabled(any(check.isChecked() for check, _ in self._checks))

    def selected_paths(self) -> list[Path]:
        return [path for check, path in self._checks if check.isChecked()]


__all__ = [
    "ACCOMPANIMENT_TASKS",
    "AccompanimentHandoffDialog",
    "collect_accompaniments",
]
