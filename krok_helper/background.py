"""把阻塞活儿丢到 Qt 线程里跑的通用外壳。

各页都用它跑 ffmpeg / 下载 / 分析这类会卡住界面的任务，所以从 ``gui_qt``
挪到这层，页面包不必反向 import 宿主。
"""

from __future__ import annotations

import logging
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal as Signal

__all__ = ["BackgroundTask"]


class BackgroundTask(QThread):
    log_message = Signal(str)
    task_succeeded = Signal(object)
    task_failed = Signal(str)

    def __init__(self, runner: Callable[[Callable[[str], None]], object]) -> None:
        super().__init__()
        self._runner = runner
        self._task_name = getattr(runner, "__qualname__", getattr(runner, "__name__", "unknown"))

    def run(self) -> None:  # noqa: D401
        task_log = logging.getLogger("krok_helper.background_task")
        task_log.info("后台任务开始: %s", self._task_name)

        def emit_log(message: str) -> None:
            task_log.info("%s: %s", self._task_name, message)
            self.log_message.emit(message)

        try:
            result = self._runner(emit_log)
        except Exception as exc:  # noqa: BLE001
            task_log.exception("后台任务失败: %s", self._task_name)
            self.task_failed.emit(str(exc))
            return
        task_log.info("后台任务完成: %s", self._task_name)
        self.task_succeeded.emit(result)
