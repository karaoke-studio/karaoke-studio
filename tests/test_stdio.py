from __future__ import annotations

import sys

from krok_helper.stdio import configure_utf8_stdio


class _FakeStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


class _BrokenStream:
    def reconfigure(self, **kwargs: str) -> None:
        raise RuntimeError("stream is not reconfigurable")


def test_configure_utf8_stdio_reconfigures_windows_streams(monkeypatch) -> None:
    stdout = _FakeStream()
    stderr = _FakeStream()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    configure_utf8_stdio()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_configure_utf8_stdio_ignores_unavailable_streams(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", _BrokenStream())

    configure_utf8_stdio()
