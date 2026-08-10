"""从 UI 点下去，一路走到功能入口 —— 波形对齐页的通路扫描。

单测每个方法都绿，不等于按钮点得动。对齐页对象化之后，「生成波形」里那句
``self._host.track_background_task("align_analysis_task", task)`` 还留着搬迁前的
两参形式，方法本身没人直接调，边界测试也只看"碰了谁"不看"怎么调的"，于是一路
绿到用户点下按钮才炸。

所以这里不测功能对不对，只测**通路通不通**：把真的页面立起来、喂上素材，然后
逐个去点每一个按钮 / 勾选框 / 下拉框，只要有一处在信号槽里抛异常就红。

两个前提要小心：
- **Qt 会吞掉信号槽里的异常**（转给 ``sys.excepthook``），所以必须挂钩子去捞，
  不能指望 ``pytest.raises``。线上那个 TypeError 就是被应用自己的全局钩子接住、
  弹成对话框的。
- **别真干活**：后台任务不 ``start``、不弹对话框、不起子进程。要留的是
  "点击 → 槽函数 → 建任务" 这一段，恰好也是出过事的那一段。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.alignment import page as align_page
from krok_helper.alignment.page import AlignmentPage
from krok_helper.audio_alignment import WaveformData
from krok_helper.background import BackgroundTask
from krok_helper.settings import AppSettings
from tests.ui_sweep import block_modals, clickable, crash_collector, fake_popen

#: 点了会走到"选文件/打开资源管理器"这类外部交互的，扫描时跳过。
SKIP = {"align_material_settings_button"}


def _fake_host(calls: list) -> SimpleNamespace:
    return SimpleNamespace(
        settings=AppSettings(),
        active_module="waveform_align",
        track_background_task=lambda task: (calls.append(("task", task)), task)[1],
        resolve_ffmpeg_dir=lambda: None,
        build_media_info=lambda path, label: f"{label}: {path.name if path else '时长未知'}",
        set_panel_enabled=lambda panel, enabled: [w.setEnabled(enabled) for w in panel.findChildren(QWidget)],
        focused_widget_is_text_input=lambda: False,
        notify_handoff=lambda title, content: calls.append(("toast", title)),
        open_settings_window=lambda context: calls.append(("settings", context)),
        set_on_vocal_path=lambda path: None,
    )


@pytest.fixture
def swept(monkeypatch, tmp_path):
    """立起页面、封住一切真会干活的出口，并把 excepthook 换成收集器。"""
    QApplication.instance() or QApplication([])

    # 后台任务只建不跑 —— 出事的正是"建"这一步。
    monkeypatch.setattr(BackgroundTask, "start", lambda self, *a, **k: None)
    monkeypatch.setattr(BackgroundTask, "isRunning", lambda self: False)
    # 「播放」这条通路的终点真的是 ffmpeg | ffplay 两个子进程；假的 Popen 让它
    # 走到底又不真起进程（页面里有 ``assert ffmpeg_process.stdout is not None``）。
    monkeypatch.setattr(align_page.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(align_page, "terminate_process", lambda *a, **k: None)
    monkeypatch.setattr(align_page, "open_in_explorer", lambda *a, **k: None)
    monkeypatch.setattr(align_page, "show_fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(align_page, "show_fluent_info", lambda *a, **k: None)
    block_modals(monkeypatch)

    calls: list = []
    page = AlignmentPage(host=_fake_host(calls))

    # 喂两份素材，否则大半按钮是灰的、根本走不到功能里去。
    video = tmp_path / "GO GHOST.mp4"
    audio = tmp_path / "GO GHOST.flac"
    video.write_bytes(b"\0")
    audio.write_bytes(b"\0")

    def prime() -> None:
        """把页面放回"素材齐备、波形已生成"的状态 —— 扫描时每点一下都要复原。

        波形也得塞：「自动对齐」「导出」这一排是拿 ``waveform_view`` 里有没有数据
        当闸门的，不塞就永远是灰的，扫描会静悄悄跳过 —— 而这两条正是出过事的通路。
        """
        page.set_align_video_path(video)
        page.set_align_audio_path(audio)
        page.waveform_view.set_waveforms(
            video_waveform=WaveformData(path=video, duration=171.4, peaks_per_second=20, peaks=[0.1] * 3428),
            audio_waveform=WaveformData(path=audio, duration=170.1, peaks_per_second=20, peaks=[0.1] * 3402),
        )
        # 波形任务真跑完时走的也是这两条：一条开整块控制面板，一条开各按钮。
        # 少调前者，导出那一排会被"父控件是灰的"挡住，看起来像通路不通。
        page._refresh_align_target_ui()
        page._refresh_alignment_preview_controls()

    prime()

    try:
        with crash_collector() as crashes:
            yield page, crashes, calls, prime
    finally:
        page.deleteLater()


def test_the_sweep_actually_reaches_the_buttons(swept) -> None:
    """先确认扫描本身没扫了个空 —— 否则下面那条会假绿。"""
    page, _, _, _ = swept

    names = {name for name, _ in clickable(page, skip=SKIP)}

    assert len(names) >= 30, f"只找到 {len(names)} 个按钮，扫描八成漏了：{sorted(names)}"
    assert "align_analyze_button" in names
    assert "align_auto_button" in names
    assert "align_mode_export_button" in names


def test_clicking_everything_raises_nothing(swept) -> None:
    page, crashes, _, prime = swept

    clicked: list[str] = []
    for name, widget in clickable(page, skip=SKIP):
        # 每次都先把素材放回去再点：否则「清空」一旦排在前面，后面整排按钮
        # 都是灰的，扫描会安安静静地跳过去 —— 出过事的「生成波形」正好在里头。
        prime()
        if not widget.isEnabled():
            continue  # 复原之后仍是灰的，说明这条路本来就要求别的前置状态
        widget.click()
        clicked.append(name)
        assert not crashes, f"点「{name}」炸了：{crashes[0]}"

    # 素材+波形都喂上之后实测点到 34 个（含微调、目标切换、日志、导出那几排）。
    # 钉个下限并点名关键按钮，免得哪天扫描悄悄退化成空转。
    assert len(clicked) >= 28, f"只点到 {len(clicked)} 个按钮：{clicked}"
    for must in ("align_analyze_button", "align_auto_button", "align_clear_button",
                 "align_preview_button", "align_mode_export_button", "align_open_output_button"):
        assert must in clicked, f"{must} 没被点到，扫描漏了：{clicked}"


@pytest.mark.parametrize(
    "button,slot",
    [
        ("align_analyze_button", "align_analysis_task"),
        ("align_auto_button", "align_auto_task"),
    ],
)
def test_the_work_buttons_really_create_a_task(swept, button: str, slot: str) -> None:
    """通路的终点：点下去要真的建出后台任务，并落进页面自己的槽位。

    只断言"没抛异常"是不够的 —— 槽函数在开头 return 掉也不会抛。
    """
    page, crashes, calls, prime = swept

    prime()
    widget = getattr(page, button)
    if not widget.isEnabled():
        pytest.skip(f"{button} 在当前状态下是灰的")
    widget.click()

    assert not crashes, f"点「{button}」炸了：{crashes[0]}"
    assert getattr(page, slot) is not None, f"{button} 点完没在 {slot} 里留下任务"
    assert any(kind == "task" for kind, _ in calls), "任务没登记到外壳去"


def test_exporting_really_creates_a_task(swept, monkeypatch, tmp_path) -> None:
    """导出这条通路的终点也要落到任务上。

    扫描里点「导出」时选文件框返回空串，等于用户按了取消，走不到建任务那一步；
    这里给它一个真的落点，把最后一段也走完 —— 三个后台任务槽位里，导出是最后
    一个，也是搬迁时同样写错过的那个。
    """
    from PyQt6.QtWidgets import QFileDialog

    page, crashes, calls, prime = swept
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "对齐成片.mp4"), ""))
    )

    prime()
    page.align_mode_export_button.click()

    assert not crashes, f"点「导出对齐视频」炸了：{crashes[0]}"
    assert page.align_export_task is not None, "导出点完没在 align_export_task 里留下任务"
    assert any(kind == "task" for kind, _ in calls), "任务没登记到外壳去"
