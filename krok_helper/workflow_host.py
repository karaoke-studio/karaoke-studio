"""工作台宿主契约。

各工作流页面被抽成独立包之后，它们仍然需要把产物交给后面的步骤 ——
分离页把伴奏塞进第 6 步、字幕渲染页把成片塞进第 6 步。这些调用原本是
``getattr(ctx, "accept_xxx", None)`` 的鸭子类型：宿主一改名就静默失效，
用户点完「转交下一步」什么也不会发生，也不报错。

这里把那层契约写成显式 :class:`WorkflowHost`：

* 页面侧把 ``workflow_context`` 标注成 ``WorkflowHost | None`` —— ``None``
  表示页面被单独拉起来跑（没有工作台外壳），此时跳过转交是正确行为；
* ``@runtime_checkable`` 让测试能断言 ``KrokHelperQtApp`` 仍然满足契约，
  宿主改名会在 CI 里当场炸掉，而不是变成线上静默失灵。

注意 ``runtime_checkable`` 的 ``isinstance`` 只查方法**存在**、不查签名，
所以它是"防改名"而不是"防改参数"；参数变动仍然要靠类型检查和测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

__all__ = ["WorkflowHost"]


@runtime_checkable
class WorkflowHost(Protocol):
    """页面把产物交回工作台时用到的那部分宿主能力。"""

    def accept_subtitle_video(self, path: Path) -> None:
        """接收第 5 步渲染好的字幕视频，并切到第 6 步 Hi-Res 混流。"""
        ...

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        """把第 2 步分离出的伴奏追加到第 6 步的伴奏卡，返回实际接收的路径。"""
        ...
