"""打包时精简 Qt 插件的边界。

``build_windows.bat`` 会删掉一批用不到的 Qt 插件来压体积。删过头是有代价的，而且
代价只在打包后的真机上才看得见（源码运行时 Qt 是完整的，本地怎么点都正常）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "build_windows.bat"


def _removed_plugins() -> set[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"\$removeFiles\s*=\s*@\((?P<items>[^)]*)\)", text)
    assert match, "build_windows.bat 里找不到 $removeFiles 列表"
    return {item.strip().strip("'").replace("\\", "/") for item in match.group("items").split(",")}


def test_webp_image_plugin_stays_in_the_package() -> None:
    """YouTube 2026-08 起把封面给成 ``vi_webp/*.webp``。

    删掉这个插件后 ``QPixmap`` 只能拿到空图，视频下载页的封面直接消失，日志里
    只有一句 ``QPixmap::scaled: Pixmap is a null pixmap``——极难往打包脚本上想。
    解析侧已经优先挑 jpg 了，这个插件是第二道保险（别的站点给 webp 也不怕）。
    """

    assert "imageformats/qwebp.dll" not in _removed_plugins()


@pytest.mark.parametrize(
    "plugin",
    [
        "platforms/qwindows.dll",  # 没有它整个程序起不来
        "imageformats/qjpeg.dll",  # 封面、缩略图
        "imageformats/qico.dll",  # 窗口与任务栏图标
        "imageformats/qsvg.dll",  # 导唱符等矢量素材
    ],
)
def test_essential_plugins_are_never_trimmed(plugin: str) -> None:
    assert plugin not in _removed_plugins()
