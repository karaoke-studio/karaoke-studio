"""工作台宿主契约。

各工作流页面被抽成独立包之后，它们仍然需要把产物交给后面的步骤 ——
分离页把伴奏塞进第 6 步、字幕渲染页把成片塞进第 6 步。这些调用原本是
``getattr(ctx, "accept_xxx", None)`` 的鸭子类型：宿主一改名就静默失效，
用户点完「转交下一步」什么也不会发生，也不报错。

这里把那层契约写成显式 Protocol。**按能力拆开**，每个页面只依赖自己真正
调用的那一条：

* :class:`SubtitleVideoSink` —— 第 5 步字幕渲染交成片；
* :class:`AccompanimentSink` —— 第 2 步音频分离交伴奏；
* :class:`OnVocalSink` —— 第 2 步音频分离把分离用的原始音频交作原唱；
* :class:`WorkflowHost` —— 工作台主窗口，两样都实现。

拆开而不是让每个调用点都去校验整个 :class:`WorkflowHost`，是因为宿主不一定
"全能"：测试里的替身、以及将来只想接一种产物的容器，都只会实现其中一条。
校验整份契约会把这些合法的宿主一并挡掉。

``@runtime_checkable`` 让测试能断言 ``KrokHelperQtApp`` 仍然满足契约，宿主
改名会在 CI 里当场炸掉，而不是变成线上静默失灵。注意它的 ``isinstance``
只查方法**存在**、不查签名，所以是"防改名"而不是"防改参数"；参数变动仍然
要靠类型检查和测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

__all__ = ["AccompanimentSink", "OnVocalSink", "SubtitleVideoSink", "WorkflowHost"]


@runtime_checkable
class SubtitleVideoSink(Protocol):
    """能接收第 5 步渲染好的字幕视频。"""

    def accept_subtitle_video(self, path: Path) -> None:
        """接收成片，并切到第 6 步 Hi-Res 混流。"""
        ...


@runtime_checkable
class AccompanimentSink(Protocol):
    """能接收第 2 步分离出的伴奏。"""

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        """把伴奏追加到第 6 步的伴奏卡，返回实际接收的路径。"""
        ...


@runtime_checkable
class OnVocalSink(Protocol):
    """能接收原唱音频。

    分离用的那份原始音频本身就是"原唱"（人声＋伴奏的完整混音），第 6 步要拿它
    和分出来的伴奏配成 on / off 两版。单独一条能力而不是并进
    :class:`AccompanimentSink`，是因为落点不同：伴奏是追加，原唱只有一张卡。
    """

    def accept_source_as_on_vocal(self, path: Path) -> bool:
        """把这条音频放进第 6 步的原唱卡；返回是否真的放进去了。"""
        ...


@runtime_checkable
class WorkflowHost(SubtitleVideoSink, AccompanimentSink, OnVocalSink, Protocol):
    """完整的工作台宿主 —— 主窗口实现全部转交能力。"""
