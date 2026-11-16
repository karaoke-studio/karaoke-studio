"""CPU preview backpressure and frame-local reuse of mutable inputs."""

import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.render.effects.metrics import style_for_state
from krok_helper.subtitle_render.engine.value_signature import track_signature_for_windows
from krok_helper.subtitle_render.frontend.preview import preview_async as pa


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.001)
    assert predicate()


def test_cpu_requests_are_bounded_before_qt_queue_and_preserve_state_order(qapp, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    paints, submitted = [], []

    def paint(_painter, width, height, _track, t_ms, style, *_args, **_kwargs):
        paints.append((t_ms, width, height, style.font_size_px))
        if len(paints) == 1:
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(pa, 'paint_frame_to_painter', paint)
    monkeypatch.setattr(pa, '_prewarm_font_axis_capabilities', lambda *a: None)
    renderer = pa.AsyncSubtitleRenderer(320, 180)
    renderer._frame_requested.connect(submitted.append)
    try:
        track = TimingTrack(lines=[TimingLine(chars=[TimingChar('歌', 0)], end_ms=10000)])
        renderer.set_state(track, Style(font_size_px=30))
        renderer.request(0)
        wait_for(qapp, entered.is_set)
        for t_ms in range(1, 1001):
            renderer.request(t_ms)
        assert submitted == [0]
        renderer.set_state(track, Style(font_size_px=44))
        renderer.set_render_target(640, 360)
        renderer.request(9000)
        release.set()
        wait_for(qapp, lambda: len(paints) == 2 and not renderer._request_in_flight)
        assert submitted == [0, 9000]
        assert paints == [(0, 320, 180, 30), (9000, 640, 360, 44)]
    finally:
        release.set()
        renderer.stop()
    renderer.request(9500)
    qapp.processEvents()
    assert submitted == [0, 9000]


def test_empty_cpu_request_releases_backpressure(qapp, monkeypatch):
    paints = []
    monkeypatch.setattr(pa, 'paint_frame_to_painter', lambda *a, **kw: paints.append(a[4]))
    monkeypatch.setattr(pa, '_prewarm_font_axis_capabilities', lambda *a: None)
    renderer = pa.AsyncSubtitleRenderer(320, 180)
    try:
        renderer.request(0)
        wait_for(qapp, lambda: not renderer._request_in_flight)
        renderer.set_state(TimingTrack(), Style())
        renderer.request(123)
        wait_for(qapp, lambda: paints == [123])
    finally:
        renderer.stop()


def test_projected_styles_are_reused_only_within_immutable_frame():
    style = Style(decoration_kind='shadow', decoration_kind_before='none', decoration_before_sync=False)
    with layout_pass():
        before = style_for_state(style, after=False)
        after = style_for_state(style, after=True)
        assert before.decoration_kind == 'none'
        assert after.decoration_kind == 'shadow'
        with layout_pass():
            assert style_for_state(style, after=False) is before
        assert style_for_state(style, after=True) is after
    style.decoration_kind_before = 'glow'
    with layout_pass():
        changed = style_for_state(style, after=False)
        assert changed is not before
        assert changed.decoration_kind == 'glow'
    style.decoration_kind_before = 'none'
    assert style_for_state(style, after=False).decoration_kind == 'none'


def test_window_signature_is_reused_but_invalidates_on_next_frame_edit():
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar('歌', 0)], end_ms=1000)])
    with layout_pass():
        original = track_signature_for_windows(track)
        with layout_pass():
            assert track_signature_for_windows(track) is original
    track.lines[0].chars[0].start_ms = 100
    with layout_pass():
        assert track_signature_for_windows(track) != original


def test_style_projection_cache_is_thread_local():
    from concurrent.futures import ThreadPoolExecutor
    style = Style(decoration_kind='shadow', decoration_kind_before='none', decoration_before_sync=False)
    barrier = threading.Barrier(2)
    def project():
        with layout_pass():
            result = style_for_state(style, after=False)
            barrier.wait(timeout=5)
            assert style_for_state(style, after=False) is result
            return result
    with ThreadPoolExecutor(2) as pool:
        left, right = pool.submit(project), pool.submit(project)
        assert left.result() == right.result()
        assert left.result() is not right.result()
