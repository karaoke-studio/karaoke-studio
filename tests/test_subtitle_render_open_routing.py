"""「打开项目」拿到 ``.n3proj`` 时要改道走 N3 导入。

同一个文件，拖进窗口能开、从「文件管理 → 打开」选进来却报
``'utf-8' codec can't decode byte 0x.. in position ..`` —— 因为拖放是按扩展名分流的，
而「打开」不管拿到什么都按 ``.yurika`` 去读，而 ``.n3proj`` 是个 zip。

命令行 / Windows 资源管理器传进来的 ``open_initial_project`` 走的也是这条路，所以
分流放在 ``_open_project_path`` 这一处，三个入口一起修好。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from krok_helper.subtitle_render.frontend.main_window import (
    PROJECT_FILTER,
    SubtitleRenderWindow,
)
from krok_helper.subtitle_render.n3.project_import import N3_PROJECT_FILE_SUFFIX


@pytest.mark.parametrize("name", ["曲名.n3proj", "曲名.N3Proj"])
def test_opening_an_n3proj_is_handed_to_the_n3_importer(name: str) -> None:
    """扩展名大小写都算数 —— Windows 上两种写法都见得到。"""
    seen: list[tuple[Path, bool]] = []
    stub = SimpleNamespace(
        _import_n3_project_path=lambda path, *, confirm_discard: (
            seen.append((path, confirm_discard)),
            True,
        )[1],
    )

    result = SubtitleRenderWindow._open_project_path(stub, Path(name))

    assert result is True
    assert seen == [(Path(name), True)], "没有改道，还是按 .yurika 去读了"


def test_the_confirm_discard_flag_is_passed_through() -> None:
    """从命令行/宿主传进来的那条是 ``confirm_discard=False``，不能在改道时丢掉。"""
    seen: list[bool] = []
    stub = SimpleNamespace(
        _import_n3_project_path=lambda path, *, confirm_discard: (
            seen.append(confirm_discard),
            True,
        )[1],
    )

    SubtitleRenderWindow._open_project_path(
        stub, Path("曲名.n3proj"), confirm_discard=False
    )

    assert seen == [False]


def test_the_open_dialog_offers_n3proj_as_well() -> None:
    """否则用户只能在「所有文件」里去翻，正是这次踩坑的起点。"""
    assert N3_PROJECT_FILE_SUFFIX in PROJECT_FILTER
    assert PROJECT_FILTER.startswith("字幕渲染项目 (*.yurika *.n3proj)")


def test_an_n3proj_really_is_a_zip_and_not_utf8_text(tmp_path: Path) -> None:
    """钉住"为什么会撞上解码错误"这条前提。

    真的 ``.n3proj`` 以 ``PK\x03\x04`` 开头（zip），按 UTF-8 文本读必然炸。
    """
    from krok_helper.subtitle_render.project.store import load_render_project

    fake = tmp_path / "曲名.n3proj"
    fake.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00\xd7\x8e\xeb\x5c")

    with pytest.raises((UnicodeDecodeError, ValueError, OSError)):
        load_render_project(fake)
