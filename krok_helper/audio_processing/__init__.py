"""音视频处理模块（工作流第 2 步容器 + 音频分离 UI）。

对齐 ``docs/音视频处理-PyMSS音频分离需求设计.md``：第 2 步由「波形对齐」升级为
「音视频处理」，内部以 Pivot 挂载「波形对齐」与「音频分离（PyMSS）」两个主 Tab。

生产页面由真实 PyMSS 后端驱动；测试可注入模拟后端。
"""

from krok_helper.audio_processing.page import AudioProcessingPage
from krok_helper.audio_processing.separation import AudioSeparationPage

__all__ = ["AudioProcessingPage", "AudioSeparationPage"]
