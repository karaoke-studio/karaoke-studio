"""Off-GUI-thread subtitle rasterisation for the preview (experimental).

Background (§9 A4 诊断)：预览预览的真实帧率天花板**不是单帧光栅化成本本身**，而是
字幕 paint 在 GUI 主线程上与视频呈现循环**串行**——单帧 14–20ms 的矢量/glow 栅格化
直接加进每帧周期，把 60Hz 的呈现循环拖到 ~30–35Hz（`--no-subtitle` 时循环可跑满 60）。

本模块把字幕栅格化搬到**独立工作线程**：worker 渲染进 ``QImage``，GUI 线程的
``SubtitleGraphicsItem.paint`` 只做一次廉价 blit。主循环不再被 14ms 阻塞 → 呈现回到
~60Hz；字幕内容按 worker 产出速率刷新（latest-wins 合并，丢弃过期请求）。

默认开启；env ``KROK_SUBTITLE_ASYNC_PREVIEW=0`` 可回退同步预览（导出路径不受影响）。
"""

from __future__ import annotations

import errno
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal as Signal, pyqtSlot as Slot
from PyQt6.QtGui import QGuiApplication, QImage, QPainter

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
from krok_helper.subtitle_render.engine.render_progress import render_progress_scope
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.native.backend import (
    NativeQueueFullError,
    NativeRendererError,
    NativeRendererProcess,
    NativeRendererProcessOwner,
    SharedFrameRingReader,
    StaleSharedFrameSlotError,
)
from krok_helper.subtitle_render.native.protocol import (
    collect_referenced_font_families,
    gpu_unsupported_feature_labels,
    gpu_unsupported_features,
)
from krok_helper.subtitle_render.engine.render.render_ir import build_style_patch_ir


class _ConfigPhaseError(NativeRendererError):
    """configure/resize/start 阶段的失败标记。

    这类失败最常见的形态是 sidecar native 楔死（共享 D3D 并发把渲染任务
    卡死、pause 排空超时后主循环全哑），重发到死进程只会每轮白等一个
    超时；帧级温和重试（连续 5 次才重启）会把恢复拖到分钟级。配置阶段
    失败必须立即进失败链杀进程重建（2026-10 拖大后永久卡死根因）。
    """


_log = logging.getLogger(__name__)


def _preview_diagnostic(*args, **kwargs) -> None:
    """诊断输出句柄失效不能让已成功的 GPU 帧进入失败恢复链。"""
    try:
        print(*args, **kwargs)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EPIPE):
            raise



def style_patch_base_key(
    track: TimingTrack,
    style: Style,
    extra_tracks: list[TimingTrack],
    *,
    width: int,
    height: int,
    fps: int,
    dpr: float,
    include_layout_signature: bool = True,
) -> tuple:
    """差分样式更新（``gpu_configure_style``）的资格闸门 key。

    key 相等 ⇔ 轨道内容、布局相关样式（逐源）、画面参数全部没变——此时
    行数据 IR 与上一次全量 configure 逐字节同源，sidecar 可以只重放样式段。
    任何一项变化都让 key 失配，自动回落全量重配；与布局计划缓存用的是同一
    套 ``lyric_layout_style_signature`` 语义（颜色等纯绘制字段不进签名，
    正是差分要优化的场景）。
    """
    from krok_helper.subtitle_render.domain.models import style_for_track
    from krok_helper.subtitle_render.engine.value_signature import (
        lyric_layout_style_signature,
        track_signature_for_windows,
        value_signature,
    )

    # 轨道用窗口语义归一化签名（P5）：逐行动画类型互换（时长不变、
    # none 性不变）不改窗口与几何 → 闸门保持命中，走 lines_style 差分
    # （行级动画字段随载荷下发）。布局签名保持严格：任何布局输入变化
    # 仍全量重配。
    parts: list[object] = [track_signature_for_windows(track)]
    if include_layout_signature:
        # layout scope（P6）：布局签名刻意缺席——行级摆放字段随差分载荷
        # 原位更新，闸门只须保证轨道内容与画面没变。
        parts.append(lyric_layout_style_signature(style))
    for source in extra_tracks or ():
        source_style = style_for_track(style, source)
        parts.append(value_signature(source))
        if include_layout_signature:
            parts.append(lyric_layout_style_signature(source_style))
    return (width, height, fps, round(float(dpr or 1.0), 4), tuple(parts))


PREVIEW_QUALITY_OPTIONS: tuple[tuple[str, str, float], ...] = (
    ("low", "流畅（1/4）", 0.25),
    ("medium", "均衡（1/2）", 0.5),
    ("high", "清晰（1/1）", 1.0),
)
DEFAULT_PREVIEW_QUALITY = "high"
_PREVIEW_QUALITY_SCALES = {
    key: scale for key, _label, scale in PREVIEW_QUALITY_OPTIONS
}


def normalize_preview_quality(value: object) -> str:
    """Return a stable preview-quality key, falling back to full quality."""
    key = str(value or "").strip().lower()
    return key if key in _PREVIEW_QUALITY_SCALES else DEFAULT_PREVIEW_QUALITY


def preview_quality_render_scale(display_scale: float, quality: object) -> float:
    """Cap subtitle raster scale for a quality tier without invisible oversampling.

    Full quality deliberately preserves the existing display-native behaviour,
    including DPR > 1 for a small project shown in a large window. Lower tiers
    follow N3's 1/4 and 1/2 project-space render targets, capped by the actual
    display scale so a small preview never renders pixels it cannot show.
    """
    safe_display_scale = max(float(display_scale or 1.0), 0.01)
    key = normalize_preview_quality(quality)
    if key == DEFAULT_PREVIEW_QUALITY:
        return safe_display_scale
    return max(min(safe_display_scale, _PREVIEW_QUALITY_SCALES[key]), 0.01)


def _env_enabled(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


_RENDER_STAGE_LABELS = {
    "display": "显示窗口",
    "page_offsets": "页偏移",
    "lines": "逐行排版",
}

# 各阶段在总进度里的起止比例。display 阶段的实测碰撞循环带逐行回调，
# 占用最大权重让百分比从接近 0% 连续爬升；GPU 路径的 configure 除整轨重排
# （引擎刻度）外还有 sidecar 场景构建与首帧实现两段无 Python 刻度的等待，
# 压缩在高位（92% 之后）；Painter 路径整段就是重排，直接爬到 100%。
_RENDER_STAGE_SPANS_PAINTER = {
    "display": (0.00, 0.60),
    "page_offsets": (0.60, 0.82),
    "lines": (0.82, 1.00),
}
_RENDER_STAGE_SPANS_GPU = {
    "display": (0.00, 0.60),
    "page_offsets": (0.60, 0.72),
    "lines": (0.72, 0.78),
}


def _render_progress_reporter(spans, emit):
    """把引擎 ``(stage, done, total)`` 刻度合成单调整数百分比再 ``emit``。

    同一次重排内阶段会交错复用（页偏移解析内部复跑显示窗口解析），取历史
    最大值防回退；只在整数百分比变化时发射，避免长曲目的信号风暴。99% 封顶，
    100% 保留给真实帧完成（届时徽标由帧到达隐藏）。
    """
    state = {"percent": -1}

    def report(stage: str, done: int, total: int) -> None:
        span = spans.get(stage)
        if span is None or total <= 0:
            return
        fraction = min(max(float(done) / float(total), 0.0), 1.0)
        percent = int(round((span[0] + (span[1] - span[0]) * fraction) * 100))
        if percent > state["percent"]:
            state["percent"] = percent
            emit(min(percent, 99), _RENDER_STAGE_LABELS.get(stage, stage))

    return report


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(int(value), int(minimum))


class _GpuRestartBreaker:
    """GPU sidecar 重启断路器（2026-10 看门狗方案）。

    旧机制只有墙钟超时 + 1s 后无限重试：合法长任务（密集符号的场景构建/
    realization 预热可达数十秒）被超时误杀，重启丢光进度后重付同样的工作、
    再次超时——「活着却被反复重启」的西西弗斯循环，残留进程/共享环段随之
    叠加。断路器在滑动窗口内记录失败重启次数，超限即熔断：本会话停用 GPU
    预览、固定回退 Painter 并明确告知，循环在构造上被禁止。心跳续租
    （backend.heartbeat_lease_s）负责让长任务不再被判死，本闸只兜底真故障。
    """

    def __init__(self, window_s: float | None = None, limit: int | None = None):
        self.window_s = float(
            window_s
            if window_s is not None
            else _env_int("KROK_SUBTITLE_GPU_RESTART_WINDOW_S", 60)
        )
        self.limit = int(
            limit
            if limit is not None
            else _env_int("KROK_SUBTITLE_GPU_RESTART_LIMIT", 3)
        )
        self._restart_times: list[float] = []
        self._open = False

    @property
    def open(self) -> bool:
        return self._open

    def record(self, now: float | None = None) -> bool:
        """记录一次失败重启；返回本窗内累计次数是否已达熔断阈值。"""
        current = time.monotonic() if now is None else now
        self._restart_times = [
            t for t in self._restart_times if current - t < self.window_s
        ] + [current]
        if len(self._restart_times) >= self.limit:
            self._open = True
            return True
        return False


def _default_native_preview_threads() -> int:
    return min(max(os.cpu_count() or 4, 1), 6)


_FALLBACK_CHANGED_COOLDOWN_S = 10.0


class _FallbackReportGate:
    """GPU/native 预览回退原因的上报闸：按原因去重 + 冷却 + 文件日志。

    旧行为是渲染器整个生命周期只上报第一次回退，之后原因变化（sidecar 崩溃、
    场景构建异常…）对用户完全不可见，事后也无日志可查。本闸的语义：

    - 与上次已上报相同的原因：完全静默（能力回退会随每个非投机请求命中一次，
      必须永久抑制避免风暴）。
    - 新原因：无论是否在冷却期内，都先以 WARNING 落 ``lin-k-lyrics.log``，
      保证事后可查；InfoBar 信号受冷却约束——冷却期内的新原因只落日志，
      冷却结束后该原因再次命中才弹出，避免重试风暴刷屏。
    """

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        log_label: str,
        cooldown_s: float | None = None,
    ) -> None:
        self._emit = emit
        self._log_label = str(log_label)
        self._cooldown_s = (
            _FALLBACK_CHANGED_COOLDOWN_S if cooldown_s is None else max(float(cooldown_s), 0.0)
        )
        self._last_message: str | None = None
        self._logged_messages: set[str] = set()
        self._emitted_at = 0.0

    def report(self, message: str) -> None:
        text = str(message)
        if text == self._last_message:
            return
        if text not in self._logged_messages:
            self._logged_messages.add(text)
            _log.warning("%s：%s", self._log_label, text)
        if (
            self._last_message is not None
            and time.monotonic() - self._emitted_at < self._cooldown_s
        ):
            return
        self._last_message = text
        self._emitted_at = time.monotonic()
        self._emit(text)


def async_preview_enabled() -> bool:
    return _env_enabled("KROK_SUBTITLE_ASYNC_PREVIEW", "1")


def native_preview_enabled() -> bool:
    """实验开关：显式 ``KROK_SUBTITLE_NATIVE_RENDER=1`` 才启用 native 预览，默认关闭。

    2026-07-19 起从硬关闭恢复为 env opt-in，用于验证修复后的调度器
    （见 GPU 计划文档 §2.5 与 G2 调度硬性要求）；产品 UI 不暴露该开关。
    """
    return _env_enabled("KROK_SUBTITLE_NATIVE_RENDER", "0")


def _gpu_preview_default_enabled() -> bool:
    """Enable G5 by default only for interactive Windows sessions."""
    qpa_platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    return os.name == "nt" and qpa_platform not in {"offscreen", "minimal"}


def gpu_preview_enabled() -> bool:
    """Enable the stable G5 shared-memory preview by default on Windows."""
    default = "1" if _gpu_preview_default_enabled() else "0"
    return _env_enabled("KROK_SUBTITLE_GPU_PREVIEW", default)


def native_due_queue_capacity(
    stale_tolerance_ms: float, fps: int, store_capacity: int
) -> int:
    """G6 到点队列容量：容忍窗内帧数，钳到帧仓容量。

    渲染前沿登记进仓才有槽可落，队列超仓只会制造「登记不进仓、present
    必丢」的无效渲染。默认仓 25 槽下 60/120fps 的容忍窗（8/15 帧）都装
    得下，钳制只在 env 缩仓时生效。
    """
    interval_ms = 1000.0 / max(int(fps), 1)
    return min(
        max(2, int(float(stale_tolerance_ms) / interval_ms) + 1),
        max(int(store_capacity), 1),
    )


def _gpu_direct_present_preference() -> bool:
    """Read the persisted G6 preference (set by the export-page toggle)."""
    try:
        from krok_helper.subtitle_render.settings.store import (
            SubtitleRenderSettingsStore,
        )
        from krok_helper.settings import load_app_settings

        store = SubtitleRenderSettingsStore(load_app_settings)
        data = store.load()
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        return bool(output.get("gpu_direct_present", False))
    except Exception:  # noqa: BLE001 - 设置读取失败按关处理
        return False


def _prewarm_font_axis_capabilities(
    track: Optional[TimingTrack],
    style: Optional[Style],
    extra_tracks: Optional[list[TimingTrack]],
) -> None:
    """GUI 线程预热字体能力缓存，让渲染线程 configure 只读缓存、不碰 Qt 字体库。

    ``apply_resolved_font_faces``（native.protocol）随 IR 下发 ``font_axis``
    标记，内部对每个 ``*font_family`` 槽位调 ``get_capabilities``——那会枚举
    QFontDatabase / QRawFont，必须留在 GUI 线程（EMBEDDING §8）。渲染线程
    持 Qt 字体互斥锁建引擎时一旦引擎创建发出 Qt 告警，消息处理器还要抢
    GIL / 写面包屑文件，等同一把锁的 GUI 线程（列表 setText→字体度量）就
    被拖成分钟级「未响应」（2026-10 用户必现，py-spy 双线程栈定位）。
    预热后 ``get_capabilities`` 走进程级缓存 dict 命中，渲染线程零 Qt 调用。
    只在本线程即应用线程时执行；非 GUI 线程调用（测试等）安全跳过。
    """
    app = QGuiApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        return
    try:
        from krok_helper.subtitle_render.engine.text.font_capabilities import (
            get_capabilities,
        )

        families = collect_referenced_font_families(
            track, style, *(extra_tracks or ())
        )
        for family in families:
            get_capabilities(family)
    except Exception:  # noqa: BLE001 — 预热失败不阻断预览，渲染线程回退原路径
        _log.debug("字体能力缓存预热失败", exc_info=True)


def gpu_native_preview_enabled() -> bool:
    """G6 DirectComposition 直画上屏（2026-10 用户决定重开，默认关）。

    开启后 sidecar 在预览 viewport 下创建 DirectComposition 子窗口，
    字幕层直接上屏（零回读/零共享内存/零 QImage），roundtrip 从 ~55ms
    到 ~5ms。开关在导出页「使用 GPU 渲染字幕预览」下面；失败自动回退
    G5（shared-memory/QImage）。

    env ``KROK_SUBTITLE_GPU_NATIVE_PREVIEW`` **存在即权威**（"1"/"0"）：
    开关处理器在重建渲染器前把它同步写成本次开关值——持久化偏好是防抖
    落盘的，渲染器重建发生在落盘之前，若 env 只能单向强制开（"0" 按未设
    处理回读磁盘），关闭开关会读到旧偏好导致 G6 关不掉（2026-10 实测）。
    env 缺省时才读磁盘偏好（进程启动 / 外部未干预的正常路径）。
    """
    env_raw = os.environ.get("KROK_SUBTITLE_GPU_NATIVE_PREVIEW")
    if env_raw is not None and env_raw.strip() != "":
        return _env_enabled("KROK_SUBTITLE_GPU_NATIVE_PREVIEW", "0")
    return _gpu_direct_present_preference()


def native_preview_timestamps(
    t_ms: int,
    *,
    playing: bool,
    fps: int,
    lookahead_frames: int,
    include_current: bool = True,
) -> list[int]:
    """Return current frame plus optional playback look-ahead timestamps."""
    current = int(t_ms)
    if not playing:
        return [current] if include_current else []
    normalized_fps = max(int(fps), 1)
    frame_ms = 1000.0 / normalized_fps
    count = max(int(lookahead_frames), 0)
    start_offset = 0 if include_current else 1
    timestamps = [
        int(round(current + frame_ms * offset))
        for offset in range(start_offset, count + 1)
    ]
    return list(dict.fromkeys(timestamps))


class NativePreviewFrameCache:
    """Small thread-safe QImage cache for native preview look-ahead frames."""

    def __init__(self, max_frames: int, fps: int = 60) -> None:
        self._max_frames = max(int(max_frames), 1)
        self._fps = max(int(fps), 1)
        self._images: OrderedDict[int, QImage] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, t_ms: int) -> int:
        return int(round(int(t_ms) * self._fps / 1000.0))

    def key_for(self, t_ms: int) -> int:
        return self._key(t_ms)

    def timestamp_for_key(self, key: int) -> int:
        return int(round(int(key) * 1000.0 / self._fps))

    def store(self, t_ms: int, image: QImage) -> None:
        copied = image.copy()
        with self._lock:
            key = self._key(t_ms)
            self._images.pop(key, None)
            self._images[key] = copied
            while len(self._images) > self._max_frames:
                self._images.popitem(last=False)

    def contains_key(self, key: int) -> bool:
        with self._lock:
            return int(key) in self._images

    def size(self) -> int:
        with self._lock:
            return len(self._images)

    def capacity(self) -> int:
        return self._max_frames

    def take(self, t_ms: int) -> Optional[QImage]:
        with self._lock:
            # store() 已复制一份私有拷贝；pop 后缓存不再持有引用，直接移交即可。
            return self._images.pop(self._key(t_ms), None)

    def evict_before(self, key: int) -> int:
        """丢弃所有帧键 < ``key`` 的条目，返回逐出数。

        播放头之前的键永远不会再被 take() 命中（请求只前进；seek 走
        代际变化 + clear）。若不主动清扫，被请求跳过的已填键会累积成
        「死键」占满容量 → free_slots=0 → 永久停填 → 播放几秒后完全
        冻结（2026-10 G5 长跑楔死，15s 探针 t=5s 起 hits+0 / cache 满）。
        """
        with self._lock:
            dead = [cached for cached in self._images if cached < int(key)]
            for cached in dead:
                del self._images[cached]
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._images.clear()


class _NullNativeRenderer:
    """cpu 填缝调度下 renderer 不被使用的占位（保持调度器签名统一）。"""


_NULL_RENDERER = _NullNativeRenderer()


class NativePreviewStats:
    """Thread-safe counters for native preview scheduler diagnostics."""

    _COUNTERS = (
        "cache_hits",
        "cache_misses",
        "future_frames_cached",
        "stale_frames_dropped",
        "generations_cancelled",
        "native_generation_cancelled_events",
        "range_done_events",
        "native_renderer_failures",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {key: 0 for key in self._COUNTERS}

    def note_cache_hit(self) -> None:
        self._increment("cache_hits")

    def note_cache_miss(self) -> None:
        self._increment("cache_misses")

    def note_future_frame_cached(self) -> None:
        self._increment("future_frames_cached")

    def note_stale_frame_dropped(self) -> None:
        self._increment("stale_frames_dropped")

    def note_generation_cancelled(self) -> None:
        self._increment("generations_cancelled")

    def note_native_generation_cancelled_event(self) -> None:
        self._increment("native_generation_cancelled_events")

    def note_range_done_event(self) -> None:
        self._increment("range_done_events")

    def note_native_renderer_failure(self) -> None:
        self._increment("native_renderer_failures")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def _increment(self, key: str) -> None:
        with self._lock:
            self._values[key] += 1


# sidecar 渲染纹理的单边硬上限（gpu_resize_target 的 1..8192 校验）。
# 超限的尺寸在源头按比例降 dpr：预览显示像素数是窗口物理像素，本来就
# 渲染不到更多（2026-10 实测：4K 工程 × 高 DPR 拖大窗口时越限报错 →
# 杀进程重启链 2.5s/次 × 5 连败 ≈ 12 秒卡顿 + 降级弹窗）。
_RENDER_TARGET_MAX_DIMENSION = 8192


def preview_render_target_size(
    logical_width: int,
    logical_height: int,
    device_pixel_ratio: float,
) -> tuple[int, int, float]:
    """Return physical image size + normalized DPR for async preview rendering."""
    logical_w = max(int(logical_width), 1)
    logical_h = max(int(logical_height), 1)
    dpr = max(float(device_pixel_ratio or 1.0), 0.01)
    dpr = min(
        dpr,
        _RENDER_TARGET_MAX_DIMENSION / max(logical_w, logical_h),
    )
    return (
        max(int(round(logical_w * dpr)), 1),
        max(int(round(logical_h * dpr)), 1),
        dpr,
    )


class _AsyncSubtitleWorker(QObject):
    """Qt-thread resident worker that rasterises latest-wins subtitle requests."""

    frame_ready = Signal(QImage, int)
    render_progress = Signal(int, str)
    finished = Signal()

    def __init__(self, width: int, height: int) -> None:
        super().__init__()
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._duration_ms: int = 0
        self._extra_tracks: list[TimingTrack] = []
        self._pending_t: Optional[int] = None
        self._rendering = False
        self._stopping = False

    @Slot(object, object, object, object)
    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: object = None,
        duration_ms: object = None,
    ) -> None:
        self._track = track
        self._style = style
        self._extra_tracks = list(extra_tracks) if isinstance(extra_tracks, (list, tuple)) else []
        self._duration_ms = max(int(duration_ms or 0), 0)

    @Slot(int, int, float)
    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)

    @Slot(int)
    def request(self, t_ms: int) -> None:
        self._pending_t = int(t_ms)
        if not self._rendering:
            QTimer.singleShot(0, self._render_pending)

    @Slot()
    def stop(self) -> None:
        self._stopping = True
        if not self._rendering:
            self.finished.emit()

    def _render_pending(self) -> None:
        if self._stopping:
            self.finished.emit()
            return
        if self._pending_t is None:
            return
        t_ms = self._pending_t
        self._pending_t = None
        track = self._track
        style = self._style
        extra_tracks = self._extra_tracks
        duration_ms = self._duration_ms
        logical_w = self._logical_w
        logical_h = self._logical_h
        dpr = self._device_pixel_ratio
        if track is None or style is None:
            return

        self._rendering = True
        try:
            physical_w, physical_h, dpr = preview_render_target_size(logical_w, logical_h, dpr)
            image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(0)
            painter = QPainter(image)
            try:
                with render_progress_scope(
                    _render_progress_reporter(
                        _RENDER_STAGE_SPANS_PAINTER, self.render_progress.emit
                    )
                ):
                    paint_frame_to_painter(
                        painter,
                        logical_w,
                        logical_h,
                        track,
                        int(t_ms),
                        style,
                        extra_tracks,
                        duration_ms=duration_ms,
                    )
            finally:
                painter.end()
            self.frame_ready.emit(image, int(t_ms))
        finally:
            self._rendering = False

        if self._stopping:
            self.finished.emit()
        elif self._pending_t is not None:
            QTimer.singleShot(0, self._render_pending)


class AsyncSubtitleRenderer(QObject):
    """Renders subtitle frames on a worker thread; emits :pyattr:`frame_ready`.

    协议：GUI 线程通过 :meth:`set_state` / :meth:`set_size` 更新轨道/样式/尺寸，
    通过 :meth:`request` 投递目标时间（latest-wins 合并）。worker 渲染完成后从工作
    线程 emit ``frame_ready(QImage, t_ms)``——接收方须用 ``QueuedConnection`` 接到
    GUI 线程槽（QImage 跨线程经队列连接复制句柄，安全）。内部使用 ``QThread``，
    避免 Qt 图形对象在普通 Python 线程里启动 ``QBasicTimer``。
    """

    frame_ready = Signal(QImage, int)
    renderProgress = Signal(int, str)
    backendModeChanged = Signal(str)
    _state_changed = Signal(object, object, object, object)
    _target_changed = Signal(int, int, float)
    _frame_requested = Signal(int)

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._stopped = False
        self._thread = QThread(self)
        self._thread.setObjectName("subtitle-preview-render")
        self._worker = _AsyncSubtitleWorker(self._logical_w, self._logical_h)
        self._worker.moveToThread(self._thread)
        self._state_changed.connect(self._worker.set_state)
        self._target_changed.connect(self._worker.set_render_target)
        self._frame_requested.connect(self._worker.request)
        self._worker.frame_ready.connect(self.frame_ready)
        self._worker.render_progress.connect(self.renderProgress)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    # ------------------------------------------------------------------ GUI API

    def __del__(self) -> None:
        try:
            self.stop()
        except RuntimeError:
            pass

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,
        *,
        duration_ms: int | None = None,
    ) -> None:
        if self._stopped:
            return
        _prewarm_font_axis_capabilities(track, style, extra_tracks)
        self._track = track
        self._style = style
        self._state_changed.emit(
            track,
            style,
            list(extra_tracks or ()),
            max(int(duration_ms or 0), 0),
        )

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        if self._stopped:
            return
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)
        self._target_changed.emit(self._logical_w, self._logical_h, self._device_pixel_ratio)

    def request(self, t_ms: int) -> None:
        """投递一帧渲染请求；只保留最新 t（合并掉过期请求）。"""
        if self._stopped:
            return
        self._frame_requested.emit(int(t_ms))

    def set_playing(self, playing: bool) -> None:  # noqa: ARG002
        """Playback state hook kept for API symmetry with the native preview path."""
        return

    @property
    def current_backend_mode(self) -> str:
        """Painter 渲染器恒为 CPU（与 GPU 渲染器的 API 对称，见该类注释）。"""
        return "cpu"

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._worker._stopping = True  # noqa: SLF001
        except RuntimeError:
            pass
        self._thread.quit()
        if not self._thread.wait(2000):
            self._thread.quit()
            self._thread.wait(1000)


class GpuAsyncSubtitleRenderer(QObject):
    """Bounded latest-wins G2 preview scheduler for the Direct2D backend.

    There is one bounded sidecar batch and at most one pending batch anchor. A
    pending timestamp can be replaced, never appended. During playback the
    workers render ahead of the media clock; a frame is emitted only when the
    matching media-clock request consumes it from the bounded cache.
    """

    frame_ready = Signal(QImage, int)
    frame_presented = Signal(int)
    renderProgress = Signal(int, str)
    fallback_occurred = Signal(str)
    backendModeChanged = Signal(str)

    _STALE_TOLERANCE_MS = 120
    _SEEK_DISCONTINUITY_MS = 250

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._extra_tracks: list[TimingTrack] = []
        self._duration_ms: int = 0
        self._generation = 0
        self._request_serial = 0
        self._latest_t: Optional[int] = None
        self._pending: Optional[tuple[int, int, bool, float]] = None
        self._needs_configure = True
        self._needs_target_resize = False
        self._relayout_scope: Optional[str] = None
        # 差分样式更新的资格闸门：上次成功（全量/缩放）configure 时的
        # style_patch_base_key。None = 尚无基准（首配 / sidecar 重建后）。
        self._style_patch_key: Optional[tuple] = None
        self._playing = False
        self._stopped = False
        self._renderer_failed = False
        # 重启断路器（见 _GpuRestartBreaker）：窗口内失败重启超限即熔断，
        # 本渲染器生命周期内固定回退 Painter，禁止无限重启叠加。
        self._gpu_restart_breaker = _GpuRestartBreaker()
        self._fallback_gate = _FallbackReportGate(
            self.fallback_occurred.emit, log_label="GPU 字幕预览回退"
        )
        self._retry_after = 0.0
        self._force_warp = _env_enabled("KROK_SUBTITLE_GPU_FORCE_WARP", "0")
        self._native_preview = gpu_native_preview_enabled()
        self._realization_ready_generation: Optional[int] = None
        self._g6_present_count = 0
        if self._native_preview:
            _preview_diagnostic(
                "[GPU 预览] 渲染器启动: G6 DirectComposition 直画模式",
                flush=True,
            )
        # G6 连续失败计数：达到阈值后永久降级到 G5（本渲染器生命周期内），
        # 不再重试 G6（避免在不支持 DComp 的机器上无限重启循环）。
        # G6/G5 共用的连续失败判定阈值：能出帧说明显卡在正常工作，
        # 偶发超时（驱动电源切换/别的进程抢占）不应触发重启或降级。
        # 成功出帧即清零；连续达到阈值才降级/重启（2026-10 用户拍板）。
        self._consecutive_failure_limit = 5
        self._native_preview_failures = 0
        # 默认 4 worker（2026-10 用户拍板「4worker 必做，一定有低端机」）：
        # G5 读回管线渲染+回读串行时延高，2 worker 填缝追不上 60fps 消费；
        # 低端机更显式需要 4 路并行摊薄单帧成本。env 可覆盖，上限 8。
        self._worker_count_requested = _env_int(
            "KROK_SUBTITLE_GPU_WORKERS", 4, minimum=1
        )
        self._worker_count_requested = min(self._worker_count_requested, 8)
        # 注意：这里不再按 native 模式把 _worker_count_requested 冻结成 1。
        # 冻结会让「G6 建立的渲染器热切换回 G5」永远单 worker。native 需要
        # worker=1 是模式约束（直画渲染走主后端），由 _worker_request_for_mode
        # 在 configure/resize 调用点按当前模式取值，切回 G5 即恢复多 worker。
        self._active_worker_count = 1
        self._native_target: Optional[
            tuple[int, int, int, int, int, int, int]
        ] = None
        # sidecar 里是否可能还挂着 DComp 子窗口；True 时若 target 被清空，
        # worker 会通过 close 哨兵主动撤掉，避免隐藏视图后残留画面。
        self._native_child_open = False
        self._native_close_requested = False
        # G6 空闲心跳间隔：暂停/空闲下没有 present 顺带泵消息，子窗口积压
        # 的鼠标转发消息（→父窗口→Qt 控件）靠它周期投递，悬浮控件才有
        # hover/点击。
        self._NATIVE_IDLE_PUMP_S = 0.03
        # G6 直画帧仓容量（2026-10 用户拍板与 G5 帧缓存同口径：
        # max_lookahead 24 + native 单 worker 1 + 1 = 25 槽；与 sidecar
        # Impl::frameStoreCapacity 同 env 同默认）。sidecar 侧 direct 渲染
        # 完成后按 (generation, t_ms) 登记进仓、present 按同一身份取槽上
        # 屏；调度队列容量钳到仓容量，渲染前沿才永远装得进仓。
        self._native_frame_store_capacity = _env_int(
            "KROK_SUBTITLE_GPU_FRAME_STORE", 25, minimum=1
        )
        # 同键去重 + 时延感知投喂（2026-10 用户提议的追帧/降无效帧方案）。
        # _render_ms_ema 服务 GPU 直渲/读回路径；CPU 回退帧量级差一个数量
        # 级，单独一条 EMA。
        self._native_last_presented: Optional[tuple[int, int]] = None
        self._render_ms_ema = 0.0
        # 读回填缝的前沿前移量（毫秒，控制器式 EMA 自校正）：让帧的完成
        # 时刻恰好落在请求键上，避免填进缓存时播放头已越过该键。
        self._fill_lead_ms = 0.0
        # G6 到点调度器的媒体时钟锚点。
        self._g6_media_t = 0
        self._g6_media_wall = 0.0
        self._cpu_render_ms_ema = 0.0
        self._native_project_ahead = _env_enabled(
            "KROK_SUBTITLE_G6_PROJECT_AHEAD", "1"
        )
        self._lookahead_frames = _env_int(
            "KROK_SUBTITLE_GPU_LOOKAHEAD_FRAMES", 12, minimum=0
        )
        # 热切换（set_native_mode）G6→G5 时恢复的配置值。
        self._configured_lookahead_frames = self._lookahead_frames
        if self._native_preview:
            self._lookahead_frames = 0
        self._max_lookahead_frames = max(
            self._lookahead_frames,
            _env_int(
                "KROK_SUBTITLE_GPU_MAX_LOOKAHEAD_FRAMES",
                24,
                minimum=0,
            ),
        )
        self._effective_lookahead_frames = self._lookahead_frames
        self._frame_cache = NativePreviewFrameCache(
            max(self._max_lookahead_frames + self._worker_count_requested + 1, 1)
        )
        self._renderer_owner = NativeRendererProcessOwner(
            process_factory=NativeRendererProcess,
            response_timeout_s=2.0,
            startup_timeout_s=5.0,
            configure_timeout_s=10.0,
            gpu_configure_timeout_s=30.0,
            close_timeout_s=1.0,
        )
        self._reader: Optional[SharedFrameRingReader] = None
        self._shm_key = f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
        self._frame_index = 0
        self._condition = threading.Condition()
        self._stats_lock = threading.Lock()
        # 最近一次确认的实际出帧后端（"gpu"=sidecar / "cpu"=Painter 回退）；
        # None = 尚无帧定论（GUI 侧按渲染器选择展示）。仅在翻转时发信号。
        self._backend_mode: Optional[str] = None
        # 连续帧级失败计数：首个失败原样重试一次（有界回读超时等瞬时设备
        # 停顿），第二次失败才进入 renderer_failed 重启链。成功出帧即清零。
        self._frame_error_streak = 0
        # CPU 补帧限速（monotonic 秒）：失败窗口内最多 2 帧/秒，避免把 worker
        # 线程按重特效 ~300ms/帧的速度堵死。
        self._last_fallback_emit = 0.0
        self._stats = {
            "requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "pending_replaced": 0,
            "frames_emitted": 0,
            "future_frames_cached": 0,
            "stale_frames_dropped": 0,
            "stale_frame_slots_skipped": 0,
            "frame_error_retries": 0,
            "queue_full_backpressure": 0,
            "fallback_frames_emitted": 0,
            "configure_count": 0,
            "renderer_failures": 0,
            "renderer_restarts": 0,
            "gpu_circuit_open": 0,
            "fallback_frames": 0,
            "fallback_failures": 0,
            "capability_fallbacks": 0,
            "max_pending": 0,
            "max_in_flight": 0,
            "worker_count": self._worker_count_requested,
            "warp_selected": int(self._force_warp),
            "pipeline_lead_frames": self._effective_lookahead_frames,
            "generations_cancelled": 0,
            "style_patches": 0,
            "style_patch_fallbacks": 0,
            "native_preview_closed": 0,
            "native_redundant_frames_skipped": 0,
            "native_idle_pumps": 0,
            "native_mode_switches": 0,
        }
        self._timings: dict[str, deque[float]] = {
            "render_ms": deque(maxlen=4096),
            "readback_ms": deque(maxlen=4096),
            "present_ms": deque(maxlen=4096),
            "roundtrip_ms": deque(maxlen=4096),
            "ready_latency_ms": deque(maxlen=4096),
        }
        self._thread = threading.Thread(
            target=self._run,
            name="subtitle-preview-gpu-render",
            daemon=True,
        )
        self._thread.start()

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,
        *,
        duration_ms: int | None = None,
        relayout_scope: str | None = None,
    ) -> None:
        _prewarm_font_axis_capabilities(track, style, extra_tracks)
        with self._condition:
            if self._stopped:
                return
            previous_generation = self._generation
            self._track = track
            self._style = style
            self._extra_tracks = list(extra_tracks or ())
            self._duration_ms = max(int(duration_ms or 0), 0)
            # relayout_scope：None = 全量重排（默认）；"titles" / "paint"
            # 分别表示仅标题或仅上色变化，configure 时歌词布局计划按签名复用。
            self._relayout_scope = relayout_scope if relayout_scope else None
            with self._stats_lock:
                self._stats["warp_selected"] = int(self._force_warp)
            self._generation += 1
            self._needs_configure = True
            self._needs_target_resize = False
            self._pending = None
            self._frame_cache.clear()
            self._cancel_native_generation_locked(previous_generation)
            self._condition.notify_all()

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(
        self,
        width: int,
        height: int,
        device_pixel_ratio: float = 1.0,
    ) -> None:
        with self._condition:
            if self._stopped:
                return
            target = (
                max(int(width), 1),
                max(int(height), 1),
                max(float(device_pixel_ratio or 1.0), 0.01),
            )
            if target != (self._logical_w, self._logical_h, self._device_pixel_ratio):
                previous_generation = self._generation
                self._logical_w, self._logical_h, self._device_pixel_ratio = target
                self._generation += 1
                if not self._needs_configure:
                    self._needs_target_resize = True
                self._pending = None
                self._frame_cache.clear()
                # QSharedMemory cannot resize an existing named segment. Give
                # each render-target generation its own key while preserving
                # the sidecar/GPU device across ordinary frame requests.
                self._shm_key = f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
                self._cancel_native_generation_locked(previous_generation)
            self._condition.notify_all()

    @property
    def uses_native_preview(self) -> bool:
        return self._native_preview

    @property
    def native_target_established(self) -> bool:
        """DComp 子窗口目标是否已建立（预览画布可见且有几何）。"""
        with self._condition:
            return self._native_target is not None

    def _worker_request_for_mode(self) -> int:
        """当前模式下应向 sidecar 申请的 worker 数。

        native（G6 直画）必须 1：多 worker 的池化 configure 只配置池的后备
        worker，主后端（direct 渲染的执行者）不会被 configure。WARP 同理
        压到 1。G5 读回用满额 _worker_count_requested（env 派生，不再在
        __init__ 冻结，热切换回 G5 才能恢复多 worker 吞吐）。
        """
        if self._force_warp or self._native_preview:
            return 1
        return self._worker_count_requested

    def _readback_slot_count(self) -> int:
        """读回环的槽位数（同 key 下所有 ensure 调用的唯一合法值）。

        ring 的 (key, slots, 宽高) 任一参数变化都会触发 sidecar detach+
        create；GUI 进程的 ring reader 若仍 attach 着旧段，Windows 命名
        对象不销毁，create 报 already exists → 之后每帧失败。因此单帧
        路径、填缝循环、池化 begin 三处必须传同一个值。
        """
        return 1 if self._force_warp else self._worker_count_requested

    def set_native_mode(self, enabled: bool) -> bool:
        """G6↔G5 热切换：同一 sidecar 内翻转直画/读回，不重建进程与场景。

        每次切换都杀进程重建会把重特效场景的 configure 成本（秒级）全部
        重付一遍，用户观感就是「多切几次后越来越慢」（2026-10 泄漏探针
        显示进程/显存本就干净，变慢的根源是冷启动）。返回 False 表示无
        变化，调用方走完整重建路径。
        """
        with self._condition:
            if self._stopped or bool(enabled) == self._native_preview:
                return False
            previous_generation = self._generation
            self._generation += 1
            self._native_preview = bool(enabled)
            self._note("native_mode_switches")
            # 两个方向都强制走一次轻量 resize（1ms 量级）：G5 池化 configure
            # 只配置池的后备 worker，主后端（direct 渲染的执行者）从未
            # configure——renderFrameOnly 会抛 "GPU backend is not
            # configured"（2026-10 最小复现钉死，曾致热切换连败进入杀
            # 进程重启链）；worker=1 的 resize 恰好 configure 主后端。
            # 反向同理：单 worker resize 会重置池，重建走池化 resize。
            self._needs_target_resize = True
            if enabled:
                # G5→G6：直画模式不用投机缓存（pending 跟随请求戳 + 投喂
                # 前移），lookahead 冻结为 0。首帧里程碑计数复位，重新进入
                # 直画时再打一次「首帧直画成功」。
                self._lookahead_frames = 0
                self._g6_present_count = 0
            else:
                # G6→G5：撤掉 DComp 子窗口（close 哨兵），恢复投机前瞻。
                self._native_target = None
                self._native_close_requested = self._native_child_open
                self._lookahead_frames = self._configured_lookahead_frames
            self._effective_lookahead_frames = self._lookahead_frames
            with self._stats_lock:
                self._stats["pipeline_lead_frames"] = (
                    self._effective_lookahead_frames
                )
            self._pending = None
            self._frame_cache.clear()
            # A mode change also replaces the native worker/realization state.
            # Reject results already queued by the previous transport even when
            # both modes have exactly the same target dimensions and DPR.
            self._cancel_native_generation_locked(previous_generation)
            # 读回环 key 必须随模式切换轮换：环按 (key, 槽位数, 物理宽高)
            # 创建，而 G6/G5 的物理几何不同（未钳制的显示缩放 vs 质量钳制
            # 的场景 DPR），切回 G5 后 sidecar 会对同 key 以新几何重建环。
            # 但 GUI 进程的 SharedFrameRingReader 可能仍 attach 着旧段，
            # Windows 上命名对象因此不销毁，create() 报 "already exists"，
            # 之后每一帧都失败——G5 永久不出帧（2026-10 热切换死亡链）。
            # 换新 key 让切换后的创建落在全新段上，读端按 key 失配重连。
            self._shm_key = f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
            self._condition.notify_all()
        return True

    def set_native_target(
        self,
        parent_hwnd: int,
        x: int,
        y: int,
        width: int,
        height: int,
        src_x: int = 0,
        src_y: int = 0,
    ) -> None:
        """Update the sidecar child HWND parent and physical target geometry.

        ``x/y/width/height`` 是子窗口在父（顶层）窗口客户区里的物理像素
        矩形（已裁剪到视口可见范围）；``src_x/src_y`` 是该矩形左上角在
        渲染纹理里的物理像素偏移——场景映射矩形超出视口被裁掉时，纹理
        与窗口起点不再重合，sidecar 从偏移处 1:1 拷贝。
        """
        target = (
            int(parent_hwnd),
            int(x),
            int(y),
            max(int(width), 1),
            max(int(height), 1),
            max(int(src_x), 0),
            max(int(src_y), 0),
        )
        with self._condition:
            if self._stopped or not self._native_preview:
                return
            if target == self._native_target:
                return
            self._native_target = target
            if self._latest_t is not None:
                self._replace_pending_locked(
                    self._latest_t,
                    self._request_serial,
                    False,
                )
            self._condition.notify_all()

    def clear_native_target(self) -> None:
        """Drop the native target and destroy the sidecar child window.

        视图隐藏（切标签页/关播放窗）时调用：子窗口挂在顶层 HWND 上，
        不会随视口隐藏，必须显式撤掉，否则残留画面浮在其他 UI 上。
        worker 可能正阻塞在无 pending 的等待里，用 close 哨兵唤醒它。
        """
        with self._condition:
            if self._stopped or not self._native_preview:
                return
            if self._native_target is None and not self._native_child_open:
                return
            self._native_target = None
            self._pending = None
            self._native_close_requested = True
            self._condition.notify_all()

    def request(self, t_ms: int) -> None:
        requested_t = int(t_ms)
        # 媒体时钟的原始毫秒值在帧键边界附近抖动时，request 的量化键
        # 会在两个相邻键间交替，emit 的帧内容时刻随之来回漂移（快动画
        # 字表现为逐帧位置抖动）。统一吸附到帧键网格，保证消费键与
        # speculative 缓存键恒同相；吸附偏差上限半个帧间隔（≈8ms），
        # 对渲染内容不可见。
        requested_t = self._frame_cache.timestamp_for_key(
            self._frame_cache.key_for(requested_t)
        )
        cached = None if self._native_preview else self._frame_cache.take(requested_t)
        with self._condition:
            if self._stopped:
                return
            self._request_serial += 1
            serial = self._request_serial
            if (
                self._latest_t is not None
                and abs(requested_t - self._latest_t) > self._SEEK_DISCONTINUITY_MS
            ):
                previous_generation = self._generation
                self._generation += 1
                self._pending = None
                self._frame_cache.clear()
                cached = None
                self._cancel_native_generation_locked(previous_generation)
            self._latest_t = requested_t
            # G6 到点调度器的媒体时钟锚点（按墙钟外推）。
            self._g6_media_t = requested_t
            self._g6_media_wall = time.monotonic()
            self._note("requests")
            if cached is not None:
                self._note("cache_hits")
                self._note("frames_emitted")
                self.frame_ready.emit(cached, requested_t)
                # 缓存里只会有 sidecar 渲染的帧（回退帧不入缓存）。
                self._note_backend_mode("gpu")
            else:
                self._note("cache_misses")
            if self._playing and self._lookahead_frames > 0:
                self._replace_pending_locked(
                    self._pipeline_anchor_timestamp(requested_t), serial, True
                )
            elif cached is None:
                self._replace_pending_locked(requested_t, serial, False)
            self._condition.notify()

    def set_playing(self, playing: bool) -> None:
        with self._condition:
            self._playing = bool(playing)
            if not self._playing and self._pending is not None and self._pending[2]:
                self._pending = None
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._pending = None
            self._cancel_native_generation_locked(self._generation)
            self._condition.notify_all()
        if not self._thread.join(timeout=3.0):
            # worker 卡在长渲染等待里（最坏 2s 帧超时 + 关闭握手 > join 窗口）：
            # 解释器退出时 daemon 线程的 finally 不会执行，sidecar 会变孤儿并
            # 持续占用显存（2026-10 实测）。从本线程直接收掉进程兜底；owner 的
            # close 幂等，worker 之后的 _close_renderer 只是 no-op。
            try:
                self._renderer_owner.close()
            except Exception:  # noqa: BLE001 - 关闭兜底不得向 GUI 抛错
                pass

    def _cancel_native_generation_locked(self, generation: int) -> None:
        renderer = self._renderer_owner.process
        if renderer is None:
            return
        try:
            renderer.send_cancel_generation(int(generation))
            self._note("generations_cancelled")
        except (AttributeError, NativeRendererError, RuntimeError):
            # The worker owns restart/fallback. State changes must stay non-blocking.
            pass

    def _replace_pending_locked(self, t_ms: int, serial: int, speculative: bool) -> None:
        if self._pending is not None:
            self._note("pending_replaced")
        self._pending = (int(t_ms), int(serial), bool(speculative), time.monotonic())
        self._note_max_pending(1)

    def _take_next_request(self):
        with self._condition:
            while True:
                if self._stopped:
                    return None
                if self._pending is not None:
                    break
                if self._native_close_requested and self._native_child_open:
                    # 只为关闭 DComp 子窗口醒来（视图隐藏，见 clear_native_target）。
                    self._native_close_requested = False
                    return "__close_native__"
                if self._native_preview and self._native_child_open:
                    # 子窗口在位但无事可做（暂停/空闲）：限时等待，超时让
                    # _run 泵一次 sidecar 消息队列——DComp 子窗口的鼠标转发
                    # 消息积压在 sidecar 线程里，没有 present 就没人投递，
                    # 悬浮控件的 hover/点击会失效。
                    self._condition.wait(timeout=self._NATIVE_IDLE_PUMP_S)
                    if (
                        self._pending is not None
                        or (self._native_close_requested and self._native_child_open)
                    ):
                        continue
                    return "__pump_native__"
                self._condition.wait()
            t_ms, serial, speculative, submitted_at = self._pending
            self._pending = None
            needs_configure = self._needs_configure
            needs_target_resize = self._needs_target_resize
            self._needs_configure = False
            self._needs_target_resize = False
            return (
                self._track,
                self._style,
                list(self._extra_tracks),
                self._duration_ms,
                self._logical_w,
                self._logical_h,
                self._device_pixel_ratio,
                t_ms,
                serial,
                speculative,
                self._generation,
                needs_configure,
                needs_target_resize,
                self._shm_key,
                submitted_at,
                self._native_target,
                self._force_warp,
                self._relayout_scope,
            )

    def _run(self) -> None:
        try:
            while True:
                snapshot = self._take_next_request()
                if snapshot is None:
                    return
                if snapshot == "__close_native__":
                    # 视图隐藏：撤掉 sidecar 里的 DComp 子窗口。close 失败
                    # 也不阻断——sidecar 重启/退出时窗口随进程销毁。
                    renderer = self._renderer_owner.process
                    if renderer is not None:
                        try:
                            renderer.close_gpu_preview(
                                force_warp=self._force_warp,
                            )
                            self._note("native_preview_closed")
                        except (AttributeError, NativeRendererError, RuntimeError):
                            pass
                    with self._condition:
                        self._native_child_open = False
                    continue
                if snapshot == "__pump_native__":
                    # 空闲心跳：投递 DComp 子窗口积压的鼠标转发消息。
                    renderer = self._renderer_owner.process
                    pump = getattr(renderer, "pump_native_preview", None)
                    if callable(pump):
                        try:
                            pump(force_warp=self._force_warp)
                            self._note("native_idle_pumps")
                        except (NativeRendererError, RuntimeError):
                            # 泵失败不影响渲染；下个心跳再试。
                            pass
                    continue
                (
                    track,
                    style,
                    extra_tracks,
                    duration_ms,
                    width,
                    height,
                    dpr,
                    t_ms,
                    serial,
                    speculative,
                    generation,
                    needs_configure,
                    needs_target_resize,
                    shm_key,
                    submitted_at,
                    native_target,
                    force_warp,
                    relayout_scope,
                ) = snapshot
                if track is None or style is None:
                    continue
                unsupported = gpu_unsupported_features(track, style, extra_tracks)
                if unsupported:
                    # 能力回退期间（含投机请求被跳过的播放态）实际后端就是
                    # Painter：哪怕本帧不回退出帧，也不该继续显示 GPU。
                    self._note_backend_mode("cpu")
                    if not speculative:
                        if self._native_preview:
                            self._close_renderer()
                        self._note("capability_fallbacks")
                        self._report_fallback(
                            "当前工程包含 GPU 尚不支持的功能，字幕预览已回退 Painter："
                            + ", ".join(gpu_unsupported_feature_labels(unsupported))
                        )
                        self._emit_python_fallback(
                            track,
                            style,
                            extra_tracks,
                            width,
                            height,
                            dpr,
                            t_ms,
                            generation,
                            duration_ms,
                        )
                    continue
                if self._gpu_restart_breaker.open:
                    # 断路器熔断（失败重启风暴）：不再触碰 GPU 路径——
                    # 不 _ensure_renderer、不重配，非投机请求补 CPU 帧，
                    # 投机请求静默丢弃。本会话由熔断时的上报向用户说明。
                    if not speculative:
                        now = time.monotonic()
                        if now - self._last_fallback_emit >= 0.5:
                            self._last_fallback_emit = now
                            self._note("fallback_frames_emitted")
                            self._emit_python_fallback(
                                track,
                                style,
                                extra_tracks,
                                width,
                                height,
                                dpr,
                                t_ms,
                                generation,
                                duration_ms,
                            )
                    continue
                if self._renderer_failed:
                    if time.monotonic() < self._retry_after:
                        if not speculative and self._playing:
                            # 播放态：GPU 重试窗口内跑 CPU 填缝调度——
                            # 按容量画未来帧入缓存（GUI 到点缓存命中），
                            # 窗口到期自动退出回主循环重配 GPU。
                            self._run_cpu_due_scheduler(
                                generation,
                                track,
                                style,
                                extra_tracks,
                                duration_ms,
                                width,
                                height,
                                dpr,
                            )
                            continue
                        if not speculative:
                            now = time.monotonic()
                            if now - self._last_fallback_emit >= 0.5:
                                # 同 _run 失败分支：补帧限速，保持 worker 空闲
                                # 以便尽快重配 GPU（详见该处注释）。
                                self._last_fallback_emit = now
                                self._note("fallback_frames_emitted")
                                self._emit_python_fallback(
                                    track,
                                    style,
                                    extra_tracks,
                                    width,
                                    height,
                                    dpr,
                                    t_ms,
                                    generation,
                                    duration_ms,
                                )
                        continue
                    self._renderer_failed = False
                    needs_configure = True
                    needs_target_resize = False
                    self._note("renderer_restarts")
                work_started = time.monotonic()
                try:
                    renderer = self._ensure_renderer()
                    scene_configured = False
                    # 差分闸门 key：预览 configure 恒按 60fps（与下方全量
                    # configure_gpu 的 fps=60 同口径），画面变化走 resize 路径。
                    current_patch_key = style_patch_base_key(
                        track,
                        style,
                        extra_tracks,
                        width=width,
                        height=height,
                        fps=60,
                        dpr=dpr,
                        include_layout_signature=(
                            relayout_scope != "layout"
                        ),
                    )
                    if needs_configure:
                        style_patched = False
                        if (
                            relayout_scope in (
                                "paint", "titles", "layout",
                            )
                            # 差分重放（2026-10-03 起默认启用）：闸门 key 相等
                            # ⇔ 轨道/布局签名/画面全部没变，sidecar 只重放
                            # style/titles/fx_sprites/行级样式段。现象B 根因
                            # （差分合并未清旧行级 bursts，逐次翻倍）已修复；
                            # 全特效真实工程上 patch 与全量重配的帧逐字节
                            # 一致（含表化载荷）。任何失败仍回落全量重配，
                            # 语义不变；KROK_SUBTITLE_GPU_STYLE_PATCH=0 可
                            # 强制关闭用于对照。
                            and _env_enabled(
                                "KROK_SUBTITLE_GPU_STYLE_PATCH", "1"
                            )
                            and self._style_patch_key is not None
                            and self._style_patch_key == current_patch_key
                        ):
                            # 纯样式变化且轨道/布局/画面全部没变：只重放
                            # style/titles/fx_sprites/行级样式字段（改色/改
                            # 装饰参数的预览延迟主体就是被省掉的那次整轨
                            # IR 重序列化 + 大 JSON 重解析）。任何失败都回
                            # 落全量重配，语义不变。
                            try:
                                renderer.configure_style_gpu(
                                    build_style_patch_ir(
                                        track,
                                        style,
                                        width=width,
                                        height=height,
                                        fps=60,
                                        dpr=dpr,
                                        extra_tracks=extra_tracks,
                                        duration_ms=duration_ms,
                                        include_lines_style=(
                                            relayout_scope != "titles"
                                        ),
                                        include_placement=(
                                            relayout_scope == "layout"
                                        ),
                                    ),
                                    force_warp=force_warp,
                                    prewarm_t_ms=t_ms,
                                    worker_count=self._worker_request_for_mode(),
                                    defer_followers=True,
                                    defer_realizations_until_first_frame=True,
                                )
                                style_patched = True
                                self._note("style_patches")
                                self._note("configure_count")
                            except (NativeRendererError, ValueError, KeyError):
                                self._note("style_patch_fallbacks")
                        if not style_patched:
                            with render_progress_scope(
                                _render_progress_reporter(
                                    _RENDER_STAGE_SPANS_GPU,
                                    self.renderProgress.emit,
                                )
                            ):
                                try:
                                    configured = renderer.configure_gpu(
                                        track,
                                        style,
                                        width=width,
                                        height=height,
                                        fps=60,
                                        dpr=dpr,
                                        force_warp=force_warp,
                                        extra_tracks=extra_tracks,
                                        duration_ms=duration_ms,
                                        prewarm_t_ms=t_ms,
                                        worker_count=self._worker_request_for_mode(),
                                        defer_followers=True,
                                        defer_realizations_until_first_frame=True,
                                        relayout_scope=relayout_scope,
                                        progress=lambda: self.renderProgress.emit(
                                            80, "场景构建"
                                        ),
                                    )
                                except NativeRendererError as exc:
                                    raise _ConfigPhaseError(str(exc)) from exc
                                self._active_worker_count = max(
                                    1, min(int(configured.get("worker_count", 1)), 8)
                                )
                                dedicated_vram = max(
                                    int(configured.get("dedicated_video_memory", 0)), 0
                                )
                                min_multiworker_vram = _env_int(
                                    "KROK_SUBTITLE_GPU_MIN_MULTIWORKER_VRAM_MB",
                                    2048,
                                    minimum=0,
                                ) * 1024 * 1024
                                if (
                                    self._worker_count_requested > 1
                                    and dedicated_vram > 0
                                    and dedicated_vram < min_multiworker_vram
                                ):
                                    self._worker_count_requested = 1
                                    try:
                                        configured = renderer.configure_gpu(
                                            track,
                                            style,
                                            width=width,
                                            height=height,
                                            fps=60,
                                            dpr=dpr,
                                            force_warp=force_warp,
                                            extra_tracks=extra_tracks,
                                            duration_ms=duration_ms,
                                            prewarm_t_ms=t_ms,
                                            worker_count=1,
                                            defer_followers=True,
                                            defer_realizations_until_first_frame=True,
                                            progress=lambda: self.renderProgress.emit(
                                                80, "场景构建"
                                            ),
                                        )
                                    except NativeRendererError as exc:
                                        raise _ConfigPhaseError(str(exc)) from exc
                                    self._active_worker_count = 1
                                with self._stats_lock:
                                    self._stats["worker_count"] = self._active_worker_count
                                self._note("configure_count")
                        scene_configured = True
                        self._style_patch_key = current_patch_key
                    elif needs_target_resize:
                        # 与 preview_render_target_size 同口径的防线：任何
                        # 路径漏进来的超限 dpr 都钳到纹理上限内，避免
                        # resize 报错进入杀进程重启链。
                        dpr = min(
                            dpr,
                            _RENDER_TARGET_MAX_DIMENSION / max(int(width), int(height)),
                        )
                        try:
                            configured = renderer.resize_gpu_target(
                                width=width,
                                height=height,
                                dpr=dpr,
                                force_warp=force_warp,
                                prewarm_t_ms=t_ms,
                                # native（直画）必须 worker=1：多 worker 走池化
                                # 路径只配置池的后备 worker，主后端（direct 渲染
                                # 执行者）不会被 configure（2026-10 热切换连败
                                # 根因）。WARP 一并压 1。
                                worker_count=self._worker_request_for_mode(),
                            )
                        except NativeRendererError as exc:
                            raise _ConfigPhaseError(str(exc)) from exc
                        self._active_worker_count = max(
                            1, min(int(configured.get("worker_count", 1)), 8)
                        )
                        with self._stats_lock:
                            self._stats["worker_count"] = self._active_worker_count
                        # resize 可能改变物理几何（G6↔G5 的 dpr 口径不同、
                        # 或外部强制 resize）：同 key 以新几何重建环会被
                        # GUI 读端的附着卡死（create already exists），随
                        # resize 成功一并轮换 key，保证环参数随 key 单调。
                        self._shm_key = (
                            f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
                        )
                        # 快照在 _take_next_request 里取的是旧 key，轮换后
                        # 同步刷新局部量，否则调度器/暂停态首帧仍会拿旧
                        # key 以新几何 ensure。
                        shm_key = self._shm_key
                        self._note("configure_count")
                        scene_configured = True
                        self._style_patch_key = current_patch_key
                        # resize 成功即清零连败计数（用户要求：resize 本身
                        # 消耗大，期间的瞬时失败是过渡态，不该累积降级）。
                        self._frame_error_streak = 0
                        self._native_preview_failures = 0
                    if scene_configured:
                        # configure 完成（IR 重排 + sidecar 场景就绪）；此刻起等待的
                        # 是首帧实现（字形光栅化）与出帧，帧到达即由 GUI 徽标收尾。
                        self.renderProgress.emit(92, "出帧")
                    if not self._native_preview and self._playing:
                        # G5 播放态：公共填缝调度器（池子持续填缓存窗口，
                        # 不再依赖 60Hz 请求接力）。暂停态走下方原路径。
                        self._run_readback_due_scheduler(
                            renderer,
                            generation,
                            force_warp,
                            shm_key=shm_key,
                            dpr=dpr,
                        )
                        continue
                    if (
                        self._active_worker_count > 1
                        and not self._native_preview
                        and native_target is None
                    ):
                        self._render_pooled_batch(
                            renderer,
                            t_ms=t_ms,
                            serial=serial,
                            speculative=speculative,
                            generation=generation,
                            shm_key=shm_key,
                            dpr=dpr,
                            submitted_at=submitted_at,
                            force_warp=force_warp,
                        )
                        continue
                    if self._native_preview and native_target is not None:
                        if self._playing:
                            # 播放态进入到点队列调度器：渲染跑到到点前、
                            # 到点出队上屏、空闲填缝（用户模型：22fps 就渲
                            # 22 帧有效的，提前完成则填 15,25,35…逐步爬回
                            # 60）。暂停态走下面的同步逐帧路径。
                            self._run_native_due_scheduler(
                                renderer, generation, force_warp
                            )
                            continue
                        (
                            parent_hwnd,
                            target_x,
                            target_y,
                            target_width,
                            target_height,
                            src_x,
                            src_y,
                        ) = native_target
                        render_t = self._native_render_timestamp(
                            t_ms, submitted_at, needs_configure or needs_target_resize
                        )
                        if render_t is None:
                            # 同键去重：请求落在已直画的帧键里（媒体时钟在
                            # 帧键内抖动 / 暂停态重复请求），该帧已在屏上，
                            # 重渲完全一致 → 跳过这次 GPU 工作（无效帧）。
                            self._note("native_redundant_frames_skipped")
                            settled_t = self._frame_cache.timestamp_for_key(
                                self._frame_cache.key_for(t_ms)
                            )
                            if self._may_emit(settled_t, generation):
                                # 帧已在屏上：仍闭合 GUI 的忙碌徽标区间，
                                # 否则暂停态最后一次请求会被去重吞掉 delivery。
                                self._note("frames_emitted")
                                self.frame_presented.emit(int(settled_t))
                            continue
                        # 拆分路径：先渲染到纹理（可提前），再上屏。暂停态
                        # 渲染完立即可上屏（_hold_until_due 对非播放态直通）。
                        render_event = renderer.render_gpu_frame_direct(
                            render_t,
                            force_warp=force_warp,
                            generation=generation,
                            frame_index=self._frame_index,
                        )
                        self._hold_until_due(t_ms, submitted_at, render_t, generation)
                        event = renderer.present_rendered_gpu_frame(
                            parent_hwnd=parent_hwnd,
                            x=target_x,
                            y=target_y,
                            width=target_width,
                            height=target_height,
                            src_x=src_x,
                            src_y=src_y,
                            t_ms=render_t,
                            force_warp=force_warp,
                            generation=generation,
                        )
                        if event.get("dropped"):
                            # 帧仓未命中（渲染期间代际被作废）：丢帧收场，
                            # 不上屏也不记账。
                            self._note("stale_frames_dropped")
                            self._accept_realization_event(event, generation)
                            self._retry_after_realization_drop(event, t_ms, serial, generation)
                            continue
                        if event.get("render_ms") in (None, 0.0):
                            event["render_ms"] = render_event.get("render_ms", 0.0)
                        self._native_note_presented(generation, render_t, event)
                        if _env_enabled("KROK_SUBTITLE_NATIVE_DUMP_PNG", "0"):
                            # 调试：G6 暂停态 present 后，把同一 t 的纹理按
                            # 读回路径落 PNG（与 present 内容同源同尺寸），
                            # 供三路一致性探针取「用户可观测点」的帧。
                            self._dump_presented_frame_png(
                                renderer,
                                int(render_t),
                                generation,
                                force_warp,
                            )
                        # 后续记账/emit 一律用实际渲染的时间戳。
                        t_ms = render_t
                    else:
                        event = renderer.render_gpu_frame(
                            t_ms,
                            force_warp=force_warp,
                            generation=generation,
                            frame_index=self._frame_index,
                            shm_key=shm_key,
                            include_checksum=False,
                            readback_bands=True,
                            # 槽位数与池化路径保持同值：同 key 下 ensure 的
                            # (slots, 宽高) 必须恒定，否则参数一变就 detach+
                            # create，被 GUI 读端的附着卡成 already exists。
                            slot_count=self._readback_slot_count(),
                        )
                    ready_workers = max(
                        1,
                        min(
                            int(event.get("worker_count_ready", self._active_worker_count)),
                            self._worker_count_requested,
                        ),
                    )
                    if ready_workers != self._active_worker_count:
                        self._active_worker_count = ready_workers
                        with self._stats_lock:
                            self._stats["worker_count"] = ready_workers
                    self._frame_index += 1
                    if (
                        not self._accept_realization_event(event, generation)
                        or event.get("event") == "gpu_frame_dropped"
                    ):
                        # A target/style generation can be cancelled while its
                        # sole foreground frame is already in flight. Dropped
                        # responses deliberately carry no shared-memory slot.
                        self._note("stale_frames_dropped")
                        if not speculative:
                            self._retry_after_realization_drop(event, t_ms, serial, generation)
                        continue
                    completed_at = time.monotonic()
                    self._frame_error_streak = 0
                    self._native_preview_failures = 0
                    if self._native_preview and native_target is not None:
                        self._native_child_open = True
                    self._record_timing("roundtrip_ms", (completed_at - work_started) * 1000.0)
                    self._adapt_pipeline_lookahead()
                    self._record_event_timing("render_ms", event.get("render_ms"))
                    self._record_event_timing("readback_ms", event.get("readback_ms"))
                    self._record_event_timing("present_ms", event.get("present_ms"))
                    if not speculative:
                        self._record_timing(
                            "ready_latency_ms", (completed_at - submitted_at) * 1000.0
                        )
                    if self._native_preview and native_target is not None:
                        if self._may_emit(t_ms, generation):
                            self._note("frames_emitted")
                            self.frame_presented.emit(int(t_ms))
                            # G6 出帧同样要翻转实际后端指示（GPU渲染中/
                            # CPU渲染中标签跟随真实出帧状态；此前只在 G5
                            # 缓存命中路径标记，G6 下标签永不更新）。
                            self._note_backend_mode("gpu")
                            self._g6_present_count += 1
                            if self._g6_present_count == 1:
                                _preview_diagnostic(
                                    f"[GPU 预览] G6 首帧直画成功 "
                                    f"present={event.get('present_ms', '?')}ms "
                                    f"render={event.get('render_ms', '?')}ms",
                                    flush=True,
                                )
                            elif self._g6_present_count % 60 == 0:
                                import subprocess as _sp
                                try:
                                    _r = _sp.run(
                                        ["nvidia-smi",
                                         "--query-gpu=memory.used,memory.total",
                                         "--format=csv,noheader,nounits"],
                                        capture_output=True, text=True,
                                        timeout=2)
                                    _vram = _r.stdout.strip().split(",")[0].strip()
                                    _vram = f" 显存={_vram}MiB"
                                except Exception:
                                    _vram = ""
                                _preview_diagnostic(
                                    f"[GPU 预览] G6 已直画 "
                                    f"{self._g6_present_count} 帧{_vram}",
                                    flush=True,
                                )
                        else:
                            self._note("stale_frames_dropped")
                        continue
                    event_key = str(event.get("shm_key") or "")
                    if self._reader is None or self._reader.shm_key != event_key:
                        if self._reader is not None:
                            self._reader.close()
                        self._reader = SharedFrameRingReader.from_event(event)
                    try:
                        image = self._reader.read_qimage(event)
                    except StaleSharedFrameSlotError:
                        # 槽位已被更新的帧复用（在途窗口 > 槽数）：本事件
                        # 对应的帧已不存在，丢弃继续——后续帧事件紧随其后。
                        # 绝不据此杀 sidecar（曾是预热负载下的无谓重启源）。
                        self._note("stale_frame_slots_skipped")
                        continue
                    image.setDevicePixelRatio(dpr)
                    if speculative:
                        self._cache_speculative(image, t_ms, generation)
                    elif self._may_emit(t_ms, generation):
                        self._note("frames_emitted")
                        self.frame_ready.emit(image, int(t_ms))
                        self._schedule_lookahead(t_ms, serial, generation)
                        self._note_backend_mode("gpu")
                    else:
                        self._note("stale_frames_dropped")
                except Exception as exc:  # noqa: BLE001 - worker 线程必须自愈
                    # 只捕获 NativeRendererError/RuntimeError 时，sidecar 协议层
                    # 之外的任何异常（IR 构建的 ValueError、读帧的 KeyError…）
                    # 都会让 _run 线程直接死亡——GPU 与 CPU 预览此后永久停摆。
                    # 统一按渲染失败处理：回退本帧、稍后重试。
                    if _env_enabled("KROK_SUBTITLE_NATIVE_DEBUG_FAILURES", "0"):
                        _preview_diagnostic(f"GPU preview failed: {exc}")
                    if isinstance(exc, NativeQueueFullError):
                        # 流控信号：in-flight 池满（某 worker 短暂停顿期间的提交
                        # 堆积），不是渲染器故障。短暂退避后重发同一请求，不杀
                        # 进程、不 CPU 补帧。
                        self._note("queue_full_backpressure")
                        # 短退避：等一个 in-flight 槽位释放的量级即可（本环境
                        # 单帧渲染 ~50-90ms；睡太久会让提交循环空转，把当前帧
                        # 饿成过期）。
                        time.sleep(0.01)
                        with self._condition:
                            if needs_configure:
                                self._needs_configure = True
                            if needs_target_resize:
                                self._needs_target_resize = True
                            if self._pending is None:
                                self._pending = (t_ms, serial, speculative, submitted_at)
                            self._condition.notify()
                        continue
                    if (
                        isinstance(exc, NativeRendererError)
                        and "resource deadlock would occur" in str(exc)
                    ):
                        # G6 直画瞬态竞态（2026-10 用户实测：刚播放即连续
                        # 拖回开头多次触发，最终熔断）：快速 seek 的代际翻
                        # 动下 present 与渲染/取消竞态，D3D/D2D 以
                        # ERROR_POSSIBLE_DEADLOCK 拒绝该次操作——sidecar
                        # 返回的是错误应答而非死亡，设备状态仍可用。温和
                        # 重试（丢这一拍），不杀进程不记断路器；持续发生
                        # 才经帧级 streak 走重启链。
                        self._note("gpu_deadlock_retries")
                        with self._condition:
                            if needs_configure:
                                self._needs_configure = True
                            if needs_target_resize:
                                self._needs_target_resize = True
                            if self._pending is None:
                                self._pending = (t_ms, serial, speculative, submitted_at)
                            self._condition.notify()
                        continue
                    with self._condition:
                        churned = generation != self._generation
                    if churned:
                        # 代际作废豁免（2026-10 用户实测：疯狂播放/暂停 +
                        # zx/space seek 高频翻代际）：失败请求所属代际已被
                        # 用户操作作废时，这是预期搅动而非渲染器生病——
                        # 温和重试，不杀进程、不 CPU 补帧、不记断路器。
                        # 真死亡经「当前代际的新请求失败」正常升级（重试
                        # 注回的 pending 会以最新代际重新提交）。
                        self._note("churn_stale_failures")
                        with self._condition:
                            if needs_configure:
                                self._needs_configure = True
                            if needs_target_resize:
                                self._needs_target_resize = True
                            if self._pending is None:
                                self._pending = (t_ms, serial, speculative, submitted_at)
                            self._condition.notify()
                        continue
                    if (
                        isinstance(exc, NativeRendererError)
                        # configure/resize 阶段的失败不做帧级温和重试：
                        # 这类失败最常见的形态是 sidecar native 楔死（共享
                        # D3D 并发把渲染任务卡死、pause 排空超时后主循环
                        # 全哑），重发到死进程只会每轮白等一个超时；必须
                        # 立即杀进程重建（2026-10 拖大后永久卡死根因：
                        # 30s 超时 ×5 次温和重试 = 150s 无恢复）。
                        and not isinstance(exc, _ConfigPhaseError)
                        and self._frame_error_streak + 1
                        < self._consecutive_failure_limit
                    ):
                        # 帧级错误（有界回读超时、瞬时设备停顿）：只要中间有
                        # 成功出帧（streak 被清零），说明显卡在正常工作。
                        # 原样重试（把请求注回 pending），连续达到 5 次才
                        # 走重启链——直接杀进程会丢掉整个已配置场景。
                        self._frame_error_streak += 1
                        self._note("frame_error_retries")
                        with self._condition:
                            # 本轮消费掉的 configure/resize 标志若未成功应用
                            # （如 configure_gpu 抛错），重试轮必须重做，否则
                            # 会在未配置的 renderer 上直接渲染。
                            if needs_configure:
                                self._needs_configure = True
                            if needs_target_resize:
                                self._needs_target_resize = True
                            if self._pending is None:
                                self._pending = (t_ms, serial, speculative, submitted_at)
                            self._condition.notify()
                        continue
                    if not isinstance(exc, (NativeRendererError, RuntimeError)):
                        _log.exception("GPU 预览路径出现非预期异常")
                    if self._native_preview:
                        # G6 present 失败：与 G5 同一判定口径——成功出帧
                        # 即清零（见上方 streak=0 赋值处），连续 5 次才
                        # 永久降级 G5。能播放说明显卡问题不大。
                        self._native_preview_failures += 1
                        if (self._native_preview_failures
                                >= self._consecutive_failure_limit):
                            # 永久降级 G5（2026-10 用户拍板：连续失败
                            # 5 次确实要永久降级，不自动重试）。
                            self._native_preview = False
                            _preview_diagnostic(
                                f"[GPU 预览] G6 连续失败 "
                                f"{self._native_preview_failures} 次，永久降级 G5",
                                flush=True,
                            )
                            self._close_renderer()
                            self._report_fallback(
                                "GPU 直画上屏连续失败，已回退到 "
                                "shared-memory 路径（60 秒后自动重试）。"
                            )
                    self._renderer_failed = True
                    self._retry_after = time.monotonic() + 1.0
                    with self._condition:
                        self._needs_configure = True
                        self._needs_target_resize = False
                    self._note("renderer_failures")
                    breaker_open = self._gpu_restart_breaker.record()
                    if (
                        self._native_preview
                        and not breaker_open
                        and not isinstance(exc, _ConfigPhaseError)
                    ):
                        # G6 帧级失败保留 sidecar 与 DComp 子窗口（2026-10 低配
                        # 机频闪主源）：杀进程会让字幕层整层消失，1 秒后重启再
                        # 闪现——弱机上反复发生就是频闪。保留进程时屏幕冻结在
                        # 最后一帧呈现上，重试走同进程重配。真楔死由两条既有
                        # 通路收尾：断路器熔断（breaker_open）与 configure 阶段
                        # 失败（_ConfigPhaseError——native 楔死时重发死进程只会
                        # 白等超时，2026-10 拖大后永久卡死的教训），两者照旧
                        # 杀进程重建。
                        pass
                    else:
                        self._close_renderer()
                    self._note_backend_mode("cpu")
                    self._report_fallback(
                        f"GPU 字幕预览异常，当前帧已回退 Painter，稍后会自动重试：{exc}"
                    )
                    if breaker_open:
                        _preview_diagnostic(
                            f"[GPU 预览] {self._gpu_restart_breaker.window_s:.0f}s 内"
                            f"第 {self._gpu_restart_breaker.limit} 次失败重启，断路器"
                            "熔断：本会话停用 GPU 预览，固定回退 Painter",
                            flush=True,
                        )
                        self._note("gpu_circuit_open")
                        self._report_fallback(
                            "GPU 预览短时间内反复重启，已停止自动重试并固定回退 "
                            "Painter；关闭再打开预览（或重启应用）后可再次尝试。"
                        )
                    if not speculative:
                        now = time.monotonic()
                        if now - self._last_fallback_emit >= 0.5:
                            # CPU 补帧限速：失败窗口内每个时钟请求都补一帧会把
                            # worker 线程按 ~300ms/帧的速度堵死（渲染恢复请求
                            # 排不进队列）。限速后 GUI 保持最后一帧 + busy 徽标，
                            # worker 保持空闲以便尽快重配 GPU。
                            self._last_fallback_emit = now
                            self._note("fallback_frames_emitted")
                            self._emit_python_fallback(
                                track,
                                style,
                                extra_tracks,
                                width,
                                height,
                                dpr,
                                t_ms,
                                generation,
                                duration_ms,
                            )
        finally:
            self._close_renderer()

    def _render_pooled_batch(
        self,
        renderer: NativeRendererProcess,
        *,
        t_ms: int,
        serial: int,
        speculative: bool,
        generation: int,
        shm_key: str,
        dpr: float,
        submitted_at: float,
        force_warp: bool,
        explicit_timestamps: Optional[list[int]] = None,
    ) -> None:
        """Submit a bounded current or media-clock-ahead batch to the pool.

        ``explicit_timestamps``：填缝调度器给定窗口内的缺失帧序列，跳过
        「从请求戳连排」的默认构造。
        """
        with self._condition:
            playing = self._playing
        if explicit_timestamps is not None:
            requests = [
                (int(t), int(serial), bool(speculative), time.monotonic())
                for t in explicit_timestamps
            ]
        else:
            requests = [(int(t_ms), int(serial), bool(speculative), float(submitted_at))]
            if playing:
                future_t = int(t_ms)
                for _ in range(1, self._active_worker_count):
                    future_t = self._next_frame_timestamp(future_t)
                    requests.append(
                        (future_t, int(serial), bool(speculative), time.monotonic())
                    )

        metadata: dict[int, tuple[int, int, bool, float]] = {}
        batch_started = time.monotonic()
        for request_t, request_serial, is_speculative, request_submitted in requests:
            frame_index = self._frame_index
            self._frame_index += 1
            wire_serial = frame_index
            metadata[wire_serial] = (
                request_t,
                request_serial,
                is_speculative,
                request_submitted,
            )
            renderer.begin_render_gpu_frame(
                request_t,
                force_warp=force_warp,
                generation=generation,
                frame_index=frame_index,
                request_serial=wire_serial,
                shm_key=shm_key,
                include_checksum=False,
                readback_bands=True,
                # Deferred followers must not resize an already attached named
                # shared-memory mapping when the pool grows from one ready
                # worker to its final size. Reserve the stable ring capacity
                # from the very first frame instead.
                slot_count=self._readback_slot_count(),
            )
        with self._stats_lock:
            self._stats["max_in_flight"] = max(
                self._stats["max_in_flight"], len(metadata)
            )

        completed: list[dict] = []
        for _ in metadata:
            try:
                completed.append(renderer.finish_render_gpu_frame())
            except NativeQueueFullError:
                # 流控：该槽位的提交被拒（池被上一批投机帧占满）。被拒帧
                # 从未开始渲染，不影响本批其余帧的交付；媒体时钟会在下一
                # tick 重发被拒的时间戳。必须逐帧容错——让异常穿透会把整
                # 批已完成的帧一起丢掉（2026-10 实测：背压计数暴涨且当前
                # 帧被饿死）。
                self._note("queue_full_backpressure")
        completed_at = time.monotonic()
        self._record_timing(
            "roundtrip_ms", (completed_at - batch_started) * 1000.0
        )
        self._adapt_pipeline_lookahead()
        completed.sort(key=lambda event: int(event.get("t_ms", -1)))
        for event in completed:
            if (
                not self._accept_realization_event(event, generation)
                or event.get("event") == "gpu_frame_dropped"
            ):
                self._note("stale_frames_dropped")
                item = metadata.get(int(event.get("request_serial", -1)))
                if item is not None and not item[2]:
                    self._retry_after_realization_drop(event, item[0], item[1], generation)
                continue
            ready_workers = max(
                1,
                min(
                    int(event.get("worker_count_ready", self._active_worker_count)),
                    self._worker_count_requested,
                ),
            )
            if ready_workers != self._active_worker_count:
                self._active_worker_count = ready_workers
                with self._stats_lock:
                    self._stats["worker_count"] = ready_workers
            wire_serial = int(event.get("request_serial", -1))
            item = metadata.get(wire_serial)
            if item is None:
                self._note("stale_frames_dropped")
                continue
            request_t, request_serial, is_speculative, request_submitted = item
            self._record_event_timing("render_ms", event.get("render_ms"))
            self._record_event_timing("readback_ms", event.get("readback_ms"))
            if not is_speculative:
                self._record_timing(
                    "ready_latency_ms", (completed_at - request_submitted) * 1000.0
                )
            event_key = str(event.get("shm_key") or "")
            if self._reader is None or self._reader.shm_key != event_key:
                if self._reader is not None:
                    self._reader.close()
                self._reader = SharedFrameRingReader.from_event(event)
            try:
                image = self._reader.read_qimage(event)
            except StaleSharedFrameSlotError:
                self._note("stale_frame_slots_skipped")
                continue
            image.setDevicePixelRatio(dpr)
            if is_speculative:
                self._cache_speculative(image, request_t, generation)
            elif self._may_emit(request_t, generation):
                self._note("frames_emitted")
                self.frame_ready.emit(image, request_t)
                self._note_backend_mode("gpu")
            else:
                self._note("stale_frames_dropped")

    def _report_fallback(self, message: str) -> None:
        self._fallback_gate.report(message)

    def progress_snapshot(self):
        """最近一拍 sidecar 心跳阶段（GUI 线程轮询，见忙碌徽标）。

        快照 dict 由 backend 管道线程整体替换发布，这里无锁转读；
        进程未起/测试假件没有该接口时返回 None。G5/G6 共用同一
        sidecar，两模式都能取到阶段。
        """
        renderer = self._renderer_owner.process
        snapshot = getattr(renderer, "progress_snapshot", None)
        if not callable(snapshot):
            return None
        try:
            return snapshot()
        except Exception:  # noqa: BLE001 - 轮询路径绝不向 GUI 抛错
            return None

    def _ensure_renderer(self) -> NativeRendererProcess:
        return self._renderer_owner.ensure()

    def _close_renderer(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        # 新 sidecar 没有已解析的行数据，差分基准作废；首个请求自动走全量。
        self._style_patch_key = None
        with self._condition:
            # 进程随子窗口一起销毁；新进程的渲染耗时基准也重新采样。
            self._native_child_open = False
            self._native_last_presented = None
            self._render_ms_ema = 0.0
        self._renderer_owner.close()

    def _project_playback_timestamp(
        self, t_ms: int, submitted_at: float, ema_ms: float
    ) -> int:
        """播放态把渲染目标戳前移到预计完成时刻（2026-10 用户提议的追帧）。

        目标帧率**永远是项目帧率**（一般 60）：这里不做任何限速，只在吞吐
        暂时跟不上时，按「拾取年龄 + 渲染耗时 EMA」让每一帧落地即当前
        （当前 22fps 就按 22fps 出有效帧），渲染变快 EMA 回落、投喂自动
        回到逐帧节奏。

        前移量天花板取**过期容忍窗**（_STALE_TOLERANCE_MS=120ms）而非
        固定帧数（2026-10 用户指出：EMA 多采样本身已抑制尖峰，固定 3 帧
        键≈50ms 会把慢机的合法补偿砍掉——别的电脑真要 120ms/帧时应全额
        前移）。天花板只兜 EMA 高估的尖峰过冲（字幕最多早一个容忍窗），
        对 120ms 内的渲染耗时永不生效。暂停态禁止前移——GUI 侧要求精确
        匹配当前请求戳。
        """
        if not self._playing or not self._native_project_ahead:
            return int(t_ms)
        age_ms = max(0.0, (time.monotonic() - float(submitted_at)) * 1000.0)
        ahead_ms = min(age_ms + float(ema_ms or 0.0), self._STALE_TOLERANCE_MS)
        if ahead_ms < 1.0:
            return int(t_ms)
        media_key = self._frame_cache.key_for(int(t_ms))
        projected_key = self._frame_cache.key_for(int(t_ms) + int(ahead_ms))
        key = max(media_key, projected_key)
        # 恢复瞬态的单调约束（2026-10 用户问询暴露的缺陷）：负载骤降时
        # EMA 仍高估，目标戳随衰减逐帧回退 → 字幕先跳超前再倒着走。地板
        # = 上次已画键+1（消灭倒走）；天花板 = 当前媒体键+容忍窗键数（把
        # 超前钳在验收窗内，防地板在渲染快于键率时越跑越前）。EMA 收敛
        # 后两条约束都不再绑定，回到逐帧节奏——等价于用户的「填缝恢复」
        # （10,20,30… → 密到 15,25… → 13… → 60fps）的端态。
        interval_keys = max(
            1,
            int(round(self._STALE_TOLERANCE_MS * self._frame_cache._fps / 1000.0)),  # noqa: SLF001
        )
        ceiling_key = media_key + interval_keys
        with self._condition:
            last = self._native_last_presented
        if last is not None and last[0] == self._generation:
            key = max(key, last[1] + 1)
        key = min(key, ceiling_key)
        return self._frame_cache.timestamp_for_key(key)

    def _media_now_ms(self) -> float:
        """播放态媒体时钟：以最近一次 request 为锚点按墙钟外推。"""
        with self._condition:
            if self._g6_media_wall <= 0.0:
                return float(self._g6_media_t)
            return float(self._g6_media_t) + (
                time.monotonic() - self._g6_media_wall
            ) * 1000.0

    def _run_native_due_scheduler(
        self,
        renderer: NativeRendererProcess,
        generation: int,
        force_warp: bool,
    ) -> None:
        """G6 直画策略的到点队列调度（见 _run_due_scheduler 总注释）。"""
        self._run_due_scheduler(renderer, generation, force_warp, native=True)

    def _run_readback_due_scheduler(
        self,
        renderer: NativeRendererProcess,
        generation: int,
        force_warp: bool,
        shm_key: str,
        dpr: float,
    ) -> None:
        """G5 读回策略的窗口填缝调度（见 _run_due_scheduler 总注释）。"""
        self._run_due_scheduler(
            renderer, generation, force_warp, native=False,
            shm_key=shm_key, dpr=dpr,
        )

    def _run_cpu_due_scheduler(
        self,
        generation: int,
        track: TimingTrack,
        style: Style,
        extra_tracks: list[TimingTrack],
        duration_ms: int,
        width: int,
        height: int,
        dpr: float,
    ) -> None:
        """CPU 回退的填缝调度（GPU 重试窗口内持续画未来帧入缓存）。"""
        def render_fill(t_ms: int) -> Optional[QImage]:
            return self._render_cpu_frame(
                track, style, extra_tracks, duration_ms, width, height, dpr, t_ms
            )

        renderer = self._renderer_owner.process
        self._run_due_scheduler(
            renderer if renderer is not None else _NULL_RENDERER,
            generation,
            False,
            native=False,
            cpu_render=render_fill,
            deadline_wall=self._retry_after,
        )

    def _render_cpu_frame(
        self,
        track: TimingTrack,
        style: Style,
        extra_tracks: list[TimingTrack],
        duration_ms: int,
        width: int,
        height: int,
        dpr: float,
        t_ms: int,
    ) -> Optional[QImage]:
        """用 QPainter 渲染一帧（CPU 回退的渲染体，含 EMA 更新）。"""
        paint_started = time.monotonic()
        physical_w, physical_h, dpr = preview_render_target_size(width, height, dpr)
        image = QImage(
            physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied
        )
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            with render_progress_scope(
                _render_progress_reporter(
                    _RENDER_STAGE_SPANS_PAINTER, self.renderProgress.emit
                )
            ):
                paint_frame_to_painter(
                    painter,
                    width,
                    height,
                    track,
                    int(t_ms),
                    style,
                    extra_tracks,
                    duration_ms=duration_ms,
                )
        except Exception:  # noqa: BLE001 - 回退帧自身的异常绝不能杀死 worker 线程
            _log.exception("CPU 回退帧渲染失败（worker 继续运行）")
            self._note("fallback_failures")
            return None
        finally:
            painter.end()
        paint_ms = (time.monotonic() - paint_started) * 1000.0
        if paint_ms > 0.0:
            self._cpu_render_ms_ema = (
                paint_ms
                if self._cpu_render_ms_ema <= 0.0
                else self._cpu_render_ms_ema * 0.7 + paint_ms * 0.3
            )
        return image

    def _run_due_scheduler(
        self,
        renderer: NativeRendererProcess,
        generation: int,
        force_warp: bool,
        *,
        native: bool,
        shm_key: str = "",
        dpr: float = 1.0,
        cpu_render: Optional[Callable[[int], Optional[QImage]]] = None,
        deadline_wall: Optional[float] = None,
    ) -> None:
        """播放态公共填缝调度器（2026-10 用户模型，G5/G6/CPU 三条运输线）。

        **一个调度模型**：目标帧率永远是项目帧率；吞吐跟不上时按容量渲
        染「来得及播放的帧」，空转产能持续填后续帧（15,25,35… 的填缝），
        到点呈现、按容量逐步爬回 60。差异只在运输层：

        - native（G6）：渲染到纹理进队，到点经 DComp 上屏；
        - 读回（G5）：池化/单命令渲染 [当前, 容忍窗] 窗口内**缓存缺失**
          的帧，GUI 请求到点时缓存命中上屏；池子不再依赖 60Hz 请求接力；
        - painter（CPU 回退）：``cpu_render(t) -> QImage`` 逐帧画未来帧
          入同一缓存窗口，GPU 重试窗口（``deadline_wall``）内持续填缝，
          到点同样缓存命中——CPU 的空转产能也变成已备好的帧。

        退出条件：暂停 / 视图隐藏 / 代际变化（seek、样式改动）/ 停止 /
        （painter）重试窗口到期回主循环重配 GPU。异常上抛给 _run 的
        失败语义统一处理。
        """
        fps = max(int(self._frame_cache._fps), 1)  # noqa: SLF001
        interval_ms = 1000.0 / fps
        queue_cap = native_due_queue_capacity(
            self._STALE_TOLERANCE_MS, fps, self._native_frame_store_capacity
        )
        rendered_queue: list[tuple[int, int]] = []  # native: (key, t_ms) 递增
        # 读回（G5）连续饱和提交的在途表：wire serial → 填充目标时间戳。
        inflight: dict[int, int] = {}
        # 在途失速看门狗的锚点（最近一次完成时刻）。
        last_completion_wall = time.monotonic()
        # queue_full 持续起点（None=当前未处于被拒状态）。
        queue_full_since: Optional[float] = None
        cpu_mode = cpu_render is not None

        while True:
            with self._condition:
                if (
                    self._stopped
                    or not self._playing
                    or generation != self._generation
                    or (native and (not self._native_preview
                                    or self._native_target is None))
                    or (not native and not cpu_mode and self._native_preview)
                ):
                    return
                if deadline_wall is not None and time.monotonic() >= deadline_wall:
                    return
                native_target = self._native_target
                serial = self._request_serial
            media_now = self._media_now_ms()

            if native:
                # 1) 出队到点帧（一帧一帧放，保持到点才播放的语义）。
                while rendered_queue and rendered_queue[0][1] <= media_now + 1.0:
                    _, due_t = rendered_queue.pop(0)
                    self._present_g6_frame(
                        renderer, due_t, native_target, generation, force_warp
                    )
                    media_now = self._media_now_ms()

                # 2) 补渲染：填缝到容忍窗前沿为止。
                last_key = rendered_queue[-1][0] if rendered_queue else None
                with self._condition:
                    last = self._native_last_presented
                if (
                    last is not None
                    and last[0] == self._generation
                    and (last_key is None or last_key < last[1])
                ):
                    last_key = last[1]
                if len(rendered_queue) < queue_cap:
                    floor_key = 0 if last_key is None else last_key + 1
                    ahead_key = self._frame_cache.key_for(
                        int(media_now + self._render_ms_ema)
                    )
                    ceiling_key = self._frame_cache.key_for(
                        int(media_now + self._STALE_TOLERANCE_MS)
                    )
                    target_key = max(floor_key, ahead_key, 0)
                    target_key = min(target_key, ceiling_key)
                    if target_key >= max(floor_key, 0) or last_key is None:
                        render_t = self._frame_cache.timestamp_for_key(target_key)
                        render_event = renderer.render_gpu_frame_direct(
                            render_t,
                            force_warp=force_warp,
                            generation=generation,
                            frame_index=self._frame_index,
                        )
                        self._frame_index += 1
                        render_ms = float(render_event.get("render_ms") or 0.0)
                        if render_ms > 0.0:
                            self._render_ms_ema = (
                                render_ms
                                if self._render_ms_ema <= 0.0
                                else self._render_ms_ema * 0.7 + render_ms * 0.3
                            )
                        self._record_event_timing("render_ms", render_ms)
                        rendered_queue.append((target_key, render_t))
                        self._note("requests")
                        continue
            else:
                # 读回（G5）/ painter（CPU 回退）：连续饱和填缝。
                # 旧模型「凑一批、提交 N、阻塞收 N」在批间把池抽干，且填充
                # 前沿从 media_now 起步——渲染完成时播放头往往已越过该键
                # （2026-10 真实工程实测：20fps 填充对 63fps 请求，缓存
                # ~91% miss，播放饿死/冻结）。新模型：完成一帧立刻补一帧
                # 提交（worker 持续饱和不排空），前沿按管线时延 EMA 前移
                # （帧完成时刻恰好落在请求键上），到点缓存命中上屏。
                cache = self._frame_cache
                # 播放头之前的死键清扫（详见 evict_before 注释）：请求跳过的
                # 已填键若不逐出会占满容量 → free_slots 恒 0 → 播放几秒后
                # 永久停摆（15s 长跑探针 t=5s 起 hits+0 / cache 29/29 不动）。
                if cache.evict_before(cache.key_for(int(media_now))):
                    self._note("stale_frames_dropped")
                if cpu_mode:
                    floor_key = cache.key_for(int(media_now))
                    ceiling_key = cache.key_for(
                        int(media_now + self._STALE_TOLERANCE_MS)
                    )
                    ceiling_key = min(
                        ceiling_key, floor_key + cache.capacity() - 1
                    )
                    painted = 0
                    key = floor_key
                    while key <= ceiling_key and painted < 2:
                        if not cache.contains_key(key):
                            fill_t = cache.timestamp_for_key(key)
                            image = cpu_render(fill_t)
                            if image is not None:
                                self._note("fallback_frames")
                                self._cache_speculative(
                                    image, fill_t, generation
                                )
                            painted += 1
                        key += 1
                    continue

                # a) 收割一帧完成（小超时等待，避免空转轮询烧 CPU）。
                if inflight:
                    if time.monotonic() - last_completion_wall > 8.0:
                        # 在途失速看门狗：sidecar native 楔死（设备级毒化，
                        # 连池重建都救不回）时异步发布永远不来、也不会有
                        # 任何异常——必须主动制造失败走杀进程重启链，
                        # 否则播放永久冻结（2026-10 拖大楔死的最后一块）。
                        raise NativeRendererError(
                            f"render stall watchdog: {len(inflight)} in-flight, "
                            f"no completion for "
                            f"{time.monotonic() - last_completion_wall:.1f}s"
                        )
                    try:
                        event = renderer.try_finish_render_gpu_frame(0.002)
                    except NativeQueueFullError:
                        # 流控：sidecar in-flight 池满。短暂退避后同批重提。
                        self._note("queue_full_backpressure")
                        if queue_full_since is None:
                            queue_full_since = time.monotonic()
                        with self._condition:
                            self._condition.wait(timeout=0.004)
                        event = None
                    if event is not None:
                        done_serial = int(event.get("request_serial", -1))
                        fill_t = inflight.pop(done_serial, None)
                        self._absorb_readback_event(
                            event, fill_t, generation, dpr
                        )
                        last_completion_wall = time.monotonic()
                        queue_full_since = None
                elif queue_full_since is not None:
                    # inflight 已空但仍在持续 queue_full：卡死的 native worker
                    # 永久占用 in-flight 槽，每次提交都被拒——退避循环永无
                    # 完成事件，上方看门狗（依赖 inflight 非空）永远不触发。
                    # 持续超阈值即判定渲染器死亡，走杀进程重启链。
                    stalled = time.monotonic() - queue_full_since
                    if stalled > 5.0:
                        raise NativeRendererError(
                            f"submit rejected (in-flight occupied) for "
                            f"{stalled:.1f}s: native pool has a dead worker"
                        )
                # b) 补提：worker 有空位且窗口内有缺失键 → 立刻提交，
                #    在途上限 = active worker 数，缓存槽位同步预留。
                render_cap = max(1, self._active_worker_count)
                lead = int(
                    max(0.0, min(self._fill_lead_ms, self._STALE_TOLERANCE_MS))
                )
                floor_key = cache.key_for(int(media_now + lead))
                ceiling_key = cache.key_for(
                    int(media_now + self._STALE_TOLERANCE_MS)
                )
                ceiling_key = min(ceiling_key, floor_key + cache.capacity() - 1)
                inflight_keys = {cache.key_for(t) for t in inflight.values()}
                key = floor_key
                while key <= ceiling_key and len(inflight) < render_cap:
                    free_slots = cache.capacity() - cache.size() - len(inflight)
                    if free_slots <= 0:
                        break
                    if key in inflight_keys or cache.contains_key(key):
                        key += 1
                        continue
                    fill_t = cache.timestamp_for_key(key)
                    wire = self._frame_index
                    self._frame_index += 1
                    renderer.begin_render_gpu_frame(
                        fill_t,
                        force_warp=force_warp,
                        generation=generation,
                        frame_index=wire,
                        request_serial=wire,
                        shm_key=shm_key,
                        include_checksum=False,
                        readback_bands=True,
                        slot_count=self._readback_slot_count(),
                    )
                    inflight[wire] = fill_t
                    inflight_keys.add(key)
                    self._note("requests")
                    key += 1
                if inflight:
                    with self._stats_lock:
                        self._stats["max_in_flight"] = max(
                            self._stats["max_in_flight"], len(inflight)
                        )
                if not inflight:
                    # 窗口已填满且无在途：短睡等唤醒（播放头推进后窗口
                    # 前移出新的缺失键）。
                    with self._condition:
                        if self._stopped or not self._playing:
                            return
                        self._condition.wait(timeout=0.002)
                continue

            # 3) 队列满/填到前沿：等到点或新事件（唤醒即重评）。
            with self._condition:
                if self._stopped or not self._playing:
                    return
                self._condition.wait(timeout=0.002)

    def _absorb_readback_event(
        self,
        event: dict,
        fill_t: Optional[int],
        generation: int,
        dpr: float,
    ) -> None:
        """处理一帧读回完成事件：worker 爬坡 + 时延记账 + 入缓存。

        填充前沿前移量（_fill_lead_ms）按控制器式 EMA 自校正：误差 =
        完成时刻媒体钟 - 填充目标（正=完成晚于目标，需要更大前移）。
        """
        ready_workers = max(
            1,
            min(
                int(event.get("worker_count_ready", self._active_worker_count)),
                self._worker_count_requested,
            ),
        )
        if ready_workers != self._active_worker_count:
            self._active_worker_count = ready_workers
            with self._stats_lock:
                self._stats["worker_count"] = ready_workers
        if fill_t is None:
            # 上一轮调度器遗留的在途响应（热切换/代际变化后冲刷）。
            self._note("stale_frames_dropped")
            return
        if (
            not self._accept_realization_event(event, generation)
            or event.get("event") == "gpu_frame_dropped"
        ):
            self._note("stale_frames_dropped")
            return
        render_ms = float(event.get("render_ms") or 0.0)
        if render_ms > 0.0:
            self._render_ms_ema = (
                render_ms
                if self._render_ms_ema <= 0.0
                else self._render_ms_ema * 0.7 + render_ms * 0.3
            )
        self._record_event_timing("render_ms", render_ms)
        self._record_event_timing("readback_ms", event.get("readback_ms"))
        error_ms = self._media_now_ms() - fill_t
        self._fill_lead_ms = max(
            0.0,
            min(
                self._fill_lead_ms + 0.3 * error_ms,
                self._STALE_TOLERANCE_MS,
            ),
        )
        event_key = str(event.get("shm_key") or "")
        if self._reader is None or self._reader.shm_key != event_key:
            if self._reader is not None:
                self._reader.close()
            self._reader = SharedFrameRingReader.from_event(event)
        try:
            image = self._reader.read_qimage(event)
        except StaleSharedFrameSlotError:
            # 槽位已被更新的帧复用（在途窗口 > 槽数）：事件过时，丢弃本
            # 次吸收即可——填缝调度器继续推进，绝不据此杀 sidecar。
            self._note("stale_frame_slots_skipped")
            return
        image.setDevicePixelRatio(dpr)
        self._cache_speculative(image, fill_t, generation)

    def _present_g6_frame(
        self,
        renderer: NativeRendererProcess,
        render_t: int,
        native_target: tuple,
        generation: int,
        force_warp: bool,
    ) -> None:
        """上屏一帧已渲染的纹理：区域钳制到当前配置纹理内 + 记账/emit。"""
        (
            parent_hwnd,
            target_x,
            target_y,
            target_width,
            target_height,
            src_x,
            src_y,
        ) = native_target
        # 窗口几何来自 GUI 线程的最新值，纹理尺寸是本 worker 最近一次
        # configure/resize 的值——resize 期间两者短暂不一致时把区域
        # 钳进纹理内，避免 present 校验失败被计入连续失败弹回退框。
        with self._condition:
            phys_w = max(int(round(self._logical_w * self._device_pixel_ratio)), 1)
            phys_h = max(int(round(self._logical_h * self._device_pixel_ratio)), 1)
        src_x = min(max(src_x, 0), max(phys_w - 1, 0))
        src_y = min(max(src_y, 0), max(phys_h - 1, 0))
        target_width = min(max(target_width, 1), phys_w - src_x)
        target_height = min(max(target_height, 1), phys_h - src_y)
        if target_width < 1 or target_height < 1:
            return

        event = renderer.present_rendered_gpu_frame(
            parent_hwnd=parent_hwnd,
            x=target_x,
            y=target_y,
            width=target_width,
            height=target_height,
            src_x=src_x,
            src_y=src_y,
            t_ms=render_t,
            force_warp=force_warp,
            generation=generation,
        )
        if event.get("dropped"):
            # 帧仓未命中（代际翻动 / 池满丢帧）：本拍不上屏，屏幕延续上一
            # 帧。绝不把别的时刻的像素端出去——这是慢机「预览回退」的根
            # 因修复点。
            self._note("stale_frames_dropped")
            return
        completed_at = time.monotonic()
        self._frame_error_streak = 0
        self._native_preview_failures = 0
        self._note("frames_emitted")
        self._record_event_timing("present_ms", event.get("present_ms"))
        self._native_note_presented(generation, render_t, event)
        if self._may_emit(render_t, generation):
            self.frame_presented.emit(int(render_t))
            self._note_backend_mode("gpu")
            self._g6_present_count += 1
            if self._g6_present_count == 1:
                _preview_diagnostic(
                    f"[GPU 预览] G6 首帧直画成功 "
                    f"present={event.get('present_ms', '?')}ms",
                    flush=True,
                )

    def _hold_until_due(
        self, requested_t: int, submitted_at: float, render_t: int, generation: int
    ) -> None:
        """到点才播放（2026-10 用户拍板）：渲染可以提前，present 必须等到
        目标戳成为当前画面。

        投喂前移把目标戳放到「预计完成时刻」，渲染通常刚好赶上；恢复瞬态
        （EMA 高估/骤快）会提前完成——提前上屏就是字幕超前/倒走的根源。
        这里按「目标戳 − 预估媒体时钟」分段持有等待，暂停/停止/代际变化
        立即放行。预估时钟与投喂同一模型：请求戳 + 请求以来的墙钟流逝。
        """
        if not self._playing:
            return
        media_now_ms = float(requested_t) + (time.monotonic() - submitted_at) * 1000.0
        hold_ms = float(render_t) - media_now_ms
        if hold_ms <= 2.0:
            return
        deadline = time.monotonic() + min(hold_ms, 200.0) / 1000.0
        while time.monotonic() < deadline:
            with self._condition:
                if (
                    self._stopped
                    or not self._playing
                    or generation != self._generation
                ):
                    return
            time.sleep(0.004)

    def _native_render_timestamp(
        self,
        t_ms: int,
        submitted_at: float,
        content_changed: bool,
    ) -> Optional[int]:
        """决定本周期直画的时间戳；与已上屏帧同键时返回 ``None``（跳过）。

        同键去重保证实际吞吐自适应：只有新帧键才值得渲（媒体时钟在帧键
        内抖动的重复请求直接跳过，见 stats native_redundant_frames_skipped）。
        """
        key = self._frame_cache.key_for(int(t_ms))
        if content_changed:
            return self._frame_cache.timestamp_for_key(key)
        projected = self._project_playback_timestamp(
            t_ms, submitted_at, self._render_ms_ema
        )
        # 同键去重放在钳位之后用最终键判断：超前顶到天花板时，地板与
        # 天花板的 clamp 可能回落到与上次相同的键——那是重复帧，跳过等
        # 媒体时钟前进，而不是再画一遍。
        with self._condition:
            last = self._native_last_presented
        if last is not None and last[0] == self._generation:
            if last[1] == key:
                return None
            projected_key = self._frame_cache.key_for(projected)
            if projected_key == last[1]:
                return None
        return projected

    def _native_note_presented(
        self, generation: int, t_ms: int, event: dict
    ) -> None:
        with self._condition:
            self._native_last_presented = (
                int(generation),
                self._frame_cache.key_for(int(t_ms)),
            )
        render_ms = float(event.get("render_ms") or 0.0)
        if render_ms > 0.0:
            self._render_ms_ema = (
                render_ms
                if self._render_ms_ema <= 0.0
                else self._render_ms_ema * 0.7 + render_ms * 0.3
            )

    def _dump_presented_frame_png(
        self,
        renderer,
        t_ms: int,
        generation: int,
        force_warp: bool,
    ) -> None:
        """调试：G6 present 后把同 t 纹理按读回路径落 PNG（env 门控）。

        与屏幕抓图不同，这条路径拿到的是 sidecar 刚 present 的那份渲染
        纹理本身（同一 D2D 场景、同 t 重渲，像素级一致），不受 DComp 无法
        BitBlt / 窗口遮挡 / 缩放映射的影响。失败只打日志，不影响出帧。
        """
        try:
            import tempfile

            with self._condition:
                shm_key = self._shm_key
            if not shm_key:
                return
            event = None
            for _ in range(2):   # present 后立即重渲可能撞已知 D2D 死锁，重试一次
                try:
                    event = renderer.render_gpu_frame(
                        t_ms,
                        force_warp=force_warp,
                        generation=generation,
                        frame_index=0,
                        shm_key=shm_key,
                        include_checksum=False,
                        slot_count=1,
                    )
                    break
                except NativeRendererError:
                    time.sleep(0.3)
            if event is None:
                _preview_diagnostic("[native-dump] render retry exhausted", flush=True)
                return
            event_key = str(event.get("shm_key") or "")
            if self._reader is None or self._reader.shm_key != event_key:
                if self._reader is not None:
                    self._reader.close()
                self._reader = SharedFrameRingReader.from_event(event)
            image = self._reader.read_qimage(event)
            out_dir = os.path.join(tempfile.gettempdir(), "consistency")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"g6_{int(t_ms)}.png")
            image.save(path)
            _preview_diagnostic(f"[native-dump] {path}", flush=True)
            self._note("native_frame_png_dumped")
        except Exception as exc:  # pragma: no cover - 调试路径
            _preview_diagnostic(f"[native-dump] failed: {exc}", flush=True)

    def _may_emit(self, t_ms: int, generation: int) -> bool:
        with self._condition:
            if self._stopped or generation != self._generation or self._latest_t is None:
                return False
            if not self._playing:
                return int(t_ms) == int(self._latest_t)
            delta = int(t_ms) - int(self._latest_t)
            return 0 <= delta <= self._STALE_TOLERANCE_MS

    def _retry_after_realization_drop(
        self, event: dict, t_ms: int, serial: int, generation: int
    ) -> None:
        """A paused foreground request still needs an image after a path drop."""
        with self._condition:
            if event.get("reason") != "realization_ready" and not (
                event.get("realization_path_ready") is False
                and self._realization_ready_generation == generation
            ):
                return
            if (
                not self._stopped
                and not self._playing
                and generation == self._generation
                and serial == self._request_serial
                and self._pending is None
            ):
                self._pending = (int(t_ms), serial, False, time.monotonic())
                self._condition.notify_all()

    def _accept_realization_event(self, event: dict, generation: int) -> bool:
        """Advance the path floor for this generation, including dropped frames."""
        with self._condition:
            if (
                generation != self._generation
                or int(event.get("generation", generation)) != generation
            ):
                return False
            if (
                event.get("realization_ready") is True
                or event.get("realization_path_ready") is True
            ):
                if self._realization_ready_generation != generation:
                    self._realization_ready_generation = generation
                    self._frame_cache.clear()
            return not (
                self._realization_ready_generation == generation
                and event.get("realization_path_ready") is False
            )

    def accepts_realization_image(self, image: QImage) -> bool:
        """Reject raw GPU images still queued in Qt after the path transition."""
        with self._condition:
            path = image.text("gpu_realization_path")
            if not path:
                return True
            return image.text("gpu_generation") == str(self._generation) and not (
                path == "raw" and self._realization_ready_generation == self._generation
            )

    def _cache_speculative(self, image: QImage, t_ms: int, generation: int) -> None:
        with self._condition:
            if (
                self._stopped
                or generation != self._generation
                or self._latest_t is None
                or not self.accepts_realization_image(image)
            ):
                self._note("stale_frames_dropped")
                return
            if self._frame_cache.key_for(t_ms) < self._frame_cache.key_for(self._latest_t):
                self._note("stale_frames_dropped")
                return
            self._frame_cache.store(t_ms, image)
        self._note("future_frames_cached")

    def _schedule_lookahead(self, t_ms: int, serial: int, generation: int) -> None:
        with self._condition:
            if (
                self._stopped
                or not self._playing
                or self._lookahead_frames <= 0
                or generation != self._generation
                or self._pending is not None
                or serial != self._request_serial
            ):
                return
            self._pending = (
                self._pipeline_anchor_timestamp(t_ms),
                serial,
                True,
                time.monotonic(),
            )
            self._note_max_pending(1)
            self._condition.notify()

    @staticmethod
    def _next_frame_timestamp(t_ms: int) -> int:
        return int(round(int(t_ms) + 1000.0 / 60.0))

    def _pipeline_anchor_timestamp(self, t_ms: int) -> int:
        current_key = self._frame_cache.key_for(int(t_ms))
        return self._frame_cache.timestamp_for_key(
            current_key + self._effective_lookahead_frames
        )

    def _adapt_pipeline_lookahead(self) -> None:
        if self._lookahead_frames <= 0:
            return
        with self._stats_lock:
            samples = list(self._timings["roundtrip_ms"])
            if samples:
                samples.sort()
                p95_index = min(
                    max(math.ceil(len(samples) * 0.95) - 1, 0),
                    len(samples) - 1,
                )
                p95_ms = samples[p95_index]
            else:
                p95_ms = 0.0
            recommended = max(
                self._lookahead_frames,
                int(math.ceil(p95_ms / (1000.0 / 60.0))) + 2,
            )
            self._effective_lookahead_frames = min(
                recommended, self._max_lookahead_frames
            )
            self._stats["pipeline_lead_frames"] = self._effective_lookahead_frames

    def _emit_python_fallback(
        self,
        track: TimingTrack,
        style: Style,
        extra_tracks: list[TimingTrack],
        width: int,
        height: int,
        dpr: float,
        t_ms: int,
        generation: int,
        duration_ms: int,
    ) -> None:
        if not self._may_emit(t_ms, generation):
            self._note("stale_frames_dropped")
            return
        physical_w, physical_h, dpr = preview_render_target_size(width, height, dpr)
        image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            with render_progress_scope(
                _render_progress_reporter(
                    _RENDER_STAGE_SPANS_PAINTER, self.renderProgress.emit
                )
            ):
                paint_frame_to_painter(
                    painter,
                    width,
                    height,
                    track,
                    int(t_ms),
                    style,
                    extra_tracks,
                    duration_ms=duration_ms,
                )
        except Exception:  # noqa: BLE001 - 回退帧自身的异常绝不能杀死 worker 线程
            # 线程一旦死亡，GPU 与 CPU 两条预览路径都会永久停摆，只能重启页面。
            _log.exception("CPU 回退帧渲染失败（worker 继续运行）")
            self._note("fallback_failures")
            return
        finally:
            painter.end()
        if self._may_emit(t_ms, generation):
            self._note("fallback_frames")
            self._note("frames_emitted")
            self.frame_ready.emit(image, int(t_ms))
            self._note_backend_mode("cpu")

    def _note_backend_mode(self, mode: str) -> None:
        """记录并广播当前实际出帧后端（"gpu"=sidecar / "cpu"=Painter 回退）。

        按翻转去重发信号：稳态运行时每帧调用只是一次锁内比较。
        """
        with self._stats_lock:
            if self._backend_mode == mode:
                return
            self._backend_mode = mode
        self.backendModeChanged.emit(mode)

    @property
    def current_backend_mode(self) -> Optional[str]:
        """最近一次确认的实际渲染后端；None = 尚无帧定论（按选择态展示）。"""
        with self._stats_lock:
            return self._backend_mode

    def _note(self, key: str) -> None:
        # 统计打点绝不抛错：未知键按 0 起计而非 KeyError——本方法跑在
        # 预览 worker 线程里，任何异常都会杀死线程导致 GPU/CPU 预览同
        # 时永久停摆（2026-10 用户实测：断路器熔断路径的未注册键
        # gpu_circuit_open 曾把「优雅回退 Painter」变成预览全死）。
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def _note_max_pending(self, value: int) -> None:
        with self._stats_lock:
            self._stats["max_pending"] = max(self._stats["max_pending"], int(value))

    def _record_event_timing(self, key: str, value: object) -> None:
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return
        self._record_timing(key, normalized)

    def _record_timing(self, key: str, value: float) -> None:
        with self._stats_lock:
            self._timings[key].append(max(float(value), 0.0))

    def stats_snapshot(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def timing_snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._stats_lock:
            result: dict[str, dict[str, float | int]] = {}
            for key, samples in self._timings.items():
                values = sorted(samples)
                if not values:
                    result[key] = {"count": 0, "mean": 0.0, "p95": 0.0, "max": 0.0}
                    continue
                p95_index = min(
                    max(math.ceil(len(values) * 0.95) - 1, 0),
                    len(values) - 1,
                )
                result[key] = {
                    "count": len(values),
                    "mean": sum(values) / len(values),
                    "p95": values[p95_index],
                    "max": values[-1],
                }
            return result


class NativeAsyncSubtitleRenderer(QObject):
    """Preview renderer backed by the native sidecar shared-memory range path."""

    frame_ready = Signal(QImage, int)
    renderProgress = Signal(int, str)
    fallback_occurred = Signal(str)
    backendModeChanged = Signal(str)

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._duration_ms: int = 0
        self._generation = 0
        self._active_generation: Optional[int] = None
        self._pending_t: Optional[int] = None
        self._pending_skip_current = False
        self._stopped = False
        self._needs_configure = True
        self._relayout_scope: Optional[str] = None
        self._restart_renderer = False
        self._renderer_owner = NativeRendererProcessOwner(
            process_factory=NativeRendererProcess,
            response_timeout_s=2.0,
            startup_timeout_s=5.0,
            configure_timeout_s=10.0,
            gpu_configure_timeout_s=30.0,
            close_timeout_s=1.0,
        )
        self._shm_key = ""
        self._renderer_failed = False
        self._fallback_gate = _FallbackReportGate(
            self.fallback_occurred.emit, log_label="native 字幕预览回退"
        )
        self._playing = False
        self._last_t: Optional[int] = None
        self._fps = 60
        self._lookahead_frames = _env_int(
            "KROK_SUBTITLE_NATIVE_LOOKAHEAD_FRAMES",
            6,
            minimum=0,
        )
        # 自适应前瞻（G2 硬性要求 6"失控自愈"）：range 端到端耗时超过前瞻窗口时收缩，
        # 恢复后逐步回涨到配置值；仅 worker 线程读写。
        self._effective_lookahead = self._lookahead_frames
        self._threads = _env_int(
            "KROK_SUBTITLE_NATIVE_THREADS",
            _default_native_preview_threads(),
            minimum=1,
        )
        self._ring_slots = max(
            _env_int(
                "KROK_SUBTITLE_NATIVE_RING_SLOTS",
                self._lookahead_frames + 2,
                minimum=1,
            ),
            self._lookahead_frames + 2,
        )
        self._frame_cache = NativePreviewFrameCache(self._lookahead_frames + 1)
        self._waiting_request_by_key: dict[int, int] = {}
        self._emitted_request_keys: set[int] = set()
        self._stats = NativePreviewStats()
        self._backend_lock = threading.Lock()
        self._backend_mode: Optional[str] = None
        self._condition = threading.Condition()
        self._process_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="subtitle-preview-native-render",
            daemon=True,
        )
        self._thread.start()

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,  # noqa: ARG002 — native 预览暂不支持副轨
        *,
        duration_ms: int | None = None,
        relayout_scope: str | None = None,
    ) -> None:
        _prewarm_font_axis_capabilities(track, style, extra_tracks)
        with self._condition:
            if self._stopped:
                return
            self._track = track
            self._style = style
            self._duration_ms = max(int(duration_ms or 0), 0)
            self._relayout_scope = relayout_scope if relayout_scope else None
            self._advance_generation_locked()
            self._needs_configure = True
            self._frame_cache.clear()
            self._waiting_request_by_key.clear()
            self._emitted_request_keys.clear()
            self._condition.notify()

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        with self._condition:
            if self._stopped:
                return
            w = max(int(width), 1)
            h = max(int(height), 1)
            dpr = max(float(device_pixel_ratio or 1.0), 0.01)
            if (w, h, dpr) != (self._logical_w, self._logical_h, self._device_pixel_ratio):
                self._needs_configure = True
                self._restart_renderer = True
                self._advance_generation_locked()
                self._frame_cache.clear()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
            self._logical_w = w
            self._logical_h = h
            self._device_pixel_ratio = dpr
            self._condition.notify()

    def request(self, t_ms: int) -> None:
        requested_t = int(t_ms)
        requested_key = self._frame_cache.key_for(requested_t)
        cached = self._frame_cache.take(requested_t)
        if cached is not None:
            self._stats.note_cache_hit()
            with self._condition:
                self._emitted_request_keys.add(requested_key)
            self.frame_ready.emit(cached, requested_t)
            # 缓存里只会有 sidecar 渲染的帧（回退帧不入缓存）。
            self._note_backend_mode("gpu")
        else:
            self._stats.note_cache_miss()
        with self._condition:
            if self._stopped:
                return
            if self._should_advance_generation_for_request_locked(requested_t):
                self._advance_generation_locked()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
            self._last_t = requested_t
            self._pending_t = self._last_t
            self._pending_skip_current = cached is not None
            if cached is None:
                self._waiting_request_by_key[requested_key] = requested_t
            self._purge_stale_waiting_locked(requested_key)
            self._condition.notify()

    def set_playing(self, playing: bool) -> None:
        with self._condition:
            if self._stopped:
                return
            normalized = bool(playing)
            if self._playing == normalized:
                return
            self._playing = normalized
            if self._last_t is not None:
                self._advance_generation_locked()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
                self._pending_t = self._last_t
                self._pending_skip_current = False
                self._waiting_request_by_key[self._frame_cache.key_for(self._last_t)] = self._last_t
                self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._condition.notify_all()
        with self._process_lock:
            self._renderer_owner.close()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            snapshot = self._take_next_request()
            if snapshot is None:
                return
            (
                track,
                style,
                width,
                height,
                dpr,
                t_ms,
                generation,
                needs_configure,
                restart_renderer,
                playing,
                skip_current,
                duration_ms,
                relayout_scope,
            ) = snapshot
            if track is None or style is None:
                continue
            if self._renderer_failed:
                self._note_backend_mode("cpu")
                self._emit_python_fallback(
                    track, style, width, height, dpr, t_ms, generation, duration_ms
                )
                continue
            try:
                self._render_native(
                    track,
                    style,
                    duration_ms=duration_ms,
                    width=width,
                    height=height,
                    dpr=dpr,
                    t_ms=t_ms,
                    generation=generation,
                    needs_configure=needs_configure,
                    restart_renderer=restart_renderer,
                    playing=playing,
                    skip_current=skip_current,
                    relayout_scope=relayout_scope,
                )
                self._note_backend_mode("gpu")
            except NativeRendererError as exc:
                self._stats.note_native_renderer_failure()
                if _env_enabled("KROK_SUBTITLE_NATIVE_DEBUG_FAILURES", "0"):
                    _preview_diagnostic(f"native preview failed: {exc}")
                self._report_fallback(
                    f"native 字幕预览异常，当前帧已回退 Painter：{exc}"
                )
                self._renderer_failed = True
                self._close_renderer()
                self._note_backend_mode("cpu")
                self._emit_python_fallback(
                    track, style, width, height, dpr, t_ms, generation, duration_ms
                )
            except Exception as exc:  # noqa: BLE001 - worker 线程必须自愈，不得静默死亡
                _log.exception("native 预览路径出现非预期异常（本帧回退 Painter）")
                self._report_fallback(
                    f"native 预览路径出现非预期异常，当前帧已回退 Painter：{exc}"
                )
                self._renderer_failed = True
                self._close_renderer()
                self._note_backend_mode("cpu")
                self._emit_python_fallback(
                    track, style, width, height, dpr, t_ms, generation, duration_ms
                )

    def _note_backend_mode(self, mode: str) -> None:
        """记录并广播当前实际出帧后端（"gpu"=sidecar / "cpu"=Painter 回退）。"""
        with self._backend_lock:
            if self._backend_mode == mode:
                return
            self._backend_mode = mode
        self.backendModeChanged.emit(mode)

    @property
    def current_backend_mode(self) -> Optional[str]:
        """最近一次确认的实际渲染后端；None = 尚无帧定论（按选择态展示）。"""
        with self._backend_lock:
            return self._backend_mode

    def _take_next_request(
        self,
    ) -> tuple[
        TimingTrack | None,
        Style | None,
        int,
        int,
        float,
        int,
        int,
        bool,
        bool,
        bool,
        bool,
        int,
    ] | None:
        with self._condition:
            while not self._stopped and self._pending_t is None:
                self._condition.wait()
            if self._stopped:
                return None
            t_ms = int(self._pending_t or 0)
            skip_current = self._pending_skip_current
            self._pending_t = None
            self._pending_skip_current = False
            needs_configure = self._needs_configure
            self._needs_configure = False
            restart_renderer = self._restart_renderer
            self._restart_renderer = False
            return (
                self._track,
                self._style,
                self._logical_w,
                self._logical_h,
                self._device_pixel_ratio,
                t_ms,
                self._generation,
                needs_configure,
                restart_renderer,
                self._playing,
                skip_current,
                self._duration_ms,
                self._relayout_scope,
            )

    def _render_native(
        self,
        track: TimingTrack,
        style: Style,
        *,
        width: int,
        height: int,
        dpr: float,
        t_ms: int,
        generation: int,
        needs_configure: bool,
        restart_renderer: bool,
        playing: bool,
        skip_current: bool,
        duration_ms: int = 0,
        relayout_scope: str | None = None,
    ) -> None:
        timestamps = native_preview_timestamps(
            t_ms,
            playing=playing,
            fps=self._fps,
            lookahead_frames=self._effective_lookahead,
            include_current=not skip_current,
        )
        # 调度硬性要求（GPU 计划 §2.5 / G2）：
        # 1. 不回灌积压——过期的 waiting 请求绝不加入新 range 重新渲染；
        # 3. 单次在途帧数 ≤ ring 槽数，杜绝发射端覆写未读槽。
        timestamps = timestamps[: self._ring_slots]
        if not timestamps:
            return
        range_started = time.monotonic()
        with self._process_lock:
            if restart_renderer and self._renderer_owner.process is not None:
                self._renderer_owner.close()
                needs_configure = True
            renderer_was_missing = self._renderer_owner.process is None
            renderer = self._ensure_renderer()
            if renderer_was_missing or needs_configure:
                # dpr 让 native 按显示分辨率光栅化（布局仍在逻辑坐标系），
                # 与 Python 预览路径一致；4K 工程预览不再渲染全分辨率帧。
                with render_progress_scope(
                    _render_progress_reporter(
                        _RENDER_STAGE_SPANS_GPU, self.renderProgress.emit
                    )
                ):
                    renderer.configure(
                        track,
                        style,
                        width=width,
                        height=height,
                        fps=60,
                        dpr=dpr,
                        duration_ms=duration_ms,
                        relayout_scope=relayout_scope,
                    )
            # 资源常驻（G2 硬性要求 4）：shm_key 与 renderer 同生命周期，
            # sidecar 端据此跨 range 复用同一块 ring，不再逐 range 重建。
            shm_key = self._shm_key
            with self._condition:
                if not self._stopped and self._generation == generation:
                    self._active_generation = generation
            try:
                reader: Optional[SharedFrameRingReader] = None
                renderer.start_render_range(
                    timestamps,
                    generation=generation,
                    threads=self._threads,
                    shm_key=shm_key,
                    ring_slots=self._ring_slots,
                )
                while True:
                    event = renderer.read_event()
                    if event.get("event") == "frame_ready":
                        if self._is_current_generation(generation):
                            try:
                                event_key = str(event.get("shm_key") or "")
                                if reader is None or reader.shm_key != event_key:
                                    if reader is not None:
                                        reader.close()
                                    reader = SharedFrameRingReader.from_event(event)
                                slot = reader.read_frame(event)
                                image = slot.to_qimage()
                                image.setDevicePixelRatio(dpr)
                            except NativeRendererError:
                                self._stats.note_stale_frame_dropped()
                                continue
                            requested_t = self._take_waiting_request_for_slot(slot.t_ms)
                            if requested_t is not None:
                                self.frame_ready.emit(image, requested_t)
                            elif int(slot.t_ms) == int(t_ms) and self._mark_emitted_if_new(slot.t_ms):
                                self.frame_ready.emit(image, slot.t_ms)
                            elif self._was_emitted(slot.t_ms):
                                continue
                            elif self._is_behind_latest_request(slot.t_ms):
                                # 早于最新请求的帧没有未来消费者，缓存只会污染 LRU。
                                self._stats.note_stale_frame_dropped()
                            else:
                                self._stats.note_future_frame_cached()
                                self._frame_cache.store(slot.t_ms, image)
                        else:
                            self._stats.note_stale_frame_dropped()
                    elif event.get("event") == "range_done":
                        self._stats.note_range_done_event()
                        self._adapt_lookahead(
                            (time.monotonic() - range_started) * 1000.0, playing=playing
                        )
                        return
                    elif event.get("event") == "generation_cancelled":
                        self._stats.note_native_generation_cancelled_event()
                        continue
            finally:
                if reader is not None:
                    reader.close()
                with self._condition:
                    if self._active_generation == generation:
                        self._active_generation = None

    def _ensure_renderer(self) -> NativeRendererProcess:
        renderer_was_missing = self._renderer_owner.process is None
        renderer = self._renderer_owner.ensure()
        if renderer_was_missing:
            self._needs_configure = True
            self._shm_key = f"krok-preview-{os.getpid()}-{uuid.uuid4().hex}"
        return renderer

    def _close_renderer(self) -> None:
        with self._process_lock:
            self._renderer_owner.close()

    def _report_fallback(self, message: str) -> None:
        self._fallback_gate.report(message)

    def _emit_python_fallback(
        self,
        track: TimingTrack,
        style: Style,
        width: int,
        height: int,
        dpr: float,
        t_ms: int,
        generation: int,
        duration_ms: int,
    ) -> None:
        if not self._is_current_generation(generation):
            return
        physical_w, physical_h, dpr = preview_render_target_size(width, height, dpr)
        image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            with render_progress_scope(
                _render_progress_reporter(
                    _RENDER_STAGE_SPANS_PAINTER, self.renderProgress.emit
                )
            ):
                paint_frame_to_painter(
                    painter,
                    width,
                    height,
                    track,
                    int(t_ms),
                    style,
                    duration_ms=duration_ms,
                )
        except Exception:  # noqa: BLE001 - 回退帧自身的异常绝不能杀死 worker 线程
            _log.exception("CPU 回退帧渲染失败（worker 继续运行）")
            return
        finally:
            painter.end()
        if self._is_current_generation(generation):
            self.frame_ready.emit(image, int(t_ms))

    def _is_current_generation(self, generation: int) -> bool:
        with self._condition:
            return not self._stopped and int(generation) == self._generation

    def _take_waiting_request_for_slot(self, t_ms: int) -> Optional[int]:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            requested_t = self._waiting_request_by_key.pop(key, None)
            if requested_t is None or key in self._emitted_request_keys:
                return None
            self._emitted_request_keys.add(key)
            return requested_t

    def _mark_emitted_if_new(self, t_ms: int) -> bool:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            if key in self._emitted_request_keys:
                return False
            self._emitted_request_keys.add(key)
            return True

    def _was_emitted(self, t_ms: int) -> bool:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            return key in self._emitted_request_keys

    def _purge_stale_waiting_locked(self, current_key: int) -> None:
        """丢弃早于当前帧桶的未兑现请求（G2 硬性要求 1"积压有上限"）。

        过期请求既不回灌新 range 重新渲染，也不再等待兑现——它们对应的画面
        已经过时，唯一正确的结局是作为丢帧统计掉。同帧桶内的毫秒抖动兑现
        （1033ms/1034ms）不受影响。
        """
        stale_keys = [key for key in self._waiting_request_by_key if key < current_key]
        for key in stale_keys:
            del self._waiting_request_by_key[key]
            self._stats.note_stale_frame_dropped()

    def _is_behind_latest_request(self, t_ms: int) -> bool:
        with self._condition:
            latest_t = self._last_t
        if latest_t is None:
            return False
        return self._frame_cache.key_for(int(t_ms)) < self._frame_cache.key_for(int(latest_t))

    def _adapt_lookahead(self, elapsed_ms: float, *, playing: bool) -> None:
        """按 range 端到端耗时收缩/恢复前瞻（G2 硬性要求 6"失控自愈"）。

        range 耗时超过前瞻窗口意味着产出注定过期（§2.5 死亡螺旋的临界条件），
        此时对半收缩前瞻，最低退化为纯 latest-wins 单帧；耗时回落后逐步涨回配置值。
        """
        if not playing or self._lookahead_frames <= 0:
            return
        frame_ms = max(1000.0 / max(self._fps, 1), 1.0)
        window_ms = frame_ms * (self._effective_lookahead + 1)
        if elapsed_ms > window_ms:
            self._effective_lookahead = self._effective_lookahead // 2
        elif elapsed_ms < window_ms * 0.5 and self._effective_lookahead < self._lookahead_frames:
            self._effective_lookahead += 1

    def _should_advance_generation_for_request_locked(self, requested_t: int) -> bool:
        if not self._playing:
            return True
        if self._last_t is None:
            return False
        frame_ms = max(1000.0 / max(self._fps, 1), 1.0)
        delta = int(requested_t) - int(self._last_t)
        if delta < -frame_ms:
            return True
        lookahead_window_ms = frame_ms * max(self._lookahead_frames + 1, 1)
        return delta > lookahead_window_ms

    def _advance_generation_locked(self) -> None:
        active_generation = self._active_generation
        self._generation += 1
        if active_generation is None:
            return
        renderer = self._renderer_owner.process
        if renderer is None:
            return
        try:
            renderer.send_cancel_generation(active_generation)
            self._stats.note_generation_cancelled()
        except NativeRendererError:
            self._renderer_failed = True

    def stats_snapshot(self) -> dict[str, int]:
        return self._stats.snapshot()
