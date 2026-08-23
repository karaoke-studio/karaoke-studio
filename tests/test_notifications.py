from __future__ import annotations

import sys
from types import SimpleNamespace

from krok_helper import notifications


def test_windows_completion_sound_uses_message_beep(monkeypatch) -> None:
    calls: list[int] = []
    fake_winsound = SimpleNamespace(
        MB_ICONASTERISK=64,
        MessageBeep=calls.append,
    )
    monkeypatch.setattr(notifications.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winsound", fake_winsound)

    notifications.play_completion_sound()

    assert calls == [64]


def test_completion_sound_falls_back_to_qt_beep(monkeypatch) -> None:
    calls: list[bool] = []
    fake_app = SimpleNamespace(beep=lambda: calls.append(True))

    class FakeQApplication:
        # 只替换 notifications 模块里的名字：patch 真 QApplication.instance
        # 会让 pytest-qt 的逐测试事件处理拿到假实例而炸掉
        @staticmethod
        def instance():
            return fake_app

    monkeypatch.setattr(notifications.sys, "platform", "linux")
    monkeypatch.setattr(notifications, "QApplication", FakeQApplication)

    notifications.play_completion_sound()

    assert calls == [True]
