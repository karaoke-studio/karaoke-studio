"""波形对齐页（工作流第 2 步「音视频处理 → 波形对齐」）。

页面主体仍在 ``KrokHelperQtApp._build_alignment_page()`` 里，这里先装它的
独立控件；后续按块搬过来。
"""

from krok_helper.alignment.drop_card import AlignmentDropCard
from krok_helper.alignment.handoff_dialog import AlignmentHandoffDialog
from krok_helper.alignment.waveform_view import WaveformView

__all__ = ["AlignmentDropCard", "AlignmentHandoffDialog", "WaveformView"]
