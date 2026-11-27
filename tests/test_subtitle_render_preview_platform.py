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


def test_skia_preview_error_distinguishes_startup_failure_from_unsupported(monkeypatch, caplog):
    """启动/权限故障与工程不支持使用各自准确的提示文案，且必须写入日志。"""
    import logging
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg

    def run_case(reason: str) -> str:
        emitted = []
        view = SimpleNamespace(
            _use_skia_backend=True,
            _async_renderer=SimpleNamespace(selection=SimpleNamespace(target="gpu")),
            sender=lambda: None,
            _note_frame_delivered=lambda: None,
            set_skia_backend_enabled=lambda enabled, *, gpu_enabled: None,
            render_backend_label=lambda: "label",
            renderBackendChanged=SimpleNamespace(emit=lambda _label: None),
            gpuFallback=SimpleNamespace(emit=emitted.append),
        )
        pg.PreviewGraphicsView._on_skia_error(view, reason)
        return emitted[0]

    monkeypatch.setattr(pg.os, "name", "nt")
    with caplog.at_level(logging.WARNING):
        startup_msg = run_case("Skia 后端启动失败：[WinError 5] 拒绝访问")
        unsupported_msg = run_case("Skia 字幕渲染失败：Skia 暂不支持当前工程：rtl")
    assert "启动或运行失败" in startup_msg and "安全软件" in startup_msg
    assert "Skia 暂不支持当前工程" not in startup_msg
    assert "Skia 暂不支持当前工程" in unsupported_msg
    assert any("Skia 预览出错，自动切换旧渲染器" in rec.message for rec in caplog.records)


def test_sidecar_start_retries_transient_winerror5_and_wraps_final_failure(tmp_path, monkeypatch, caplog):
    """Windows 杀软短暂锁定触发 WinError 5 时自动重试；持续失败包装为 NativeRendererLaunchError。"""
    import logging
    from krok_helper.subtitle_render.native import backend as nb

    exe = tmp_path / "krok_skia_subtitle_renderer.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(nb.os, "name", "nt")
    monkeypatch.setattr(nb, "_LAUNCH_RETRY_DELAYS_S", (0.0, 0.0))

    attempts = {"count": 0}

    def fail_then_succeed(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            err = PermissionError(13, "拒绝访问")
            err.winerror = 5
            raise err
        raise FileNotFoundError("stop-after-retry")

    monkeypatch.setattr(nb.subprocess, "Popen", fail_then_succeed)
    proc = nb.NativeRendererProcess(executable_path=exe)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(nb.NativeRendererLaunchError) as caught:
            proc.start()
    assert attempts["count"] == 2
    assert isinstance(caught.value, nb.NativeRendererError)
    assert any("重试" in rec.message for rec in caplog.records)

    # 持续 WinError 5 时重试耗尽后给出明确的白名单提示
    always_denied = {"count": 0}

    def always_winerror5(*args, **kwargs):
        always_denied["count"] += 1
        err = PermissionError(13, "拒绝访问")
        err.winerror = 5
        raise err

    monkeypatch.setattr(nb.subprocess, "Popen", always_winerror5)
    proc2 = nb.NativeRendererProcess(executable_path=exe)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(nb.NativeRendererLaunchError, match="白名单") as caught2:
            proc2.start()
    assert always_denied["count"] == 3
    assert isinstance(caught2.value.__cause__, PermissionError)


def test_skia_sidecar_uses_relaxed_startup_and_configure_timeouts(tmp_path):
    """Skia sidecar 即使 response_timeout_s=5，启动/Vulkan 握手/配置也保留宽松超时。"""
    from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
    from krok_helper.subtitle_render.native.skia_process import (
        SKIA_CONFIGURE_TIMEOUT_S,
        SKIA_GPU_SELECT_TIMEOUT_S,
        SKIA_METRICS_TIMEOUT_S,
        SKIA_STARTUP_TIMEOUT_S,
    )
    from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess

    exe = tmp_path / "krok_skia_subtitle_renderer.exe"
    exe.write_bytes(b"MZ")
    renderer = SkiaRendererProcess(exe, response_timeout_s=5.0)
    assert renderer.response_timeout_s == 5.0
    assert renderer.startup_timeout_s == SKIA_STARTUP_TIMEOUT_S >= 15.0
    assert renderer.gpu_configure_timeout_s == SKIA_GPU_SELECT_TIMEOUT_S >= 30.0
    assert renderer.configure_timeout_s == SKIA_CONFIGURE_TIMEOUT_S >= 30.0

    metrics = SkiaTextMetricsProcess(exe)
    assert metrics.startup_timeout_s == SKIA_STARTUP_TIMEOUT_S
    assert metrics.configure_timeout_s == SKIA_METRICS_TIMEOUT_S >= 15.0


def test_skia_configure_does_not_misclassify_timeout_as_scene_unsupported(tmp_path, monkeypatch):
    """configure 等待超时/进程退出属于运行故障，不应被包装成 SkiaSceneUnsupportedError。"""
    from krok_helper.subtitle_render.native import skia_backend as sb
    from krok_helper.subtitle_render.native import skia_scene as ss
    from krok_helper.subtitle_render.native.backend import NativeRendererTimeoutError

    exe = tmp_path / "krok_skia_subtitle_renderer.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(ss, "prepare_skia_scene_ir", lambda *a, **k: {"skia_layout": {"metrics_identity": "m1"}})

    proc = sb.SkiaRendererProcess(exe)
    monkeypatch.setattr(proc, "_send", lambda payload: None)
    monkeypatch.setattr(
        proc,
        "_read_until_event",
        lambda *a, **k: (_ for _ in ()).throw(NativeRendererTimeoutError("timed out")),
    )
    with pytest.raises(NativeRendererTimeoutError):
        proc.configure(object(), object(), width=640, height=360, fps=60)

    monkeypatch.setattr(
        proc,
        "_read_until_event",
        lambda *a, **k: {"ok": False, "event": "error", "error": "unsupported feature"},
    )
    with pytest.raises(sb.SkiaSceneUnsupportedError, match="无法配置"):
        proc.configure(object(), object(), width=640, height=360, fps=60)


def test_skia_async_retries_transient_launch_failure_and_logs(qapp, monkeypatch, caplog):
    """SkiaAsyncSubtitleRenderer 对瞬时启动故障重试一次，彻底失败时记错误日志并上报。"""
    import logging
    import time
    from krok_helper.subtitle_render.frontend.preview import skia_async as sa
    from krok_helper.subtitle_render.native.backend import NativeRendererLaunchError

    monkeypatch.setattr(sa, "_TRANSIENT_RETRY_DELAY_S", 0.01)
    starts = {"count": 0}

    class FakeSkiaProcess:
        def __init__(self, *args, **kwargs):
            self.process_id = None
            self.executable_path = "fake-skia.exe"
            self.skia_info = {}

        def start(self):
            starts["count"] += 1
            raise NativeRendererLaunchError("无法启动字幕渲染进程（[WinError 5] 拒绝访问）")

        def close(self):
            pass

        def abort(self, **kwargs):
            pass

    monkeypatch.setattr(sa, "SkiaRendererProcess", FakeSkiaProcess)
    renderer = sa.SkiaAsyncSubtitleRenderer(640, 360, gpu_enabled=True)
    errors = []
    renderer.renderError.connect(errors.append)
    try:
        with caplog.at_level(logging.WARNING):
            renderer.set_state(object(), object())
            renderer.request(0)
            deadline = time.monotonic() + 5.0
            while not errors and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
        assert starts["count"] == 2
        assert errors and errors[0].startswith("Skia 后端启动失败：")
        assert any("瞬时失败" in rec.message for rec in caplog.records)
        assert any("Skia 预览失败" in rec.message for rec in caplog.records)
    finally:
        renderer.stop()
        qapp.processEvents()
