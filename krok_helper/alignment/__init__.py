"""波形对齐页（工作流第 2 步「音视频处理 → 波形对齐」）。

目前只装了从 ``gui_qt`` 抽出来的素材拖放卡片；页面主体仍在
``KrokHelperQtApp._build_alignment_page()`` 里，后续按块搬过来。
"""

from krok_helper.alignment.drop_card import AlignmentDropCard

__all__ = ["AlignmentDropCard"]
