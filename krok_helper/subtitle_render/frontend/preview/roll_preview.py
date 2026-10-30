"""前黑/后黑预览段：无 UI 的帧/音频源 + graphics 场景展示层。

预览与导出的严格一致（AGENTS §9「背景缩放以预览为主口径」口径延伸）：

- 填充几何用与导出滤镜图同一套纯函数（``paint_roll_fill``）：段素材一帧
  先变换到画布尺寸再交给展示层——QGraphicsVideoItem 表达不了拉伸/平铺/
  原尺寸，所以段视频走 QVideoSink → QImage → Python 变换（与 raster 预览
  的主背景同一技术），动图（GIF/WebP）按项目时间定位 QMovie 帧。
- 过渡透明度直接消费 ``roll_layer_alphas``（黑/白场为 Adobe 式剪切点两侧
  fade，交叉溶解为重叠窗口；与导出滤镜同一口径的单一事实源）；graphics 与
  raster 两条预览路径共用本模块的帧源。
- 预览素材：段视频复用普通背景的预览源准备（高画质复制流封装、中低画质
  缩小代理），播放器只吃准备后的路径；缓存缺失时后台准备，不在 GUI 线程
  跑 FFmpeg。动图按项目时间选帧，图片序列按 ``source_fps`` 逐帧索引。
  正式导出仍从原素材按项目帧率重采样——预览不宣称逐帧等价于导出。
- 段音频：独立 QMediaPlayer 播放，``audio_display_position`` 暴露给传输条
  做跨段时钟锚定（前黑/后黑区不再只靠墙钟自走）；暂停/seek 与主媒体同
  口径同步。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Optional

from PyQt6.QtCore import QObject, QRectF, Qt, QUrl, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
)

from krok_helper.qt_audio import follow_default_audio_output
from krok_helper.subtitle_render.domain.background import (
    background_sequence_frame_path,
)
from krok_helper.subtitle_render.domain.roll import (
    RollLayerAlphas,
    RollSegmentConfig,
    RollTimelinePlan,
    roll_layer_alphas,
)
from krok_helper.subtitle_render.frontend.preview.preview_media import (
    normalize_preview_media_quality,
    prepared_qt_playback_source,
    protect_preview_source,
    qt_playback_source,
    unprotect_preview_source,
)

_PLAYBACK_RESYNC_MS = 500
"""播放中允许的画面/音频时钟偏差；只有大跳变（拖动、卡顿时钟）才重新定位。

逐 tick 按小容差 seek 会在 Qt 位置回报粒度较粗时变成每帧一次 seek，
表现为「一卡一卡」；播放中只做大偏差纠偏，起播/暂停/拖动仍精确 seek。
"""


def paint_roll_fill(
    canvas: QPixmap,
    source: QPixmap,
    fill: str,
    *,
    source_width: int = 0,
    source_height: int = 0,
    logical_size: tuple[int, int] | None = None,
) -> QPixmap:
    """把段素材一帧按填充风格画进画布尺寸的 ``canvas``（原地修改后返回）。

    与导出侧 ``_roll_fill_chain`` 的滤镜几何一一对应；平铺/原始尺寸若带
    ``source_width/height`` 则按**素材像素尺寸**算格子，而不是按低画质代理
    的缩放后尺寸（否则 low/medium 预览与导出的格子数会不同）。
    """

    from krok_helper.subtitle_render.domain.roll import ROLL_TILE_MAX_CELLS

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    width, height = logical_size or (canvas.width(), canvas.height())
    painter.scale(canvas.width() / width, canvas.height() / height)
    sw = int(source_width or 0)
    sh = int(source_height or 0)
    if fill == "stretch":
        painter.drawPixmap(0, 0, width, height, source)
    elif fill == "original" and sw > 0 and sh > 0:
        # 原始尺寸居中：目标尺寸取素材像素尺寸，代理只作为缩放源。
        painter.drawPixmap(
            (width - sw) // 2,
            (height - sh) // 2,
            sw,
            sh,
            source,
        )
    elif fill == "original":
        painter.drawPixmap(
            (width - source.width()) // 2,
            (height - source.height()) // 2,
            source,
        )
    elif fill == "tile" and sw > 0 and sh > 0:
        cols = -(-width // sw)
        rows = -(-height // sh)
        if cols * rows > ROLL_TILE_MAX_CELLS:
            # 与导出同一回退：格子过多时改用 fit，避免 split 爆炸。
            scaled = source.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                (width - scaled.width()) // 2,
                (height - scaled.height()) // 2,
                scaled,
            )
        else:
            for row in range(rows):
                for col in range(cols):
                    painter.drawPixmap(col * sw, row * sh, sw, sh, source)
    else:  # contain: draw directly into the preview-sized surface.
        ratio = min(width / max(source.width(), 1), height / max(source.height(), 1))
        dw, dh = source.width() * ratio, source.height() * ratio
        painter.drawPixmap(QRectF((width-dw)/2, (height-dh)/2, dw, dh),
                           source, QRectF(source.rect()))
    painter.end()
    return canvas


_PREVIEW_FIRST_FRAMES: dict[Path, QImage] = {}


def remember_roll_preview_frame(path: Path | str, image: QImage) -> None:
    """缓存后台读到的首帧；preview 首次绘制优先用它，避免 GUI 再读盘。"""

    if image.isNull():
        return
    _PREVIEW_FIRST_FRAMES[Path(path)] = image
    while len(_PREVIEW_FIRST_FRAMES) > 8:
        _PREVIEW_FIRST_FRAMES.pop(next(iter(_PREVIEW_FIRST_FRAMES)))


def _prepared_first_frame(path: Path) -> QImage | None:
    return _PREVIEW_FIRST_FRAMES.get(path)


def roll_frame_source_is_animated(path: Path) -> bool:
    """一图流素材是否为动图（GIF/WebP 多帧）。"""

    from PyQt6.QtGui import QImageReader

    if path.suffix.lower() not in {".gif", ".webp"}:
        return False
    reader = QImageReader(str(path))
    return reader.supportsAnimation() and reader.imageCount() > 1


class _RollProxyPreparer(QObject):
    """后台准备与普通背景同口径的预览源；完成信号回到 GUI 线程后再换源。

    纯 Python daemon 线程 + Qt queued signal：不创建 QThread，窗口关闭时
    不需要等待封装/缩放结束；结果回来时段/播放器已被释放则直接忽略。
    """

    ready = Signal(object, object)  # (segment, prepared path | None)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    def prepare(
        self,
        segment: "RollSegmentRuntime",
        path: Path,
        quality: str,
    ) -> None:
        def work() -> None:
            try:
                prepared = qt_playback_source(path, quality)
            except Exception:  # noqa: BLE001 - 后台准备失败回退源文件
                prepared = None
            try:
                self.ready.emit(segment, prepared)
            except RuntimeError:
                pass  # QObject 已随窗口销毁

        threading.Thread(
            target=work,
            name="roll-preview-prepare",
            daemon=True,
        ).start()


@dataclass
class RollSegmentRuntime:
    """一段（前黑/后黑）的帧/音频运行态（无 UI）。"""

    config: RollSegmentConfig
    start_ms: int
    end_ms: int
    video_player: Optional[QMediaPlayer] = None
    video_sink: Optional[QVideoSink] = None
    video_audio_out: Optional[QAudioOutput] = None
    video_frame: Optional[QImage] = None
    video_playback_path: Optional[Path] = None
    """普通背景同口径的预览源（复制流封装/缩小代理；失败时回退源路径）。"""
    proxy_pending: bool = False
    priming_frame: bool = False
    movie: Optional[object] = None
    movie_pixmap: Optional[QPixmap] = None
    movie_frame_ends: list[int] = field(default_factory=list)
    sequence_frames: list[Path] = field(default_factory=list)
    audio_player: Optional[QMediaPlayer] = None
    audio_output: Optional[QAudioOutput] = None
    frame_cache: dict[Path, QPixmap] = field(default_factory=dict)
    last_canvas_key: object = None
    canvas_cache: Optional[QPixmap] = None

    @property
    def source_kind(self) -> str:
        return self.config.source.kind

    @property
    def audio_media_path(self) -> Optional[Path]:
        normalized = self.config
        if normalized.audio_path:
            return Path(normalized.audio_path)
        if (
            normalized.source.kind == "video"
            and normalized.source_has_audio
            and normalized.source.path
        ):
            return Path(normalized.source.path)
        return None


class RollSegmentFrameSource(QObject):
    """前黑/后黑两段的帧采样、填充变换与音频播放（无 UI，两条预览共用）。

    graphics 预览把 ``layer_pixmap`` 贴到场景图层；raster 回退预览在
    paintEvent 里直接绘制 ``layer_pixmap``。段视频帧由 QVideoSink 解码回调
    更新；动图由项目时间定位 QMovie 帧，不使用独立动画时钟。
    """

    frame_changed = Signal()
    """视频/动图解码出新帧（展示层应请求重绘）。"""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._plan: Optional[RollTimelinePlan] = None
        self._segments: list[RollSegmentRuntime] = []
        self._output_w = 1920
        self._output_h = 1080
        self._preview_quality: str = "high"
        self._render_scale = 1.0
        self._t_ms = 0
        self._playing = False
        self._volume = 1.0
        self._proxy_preparer = _RollProxyPreparer(self)
        self._proxy_preparer.ready.connect(self._on_proxy_ready)

    # ------------------------------------------------------------------ 状态

    @property
    def plan(self) -> Optional[RollTimelinePlan]:
        return self._plan

    @property
    def preview_quality(self) -> str:
        return self._preview_quality

    def set_preview_quality(self, quality: object = "high") -> None:
        """切换预览画质；项目帧率变化不再触发段视频重建/转码。"""

        normalized_quality = normalize_preview_media_quality(quality)
        if normalized_quality == self._preview_quality:
            return
        self._preview_quality = normalized_quality
        if self._plan is not None and self._plan.active:
            old_segments = self._segments
            self._segments = []
            for segment in old_segments:
                self._release_segment(segment)
            plan = self._plan
            t_ms, playing = self._t_ms, self._playing
            self.set_plan(plan)
            self.set_time(t_ms, playing, force=True)

    @property
    def pre(self) -> Optional[RollSegmentRuntime]:
        return self._segments[0] if self._plan and self._plan.has_pre and self._segments else None

    @property
    def post(self) -> Optional[RollSegmentRuntime]:
        return self._segments[-1] if self._plan and self._plan.has_post and self._segments else None

    def set_output_size(self, width: int, height: int) -> None:
        self._output_w = max(int(width), 1)
        self._output_h = max(int(height), 1)
        for segment in self._segments:
            segment.last_canvas_key = None

    def set_render_scale(self, scale: float) -> None:
        scale = max(0.01, min(float(scale), 1.0))
        if abs(scale - self._render_scale) < 0.001:
            return
        self._render_scale = scale
        for segment in self._segments:
            segment.last_canvas_key = None
        self.frame_changed.emit()

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        for segment in self._segments:
            if segment.audio_output is not None:
                segment.audio_output.setVolume(self._volume)

    def set_plan(self, plan: Optional[RollTimelinePlan]) -> None:
        old_segments = list(self._segments)
        self._plan = plan
        if plan is None or not plan.active:
            for segment in old_segments:
                self._release_segment(segment)
            self._segments = []
            return

        reused: set[int] = set()
        segments: list[RollSegmentRuntime] = []
        slots: list[tuple[RollSegmentConfig, int, int]] = []
        if plan.pre_ms > 0:
            slots.append((plan.pre, 0, plan.pre_ms))
        if plan.post_ms > 0:
            slots.append(
                (
                    plan.post,
                    plan.main_end_ms,
                    plan.main_end_ms + plan.post_ms,
                )
            )
        for config, start_ms, end_ms in slots:
            segment = self._reuse_segment(old_segments, reused, config, start_ms, end_ms)
            if segment is None:
                segment = self._build_segment(config, start_ms, end_ms)
            segments.append(segment)
        for index, segment in enumerate(old_segments):
            if index not in reused:
                self._release_segment(segment)
        self._segments = segments
        self.set_time(self._t_ms, self._playing, force=True)

    def _reuse_segment(
        self,
        old_segments: list[RollSegmentRuntime],
        reused: set[int],
        config: RollSegmentConfig,
        start_ms: int,
        end_ms: int,
    ) -> Optional[RollSegmentRuntime]:
        """同一素材只更新时长/填充/转场参数，不重建播放器或准备素材。"""

        normalized = config.normalized()
        for index, old in enumerate(old_segments):
            if index in reused:
                continue
            old_config = old.config
            if old_config.source != normalized.source:
                continue
            if old_config.audio_path != normalized.audio_path:
                continue
            if old_config.source_has_audio != normalized.source_has_audio:
                continue
            old.config = normalized
            old.start_ms = start_ms
            old.end_ms = end_ms
            old.frame_cache.clear()
            old.canvas_cache = None
            old.last_canvas_key = None
            reused.add(index)
            return old
        return None

    def release(self) -> None:
        for segment in self._segments:
            self._release_segment(segment)
        self._segments = []
        self._plan = None

    def shutdown(self) -> None:
        self.release()

    def _build_segment(
        self, config: RollSegmentConfig, start_ms: int, end_ms: int
    ) -> RollSegmentRuntime:
        segment = RollSegmentRuntime(
            config=config.normalized(), start_ms=start_ms, end_ms=end_ms
        )
        source = segment.config.source
        if source.kind == "video" and source.path:
            source_path = Path(str(source.path))
            protect_preview_source(source_path)
            playback_path = prepared_qt_playback_source(
                source_path,
                self._preview_quality,
            )
            if playback_path is None:
                # 播放器只接收已准备好的路径；缓存缺失时回到后台准备，
                # 绝不在 GUI 线程同步跑 FFmpeg。
                segment.proxy_pending = True
                self._proxy_preparer.prepare(
                    segment,
                    source_path,
                    self._preview_quality,
                )
            else:
                segment.video_playback_path = playback_path
                self._ensure_video_media(segment, playback_path)
        elif source.kind == "image" and source.path:
            path = Path(str(source.path))
            if roll_frame_source_is_animated(path):
                from PyQt6.QtGui import QMovie

                movie = QMovie(str(path), parent=self)
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                # Index frame delays once; playback and seeking both use project time.
                from PyQt6.QtGui import QImageReader
                reader = QImageReader(str(path))
                elapsed = 0
                while reader.canRead():
                    if reader.read().isNull():
                        break
                    elapsed += max(reader.nextImageDelay(), 1)
                    segment.movie_frame_ends.append(elapsed)
                movie.frameChanged.connect(
                    lambda _frame, seg=segment: self._on_movie_frame(seg)
                )
                segment.movie = movie
        if source.kind == "image_sequence" and source.path:
            import re
            match = re.search(r"%0?(\d*)d", source.path)
            if match:
                index = source.sequence_start_number
                while True:
                    number = str(index).zfill(int(match.group(1) or 0))
                    path = Path(source.path[:match.start()] + number + source.path[match.end():])
                    if not path.is_file():
                        break
                    segment.sequence_frames.append(path)
                    index += 1
        if source.kind != "video":
            audio_path = segment.audio_media_path
            if audio_path is not None and audio_path.is_file():
                audio_player = QMediaPlayer(self)
                audio_output = QAudioOutput(self)
                follow_default_audio_output(audio_output)
                audio_output.setVolume(self._volume)
                audio_player.setAudioOutput(audio_output)
                audio_player.setSource(QUrl.fromLocalFile(str(audio_path)))
                segment.audio_player = audio_player
                segment.audio_output = audio_output
        return segment

    def _ensure_video_media(
        self, segment: RollSegmentRuntime, playback_path: Path
    ) -> None:
        """用已准备好的路径创建播放器/内嵌音频；后台未完成前保持不建。"""

        if segment.video_player is not None:
            return
        segment.video_playback_path = Path(playback_path)
        player = QMediaPlayer(self)
        audio_out = QAudioOutput(self)
        follow_default_audio_output(audio_out)
        audio_out.setVolume(0.0)  # 画面静音：段音频统一走独立播放器
        player.setAudioOutput(audio_out)
        sink = QVideoSink(self)
        player.setVideoSink(sink)
        sink.videoFrameChanged.connect(
            lambda frame, seg=segment: self._on_video_frame(seg, frame)
        )
        segment.video_player = player
        segment.video_sink = sink
        segment.video_audio_out = audio_out
        player.mediaStatusChanged.connect(lambda status, seg=segment: self._on_video_status(seg, status))
        player.setSource(QUrl.fromLocalFile(str(playback_path)))

        audio_path = segment.audio_media_path
        if audio_path is None:
            return
        if not segment.config.audio_path:
            # One player decodes the video's picture and embedded audio together.
            segment.audio_player = player
            segment.audio_output = audio_out
            audio_out.setVolume(self._volume)
            return
        if not audio_path.is_file():
            return
        audio_player = QMediaPlayer(self)
        audio_output = QAudioOutput(self)
        follow_default_audio_output(audio_output)
        audio_output.setVolume(self._volume)
        audio_player.setAudioOutput(audio_output)
        audio_player.setSource(QUrl.fromLocalFile(str(audio_path)))
        segment.audio_player = audio_player
        segment.audio_output = audio_output

    def _on_video_status(self, segment: RollSegmentRuntime, status) -> None:
        if status != QMediaPlayer.MediaStatus.LoadedMedia or segment.video_frame is not None:
            return
        player = segment.video_player
        if player is None or player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        # Qt does not emit a first frame for an idle player at position zero.
        # Prime silently, then pause on the first decoded frame.
        segment.priming_frame = True
        segment.video_audio_out.setMuted(True)
        player.setPosition(max(self._t_ms - segment.start_ms, 0))
        player.play()

    def _on_proxy_ready(
        self, segment: RollSegmentRuntime, prepared: object
    ) -> None:
        """后台预览源完成：会话仍有效时切换并恢复当前位置。"""

        if segment not in self._segments:
            return
        segment.proxy_pending = False
        if prepared is None:
            return
        path = Path(prepared)
        if not path.is_file():
            return
        if segment.video_player is None:
            self._ensure_video_media(segment, path)
            self.set_time(self._t_ms, self._playing, force=True)
            return
        if path == segment.video_playback_path:
            return
        segment.video_playback_path = path
        segment.video_player.setSource(QUrl.fromLocalFile(str(path)))
        if (
            segment.audio_player is not None
            and not segment.config.audio_path
        ):
            # 视频内嵌音频与画面共用同一份预览源。
            segment.audio_player.setSource(QUrl.fromLocalFile(str(path)))
        self.set_time(self._t_ms, self._playing, force=True)

    def _release_segment(self, segment: RollSegmentRuntime) -> None:
        if (
            segment.config.source.kind == "video"
            and segment.config.source.path
        ):
            unprotect_preview_source(segment.config.source.path)
        for player in dict.fromkeys((segment.video_player, segment.audio_player)):
            if player is not None:
                # 不用 stop()：Qt 6.11 FFmpeg 后端在 pause/seek 后 stop 可能
                # 阻塞主线程（实机 Offscreen 探针复现）。清空 source 同样停播，
                # 且不触发该后端路径。
                if (
                    player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState
                ):
                    player.pause()
                player.setSource(QUrl())
        if segment.movie is not None:
            segment.movie.stop()
            segment.movie.setFileName("")
        for obj in dict.fromkeys((segment.video_player, segment.video_sink, segment.video_audio_out,
                    segment.audio_player, segment.audio_output, segment.movie)):
            if obj is not None:
                obj.deleteLater()
        segment.video_player = None
        segment.video_sink = None
        segment.video_audio_out = None
        segment.audio_player = None
        segment.audio_output = None
        segment.movie = None
        segment.video_frame = None
        segment.movie_pixmap = None
        segment.frame_cache.clear()
        segment.canvas_cache = None

    # ------------------------------------------------------------------ 解码回调

    def _on_video_frame(self, segment: RollSegmentRuntime, frame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        segment.video_frame = image
        if segment.priming_frame:
            segment.priming_frame = False
            active = self._playing and segment.start_ms <= self._t_ms < segment.end_ms
            if not active and segment.video_player is not None:
                segment.video_player.pause()
            segment.video_audio_out.setMuted(not active)
        segment.last_canvas_key = None
        self.frame_changed.emit()

    def _on_movie_frame(self, segment: RollSegmentRuntime) -> None:
        movie = segment.movie
        if movie is None:
            return
        pixmap = movie.currentPixmap()
        if pixmap.isNull():
            return
        segment.movie_pixmap = pixmap
        segment.last_canvas_key = None
        self.frame_changed.emit()

    # ------------------------------------------------------------------ 帧采样

    def _segment_source_pixmap(
        self, segment: RollSegmentRuntime
    ) -> tuple[object, QPixmap]:
        """返回 (缓存键, 段素材当前帧 pixmap)；无内容返回 (None, QPixmap())。"""

        source = segment.config.source
        if segment.video_frame is not None:
            return ("video", segment.video_frame.cacheKey()), QPixmap.fromImage(
                segment.video_frame
            )
        if segment.movie_pixmap is not None:
            return ("movie", segment.movie_pixmap.cacheKey()), segment.movie_pixmap
        if source.kind == "image_sequence":
            path = background_sequence_frame_path(
                source, max(self._t_ms - segment.start_ms, 0)
            )
            if segment.sequence_frames:
                index = max(self._t_ms - segment.start_ms, 0) * max(int(source.source_fps or 60), 1) // 1000
                path = segment.sequence_frames[index % len(segment.sequence_frames)]
            if path is None:
                return None, QPixmap()
            cached = segment.frame_cache.get(path)
            if cached is not None:
                return path, cached
            prepared = _prepared_first_frame(path)
            if prepared is not None:
                pixmap = QPixmap.fromImage(prepared)
            else:
                pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return None, QPixmap()
            segment.frame_cache[path] = pixmap
            while len(segment.frame_cache) > 8:
                segment.frame_cache.pop(next(iter(segment.frame_cache)))
            return path, pixmap
        if source.kind == "image" and source.path:
            path = Path(str(source.path))
            cached = segment.frame_cache.get(path)
            if cached is not None:
                return path, cached
            prepared = _prepared_first_frame(path)
            if prepared is not None:
                pixmap = QPixmap.fromImage(prepared)
            else:
                pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return None, QPixmap()
            segment.frame_cache[path] = pixmap
            return path, pixmap
        return None, QPixmap()

    def layer_pixmap(self, segment: RollSegmentRuntime) -> Optional[QPixmap]:
        """段素材当前帧按填充风格变换到画布尺寸（solid 段返回 None）。"""

        if segment.source_kind == "solid":
            return None
        key, source_pixmap = self._segment_source_pixmap(segment)
        if key is None:
            return None
        canvas_key = (key, segment.config.fill, self._output_w, self._output_h, self._render_scale)
        if segment.last_canvas_key == canvas_key and segment.canvas_cache is not None:
            return segment.canvas_cache
        canvas = QPixmap(max(1, round(self._output_w*self._render_scale)), max(1, round(self._output_h*self._render_scale)))
        canvas.fill(Qt.GlobalColor.black)
        paint_roll_fill(
            canvas,
            source_pixmap,
            segment.config.fill,
            source_width=segment.config.source_width,
            source_height=segment.config.source_height,
            logical_size=(self._output_w, self._output_h),
        )
        segment.last_canvas_key = canvas_key
        segment.canvas_cache = canvas
        return canvas

    def solid_color(self, segment: RollSegmentRuntime) -> QColor:
        color = QColor(segment.config.source.color)
        return color if color.isValid() else QColor(0, 0, 0)

    def alphas_at(self, t_ms: int) -> RollLayerAlphas:
        if self._plan is None:
            return RollLayerAlphas(main_alpha=1.0)
        return roll_layer_alphas(self._plan, t_ms)

    # ------------------------------------------------------------------ 时间驱动

    def set_time(self, t_ms: int, playing: bool, *, force: bool = False) -> None:
        if not force and t_ms == self._t_ms and playing == self._playing:
            return
        self._t_ms = int(t_ms)
        self._playing = bool(playing)
        if self._plan is None or not self._segments:
            return
        alphas = self.alphas_at(self._t_ms)
        for segment, alpha in ((self.pre, alphas.pre_alpha), (self.post, alphas.post_alpha)):
            if segment is not None:
                self._update_segment(segment, alpha)

    def set_playing(self, playing: bool) -> None:
        self.set_time(self._t_ms, playing, force=True)

    def _update_segment(self, segment: RollSegmentRuntime, alpha: float) -> None:
        segment_t = self._t_ms - segment.start_ms
        in_window = segment.start_ms <= self._t_ms < segment.end_ms
        # 段外不要让播放器光标越过后黑/前黑素材长度（暂停光标也停在段内）。
        local_ms = max(segment_t, 0)
        if not in_window:
            local_ms = min(local_ms, max(segment.end_ms - segment.start_ms - 1, 0))
        active = self._playing and in_window
        if segment.video_player is not None and not segment.priming_frame:
            segment.video_audio_out.setMuted(not active)
            self._sync_player(segment.video_player, local_ms, active)
        if segment.movie is not None and segment.movie_frame_ends:
            ends = segment.movie_frame_ends
            frame = bisect_right(ends, local_ms % ends[-1])
            segment.movie.jumpToFrame(frame)
        if segment.audio_player is not None and segment.audio_player is not segment.video_player:
            self._sync_player(segment.audio_player, local_ms, active)

    def _sync_player(
        self, player: QMediaPlayer, position_ms: int, play: bool
    ) -> None:
        target = max(int(position_ms), 0)
        duration = int(getattr(player, "duration", lambda: 0)())
        if duration > 0 and target >= duration:
            target = max(duration - 1, 0)
            play = False
        if play:
            if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                # 起播必须对齐到目标时刻（拖动/暂停后恢复）。
                if abs(int(player.position()) - target) > 1:
                    player.setPosition(target)
                player.play()
            elif abs(int(player.position()) - target) > _PLAYBACK_RESYNC_MS:
                # 播放中只处理大跳变，避免 Qt 位置回报粒度导致的逐帧 seek。
                player.setPosition(target)
        else:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                player.pause()
            if int(player.position()) != target:
                player.setPosition(target)

    # ------------------------------------------------------------------ 时钟锚定

    def audio_display_position(self, t_ms: int) -> Optional[int]:
        """传输条跨段锚定源：display 时刻所在段的音频媒体位置（display 坐标）。

        前黑/后黑区且该段配了音频并正在播 → 返回
        ``段起点 + 播放器位置``；其余情况 None（调用方回退墙钟/主媒体锚定）。
        """

        if self._plan is None or not self._playing:
            return None
        for segment in self._segments:
            if not (segment.start_ms <= t_ms < segment.end_ms):
                continue
            player = segment.audio_player
            if (
                player is None
                or player.playbackState()
                != QMediaPlayer.PlaybackState.PlayingState
            ):
                return None
            return segment.start_ms + int(player.position())
        return None


class RollPreviewController(QObject):
    """graphics 预览的段展示层：场景图层 + 白闪层，内容来自帧源。"""

    def __init__(self, scene: QGraphicsScene, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.source = RollSegmentFrameSource(self)
        self.source.frame_changed.connect(self._refresh_items)
        self._scene = scene
        self._plan: Optional[RollTimelinePlan] = None
        self._pre_item = QGraphicsPixmapItem()
        self._pre_item.setZValue(0)
        self._pre_item.setVisible(False)
        scene.addItem(self._pre_item)
        self._post_item = QGraphicsPixmapItem()
        self._post_item.setZValue(2)
        self._post_item.setVisible(False)
        scene.addItem(self._post_item)
        self._pre_rect = QGraphicsRectItem()
        self._pre_rect.setPen(QPen(Qt.PenStyle.NoPen))
        self._pre_rect.setZValue(0)
        self._pre_rect.setVisible(False)
        scene.addItem(self._pre_rect)
        self._post_rect = QGraphicsRectItem()
        self._post_rect.setPen(QPen(Qt.PenStyle.NoPen))
        self._post_rect.setZValue(2)
        self._post_rect.setVisible(False)
        scene.addItem(self._post_rect)
        self._flash_rect = QGraphicsRectItem()
        self._flash_rect.setPen(QPen(Qt.PenStyle.NoPen))
        self._flash_rect.setBrush(QColor(255, 255, 255))
        self._flash_rect.setZValue(3)
        self._flash_rect.setVisible(False)
        scene.addItem(self._flash_rect)
        self._output_w = 1920
        self._output_h = 1080
        self._t_ms = 0
        self._playing = False
        # 初始化纯色/白闪矩形：默认输出尺寸下宿主可能不会再次调用
        # set_output_size（早退），不在这里初始化会留下空 QRectF，纯色段和
        # 白闪只剩一个点（用户看到的“一开始非常小”）。
        self.set_output_size(self._output_w, self._output_h)

    # ------------------------------------------------------------------ 委托

    @property
    def plan(self) -> Optional[RollTimelinePlan]:
        return self._plan

    def set_output_size(self, width: int, height: int) -> None:
        self._output_w = max(int(width), 1)
        self._output_h = max(int(height), 1)
        for rect in (self._pre_rect, self._post_rect, self._flash_rect):
            rect.setRect(QRectF(0, 0, self._output_w, self._output_h))
        self.source.set_output_size(width, height)
        self.set_time(self._t_ms, self._playing, force=True)

    def set_volume(self, volume: float) -> None:
        self.source.set_volume(volume)

    def set_preview_quality(self, quality: object = "high") -> None:
        self.source.set_preview_quality(quality)
        self.set_time(self._t_ms, self._playing, force=True)

    def set_plan(self, plan: Optional[RollTimelinePlan]) -> None:
        # 保证纯色/白闪矩形的尺寸与当前画布一致（默认 1920 时宿主可能早退）。
        self.set_output_size(self._output_w, self._output_h)
        self._plan = plan
        self.source.set_plan(plan)
        for item in (
            self._pre_item,
            self._post_item,
            self._pre_rect,
            self._post_rect,
            self._flash_rect,
        ):
            item.setVisible(False)
        self.set_time(self._t_ms, self._playing, force=True)

    def clear(self) -> None:
        self._plan = None
        self.source.release()
        for item in (
            self._pre_item,
            self._post_item,
            self._pre_rect,
            self._post_rect,
            self._flash_rect,
        ):
            item.setVisible(False)

    def shutdown(self) -> None:
        self.clear()

    def alphas_at(self, t_ms: int) -> RollLayerAlphas:
        return self.source.alphas_at(t_ms)

    def audio_display_position(self, t_ms: int) -> Optional[int]:
        return self.source.audio_display_position(t_ms)

    # ------------------------------------------------------------------ 时间驱动

    def set_time(self, t_ms: int, playing: bool, *, force: bool = False) -> None:
        if not force and t_ms == self._t_ms and playing == self._playing:
            return
        self._t_ms = int(t_ms)
        self._playing = bool(playing)
        self.source.set_time(t_ms, playing, force=force)
        if self._plan is None or not self._plan.active:
            return
        alphas = self.source.alphas_at(self._t_ms)
        self._apply_layer(self.source.pre, alphas.pre_alpha, is_pre=True)
        self._apply_layer(self.source.post, alphas.post_alpha, is_pre=False)
        self._flash_rect.setRect(QRectF(0, 0, self._output_w, self._output_h))
        self._flash_rect.setOpacity(max(0.0, min(1.0, alphas.flash_alpha)))
        self._flash_rect.setVisible(alphas.flash_alpha > 0.001)

    def set_playing(self, playing: bool) -> None:
        self.set_time(self._t_ms, playing, force=True)

    def _apply_layer(
        self, segment: Optional[RollSegmentRuntime], alpha: float, *, is_pre: bool
    ) -> None:
        item = self._pre_item if is_pre else self._post_item
        rect = self._pre_rect if is_pre else self._post_rect
        if segment is None:
            item.setVisible(False)
            rect.setVisible(False)
            return
        if segment.source_kind == "solid":
            item.setVisible(False)
            rect.setBrush(self.source.solid_color(segment))
            rect.setOpacity(max(0.0, min(1.0, alpha)))
            rect.setVisible(alpha > 0.001)
            return
        rect.setVisible(False)
        visible = alpha > 0.001
        item.setOpacity(max(0.0, min(1.0, alpha)))
        item.setVisible(visible)
        if visible:
            pixmap = self.source.layer_pixmap(segment)
            if pixmap is not None:
                item.setPixmap(pixmap)
                item.setTransform(QTransform.fromScale(self._output_w / pixmap.width(), self._output_h / pixmap.height()))
                item.setPos(0, 0)

    def _refresh_items(self) -> None:
        """解码新帧到达：刷新当前可见的段图层。"""
        if self._plan is None or not self._plan.active:
            return
        alphas = self.source.alphas_at(self._t_ms)
        for segment, alpha, is_pre in (
            (self.source.pre, alphas.pre_alpha, True),
            (self.source.post, alphas.post_alpha, False),
        ):
            if segment is None or alpha <= 0.001 or segment.source_kind == "solid":
                continue
            pixmap = self.source.layer_pixmap(segment)
            item = self._pre_item if is_pre else self._post_item
            if pixmap is not None:
                item.setPixmap(pixmap)
                item.setTransform(QTransform.fromScale(self._output_w / pixmap.width(), self._output_h / pixmap.height()))
                item.setPos(0, 0)
