"""Unsupported platforms must not enter the Direct2D restart loop."""

import os
import sys
from types import SimpleNamespace

import pytest

from krok_helper.subtitle_render.frontend.preview import preview_async as pa


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('platform_name,skia,frozen,expected', [
    ('posix', False, False, False),
    ('posix', True, False, True),
    # Packaged builds ship Skia (4.5.0); an explicit choice now works there too.
    ('posix', True, True, True),
    ('nt', False, False, True),
    ('nt', False, True, True),
])
def test_gpu_platform_gate_preserves_explicit_source_skia(monkeypatch, platform_name, skia, frozen, expected):
    monkeypatch.setattr(pa, 'os', SimpleNamespace(name=platform_name, environ=os.environ))
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia' if skia else 'native')
    monkeypatch.setenv('KROK_SUBTITLE_GPU_PREVIEW', '1')
    assert pa.gpu_preview_supported() is expected
    assert pa.gpu_preview_enabled() is expected


def test_unsupported_toggle_keeps_painter_and_reports_rejection(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.preview_view import PreviewPanel

    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'native')
    monkeypatch.setenv('KROK_SUBTITLE_GPU_PREVIEW', '1')
    monkeypatch.setenv('KROK_SUBTITLE_NATIVE_RENDER', '0')
    monkeypatch.setenv('KROK_SUBTITLE_ASYNC_PREVIEW', '1')
    monkeypatch.setattr(pa, 'gpu_preview_supported', lambda **_: False)
    def unexpected_gpu(*args, **kwargs):
        pytest.fail('Unsupported platform started a GPU renderer')
    monkeypatch.setattr(pg, 'GpuAsyncSubtitleRenderer', unexpected_gpu)
    panel = PreviewPanel()
    try:
        renderer = panel.canvas._async_renderer
        assert isinstance(renderer, pa.AsyncSubtitleRenderer)
        for index in range(10):
            assert panel.set_gpu_preview_enabled(True) is False
            assert panel.canvas._async_renderer is renderer
            panel.canvas.set_time(index * 100)
            qapp.processEvents()
        assert panel.set_gpu_preview_enabled(False) is True
    finally:
        panel.canvas.close()
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_unsupported_player_does_not_offer_gpu_retry(qapp, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    from krok_helper.subtitle_render.frontend.preview import player_window as pw

    monkeypatch.delenv('KROK_SUBTITLE_RENDER_BACKEND', raising=False)
    monkeypatch.setenv('KROK_SUBTITLE_NATIVE_RENDER', '0')
    monkeypatch.setattr(pa, 'gpu_preview_supported', lambda **_: False)
    monkeypatch.setattr(pw, 'gpu_preview_supported', lambda: False)
    owner = QWidget()
    window = pw.PreviewPlayerWindow(owner)
    try:
        requests = []
        window.gpuPreviewRequested.connect(lambda: requests.append(True))
        window._set_render_backend_label('CPU')
        assert not window._backend_label.isEnabled()
        assert '建议开启' not in window._backend_label.text()
        assert '当前平台' in window._backend_label.toolTip()
        window._backend_label.click()
        window._confirm_gpu_preview()  # Also protect programmatic requests.
        assert requests == []
    finally:
        window.preview_panel.canvas.close()
        window.close()
        window.deleteLater()
        owner.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("platform,target,expected_gpu", [
    ("nt", "gpu", True), ("nt", "raster", False), ("posix", "gpu", False),
])
def test_skia_preview_error_falls_back_to_the_old_renderer(monkeypatch, platform, target, expected_gpu):
    """Skia 预览失败时自动改用旧渲染器：Windows 保持 GPU（D2D），其他平台回到 Painter。"""
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    calls, emitted = [], []
    view = SimpleNamespace(
        _use_skia_backend=True,
        _async_renderer=SimpleNamespace(selection=SimpleNamespace(target=target)),
        sender=lambda: None,
        _note_frame_delivered=lambda: None,
        set_skia_backend_enabled=lambda enabled, *, gpu_enabled: calls.append((enabled, gpu_enabled)),
        render_backend_label=lambda: "label",
        renderBackendChanged=SimpleNamespace(emit=lambda _label: None),
        gpuFallback=SimpleNamespace(emit=emitted.append),
    )
    monkeypatch.setattr(pg.os, "name", platform)
    pg.PreviewGraphicsView._on_skia_error(view, "原因")
    assert calls == [(False, expected_gpu)]
    assert emitted and "已自动改用旧渲染器" in emitted[0] and "原因" in emitted[0]
