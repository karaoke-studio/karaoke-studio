"""各页面的宿主替身（不是测试文件，是给测试用的工具）。

一处定义、各处复用。分散着写会出事：对齐页的 ``build_media_info`` 替身曾照着
契约的类型标注写成"一定收得到 Path"，而实际清空素材后传的就是 ``None`` ——
替身和真实现不一致，测出来的绿是假的。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QWidget

from krok_helper.settings import AppSettings


def alignment_host(calls: list | None = None, *, settings: AppSettings | None = None) -> SimpleNamespace:
    """满足 :class:`~krok_helper.alignment.page.AlignmentHost` 的替身。"""
    log = calls if calls is not None else []
    return SimpleNamespace(
        settings=settings or AppSettings(),
        active_module="waveform_align",
        track_background_task=lambda task: (log.append(("task", task)), task)[1],
        resolve_ffmpeg_dir=lambda: None,
        # 真实现遇到 None 会回「时长未知」，替身也得照做。
        build_media_info=lambda path, label: f"{label}: {path.name if path else '时长未知'}",
        # 真外壳是递归开关整块面板的；写成空函数会让「导出」那一排永远是灰的，
        # 扫描看着通、其实一步都没走。
        set_panel_enabled=lambda panel, enabled: [w.setEnabled(enabled) for w in panel.findChildren(QWidget)],
        focused_widget_is_text_input=lambda: False,
        notify_handoff=lambda title, content: log.append(("toast", title)),
        open_settings_window=lambda context: log.append(("settings", context)),
        set_on_vocal_path=lambda path: log.append(("vocal", path)),
        # 真外壳把接收结果如实回给页面（渲染页拒收就是 False），替身也得回布尔，
        # 不然「没真放进去时不报提示」那条分支测不出来。
        set_subtitle_background_video=lambda path: (log.append(("background", path)), True)[1],
    )


def hires_host(calls: list | None = None) -> SimpleNamespace:
    """满足 :class:`~krok_helper.hires.page.HiResHost` 的替身。"""
    log = calls if calls is not None else []
    return SimpleNamespace(
        track_background_task=lambda task: (log.append(("task", task)), task)[1],
        resolve_ffmpeg_dir=lambda: None,
        resolve_output_name_mode=lambda: "fixed",
        resolve_output_name_templates=lambda: ("{video_name}_on", "{video_name}_off"),
        notify_handoff=lambda title, content: log.append(("toast", title)),
        open_settings_window=lambda context: log.append(("settings", context)),
    )


def lyrics_host(calls: list | None = None, *, settings: AppSettings | None = None) -> SimpleNamespace:
    """满足 :class:`~krok_helper.lyrics_search.page.LyricsSearchHost` 的替身。"""
    log = calls if calls is not None else []
    return SimpleNamespace(
        settings=settings or AppSettings(),
        track_background_task=lambda task: (log.append(("task", task)), task)[1],
        install_single_click_combo_behavior=lambda combo: log.append(("combo", combo)),
        import_current_lyrics_to_timing=lambda: log.append(("import", None)),
    )


def media_files(tmp_path: Path) -> tuple[Path, Path]:
    """一对占位素材文件（只要存在即可，页面不读内容）。"""
    video = tmp_path / "GO GHOST.mp4"
    audio = tmp_path / "GO GHOST.flac"
    video.write_bytes(b"\0")
    audio.write_bytes(b"\0")
    return video, audio
