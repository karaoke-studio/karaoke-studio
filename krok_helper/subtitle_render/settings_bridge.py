"""字幕视频渲染模块的设置桥（骨架）。

模仿 ``gui_qt.py`` 中的 ``KrokHelperSettingsBridge``（SUG 用），把本模块的
配置读写映射到工作台全局 ``AppSettings.subtitle_render`` 命名空间。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from krok_helper.settings import (
    AppSettings,
    load_app_settings,
    save_app_settings,
    sync_baseline_field,
)


class KrokHelperSubtitleRenderSettingsBridge:
    """把字幕渲染模块的内部配置读写桥接到工作台 ``AppSettings``。"""

    def __init__(self, app_settings: AppSettings, save_callback: Callable[[], object]) -> None:
        self._app_settings = app_settings
        self._save_callback = save_callback

    def load(self) -> dict:
        latest = load_app_settings()
        self._app_settings.subtitle_render = deepcopy(latest.subtitle_render)
        sync_baseline_field(self._app_settings, "subtitle_render")
        return deepcopy(self._app_settings.subtitle_render)

    def save(self, data: dict) -> None:
        latest = load_app_settings()
        latest.subtitle_render = deepcopy(data)
        # 这里要写的正是模块命名空间，合并回盘上的旧值等于把自己的改动丢掉。
        save_app_settings(latest, merge_module_namespaces=False)
        self._app_settings.subtitle_render = deepcopy(latest.subtitle_render)
        # 宿主的整份写盘不该再把这一段当成"自己的改动"端出去。
        sync_baseline_field(self._app_settings, "subtitle_render")
