"""更新相关的两个对话框：发现新版本、下载源排序。

从 ``gui_qt`` 搬过来，和 :mod:`krok_helper.updater` 的其余部分放一起。
两者都是 ``ModelessDialog``：无遮罩、无 Qt 模态，弹出时工作台仍可操作。
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon as FIF,
    ListWidget as FluentListWidget,
    PrimaryPushButton,
    PushButton as QPushButton,
    TitleLabel,
)

from krok_helper.config import APP_TITLE
from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.updater.sources import SOURCE_IDS, SOURCE_LABELS, normalize_order

__all__ = ["UpdateSourceOrderDialog", "WorkbenchUpdateDialog"]


class UpdateSourceOrderDialog(ModelessDialog):
    """更新源优先级编辑弹窗（拖拽重排 + 上移 / 下移 / 恢复默认）。

    确定后从 :attr:`order` 读出最终顺序（已 ``normalize_order``）。
    """

    def __init__(self, current_order: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.order: list[str] = normalize_order(current_order)
        self.setWindowTitle("更新源优先级")
        self.setMinimumWidth(540)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(12)

        title = TitleLabel("更新源优先级", self)
        root.addWidget(title)

        hint = BodyLabel(
            "按顺序尝试，前一项失败时自动降级到下一项。\n"
            "可以直接拖动条目，或选中后用右侧按钮微调。",
            self,
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QWidget(self)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 4)
        body_layout.setSpacing(12)

        self.order_list = FluentListWidget(body)
        self.order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.order_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.order_list.setMinimumWidth(340)
        self.order_list.setMinimumHeight(170)
        for source in self.order:
            item = QListWidgetItem(SOURCE_LABELS.get(source, source))
            item.setData(Qt.ItemDataRole.UserRole, source)
            self.order_list.addItem(item)
        body_layout.addWidget(self.order_list, 1)

        button_column = QWidget(body)
        column_layout = QVBoxLayout(button_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(8)
        self.move_up_button = QPushButton(FIF.UP, "上移", button_column)
        self.move_down_button = QPushButton(FIF.DOWN, "下移", button_column)
        self.reset_button = QPushButton("恢复默认", button_column)
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.reset_button.clicked.connect(self._reset_order)
        column_layout.addWidget(self.move_up_button)
        column_layout.addWidget(self.move_down_button)
        column_layout.addWidget(self.reset_button)
        column_layout.addStretch(1)
        body_layout.addWidget(button_column, 0)

        root.addWidget(body, 1)

        button_row = QHBoxLayout()
        button_row.setSpacing(12)
        self.yesButton = PrimaryPushButton("确定", self)
        self.cancelButton = QPushButton("取消", self)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.clicked.connect(self.reject)
        button_row.addWidget(self.yesButton, 1)
        button_row.addWidget(self.cancelButton, 1)
        root.addLayout(button_row)

    def _selected_row(self) -> int:
        items = self.order_list.selectedItems()
        return self.order_list.row(items[0]) if items else -1

    def _move_selected(self, delta: int) -> None:
        row = self._selected_row()
        target = row + delta
        if row < 0 or target < 0 or target >= self.order_list.count():
            return
        item = self.order_list.takeItem(row)
        if item is None:
            return
        self.order_list.insertItem(target, item)
        self.order_list.setCurrentRow(target)

    def _reset_order(self) -> None:
        self.order_list.clear()
        for source in SOURCE_IDS:
            item = QListWidgetItem(SOURCE_LABELS.get(source, source))
            item.setData(Qt.ItemDataRole.UserRole, source)
            self.order_list.addItem(item)
        self.order_list.setCurrentRow(0)

    def accept(self) -> None:  # noqa: N802
        raw: list[str] = []
        for row in range(self.order_list.count()):
            item = self.order_list.item(row)
            if item is not None:
                raw.append(str(item.data(Qt.ItemDataRole.UserRole)))
        self.order = normalize_order(raw)
        super().accept()


class WorkbenchUpdateDialog(ModelessDialog):
    """Update prompt that shows the GitHub Release body directly."""

    def __init__(
        self,
        release,
        *,
        local_version: str,
        source_label: str = "",
        all_releases: list | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.user_choice = "later"
        self._release = release
        self.setWindowTitle(APP_TITLE)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(720, 560)
        self._build_ui(release, local_version, source_label, all_releases or [])

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._bring_to_front)

    def _bring_to_front(self) -> None:
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _compose_changelog(release, all_releases: list) -> tuple[str, int]:
        """聚合跨版本更新日志。

        返回 ``(markdown 文本, 版本数)``；只有 1 个版本时保持原样输出该版本 body。
        """
        releases = list(all_releases)
        if len(releases) <= 1:
            single = releases[0] if releases else release
            return (getattr(single, "body", "") or "").strip(), 1
        sections: list[str] = []
        for item in releases:
            date = (getattr(item, "published_at", "") or "")[:10]
            heading = f"## v{item.version}" + (f"（{date}）" if date else "")
            body = (getattr(item, "body", "") or "").strip() or "（该版本没有填写发布说明）"
            sections.append(f"{heading}\n\n{body}")
        return "\n\n---\n\n".join(sections), len(releases)

    def _build_ui(self, release, local_version: str, source_label: str, all_releases: list) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 22)
        layout.setSpacing(14)

        title = QLabel("发现新版本")
        title.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 22pt; font-weight: 700; color: #111827;')
        layout.addWidget(title)

        version = QLabel(f"v{release.version}")
        version.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 15pt; font-weight: 700; color: #111827;')
        layout.addWidget(version)

        published_at = release.published_at[:10] if getattr(release, "published_at", "") else "未知日期"
        meta_parts = [f"当前版本 v{local_version}", f"发布于 {published_at}"]
        if source_label:
            meta_parts.append(f"下载源：{source_label}")
        meta = QLabel("  |  ".join(meta_parts))
        meta.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10pt; color: #6b7280;')
        meta.setWordWrap(True)
        layout.addWidget(meta)

        body_text, release_count = self._compose_changelog(release, all_releases)
        content_label = QLabel(
            f"更新内容（当前版本之后共 {release_count} 个版本）："
            if release_count > 1
            else "更新内容："
        )
        content_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10.5pt; color: #111827;')
        layout.addWidget(content_label)

        body_view = QTextBrowser()
        body_view.setReadOnly(True)
        body_view.setOpenExternalLinks(True)
        body_view.setMinimumHeight(300)
        body_view.setStyleSheet(
            """
            QTextBrowser {
                background: #ffffff;
                border: 1px solid #d9dfe8;
                border-radius: 8px;
                padding: 12px;
                font-family: "Microsoft YaHei UI";
                font-size: 10.5pt;
                color: #111827;
            }
            """
        )
        if body_text:
            try:
                body_view.setMarkdown(body_text)
            except Exception:
                body_view.setPlainText(body_text)
        else:
            body_view.setPlainText("本次 Release 没有填写发布说明。")
        layout.addWidget(body_view, 1)

        html_url = (getattr(release, "html_url", "") or "").strip()
        if html_url:
            open_release_button = QPushButton(FIF.LINK, "在浏览器中查看完整发布说明")
            open_release_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(html_url)))
            layout.addWidget(open_release_button, 0, Qt.AlignmentFlag.AlignHCenter)

        button_row = QHBoxLayout()
        button_row.setSpacing(12)
        skip_button = QPushButton("跳过此版本")
        later_button = QPushButton("稍后再说")
        update_button = PrimaryPushButton("立即更新")
        skip_button.clicked.connect(self._choose_skip)
        later_button.clicked.connect(self._choose_later)
        update_button.clicked.connect(self._choose_update)
        button_row.addWidget(skip_button)
        button_row.addStretch(1)
        button_row.addWidget(later_button)
        button_row.addWidget(update_button)
        layout.addLayout(button_row)

    def _choose_update(self) -> None:
        self.user_choice = "update"
        self.accept()

    def _choose_later(self) -> None:
        self.user_choice = "later"
        self.reject()

    def _choose_skip(self) -> None:
        self.user_choice = "skip"
        self.accept()
