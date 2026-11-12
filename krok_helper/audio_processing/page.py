"""工作流第 2 步「音视频处理」容器页（需求文档 §3.1 / §3.2）。

顶部 Pivot 主导航（与全局设置一致）+ QStackedWidget：
- 「波形对齐」：挂载既有 align_page，内容与状态完全不动；
- 「音频分离」：挂载 AudioSeparationPage；
- 「音频合成」：挂载 MergePage（多段拼接 + 去静音 + 接缝淡化）。

切换内部 Tab 不清空任何页面状态；最后使用的内部 Tab 持久化到
``settings.pymss["last_internal_tab"]``（需求文档 §3.7）。切换只更新内存，
由工作台统一保存设置（含正常退出和更新退出）时写盘。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF

from krok_helper.workspace_switcher import WorkspaceSwitcher

TAB_ALIGNMENT = "alignment"
TAB_SEPARATION = "separation"
TAB_MERGE = "merge"


class AudioProcessingPage(QWidget):
    """第 2 步容器。构造时传入已构建好的子页面（merge 页可选）。"""

    def __init__(
        self,
        alignment_page: QWidget,
        separation_page: QWidget,
        settings,
        parent: QWidget | None = None,
        merge_page: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self.alignment_page = alignment_page
        self.separation_page = separation_page
        self.merge_page = merge_page

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
        if merge_page is not None:
            self._pivot.addItem(
                routeKey=TAB_MERGE,
                text="音频合成",
                onClick=lambda _checked=False: self.switch_tab(TAB_MERGE),
                icon=FIF.MUSIC,
            )
        switcher_row.addWidget(self._pivot, 0)
        switcher_row.addStretch(1)
        layout.addLayout(switcher_row)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(alignment_page)
        self._stack.addWidget(separation_page)
        self._pages = {
            TAB_ALIGNMENT: alignment_page,
            TAB_SEPARATION: separation_page,
        }
        if merge_page is not None:
            self._stack.addWidget(merge_page)
            self._pages[TAB_MERGE] = merge_page
        layout.addWidget(self._stack, 1)

        initial = self._settings_ns().get("last_internal_tab", TAB_ALIGNMENT)
        if initial not in self._pages:
            initial = TAB_ALIGNMENT
        self.switch_tab(initial, persist=False)

    def _settings_ns(self) -> dict:
        namespace = getattr(self._settings, "pymss", None)
        if not isinstance(namespace, dict):
            namespace = {}
            setattr(self._settings, "pymss", namespace)
        return namespace

    def current_tab(self) -> str:
        for tab, page in self._pages.items():
            if self._stack.currentWidget() is page:
                return tab
        return TAB_ALIGNMENT

    def switch_tab(self, tab: str, *, persist: bool = True) -> None:
        target = self._pages.get(tab)
        if target is None:
            tab, target = TAB_ALIGNMENT, self._pages[TAB_ALIGNMENT]
        self._stack.setCurrentWidget(target)
        self._pivot.setCurrentItem(tab)
        if persist:
            # Navigation must not collect/serialize all module settings on the
            # GUI thread. The host already saves these on normal/update exit.
            self._settings_ns()["last_internal_tab"] = tab
