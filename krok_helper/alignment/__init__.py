"""波形对齐页（工作流第 2 步「音视频处理 → 波形对齐」）。

页面主体见 :mod:`krok_helper.alignment.page`；这里只导出被外壳直接引用的
几个控件，避免和 ``page`` 形成循环导入。
"""

from krok_helper.alignment.drop_card import AlignmentDropCard
from krok_helper.alignment.handoff_dialog import AlignmentHandoffDialog
from krok_helper.alignment.waveform_view import WaveformView

__all__ = ["AlignmentDropCard", "AlignmentHandoffDialog", "WaveformView"]
