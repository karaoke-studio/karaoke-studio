"""与主程序同风格的「目录被占用」恢复弹窗。

基于 qfluentwidgets ``MessageBox``(蒙层 + 圆角面板 + 阴影)——与 SUG 主程序
的 Fluent 对话框同一条实现路径，在用户机器上久经验证。

⚠ 历史教训：不要手搓 QWidget/QDialog + FramelessWindowHint +
WA_TranslucentBackground 的自绘弹窗——在部分 Windows 机器上无法渲染，恢复
Qt::Dialog 类型位后甚至会在 exec() 阶段触发 Qt 原生层 fail-fast（0xC0000409）。
主更新窗口那种写法仅对 QWidget 成立，对 QDialog 不成立。
"""

from __future__ import annotations

from typing import Optional

from qfluentwidgets import MessageBox

DEFAULT_INFORMATIVE_TEXT = (
    "结束正在使用的程序可能丢失其中未保存的内容"
    "（资源管理器结束后会自动重新启动）。\n"
    "系统进程与安全软件不会被更新器结束；如仍失败，请重启电脑后再次尝试更新。"
)


class LockRecoveryDialog(MessageBox):
    """占用进程恢复弹窗；点重试按钮后 :attr:`retry_requested` 为 True。"""

    def __init__(
        self,
        body_text: str,
        detail_text: str,
        retry_label: str,
        parent=None,
        informative_text: Optional[str] = None,
    ) -> None:
        if parent is None:
            # MaskDialogBase 按父窗口尺寸铺蒙层，没有 parent 无法工作
            raise ValueError("LockRecoveryDialog 必须提供 parent")
        if informative_text is None:
            informative_text = DEFAULT_INFORMATIVE_TEXT

        content = f"{body_text}\n{detail_text}\n{informative_text}"
        super().__init__("更新被占用停止", content, parent)

        self.retry_requested = False
        self.yesSignal.connect(self._mark_retry)
        self.yesButton.setText(retry_label)
        self.yesButton.adjustSize()
        self.cancelButton.setText("关闭")
        # 进程清单可选中复制，方便用户反馈
        self.setContentCopyable(True)

    def _mark_retry(self) -> None:
        self.retry_requested = True
