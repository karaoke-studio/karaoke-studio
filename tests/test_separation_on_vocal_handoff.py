"""分离用的原始音频，可以一并作为第 6 步的原唱。

分离跑完那条转交对话框原先只管伴奏。但送进去分离的那份音频本身就是完整混音，
也就是"原唱"—— 勾上就能在第 6 步一次凑齐 on / off 两版所需的素材。

和伴奏那条的区别在落点：伴奏是追加（可以多条），原唱只有一张卡、放进去是覆盖。
所以这条默认**不勾**，且覆盖了什么要在提示里说清楚。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from krok_helper.audio_processing.separation.handoff import AccompanimentHandoffDialog
from krok_helper.audio_processing.separation import handoff as handoff_module
from krok_helper.audio_processing.separation import page as page_module
from krok_helper.hires.page import HiResPage
from krok_helper.workflow_host import OnVocalSink
from tests.page_fakes import hires_host


@pytest.fixture
def files(tmp_path):
    source = tmp_path / "GO GHOST.flac"
    instrumental = tmp_path / "GO GHOST_instrumental.flac"
    for path in (source, instrumental):
        path.write_bytes(b"\0")
    return source, instrumental


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


# ── 对话框 ──────────────────────────────────────────────────────


def test_the_option_shows_up_with_the_source_audio(app, files) -> None:
    source, instrumental = files

    dialog = AccompanimentHandoffDialog([("伴奏", instrumental)], source_audio=source)
    try:
        assert dialog._source_check is not None
        assert source.name in dialog._source_check.text()
        # 默认不勾 —— 原唱卡只有一张，不该在用户没表态时覆盖。
        assert not dialog._source_check.isChecked()
        assert dialog.source_as_on_vocal() is None
    finally:
        dialog.close()


def test_checking_it_hands_the_source_over(app, files) -> None:
    source, instrumental = files

    dialog = AccompanimentHandoffDialog([("伴奏", instrumental)], source_audio=source)
    try:
        dialog._source_check.setChecked(True)
        assert dialog.source_as_on_vocal() == source
    finally:
        dialog.close()


def test_no_option_without_a_source(app, files) -> None:
    _source, instrumental = files

    dialog = AccompanimentHandoffDialog([("伴奏", instrumental)])
    try:
        assert dialog._source_check is None
        assert dialog.source_as_on_vocal() is None
    finally:
        dialog.close()


def test_a_missing_source_file_is_not_offered(app, files, tmp_path) -> None:
    """文件在分离期间被移走了就别摆出来 —— 勾了也放不进去。"""
    _source, instrumental = files

    dialog = AccompanimentHandoffDialog([("伴奏", instrumental)], source_audio=tmp_path / "没有这个.flac")
    try:
        assert dialog._source_check is None
    finally:
        dialog.close()


def test_only_the_source_checked_still_confirms(app, files) -> None:
    """伴奏一条都不要、只把原唱送过去，也该点得动确认。"""
    source, instrumental = files

    dialog = AccompanimentHandoffDialog([("伴奏", instrumental)], source_audio=source)
    try:
        for check, _path in dialog._checks:
            check.setChecked(False)
        assert not dialog.yesButton.isEnabled()

        dialog._source_check.setChecked(True)

        assert dialog.yesButton.isEnabled()
    finally:
        dialog.close()


# ── 落点：Hi-Res 页 ─────────────────────────────────────────────


def test_the_hires_page_takes_the_source_as_on_vocal(app, files) -> None:
    source, _instrumental = files
    calls: list = []
    page = HiResPage(host=hires_host(calls))
    try:
        assert page.accept_source_as_on_vocal(source)

        assert page.on_vocal_zone.path == source
        assert any(kind == "toast" for kind, _ in calls), "没冒转交提示"
    finally:
        page.deleteLater()


def test_replacing_an_existing_on_vocal_says_so(app, files, tmp_path) -> None:
    source, _instrumental = files
    previous = tmp_path / "上一首.flac"
    previous.write_bytes(b"\0")
    messages: list[str] = []
    host = hires_host()
    host.notify_handoff = lambda title, content: messages.append(content)
    page = HiResPage(host=host)
    try:
        page.set_on_vocal_path(previous)

        page.accept_source_as_on_vocal(source)

        assert page.on_vocal_zone.path == source
        assert "上一首.flac" in messages[-1], f"没提被替换的是哪一条：{messages[-1]}"
    finally:
        page.deleteLater()


def test_a_vanished_file_is_refused(app, tmp_path) -> None:
    page = HiResPage(host=hires_host())
    try:
        assert not page.accept_source_as_on_vocal(tmp_path / "没有这个.flac")
        assert page.on_vocal_zone.path is None
    finally:
        page.deleteLater()


# ── 串起来：分离页 → 宿主 ────────────────────────────────────────


def _fake_separation_page(host, source: Path) -> SimpleNamespace:
    return SimpleNamespace(_batch_results=[], _batch_input_path=source, _workflow_context=host)


def test_the_separation_page_passes_the_source_to_the_host(monkeypatch, app, files) -> None:
    source, instrumental = files
    taken: list = []

    class _Host:
        def accept_separated_accompaniment(self, paths):
            taken.append(("accompaniment", list(paths)))
            return list(paths)

        def accept_source_as_on_vocal(self, path):
            taken.append(("on_vocal", path))
            return True

    monkeypatch.setattr(handoff_module, "collect_accompaniments", lambda _r: [("伴奏", instrumental)])

    def _fake_dialog(candidates, parent=None, *, source_audio=None):
        assert source_audio == source, "原始音频没传进对话框"
        return SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Accepted,
            selected_paths=lambda: [instrumental],
            source_as_on_vocal=lambda: source,
        )

    monkeypatch.setattr(handoff_module, "AccompanimentHandoffDialog", _fake_dialog)
    page = _fake_separation_page(_Host(), source)
    page.window = lambda: None

    page_module.AudioSeparationPage._offer_accompaniment_handoff(page)

    assert ("on_vocal", source) in taken
    assert ("accompaniment", [instrumental]) in taken


def test_a_host_that_cannot_take_on_vocals_is_not_offered_the_option(monkeypatch, app, files) -> None:
    """宿主不收原唱时那条勾选项根本不该摆出来 —— 勾了也没地方去。"""
    source, instrumental = files
    seen: list = []

    class _AccompanimentOnly:
        def accept_separated_accompaniment(self, paths):
            return list(paths)

    assert not isinstance(_AccompanimentOnly(), OnVocalSink)
    monkeypatch.setattr(handoff_module, "collect_accompaniments", lambda _r: [("伴奏", instrumental)])

    def _fake_dialog(candidates, parent=None, *, source_audio=None):
        seen.append(source_audio)
        return SimpleNamespace(
            exec=lambda: QDialog.DialogCode.Rejected,
            selected_paths=list,
            source_as_on_vocal=lambda: None,
        )

    monkeypatch.setattr(handoff_module, "AccompanimentHandoffDialog", _fake_dialog)
    page = _fake_separation_page(_AccompanimentOnly(), source)
    page.window = lambda: None

    page_module.AudioSeparationPage._offer_accompaniment_handoff(page)

    assert seen == [None]
