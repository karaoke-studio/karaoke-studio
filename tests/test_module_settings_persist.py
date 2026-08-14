"""各模块自己的设置要活过一次重启。

页面对象化之后出的一个安静的 bug：``AlignmentPage._build_ui`` 里的 ``setChecked``
会触发 ``_on_alignment_target_changed`` → ``_persist_alignment_preferences``，而那时
:meth:`load_settings` 还没跑过、页面上全是默认值 —— 一存就把用户上次的导出目录、
编码方式原样冲掉（外壳的 ``_build_ui`` 排在 ``_load_settings_into_ui`` 前面）。等
``load_settings`` 真的跑起来，读到的已经是被自己冲掉的那份。

歌词页是同一个形状（``_persist_lyrics_preferences`` 挂在几个下拉框上）。两边的
「正在恢复设置」旗现在都从 ``True`` 起步：加载过一次才允许回写。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.settings import (  # noqa: E402
    get_settings_path,
    load_app_settings,
    save_app_settings,
)

#: 用户改过、重启后必须还在的那些项。
SAVED_SETTINGS = {
    "align_video_name_template": "{video_name}_我的视频",
    "align_audio_name_template": "{audio_name}_我的音频",
    "align_output_dir_mode": "custom",
    "align_target": "audio",
    "align_encode_mode": "hardware",
    "align_force_1080p60": True,
    "align_export_use_video_audio": True,
    "on_name_template": "{video_name}_我的原唱",
    "off_name_template": "{video_name}_我的伴奏",
    "output_name_mode": "template",
    "lyrics_language": "translation",
    "lyrics_strip_intro_lines": False,
}


@pytest.fixture
def workbench(monkeypatch, tmp_path: Path):
    """一个用干净配置目录起来的工作台，配置里预先写好"用户改过的值"。

    ⚠ 全量回归卡点（2026-08 实测，详见 AGENTS.md §8.9）：teardown 走
    ``window.close() → _shutdown_project_modules → _finalize_lyrics_timing_shutdown
    → SUG cleanup_temp_files → app_dirs.config_dir()``，后者对
    ``Path(sys.argv[0]).parent`` 做临时文件写探测。当 pytest 以
    ``python -m pytest`` 启动时 argv[0] 是 site-packages 里的 pytest 包目录
    （ProgramData）；在受限/沙箱环境下对该目录的写探测会**无限阻塞并烧
    CPU**（表现：全量跑到本文件附近进度停住，py-spy 栈顶卡在
    ``tempfile._mkstemp_inner ← app_dirs._is_dir_writable``）。

    规避（无需改产品代码）：
      1. 正常开发者终端 / CI 不受影响，直接跑即可；
      2. 沙箱/受限环境：把一个极简 ``run_pytest.py``（内容仅
         ``sys.exit(console_main())``）放到**可写目录**（如 %TEMP%），
         再 ``python %TEMP%/run_pytest.py tests/ ...`` —— argv[0] 解析到
         可写目录，探测立即通过；同时建议 ``--basetemp=%TEMP%/xxx``
         绕开沙箱对 ``pytest-of-<user>`` 目录的建目录限制，并设
         ``PYTHONPATH=<仓库根>``（``python -m`` 会自动加 CWD，脚本方式不会）。
    """
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    settings = load_app_settings()
    for name, value in SAVED_SETTINGS.items():
        setattr(settings, name, value)
    settings.align_output_custom_dir = str(tmp_path)
    save_app_settings(settings)

    from krok_helper import gui_qt

    app = QApplication.instance() or QApplication([])
    window = gui_qt.KrokHelperQtApp()
    yield window
    window.close()
    window.deleteLater()
    app.processEvents()


def _on_disk() -> dict:
    return json.loads(Path(get_settings_path()).read_text(encoding="utf-8"))


def test_every_saved_setting_survives_startup(workbench, tmp_path: Path) -> None:
    """一个工作台把所有项一次断完 —— 起一次窗口要好几秒，别按项参数化。"""
    disk = _on_disk()
    lost = {
        name: (expected, disk.get(name))
        for name, expected in SAVED_SETTINGS.items()
        if disk.get(name) != expected
    }

    assert not lost, f"启动之后被冲掉了：{lost}"
    # 导出目录是这次真正丢掉的那一项。
    assert disk["align_output_custom_dir"] == str(tmp_path)


def test_the_pages_load_what_was_saved(workbench, tmp_path: Path) -> None:
    assert workbench.align_page.name_templates() == (
        SAVED_SETTINGS["align_video_name_template"],
        SAVED_SETTINGS["align_audio_name_template"],
    )
    assert workbench.align_page.output_dir_settings() == ("custom", str(tmp_path))


# ── 修好之后仍然要能存 ──────────────────────────────────────


def test_the_alignment_page_still_persists_after_startup(workbench) -> None:
    """旗从 True 起步，但加载完必须放开 —— 否则就变成"永远存不下去"。"""
    assert workbench.align_page._restoring_alignment_settings is False

    workbench.align_page.align_force_1080p60_check.setChecked(False)
    QApplication.instance().processEvents()

    assert _on_disk()["align_force_1080p60"] is False


def test_the_lyrics_page_still_persists_after_startup(workbench) -> None:
    assert workbench.lyrics_page._restoring_preferences is False

    workbench.lyrics_page.lyrics_strip_intro_checkbox.setChecked(True)
    QApplication.instance().processEvents()

    assert _on_disk()["lyrics_strip_intro_lines"] is True


# ── 外壳这一层：界面还没灌过设置时不许"收集" ──────────────────


def test_a_save_during_construction_keeps_everything(monkeypatch, tmp_path: Path) -> None:
    """``_build_ui`` 期间任何一个页面叫到 ``_save_all_settings`` 都不该冲掉配置。

    音频分离页建后端时就会叫（``_persist_inputs`` → ``_save_settings``）。那时外壳
    的 ``output_name_mode_value`` 之类还是构造默认值、对齐页也没 ``load_settings``
    过，一"收集"就把用户上次的整份配置盖成默认。
    """
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    settings = load_app_settings()
    for name, value in SAVED_SETTINGS.items():
        setattr(settings, name, value)
    settings.pymss = {"mode": "local", "output_format": "flac"}
    save_app_settings(settings)

    from krok_helper import gui_qt

    app = QApplication.instance() or QApplication([])
    window = gui_qt.KrokHelperQtApp()
    try:
        disk = _on_disk()
        lost = {
            name: (expected, disk.get(name))
            for name, expected in SAVED_SETTINGS.items()
            if disk.get(name) != expected
        }
        assert not lost, f"构造期间的落盘把配置冲掉了：{lost}"
        # 页面在构造期间写进 settings 的东西照样该存下去（分离页自己还会往这个
        # 命名空间里补 output_dir，所以只断言我们放进去的那几项还在）。
        assert disk["pymss"]["mode"] == "local"
        assert disk["pymss"]["output_format"] == "flac"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_the_shell_collects_again_once_loaded(workbench) -> None:
    """旗放开之后，收集方向必须恢复 —— 否则就变成"永远存不了"。"""
    assert workbench._settings_loaded is True

    workbench.on_name_template_value = "{video_name}_换了"
    workbench._save_all_settings()

    assert _on_disk()["on_name_template"] == "{video_name}_换了"


def test_a_save_before_loading_does_not_collect(workbench) -> None:
    """直接把旗按回去，模拟构造期间的那次落盘。"""
    workbench._settings_loaded = False
    workbench.on_name_template_value = "{video_name}_不该被存"

    workbench._save_all_settings()

    assert _on_disk()["on_name_template"] == SAVED_SETTINGS["on_name_template"]
