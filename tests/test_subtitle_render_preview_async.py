"""预览帧缓存（P-A，docs/字幕渲染-预渲染帧缓存方案评估.md §3.2）单元测试。

聚焦无需真实渲染/事件循环即可验证的契约：帧网格量子化纯函数、开关解析，以及
``AsyncSubtitleRenderer`` 的缓存命中/未命中/失效行为（直接驱动 GUI 线程槽，不依赖
worker 线程实际渲染）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend import preview_async  # noqa: E402
from krok_helper.subtitle_render.frontend.preview_async import (  # noqa: E402
    AsyncSubtitleRenderer,
    frame_cache_enabled,
    preview_frame_canonical_ms,
    preview_frame_index,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------- 纯函数


def test_frame_index_quantizes_nearby_times_to_same_bucket():
    # 60fps 网格：同一 ~16.7ms 桶内的连续墙钟时间落到同一帧索引。
    assert preview_frame_index(1000, fps=60) == preview_frame_index(1008, fps=60)
    assert preview_frame_index(1000, fps=60) == 60


def test_frame_index_canonical_round_trip():
    for index in (0, 1, 37, 600):
        ms = preview_frame_canonical_ms(index, fps=60)
        assert preview_frame_index(ms, fps=60) == index


def test_frame_index_handles_zero_fps_guard():
    # fps 兜底为 1，不抛除零。
    assert preview_frame_index(1234, fps=0) == 1


def test_frame_cache_flag_parsing(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", raising=False)
    assert frame_cache_enabled() is False  # 默认关
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    assert frame_cache_enabled() is True
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "off")
    assert frame_cache_enabled() is False


# --------------------------------------------------------------- 缓存行为


@pytest.fixture
def make_renderer(qapp):  # noqa: ARG001
    renderers: list[AsyncSubtitleRenderer] = []

    def _make() -> AsyncSubtitleRenderer:
        r = AsyncSubtitleRenderer(64, 36)
        renderers.append(r)
        return r

    yield _make
    for r in renderers:
        r.stop()


def _image() -> QImage:
    img = QImage(64, 36, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    return img


def test_request_returns_cached_frame_without_forwarding_to_worker(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    renderer = make_renderer()

    emitted: list[int] = []
    forwarded: list[int] = []
    renderer.frame_ready.connect(lambda _img, t: emitted.append(t))
    renderer._frame_requested.connect(lambda t: forwarded.append(t))  # noqa: SLF001

    # 模拟 worker 产出 t=100 的帧 → 入缓存 + 上抛一次。
    renderer._on_worker_frame(_image(), 100)  # noqa: SLF001
    assert emitted == [100]

    # 同桶请求 → 命中：再上抛一次（用规范 t），且不投 worker。
    renderer.request(100)
    assert len(emitted) == 2
    assert forwarded == []


def test_request_miss_forwards_canonical_frame_time(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    renderer = make_renderer()

    forwarded: list[int] = []
    renderer._frame_requested.connect(lambda t: forwarded.append(t))  # noqa: SLF001

    # 未命中 → 投 worker，且投的是吸附到网格的规范 t（非原始 t）。
    renderer.request(5008)
    expected = preview_frame_canonical_ms(preview_frame_index(5008))
    assert forwarded == [expected]


def test_set_state_invalidates_cache(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    renderer = make_renderer()

    forwarded: list[int] = []
    renderer._frame_requested.connect(lambda t: forwarded.append(t))  # noqa: SLF001

    renderer._on_worker_frame(_image(), 100)  # noqa: SLF001
    renderer.set_state(None, None)  # 轨道/样式变 → 清缓存

    renderer.request(100)  # 现在应未命中 → 投 worker
    assert forwarded == [preview_frame_canonical_ms(preview_frame_index(100))]


def test_set_render_target_invalidates_cache(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    renderer = make_renderer()

    forwarded: list[int] = []
    renderer._frame_requested.connect(lambda t: forwarded.append(t))  # noqa: SLF001

    renderer._on_worker_frame(_image(), 100)  # noqa: SLF001
    renderer.set_render_target(128, 72, 2.0)  # 尺寸/DPR 变 → 清缓存

    renderer.request(100)
    assert forwarded == [preview_frame_canonical_ms(preview_frame_index(100))]


def test_disabled_cache_forwards_raw_time_and_does_not_store(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "0")
    renderer = make_renderer()

    forwarded: list[int] = []
    renderer._frame_requested.connect(lambda t: forwarded.append(t))  # noqa: SLF001

    # 关闭时：worker 帧不入缓存，request 原样投递原始 t（行为与改造前一致）。
    renderer._on_worker_frame(_image(), 100)  # noqa: SLF001
    renderer.request(100)
    renderer.request(5008)
    assert forwarded == [100, 5008]


def test_cache_evicts_beyond_max(monkeypatch, make_renderer):
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "1")
    monkeypatch.setenv("KROK_SUBTITLE_PREVIEW_FRAME_CACHE_MAX", "3")
    renderer = make_renderer()

    for t in (0, 100, 200, 300, 400):  # 5 帧 → 上界 3
        renderer._on_worker_frame(_image(), t)  # noqa: SLF001
    assert len(renderer._frame_cache) == 3  # noqa: SLF001
    # 最早的被逐出（LRU）：保留最近 3 个索引。
    kept = set(renderer._frame_cache.keys())  # noqa: SLF001
    assert kept == {preview_frame_index(t) for t in (200, 300, 400)}
