"""宿主契约 :class:`WorkflowHost`。

页面转交产物走的是这层契约。宿主一改名，以前是静默失灵（用户点了「转交
下一步」什么也不发生），现在应当在这里当场炸出来。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from krok_helper.workflow_host import WorkflowHost


class _FullHost:
    def accept_subtitle_video(self, path: Path) -> None: ...

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        return list(paths)


class _RenamedHost:
    """模拟"宿主把方法改了名"——这正是契约要挡住的情况。"""

    def accept_subtitle_video(self, path: Path) -> None: ...

    def accept_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        return list(paths)


def test_main_window_satisfies_the_contract() -> None:
    from krok_helper.gui_qt import KrokHelperQtApp

    assert issubclass(KrokHelperQtApp, WorkflowHost)


def test_a_complete_host_satisfies_the_contract() -> None:
    assert isinstance(_FullHost(), WorkflowHost)


def test_a_renamed_method_breaks_the_contract() -> None:
    assert not isinstance(_RenamedHost(), WorkflowHost)


def test_separation_page_skips_handoff_without_a_host(monkeypatch) -> None:
    """没有工作台外壳时跳过转交是正确行为，不该抛异常。"""
    from krok_helper.audio_processing.separation import page as page_module

    called: list[int] = []
    monkeypatch.setattr(page_module, "collect_accompaniments", lambda _r: called.append(1), raising=False)

    # 这两条分支在碰任何 Qt 状态之前就返回了，用替身即可，不必真造一个页面。
    host_free = SimpleNamespace(_batch_results=[], _workflow_context=None)

    page_module.AudioSeparationPage._offer_accompaniment_handoff(host_free)

    assert not called, "没有宿主时不该继续走转交流程"


def test_separation_page_warns_when_the_host_broke_the_contract(monkeypatch, caplog) -> None:
    from krok_helper.audio_processing.separation import page as page_module

    broken = SimpleNamespace(_batch_results=[], _workflow_context=_RenamedHost())

    with caplog.at_level("WARNING"):
        page_module.AudioSeparationPage._offer_accompaniment_handoff(broken)

    assert any("accept_separated_accompaniment" in record.message for record in caplog.records)
