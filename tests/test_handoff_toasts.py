"""转交产物时的右下角提示。

四条链路都只放素材、不跳页面，界面上没有任何动静，提示条是唯一的反馈 ——
所以每条都得有，而且没真放进去时不能瞎报。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from krok_helper.gui_qt import KrokHelperQtApp


def _host(**extra):
    toasts: list[tuple[str, str]] = []
    host = SimpleNamespace(
        _notify_handoff=lambda title, content: toasts.append((title, content)),
        **extra,
    )
    return host, toasts


def test_subtitle_video_handoff_reports_the_file() -> None:
    host, toasts = _host(set_video_path=lambda _p: None)

    KrokHelperQtApp.accept_subtitle_video(host, Path("D:/tmp/成片.mp4"))

    assert len(toasts) == 1
    title, content = toasts[0]
    assert "成片" in title
    assert "成片.mp4" in content
    assert "第 6 步" in content


def test_single_accompaniment_handoff_names_the_file() -> None:
    accepted = [Path("D:/tmp/伴奏.wav")]
    host, toasts = _host(add_off_vocal_paths=lambda _p: accepted)

    assert KrokHelperQtApp.accept_separated_accompaniment(host, accepted) == accepted
    assert len(toasts) == 1
    assert "伴奏.wav" in toasts[0][1]


def test_multiple_accompaniment_handoff_reports_a_count() -> None:
    accepted = [Path("D:/tmp/a.wav"), Path("D:/tmp/b.wav"), Path("D:/tmp/c.wav")]
    host, toasts = _host(add_off_vocal_paths=lambda _p: accepted)

    KrokHelperQtApp.accept_separated_accompaniment(host, accepted)

    assert "3 个伴奏" in toasts[0][1]


def test_no_toast_when_every_accompaniment_was_a_duplicate() -> None:
    """伴奏卡去重后一个都没加进去 —— 这时候报「已放入」是骗人的。"""
    host, toasts = _host(add_off_vocal_paths=lambda _p: [])

    KrokHelperQtApp.accept_separated_accompaniment(host, [Path("D:/tmp/dup.wav")])

    assert toasts == []


def _alignment_host(*, selections, payload, **extra):
    host, toasts = _host(
        _alignment_handoff_dialog=SimpleNamespace(selections=lambda: selections),
        _alignment_handoff_payload=payload,
        **extra,
    )
    return host, toasts


def test_alignment_handoff_reports_both_targets() -> None:
    loaded: list[Path] = []
    vocals: list[Path] = []
    host, toasts = _alignment_host(
        selections=(True, True),
        payload=(True, Path("D:/tmp/对齐后.mp4"), Path("D:/tmp/源.mkv"), Path("D:/tmp/原唱.flac")),
        subtitle_render_page=SimpleNamespace(load_video=loaded.append),
        set_on_vocal_path=vocals.append,
    )

    KrokHelperQtApp._apply_alignment_handoff(host)

    assert loaded == [Path("D:/tmp/对齐后.mp4")]
    assert vocals == [Path("D:/tmp/原唱.flac")]
    assert len(toasts) == 2
    assert "第 5 步" in toasts[0][1] and "对齐后.mp4" in toasts[0][1]
    assert "第 6 步" in toasts[1][1] and "原唱.flac" in toasts[1][1]


def test_alignment_handoff_only_reports_what_was_ticked() -> None:
    vocals: list[Path] = []
    host, toasts = _alignment_host(
        selections=(False, True),
        payload=(True, Path("D:/tmp/对齐后.mp4"), Path("D:/tmp/源.mkv"), Path("D:/tmp/原唱.flac")),
        subtitle_render_page=SimpleNamespace(load_video=lambda _p: None),
        set_on_vocal_path=vocals.append,
    )

    KrokHelperQtApp._apply_alignment_handoff(host)

    assert len(toasts) == 1
    assert "Hi-Res" in toasts[0][0]


def test_alignment_handoff_stays_quiet_without_a_source_path() -> None:
    """音频目标下没有源视频可交，那一路应当既不投放也不提示。"""
    host, toasts = _alignment_host(
        selections=(True, False),
        payload=(False, Path("D:/tmp/对齐后.wav"), None, Path("D:/tmp/原唱.flac")),
        subtitle_render_page=SimpleNamespace(load_video=lambda _p: None),
        set_on_vocal_path=lambda _p: None,
    )

    KrokHelperQtApp._apply_alignment_handoff(host)

    assert toasts == []
