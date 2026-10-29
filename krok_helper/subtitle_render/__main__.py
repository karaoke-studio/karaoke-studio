"""字幕视频渲染模块 standalone 入口。

用法：``python -m krok_helper.subtitle_render``。
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication, QMainWindow

from krok_helper.config import APP_TITLE
from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow
from krok_helper.subtitle_render.n3.system_font_inventory import (
    register_missing_system_fonts,
)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    # 与工作台入口同一套字体补注册：Qt 字体库漏掉的已安装字体（名字表只有
    # 非当前 locale 的旧字体）在 standalone 运行时也要能被列表/CPU/GPU 用上。
    try:
        register_missing_system_fonts()
    except Exception:
        logging.getLogger(__name__).warning("系统字体补注册失败", exc_info=True)

    window = QMainWindow()
    window.setWindowTitle(f"字幕视频生成 — {APP_TITLE}")
    window.resize(1280, 800)

    content = SubtitleRenderWindow(embedded=False)
    window.setCentralWidget(content)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
