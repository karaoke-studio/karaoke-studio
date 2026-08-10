"""分离产物转交给后续模块。

音频分离跑完一整批以后，伴奏类的产物（伴奏 / 和声伴奏）正好是第 6 步 Hi-Res 混流
要的素材。这里只负责问用户「要不要送过去、送哪几条」，真正落地由工作流上下文
（``gui_qt`` 那边的 ``accept_separated_accompaniment``）完成——分离包不反向依赖
主窗口。

**分离用的那份原始音频本身就是"原唱"**（人声＋伴奏的完整混音），所以对话框
底下还多一条可选项，把它放进第 6 步的原唱卡，一次凑齐 on / off 两版所需的素材。
这条默认**不勾**：原唱卡只有一张、放进去是覆盖，不该在用户没表态时动它。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, PrimaryPushButton, PushButton, TitleLabel

from krok_helper.audio_processing.separation.states import TaskType
from krok_helper.qfluent_compat import ModelessDialog

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


class AccompanimentHandoffDialog(ModelessDialog):
    """问用户把哪几条伴奏放进 Hi-Res 混流。

    版式对齐波形对齐模块的导出完成弹窗（``gui_qt.AlignmentHandoffDialog``）：同样
    是「标题 + 一段说明 + 若干勾选项 + 确认/取消」，用户在两处看到的是同一种东西。

    勾选框用 qfluentwidgets 的 ``CheckBox`` 而不是裸 ``QCheckBox``——工作台的全局
    QSS 只给 QCheckBox 设了透明背景、没管指示器，勾上以后那个对勾根本看不见。

    即使只有一条也用勾选框：两条时必须能选，一条时保持同一套交互。
    """

    def __init__(
        self,
        candidates: list[tuple[str, Path]],
        parent: QWidget | None = None,
        *,
        source_audio: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._checks: list[tuple[CheckBox, Path]] = []
        self._source_audio = source_audio if source_audio and source_audio.is_file() else None
        self._source_check: CheckBox | None = None

        self.setWindowTitle("音频分离完成")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("音频分离完成", self))

        # 两条伴奏几乎总在同一个目录，把目录提到说明里，勾选项只留文件名，
        # 免得每一项都拖一条长路径。
        folders = {path.parent for _label, path in candidates}
        location = f"文件已保存到：\n{folders.pop()}\n\n" if len(folders) == 1 else ""
        summary = BodyLabel(
            f"{location}请选择要放进第 6 步 Hi-Res 混流的伴奏音频：", self
        )
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)

        for label, path in candidates:
            check = CheckBox(f"{label}：{path.name}", self)
            check.setChecked(True)
            check.setToolTip(str(path))
            layout.addWidget(check)
            self._checks.append((check, path))

        if len(candidates) > 1:
            note = BodyLabel("放入多条伴奏时，Hi-Res 会为每条各生成一个混流视频。", self)
            note.setWordWrap(True)
            layout.addWidget(note)

        if self._source_audio is not None:
            layout.addSpacing(6)
            source_hint = BodyLabel("这次用来分离的原始音频就是完整混音，也可以一并作为原唱：", self)
            source_hint.setWordWrap(True)
            layout.addWidget(source_hint)
            self._source_check = CheckBox(f"原唱：{self._source_audio.name}", self)
            # 默认不勾：原唱卡只有一张，放进去是覆盖。
            self._source_check.setChecked(False)
            self._source_check.setToolTip(str(self._source_audio))
            layout.addWidget(self._source_check)

        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.yesButton = PrimaryPushButton("放入 Hi-Res 混流", self)
        # 必须也是 qfluentwidgets 的按钮：裸 QPushButton 的高度和内边距跟
        # PrimaryPushButton 对不上，并排放会明显矮一圈。
        self.cancelButton = PushButton("暂不放入", self)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.clicked.connect(self.reject)
        buttons.addWidget(self.yesButton, 1)
        buttons.addWidget(self.cancelButton, 1)
        layout.addLayout(buttons)

        for check, _path in self._checks:
            check.toggled.connect(self._sync_confirm_enabled)
        if self._source_check is not None:
            self._source_check.toggled.connect(self._sync_confirm_enabled)
        self._sync_confirm_enabled()
        self.setMinimumWidth(620)

    def _sync_confirm_enabled(self) -> None:
        # 一条都没勾还点确认没有意义 —— 原唱那条也算数。
        anything = any(check.isChecked() for check, _ in self._checks) or self.source_as_on_vocal() is not None
        self.yesButton.setEnabled(anything)

    def selected_paths(self) -> list[Path]:
        return [path for check, path in self._checks if check.isChecked()]

    def source_as_on_vocal(self) -> Path | None:
        """勾了「作为原唱」就返回那条原始音频，否则 ``None``。"""
        if self._source_check is not None and self._source_check.isChecked():
            return self._source_audio
        return None


__all__ = [
    "ACCOMPANIMENT_TASKS",
    "AccompanimentHandoffDialog",
    "collect_accompaniments",
]
