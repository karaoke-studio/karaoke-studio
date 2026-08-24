"""宿主契约 :class:`WorkflowHost`。

页面转交产物走的是这层契约。宿主一改名，以前是静默失灵（用户点了「转交
下一步」什么也不发生），现在应当在这里当场炸出来。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from krok_helper.workflow_host import AccompanimentSink, OnVocalSink, SubtitleVideoSink, WorkflowHost
from krok_helper.subtitle_render.contracts import (
    SubtitleProjectState,
    SubtitleRenderPage,
    SubtitleRenderSettingsProvider,
)


class _FullHost:
    def accept_subtitle_video(self, path: Path) -> None: ...

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        return list(paths)

    def accept_source_as_on_vocal(self, path: Path) -> bool:
        return True


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
    assert not isinstance(_RenamedHost(), AccompanimentSink)
    assert not isinstance(_RenamedHost(), WorkflowHost)


def test_capabilities_are_checked_one_by_one() -> None:
    """只实现一条能力的宿主是合法的：字幕渲染页不该因为它不收伴奏而拒绝转交。"""

    class SubtitleOnly:
        def accept_subtitle_video(self, path: Path) -> None: ...

    host = SubtitleOnly()
    assert isinstance(host, SubtitleVideoSink)
    assert not isinstance(host, AccompanimentSink)
    assert not isinstance(host, OnVocalSink)
    assert not isinstance(host, WorkflowHost)


def test_subtitle_render_window_satisfies_the_host_page_contract() -> None:
    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

    assert issubclass(SubtitleRenderWindow, SubtitleRenderPage)


def test_subtitle_project_state_is_the_stable_public_contract() -> None:
    from krok_helper.subtitle_render import SubtitleProjectState as PublicState
    from krok_helper.subtitle_render.project.session import SubtitleProjectState as SessionState

    assert PublicState is SubtitleProjectState
    assert SessionState is SubtitleProjectState


def test_subtitle_render_settings_bridge_satisfies_the_provider_contract() -> None:
    from krok_helper.subtitle_render.settings_bridge import (
        KrokHelperSubtitleRenderSettingsBridge,
    )

    assert issubclass(
        KrokHelperSubtitleRenderSettingsBridge,
        SubtitleRenderSettingsProvider,
    )


def test_separation_page_skips_handoff_without_a_host(monkeypatch) -> None:
    """没有工作台外壳时跳过转交是正确行为，不该抛异常。"""
    from krok_helper.audio_processing.separation import handoff as handoff_module
    from krok_helper.audio_processing.separation import page as page_module

    called: list[int] = []
    # 页面是在函数体里 ``from ...handoff import ...`` 的，桩必须打在 handoff 模块上；
    # 打在 page 模块的名字上不生效，这条测试会假绿。
    monkeypatch.setattr(handoff_module, "collect_accompaniments", lambda _r: called.append(1))

    # 这两条分支在碰任何 Qt 状态之前就返回了，用替身即可，不必真造一个页面。
    host_free = SimpleNamespace(_batch_results=[], _batch_input_path=None, _workflow_context=None)

    page_module.AudioSeparationPage._offer_accompaniment_handoff(host_free)

    assert not called, "没有宿主时不该继续走转交流程"


def test_separation_page_warns_when_the_host_broke_the_contract(monkeypatch, caplog) -> None:
    from krok_helper.audio_processing.separation import page as page_module

    broken = SimpleNamespace(_batch_results=[], _batch_input_path=None, _workflow_context=_RenamedHost())

    with caplog.at_level("WARNING"):
        page_module.AudioSeparationPage._offer_accompaniment_handoff(broken)

    assert any("accept_separated_accompaniment" in record.message for record in caplog.records)
