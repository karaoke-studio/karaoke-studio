"""工作流第 2 步「音视频处理」容器页（需求文档 §3.1 / §3.2）。

顶部 Pivot 主导航（与全局设置一致）+ QStackedWidget：
- 「波形对齐」：挂载既有 align_page，内容与状态完全不动；
- 「音频分离」：挂载新的 AudioSeparationPage。

切换内部 Tab 不清空任何页面状态；最后使用的内部 Tab 持久化到
``settings.pymss["last_internal_tab"]``（需求文档 §3.7）。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF

from krok_helper.workspace_switcher import WorkspaceSwitcher

TAB_ALIGNMENT = "alignment"
TAB_SEPARATION = "separation"


class AudioProcessingPage(QWidget):
    """第 2 步容器。构造时传入已构建好的两个子页面。"""

    def __init__(
        self,
        alignment_page: QWidget,
        separation_page: QWidget,
        settings,
        save_settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._save_settings = save_settings
        self.alignment_page = alignment_page
        self.separation_page = separation_page

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        switcher_row = QHBoxLayout()
        switcher_row.setContentsMargins(4, 2, 4, 10)
        self._pivot = WorkspaceSwitcher(self)
        self._pivot.addItem(
            routeKey=TAB_ALIGNMENT,
            text="波形对齐",
            onClick=lambda _checked=False: self.switch_tab(TAB_ALIGNMENT),
            icon=FIF.ALIGNMENT,
        )
        self._pivot.addItem(
            routeKey=TAB_SEPARATION,
            text="音频分离",
            onClick=lambda _checked=False: self.switch_tab(TAB_SEPARATION),
            icon=FIF.MIX_VOLUMES,
        )
        switcher_row.addWidget(self._pivot, 0)
        switcher_row.addStretch(1)
        layout.addLayout(switcher_row)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(alignment_page)
        self._stack.addWidget(separation_page)
        layout.addWidget(self._stack, 1)

        initial = self._settings_ns().get("last_internal_tab", TAB_ALIGNMENT)
        if initial not in (TAB_ALIGNMENT, TAB_SEPARATION):
            initial = TAB_ALIGNMENT
        self.switch_tab(initial, persist=False)

    def _settings_ns(self) -> dict:
        namespace = getattr(self._settings, "pymss", None)
        if not isinstance(namespace, dict):
            namespace = {}
            setattr(self._settings, "pymss", namespace)
        return namespace

    def current_tab(self) -> str:
        return TAB_ALIGNMENT if self._stack.currentWidget() is self.alignment_page else TAB_SEPARATION

    def switch_tab(self, tab: str, *, persist: bool = True) -> None:
        target = self.alignment_page if tab == TAB_ALIGNMENT else self.separation_page
        self._stack.setCurrentWidget(target)
        self._pivot.setCurrentItem(tab)
        if persist:
            self._settings_ns()["last_internal_tab"] = tab
            self._save_settings()
