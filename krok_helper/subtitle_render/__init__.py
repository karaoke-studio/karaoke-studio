"""字幕视频渲染模块（工作流第 5 步）。

对标 NicoKaraMaker3 等效功能：把带逐字时间戳的歌词渲染成卡拉ok高亮动画字幕视频。

双模式：
- standalone：``python -m krok_helper.subtitle_render`` 单独跑
- embedded：通过 :meth:`SubtitleRenderWindow.for_embedding` 嵌入工作台第 5 步

设计文档：``C:/Users/18007/.claude/plans/ok-ok-main-bug-merge-merge-main-nicokar-toasty-blum.md``。
"""

import sys
from typing import TYPE_CHECKING, Any

from krok_helper.subtitle_render.contracts import (
    SubtitleProjectState,
    SubtitleRenderPage,
    SubtitleRenderSettingsProvider,
)
from krok_helper.subtitle_render.domain import models as models

# Preserve the original public module path while keeping its implementation in
# the domain package.  Existing plugins and automation scripts may still use
# ``import krok_helper.subtitle_render.models``.
sys.modules[f"{__name__}.models"] = models

if TYPE_CHECKING:
    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

__all__ = [
    "SubtitleProjectState",
    "SubtitleRenderPage",
    "SubtitleRenderSettingsProvider",
    "SubtitleRenderWindow",
    "create_embedded_subtitle_render",
    "models",
]


def create_embedded_subtitle_render(
    parent: Any = None,
    settings_provider: SubtitleRenderSettingsProvider | None = None,
    workflow_context: Any = None,
) -> SubtitleRenderPage:
    """Create the workbench page without exposing its concrete widget class."""

    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

    return SubtitleRenderWindow.for_embedding(
        parent=parent,
        settings_provider=settings_provider,
        workflow_context=workflow_context,
    )


def __getattr__(name: str) -> Any:
    """Keep pure model/engine imports independent from the Qt frontend."""

    if name == "SubtitleRenderWindow":
        from krok_helper.subtitle_render.frontend.main_window import (
            SubtitleRenderWindow,
        )

        return SubtitleRenderWindow
    raise AttributeError(name)
