"""字幕视频渲染模块 standalone 入口。

用法：``python -m krok_helper.subtitle_render``。
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMainWindow

from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    window = QMainWindow()
    window.setWindowTitle("字幕视频生成 — 卡拉ok工作台")
    window.resize(1280, 800)

    content = SubtitleRenderWindow(embedded=False)
    window.setCentralWidget(content)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
