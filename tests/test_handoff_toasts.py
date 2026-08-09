"""转交产物时的右下角提示。

四条链路都只放素材、不跳页面，界面上没有任何动静，提示条是唯一的反馈 ——
所以每条都得有，而且没真放进去时不能瞎报。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from krok_helper.gui_qt import KrokHelperQtApp
from krok_helper.alignment.page import AlignmentPage
from krok_helper.hires.page import HiResPage


def _host(**extra):
    toasts: list[tuple[str, str]] = []
    host = SimpleNamespace(
        _notify_handoff=lambda title, content: toasts.append((title, content)),
        **extra,
    )
    return host, toasts


def _hires_page(**extra):
    """Hi-Res 页的替身：伴奏转交的提示由它自己经 ``_host`` 发出。"""
    toasts: list[tuple[str, str]] = []
    page = SimpleNamespace(
        _host=SimpleNamespace(notify_handoff=lambda title, content: toasts.append((title, content))),
        **extra,
    )
    return page, toasts


def test_subtitle_video_handoff_reports_the_file() -> None:
    host, toasts = _host(set_video_path=lambda _p: None, _show_module=lambda _m: None)

    KrokHelperQtApp.accept_subtitle_video(host, Path("D:/tmp/成片.mp4"))

    assert len(toasts) == 1
    title, content = toasts[0]
    assert "成片" in title
    assert "成片.mp4" in content
    assert "第 6 步" in content


def test_single_accompaniment_handoff_names_the_file() -> None:
    accepted = [Path("D:/tmp/伴奏.wav")]
    page, toasts = _hires_page(add_off_vocal_paths=lambda _p: accepted)

    assert HiResPage.accept_separated_accompaniment(page, accepted) == accepted
    assert len(toasts) == 1
    assert "伴奏.wav" in toasts[0][1]


def test_multiple_accompaniment_handoff_reports_a_count() -> None:
    accepted = [Path("D:/tmp/a.wav"), Path("D:/tmp/b.wav"), Path("D:/tmp/c.wav")]
    page, toasts = _hires_page(add_off_vocal_paths=lambda _p: accepted)

    HiResPage.accept_separated_accompaniment(page, accepted)

    assert "3 个伴奏" in toasts[0][1]


def test_no_toast_when_every_accompaniment_was_a_duplicate() -> None:
    """伴奏卡去重后一个都没加进去 —— 这时候报「已放入」是骗人的。"""
    page, toasts = _hires_page(add_off_vocal_paths=lambda _p: [])

    HiResPage.accept_separated_accompaniment(page, [Path("D:/tmp/dup.wav")])

    assert toasts == []


def _alignment_page(*, selections, payload, vocals=None, **extra):
    """对齐页替身：转交提示与"交原唱给第 6 步"现在都经 ``_host``。"""
    toasts: list[tuple[str, str]] = []
    page = SimpleNamespace(
        _alignment_handoff_dialog=SimpleNamespace(selections=lambda: selections),
        _alignment_handoff_payload=payload,
        _host=SimpleNamespace(
            notify_handoff=lambda title, content: toasts.append((title, content)),
            set_on_vocal_path=(vocals.append if vocals is not None else (lambda _p: None)),
        ),
        **extra,
    )
    return page, toasts


def test_alignment_handoff_reports_both_targets() -> None:
    loaded: list[Path] = []
    vocals: list[Path] = []
    page, toasts = _alignment_page(
        selections=(True, True),
        payload=(True, Path("D:/tmp/对齐后.mp4"), Path("D:/tmp/源.mkv"), Path("D:/tmp/原唱.flac")),
        vocals=vocals,
        subtitle_render_page=SimpleNamespace(load_video=loaded.append),
    )

    AlignmentPage._apply_alignment_handoff(page)

    assert loaded == [Path("D:/tmp/对齐后.mp4")]
    assert vocals == [Path("D:/tmp/原唱.flac")]
    assert len(toasts) == 2
    assert "第 5 步" in toasts[0][1] and "对齐后.mp4" in toasts[0][1]
    assert "第 6 步" in toasts[1][1] and "原唱.flac" in toasts[1][1]


def test_alignment_handoff_only_reports_what_was_ticked() -> None:
    vocals: list[Path] = []
    page, toasts = _alignment_page(
        selections=(False, True),
        payload=(True, Path("D:/tmp/对齐后.mp4"), Path("D:/tmp/源.mkv"), Path("D:/tmp/原唱.flac")),
        vocals=vocals,
        subtitle_render_page=SimpleNamespace(load_video=lambda _p: None),
    )

    AlignmentPage._apply_alignment_handoff(page)

    assert len(toasts) == 1
    assert "Hi-Res" in toasts[0][0]


def test_alignment_handoff_stays_quiet_without_a_source_path() -> None:
    """音频目标下没有源视频可交，那一路应当既不投放也不提示。"""
    page, toasts = _alignment_page(
        selections=(True, False),
        payload=(False, Path("D:/tmp/对齐后.wav"), None, Path("D:/tmp/原唱.flac")),
        subtitle_render_page=SimpleNamespace(load_video=lambda _p: None),
    )

    AlignmentPage._apply_alignment_handoff(page)

    assert toasts == []
