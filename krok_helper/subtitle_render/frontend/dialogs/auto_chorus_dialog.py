"""「自动识别和声」的设置弹窗。

对标 NicoKaraMaker3 的「歌詞のコーラス部分を自動色分けする」：选一个角色方案、
一对起止字符，整个歌词源扫一遍，括号里的字符（含括号本身）分配到该角色。

版式对齐同一个右键菜单里的「批量识别导唱标记」（同为 :class:`ModelessDialog`，
不加全局遮罩，弹出时不会锁住主界面）。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.auto_chorus import (
    DEFAULT_CHORUS_BEGIN_CHARS,
    DEFAULT_CHORUS_END_CHARS,
)

__all__ = ["AutoChorusDialog"]

#: 新建角色时用的名字（下拉里排在已有角色之后）。
NEW_ROLE_SENTINEL = "\x00new"


class AutoChorusDialog(ModelessDialog):
    """选角色 + 起止字符 + 是否覆盖已有角色。"""

    def __init__(
        self,
        *,
        role_options: list[str],
        selected_role: str = "",
        begin_chars: str = DEFAULT_CHORUS_BEGIN_CHARS,
        end_chars: str = DEFAULT_CHORUS_END_CHARS,
        overwrite: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self._role_options = [str(name) for name in role_options if str(name).strip()]
        self.setWindowTitle("自动识别和声")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("自动识别和声", self))
        hint = CaptionLabel(
            "扫描整个歌词源，把成对括号之间的文字（含括号本身）分配到指定角色方案。"
            "括号没有配对的行会整行跳过。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        role_row = QHBoxLayout()
        role_row.addWidget(BodyLabel("角色方案：", self))
        self.role_combo = ComboBox(self)
        for name in self._role_options:
            self.role_combo.addItem(name, userData=name)
        self.role_combo.addItem("新建「和声」角色", userData=NEW_ROLE_SENTINEL)
        target = selected_role.strip()
        index = self.role_combo.findData(target) if target else -1
        self.role_combo.setCurrentIndex(index if index >= 0 else self.role_combo.count() - 1)
        role_row.addWidget(self.role_combo, 1)
        layout.addLayout(role_row)

        chars_row = QHBoxLayout()
        chars_row.addWidget(BodyLabel("起始字符：", self))
        self.begin_edit = LineEdit(self)
        self.begin_edit.setText(begin_chars)
        self.begin_edit.setFixedWidth(96)
        chars_row.addWidget(self.begin_edit)
        chars_row.addWidget(BodyLabel("结束字符：", self))
        self.end_edit = LineEdit(self)
        self.end_edit.setText(end_chars)
        self.end_edit.setFixedWidth(96)
        chars_row.addWidget(self.end_edit)
        chars_row.addStretch(1)
        layout.addLayout(chars_row)

        chars_hint = CaptionLabel(
            "这两栏各填一组字符，任意一个都算数（默认同时认全角与半角括号）。",
            self,
        )
        chars_hint.setWordWrap(True)
        layout.addWidget(chars_hint)

        self.overwrite_check = CheckBox("覆盖已经分配过角色的字符", self)
        self.overwrite_check.setChecked(bool(overwrite))
        self.overwrite_check.setToolTip(
            "默认只填还没有角色的字符，避免抹掉在歌词打轴里逐字点出来的歌手分配。"
        )
        layout.addWidget(self.overwrite_check)

        layout.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = PushButton("取消", self)
        self.apply_button = PrimaryPushButton("开始识别", self)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        for edit in (self.begin_edit, self.end_edit):
            edit.textChanged.connect(self._sync_apply_enabled)
        self._sync_apply_enabled()

    # ── 结果 ────────────────────────────────────────────────────

    def selected_role(self) -> str:
        """选中的角色名；选了「新建」时返回空串，由调用方决定新名字。"""
        value = self.role_combo.currentData()
        return "" if value == NEW_ROLE_SENTINEL else str(value or "")

    def begin_chars(self) -> str:
        return self.begin_edit.text()

    def end_chars(self) -> str:
        return self.end_edit.text()

    def overwrite(self) -> bool:
        return self.overwrite_check.isChecked()

    # ── 内部 ────────────────────────────────────────────────────

    def _sync_apply_enabled(self) -> None:
        # 起止任一为空就识别不出任何东西，别让用户点了没反应。
        self.apply_button.setEnabled(
            bool(self.begin_edit.text().strip()) and bool(self.end_edit.text().strip())
        )
