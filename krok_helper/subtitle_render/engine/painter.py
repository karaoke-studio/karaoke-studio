"""单帧 QPainter 绘制（A4 阶段）。

入口 :func:`paint_frame` 把一行已唱 / 未唱字符渲染到给定 ``QImage`` 上；
预览路径可用 :func:`paint_frame_to_painter` 直接画到已有 ``QPainter``，避免每帧
额外分配整张离屏图。

绘制顺序（自底向上）：

1. **阴影**：整行文本按 ``shadow_offset_*`` 偏移绘一份阴影色
2. **描边**：用 ``QPainterPath.addText`` 取字形轮廓，``strokePath`` 描宽线
3. **底色**：整行字符（``base_color``）
4. **Ruby 注音**：按 ``@Ruby`` 时间区间映射到主歌词字符范围，画在主行上方
5. **填充层**：同样字符以 ``fill_color`` 重绘，但用 ``setClipRect`` 把每个字符
   裁切到"已唱比例"（左→右扫光）

预览路径与渲染路径**共用本函数**——预览给到的 image 是缩放后的 QImage、
渲染管线给的是 1080p QImage，绘制逻辑一致。

**性能优化**：1~3 步（阴影 + 描边 + 底色）每帧的内容 *完全不依赖* ``t_ms``，
只随 line text + font + style 变化。横排文本会按连续同 style 的 glyph run
烘焙成透明 QImage 缓存，绘制时一次 ``drawImage`` blit；每帧只重画 5 步的逐字 clip。1080p 双行场景下，单帧
``paintEvent`` 工作量从 ~2× ``QPainterPath.addText + strokePath`` 降到一次
位图 blit，CPU 时间降幅 3~5×。缓存按 line/font/style 哈希索引，LRU 退役，
样式实时改动会自动 invalidate。

P1 阶段会在本函数基础上加：渐变填充（B3）、入场退场动画（B4）、
多歌手分色（B2）。
"""

from __future__ import annotations

import math
import os
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from threading import Lock, local as thread_local
from typing import Hashable, Optional

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from krok_helper.subtitle_render.engine.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerCompositor,
    LayerContext,
    SCOPE_GROUP,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.guide_symbols import scaled_guide_symbol_path
from krok_helper.subtitle_render.n3_font_catalog import resolve_qt_font_family


_IMAGE_FILL_CACHE_MAX = 16
_IMAGE_FILL_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_IMAGE_BRUSH_CACHE: "OrderedDict[tuple, QBrush]" = OrderedDict()
_BITMAP_GUIDE_CACHE_MAX = 64
_BITMAP_GUIDE_CACHE: "OrderedDict[tuple[str, int, int], QImage]" = OrderedDict()
_HARD_BAND_BRUSH_CACHE_MAX = 128
_HARD_BAND_BRUSH_CACHE: "OrderedDict[tuple, QBrush]" = OrderedDict()
_IMAGE_FILL_LOCK = Lock()
# 横排 glyph run 层缓存：普通行与分色行都按连续同 style 的 run 烘焙。
# 每个 run 的「未唱」层（含 before-glow）、「已唱」主体层与 after-glow
# 各烘焙一次；逐帧只按扫光半平面 clip blit。
_TEXT_RUN_LAYER_CACHE = LayerCache(max_items=128)
_TEXT_RUN_COMPOSITOR = LayerCompositor(_TEXT_RUN_LAYER_CACHE)
# A3（§9.7）：utopia transition 路径每帧重算 glow 高斯（实测 18ms 主因）。把 glow 按
# **上正 glyph 身份**烘焙一次进此缓存（before/after 各一条），逐帧在 utopia 变换下 blit。
# glow 是软晕、对 bitmap-transform 不敏感 → 复用无明显软化；body 仍逐帧矢量保持锐利（B 档再缓存）。
_RUN_GLOW_CACHE = LayerCache(max_items=128)
# 行级布局缓存：_LineLayout（纯几何 + 字体资源）与 t_ms 无关，但此前每帧重算
# （full 场景约 30% paint 时间）。key = (整 track 值签名, display_style 值签名,
# 行索引, 画布尺寸, baseline/line_x/lane)——签名每帧从当前值重建（models 是可变
# dataclass、前端不调失效接口），track/style 就地改动下一帧自然 miss，不会取脏值。
# 行索引而非行内容进 key：SmartHorizon 的页定位用 `item is line` 身份判断，
# 值相同的两行也可能落在不同页。
# 一次排版会把每行的布局问上三遍，但三遍是分批扫全轨的：48 项装不下一条曲目，
# 等第二遍回到第一行时它早被挤掉了，长曲目命中率直接归零。按整轨都能留住来定容量。
_LINE_LAYOUT_CACHE = LayerCache(max_items=2048)
_DISPLAY_LINES_CACHE_MAX = 24
_DISPLAY_LINES_CACHE: (
    "OrderedDict[tuple, tuple[TimingTrack, tuple[DisplayLine, ...]]]"
) = OrderedDict()
_LAYOUT_PASS = thread_local()
# Scratch buffers for N3-style opacity layers; see _paint_through_opacity_layer.
_OPACITY_LAYER_LOCAL = thread_local()
_PAGE_PLACEMENT_CACHE_MAX = 24
_PAGE_PLACEMENT_CACHE: "OrderedDict[tuple, dict[int, tuple[tuple[int, int, float, float], ...]]]" = (
    OrderedDict()
)


def _layout_cache_enabled() -> bool:
    """行级布局缓存（默认开）。``KROK_SUBTITLE_LAYOUT_CACHE=0`` 退回逐帧重算
    （A/B 验收 / 紧急回退用）。"""
    return os.environ.get("KROK_SUBTITLE_LAYOUT_CACHE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _glow_cache_enabled() -> bool:
    """A3：utopia 路径复用 glow 烘焙缓存（默认开）。``KROK_SUBTITLE_GLOW_CACHE=0`` 退回
    逐帧 ``_paint_glow_path``（A/B 验收 / 紧急回退用）。"""
    return os.environ.get("KROK_SUBTITLE_GLOW_CACHE", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _vertical_layer_enabled() -> bool:
    """竖排（縦書き）整条路径（主文本 + ruby）走 LayerCompositor + bake 缓存。

    默认开启：与横排一致地把 before/after/ruby 烘焙成位图缓存，逐帧只 blit + clip，
    省掉每帧重光栅化。``KROK_SUBTITLE_VERTICAL_LAYER=0`` 回退到旧的逐帧直绘路径
    （亦作像素一致性 A/B oracle）。
    """
    return os.environ.get("KROK_SUBTITLE_VERTICAL_LAYER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _horizontal_layer_enabled() -> bool:
    """横排主文本走 LayerCompositor + bake 缓存。

    默认开启；``KROK_SUBTITLE_HORIZONTAL_LAYER=0`` 保留同 layout 的矢量直绘
    oracle，供 direct-vs-bake 像素回归使用。
    """
    return os.environ.get("KROK_SUBTITLE_HORIZONTAL_LAYER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


_RUBY_COMBINING_CHARS = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ\u3099\u309A")


@dataclass(frozen=True)
class _FillSegment:
    left: int
    right: int
    start_ms: int = 0
    end_ms: int = 0
    ruby: RubyAnnotation | None = None
    indices: tuple[int, ...] = ()
    release_left: float | None = None
    release_right: float | None = None
    layout_left: int | None = None
    layout_right: int | None = None
    ruby_base_index: int | None = None
    ruby_base_count: int = 1


@dataclass(frozen=True)
class _LineCharTransition:
    phase: str
    effect: str
    progress: float
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class _SignalLitGroup:
    x: float
    y: float
    elapsed_ms: int
    duration_ms: int
    active_index: int | None
    opacity: float = 1.0
    active_opacity: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    phase: float = 0.0


@dataclass(frozen=True)
class _SignalLayoutMetrics:
    count: int
    size: int
    item_width: int
    tracking: int
    stroke_extent: float
    group_width: float
    is_volume: bool


@dataclass(frozen=True)
class _VolumeSignalGeometry:
    count: int
    size: int
    column_width: int
    column_spacing: int
    spacing: int
    stroke_extent: float
    local_left: float
    group_width: float
    pitch: float
    front_height: float
    height_delta: float
    align_base_shift: float
    align_delta_shift: float


@dataclass(frozen=True)
class _SayatooLineLayout:
    baseline_y: int
    text_x: int
    line_style: Style
    metrics: QFontMetrics
    total_w: int
    signal_x: float | None = None
    signal_y: float | None = None


@dataclass(frozen=True)
class _GlyphLayout:
    index: int
    text: str
    role_label: str | None
    style: Style
    font: QFont
    metrics: QFontMetrics
    left: int
    width: int
    path_offset_x: float = 0.0
    # N3's SetMultiColorAreas selects the first character's Japanese/main font
    # slot even when that glyph itself is Latin.  Keep the pre-script style for
    # the gradient inset while ``style`` retains the actual glyph stroke.
    brush_style: Style | None = None
    vector_glyph: object | None = None


@dataclass(frozen=True)
class _TextLayout:
    glyphs: list[_GlyphLayout]
    total_width: int
    ascent: int
    descent: int
    height: int
    line_rect: QRectF


@dataclass(frozen=True)
class _LineLayout:
    """横排歌词行的纯几何布局（**不依赖 t_ms**）+ 渲染所需字体资源。

    P1.a 三段式（layout→animation→paint）的 layout 段产物：字符几何 / 基线 /
    fill_segments（含时序但不含当前进度）都与帧无关、可缓存。普通行与分色行都
    表达为同一个 glyph-list 模型：普通行只是所有 glyph 使用同一 style 的特例。
    """
    text_layout: _TextLayout
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    font_for: object  # Callable[[str], QFont] | None
    active_rubies: list
    ruby_font: QFont
    ruby_metrics: QFontMetrics | None
    char_widths: list[int]
    total_w: int
    x0: int
    baseline_y: int
    intervals: list
    char_lefts: list[int]
    char_x_ranges: list
    fill_segments: list
    line_rect: QRectF
    colors: KaraokeColors
    rtl: bool
    has_inline_styles: bool
    # 各字符墨水边界（绝对坐标）；走字按墨水推进，不含 advance 两侧空白。
    # 仅用于扫光 ratio/分段计算，绘制定位仍用 advance 的 char_x_ranges。
    ink_x_ranges: list = field(default_factory=list)
    # Ruby geometry is frame-independent.  Keep it with the line layout so the
    # per-character wipe paths are not measured again for every preview frame.
    ruby_layouts: tuple["_RubyLayout", ...] = ()
    render_line: TimingLine | None = None


@dataclass(frozen=True)
class _VerticalLineLayout:
    """竖排行的纯几何布局（不依赖 t_ms）。"""

    font: QFont
    metrics: QFontMetrics
    cell_w: int
    cell_h: int
    ascent: int
    column_x: int
    y_top: int
    block_h: int
    intervals: list[tuple[int, int]]
    cells: list[tuple[int, int]]
    line_rect: QRectF
    text_path: QPainterPath
    colors: KaraokeColors
    active_rubies: list[RubyAnnotation]


@dataclass(frozen=True)
class _RubyWipeSegment:
    """One timed ruby glyph sweep on the horizontal visual axis."""

    start_ms: int
    end_ms: int
    axis_start: float
    axis_end: float


@dataclass(frozen=True)
class _RubyLayout:
    """横排 ruby 的纯几何/目标布局（不依赖 t_ms）。"""

    ruby: RubyAnnotation
    indices: list[int]
    style: Style
    x: int
    baseline_y: int
    target_width: int
    reading_width: float
    gradient_rect: QRectF
    horizontal_gradient_rect: QRectF | None = None
    wipe_segments: tuple[_RubyWipeSegment, ...] = ()
    wipe_left: float = 0.0
    wipe_right: float = 0.0
    geometry_signature: tuple = ()
    font: QFont | None = field(default=None, compare=False)
    metrics: QFontMetrics | None = field(default=None, compare=False)


@dataclass(frozen=True)
class _TitleOverlayLayout:
    """标题 overlay 的纯几何/排版布局（不依赖 t_ms）。"""

    lines: list[str]
    widths: list[float]
    block_w: float
    block_h: float
    line_h: float
    gap: int
    x0: float
    y_top: float
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    latin_metrics: QFontMetrics
    font_for: object
    glyph_rows: list[list["_TitleGlyphLayout"]]
    line_heights: list[float]
    line_ascents: list[float]


@dataclass(frozen=True)
class _TitleGlyphLayout:
    text: str
    x: float
    advance: float
    font: QFont
    metrics: QFontMetrics
    title: TitleOverlay


_UTOPIA_INTRO_TIME_MS = 700
_UTOPIA_INTRO_DELAY_MS = 200
_UTOPIA_INTRO_ENLARGE_MS = 400
_UTOPIA_INTRO_CONDENSE_MS = 100
_UTOPIA_INTRO_OVER_RATIO = 1.3
_UTOPIA_WIPE_OVER_RATIO = 1.15
_UTOPIA_WIPE_OVER_TIME_RATIO = 0.25
_UTOPIA_WIPE_OVER_TIME_LIMIT_MS = 100
_UTOPIA_FADE_OUT_TIME_MS = 750
_CHAR_FADE_INTRO_DELAY_MS = 350
_CHAR_FADE_IN_TIME_MS = 250
_CHAR_FADE_OUT_TIME_MS = 250


@contextmanager
def layout_pass():
    """标记一次排版过程：其间轨道与样式不会被改动。

    排版是逐行问「这一行同页还有谁」的，而答案对整条轨道只有一份。把这段区间圈
    出来，就能用对象身份做 O(1) 键把它算一次；区间结束即丢弃，所以外部改了轨道
    也不会读到旧分页。可重入，按线程隔离（预览与导出各跑各的）。
    """
    depth = getattr(_LAYOUT_PASS, "depth", 0)
    if depth == 0:
        _LAYOUT_PASS.page_maps = {}
        _LAYOUT_PASS.line_styles = {}
        _LAYOUT_PASS.line_indices = {}
        _LAYOUT_PASS.active_rubies = {}
        _LAYOUT_PASS.ruby_gaps = {}
        _LAYOUT_PASS.char_advances = {}
        _LAYOUT_PASS.ink_rects = {}
        _LAYOUT_PASS.sayatoo_layouts = {}
        _LAYOUT_PASS.tracks = []
        _LAYOUT_PASS.styles = []
        _LAYOUT_PASS.lines = []
        _LAYOUT_PASS.ruby_lists = []
        _LAYOUT_PASS.metrics = []
    _LAYOUT_PASS.depth = depth + 1
    try:
        yield
    finally:
        _LAYOUT_PASS.depth = depth
        if depth == 0:
            _LAYOUT_PASS.page_maps = None
            _LAYOUT_PASS.line_styles = None
            _LAYOUT_PASS.line_indices = None
            _LAYOUT_PASS.active_rubies = None
            _LAYOUT_PASS.ruby_gaps = None
            _LAYOUT_PASS.char_advances = None
            _LAYOUT_PASS.ink_rects = None
            _LAYOUT_PASS.sayatoo_layouts = None
            _LAYOUT_PASS.tracks = []
            _LAYOUT_PASS.styles = []
            _LAYOUT_PASS.lines = []
            _LAYOUT_PASS.ruby_lists = []
            _LAYOUT_PASS.metrics = []


def clear_before_layer_cache() -> None:
    """测试 / 调试用：把字幕层位图缓存全部丢掉。"""
    with _IMAGE_FILL_LOCK:
        _IMAGE_FILL_CACHE.clear()
        _IMAGE_BRUSH_CACHE.clear()
        _HARD_BAND_BRUSH_CACHE.clear()
    _TEXT_RUN_LAYER_CACHE.clear()
    _RUN_GLOW_CACHE.clear()
    _CHAR_METRIC_CACHE.clear()
    _RUBY_MEASURE_CACHE.clear()
    _RUBY_UNIT_LAYOUT_CACHE.clear()
    _LINE_LAYOUT_CACHE.clear()
    _DISPLAY_LINES_CACHE.clear()
    _PAGE_PLACEMENT_CACHE.clear()


_SIG_FIELD_NAMES_BY_TYPE: dict[type, tuple[str, ...]] = {}


def _value_signature(value) -> Hashable:
    """任意 models 值的递归值签名（dataclass / list / dict / 标量）。

    用于布局缓存 key：models 全部是可变 dataclass 且前端从不调用失效接口，
    所以 key 必须完全由当前值构成，不掺对象 id。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # GuideSymbol is the only frozen model carrying a potentially very large
    # immutable tuple (the complete SVG outline).  Reusing the frozen value as
    # the key preserves value-based invalidation while avoiding a Python-level
    # recursive copy of every path command for every layout/layer cache lookup.
    if isinstance(value, GuideSymbol):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_value_signature(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, _value_signature(item))
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        )
    if is_dataclass(value) and not isinstance(value, type):
        tp = type(value)
        names = _SIG_FIELD_NAMES_BY_TYPE.get(tp)
        if names is None:
            names = tuple(f.name for f in dataclass_fields(value))
            _SIG_FIELD_NAMES_BY_TYPE[tp] = names
        return (tp.__name__,) + tuple(
            _value_signature(getattr(value, name)) for name in names
        )
    return repr(value)


def _track_layout_signature(track: TimingTrack) -> tuple:
    """track 中影响**邻行可见**布局的值（手写快速版）。

    邻行只通过 SmartHorizon 分页 / 页宽参与本行布局（``assign_lanes`` 与
    ``_line_total_width`` 都不读邻行计时），所以帧级签名只收每行文本 / 布局
    结构字段 + 全部注音 + meta 偏移；目标行自己的逐字符计时细节由
    :func:`_line_layout_signature` 按行补充，避免大轨每帧走全量计时元组。"""
    return (
        tuple(
            (
                "".join(c.text for c in line.chars),
                tuple(
                    (index, c.role_label)
                    for index, c in enumerate(line.chars)
                    if c.role_label is not None
                ),
                line.singer_id,
                line.is_blank,
                line.layout_index,
                line.break_before,
                _value_signature(line.animation_override),
                _value_signature(line.guide_symbol),
                _value_signature(line.inline_guide_symbols),
            )
            for line in track.lines
        ),
        tuple(
            (
                ruby.kanji,
                ruby.reading,
                tuple(ruby.reading_part_ms),
                ruby.pos_start_ms,
                ruby.pos_end_ms,
                tuple(ruby.reading_parts),
            )
            for ruby in track.rubies
        ),
        page_plan_signature(track),
        _value_signature(track.loading_settings_snapshot),
        track.loading_settings_mode,
        (track.meta.silence_ms, track.meta.offset_ms),
    )


def _line_layout_signature(line: TimingLine) -> tuple:
    """目标行的逐字符计时细节（intervals / fill_segments / ruby 时窗的输入）。"""
    return (
        tuple(c.start_ms for c in line.chars),
        tuple(
            (index, c.pause_release_ms)
            for index, c in enumerate(line.chars)
            if c.pause_release_ms is not None
        ),
        tuple(
            (
                index,
                c.source_span_start_ms,
                c.source_span_end_ms,
                c.source_span_index,
                c.source_span_count,
            )
            for index, c in enumerate(line.chars)
            if c.source_span_count != 1 or c.source_span_start_ms is not None
        ),
        tuple(
            (index, c.explicit_start, c.explicit_end)
            for index, c in enumerate(line.chars)
            if c.explicit_start or c.explicit_end
        ),
        line.end_ms,
        line.display_start_override_ms,
        line.display_end_override_ms,
        _value_signature(line.guide_symbol),
        _value_signature(line.inline_guide_symbols),
    )


def _layout_cache_sig(track: TimingTrack, display_style: Style) -> tuple | None:
    """每帧一次的布局缓存基础签名；关闭开关或竖排时返回 None（竖排走独立路径）。"""
    if not _layout_cache_enabled() or display_style.vertical:
        return None
    return (_track_layout_signature(track), _value_signature(display_style))

from krok_helper.subtitle_render.engine.timeline import (
    DisplayLine,
    apply_display_overrides,
    assign_lanes,
    char_fill_ratio,
    compute_char_intervals,
    compute_display_lines,
    track_duration_ms,
)
from krok_helper.subtitle_render.engine.page_plan import (
    page_plan_signature,
    resolve_page_plan,
    set_pages_layout,
    use_default_layouts,
)
from krok_helper.subtitle_render.engine.page_placement import (
    AxisOffsetWindow,
    LineVisualBand,
    PageVisualBands,
    bands_require_separation,
    solve_page_axis_offsets,
    time_windows_overlap,
)
from krok_helper.subtitle_render.engine.animator import line_animation_state
from krok_helper.subtitle_render.engine.show_time import (
    MIN_AUTO_ENTRY_ANIMATION_MS,
    protect_time_ms,
)
from krok_helper.subtitle_render.models import (
    DecorationKind,
    GuideSymbol,
    KaraokeColors,
    KaraokeColorState,
    LYRICS_LAYOUT_CHAR_FIELDS,
    LYRICS_LAYOUT_FIELDS,
    N3_FONT_INHERITANCE_FIELDS,
    PaintFill,
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TimingChar,
    TimingLine,
    TimingTrack,
    TitleOverlay,
    effective_karaoke_animation,
    normalize_title_char_role_labels,
    normalize_glow_concentration_level,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    layout_capacity,
    layout_id_for_index,
    line_visible_chars,
    style_with_line_animation,
    timing_line_start_ms,
)


def _resolve_visible_content(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    duration_ms: Optional[int] = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
):
    """计算某帧的可见内容元组：``(track_t_ms, display_style, display_lines,
    signal_lines, title_opacity)``。

    :func:`paint_frame_to_painter` 的早退判断与 :func:`frame_has_content` 共用本函数，
    保证"是否有可见内容"两处口径一致（A4 空帧短路用）。
    """
    track_t_ms = _effective_track_time_ms(track, t_ms, style)
    display_style = _display_style_for_signal_window(style)
    display_lines = _visible_lines_for_style(
        track,
        track_t_ms,
        display_style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    signal_lines = _signal_display_lines_for_style(
        track,
        track_t_ms,
        display_style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    # Lyrics follow the track/global offset; the project title does not. N3
    # anchors title show times to the background movie timeline.
    title_opacity = _title_overlay_opacity(
        style.title_overlay,
        track,
        t_ms,
        duration_ms=duration_ms,
    )
    return track_t_ms, display_style, display_lines, signal_lines, title_opacity


def frame_has_content(
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> bool:
    """该帧是否会画出任何字幕内容（行 / 信号 / 标题）。

    用于导出 / 预览的"空帧短路"：返回 ``False`` 时可直接写全透明帧，省去
    ``fill`` + 光栅化 + 字节拷贝。与 :func:`paint_frame_to_painter` 的早退条件同源。

    ``extra_tracks``：副字幕源（N3 多歌词文件，如コーラス轨）；任一轨有内容即为真。
    标题只随主轨。
    """
    if track is not None:
        _, _, display_lines, signal_lines, title_opacity = _resolve_visible_content(
            track,
            t_ms,
            style,
            duration_ms=duration_ms,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if display_lines or signal_lines or title_opacity > 0.0:
            return True
    for extra in extra_tracks or ():
        _, _, display_lines, signal_lines, _unused = _resolve_visible_content(
            extra,
            t_ms,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if display_lines or signal_lines:
            return True
    return False


def frame_content_intervals(
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int]] | None:
    """Return per-source (lyric / title) vertical content intervals, **unmerged**.

    Each entry is a clamped ``(top, bottom)`` for one content group (the lyric +
    signal group, and the title overlay group).  Disjoint groups stay separate so
    the export pipeline can pack them into multiple strips (A2 方案 B).  Returns
    ``None`` for paths not yet migrated to layer bounds (竖排 / viewport 旋转 /
    逐字 transition)，调用方应回退到整帧 / alpha 扫描。
    """
    track_entries: list[tuple[TimingTrack, bool]] = []
    if track is not None:
        track_entries.append((track, True))
    track_entries.extend((extra, False) for extra in extra_tracks or ())
    if not track_entries:
        return None

    intervals: list[tuple[int, int]] = []
    any_content = False
    for entry_track, with_title in track_entries:
        track_t_ms, display_style, display_lines, signal_lines, title_opacity = (
            _resolve_visible_content(
                entry_track,
                t_ms,
                style,
                duration_ms=duration_ms if with_title else None,
                logical_w=logical_w,
                logical_h=logical_h,
            )
        )
        if not with_title:
            title_opacity = 0.0
        if not display_lines and not signal_lines and title_opacity <= 0.0:
            continue
        any_content = True
        if display_lines:
            lyric_bounds = _subtitle_lines_vertical_bounds(
                logical_w,
                logical_h,
                entry_track,
                track_t_ms,
                display_style,
                display_lines,
                signal_lines,
            )
            if lyric_bounds is None:
                return None
            intervals.append(lyric_bounds)

        if with_title and title_opacity > 0.0 and style.title_overlay is not None:
            resolved_title = resolve_title_overlay(style)
            title_layout = _layout_title_overlay(
                logical_w, logical_h, entry_track, resolved_title, style=style
            )
            if title_layout is not None:
                title_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
                    LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h),
                    [_TitleOverlayLayer(title_layout, resolved_title, title_opacity)],
                )
                if title_bounds is not None:
                    intervals.append(title_bounds)

    if not any_content:
        return None
    clamped: list[tuple[int, int]] = []
    for top, bottom in intervals:
        ct = max(0, top)
        cb = min(logical_h - 1, bottom)
        if cb >= ct:
            clamped.append((ct, cb))
    return clamped or None


def frame_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> tuple[int, int] | None:
    """Return conservative vertical content bounds (union) for the current frame.

    This is the P1.b layer-bounds query used by export strip selection and
    preview dirty updates.  It deliberately returns ``None`` for render paths
    that have not migrated to layer bounds yet; callers should then fall back to
    the existing pixel scan / full repaint path.
    """
    intervals = frame_content_intervals(
        logical_w,
        logical_h,
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
    )
    if not intervals:
        return None
    top = min(item[0] for item in intervals)
    bottom = max(item[1] for item in intervals)
    if bottom < top:
        return None
    return top, bottom


def paint_frame(
    image: QImage,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> QImage:
    """把 ``track`` 在 ``t_ms`` 时刻的活跃行渲染到 ``image``（原地修改）。

    若无活跃行则不画任何字（image 不变）。返回同一个 image 以便链式调用。
    ``extra_tracks`` 为副字幕源（N3 多歌词文件），在主轨之上依次叠绘。
    """
    painter = QPainter(image)
    try:
        # QImage 上 setDevicePixelRatio 后，QPainter 在该 image 上的坐标系
        # 自动按 dpr 缩放——绘制坐标用"逻辑像素"，而 image.width()/height()
        # 返回的是物理像素。这里取逻辑尺寸，让上层布局算居中等都按屏幕
        # 实际可见尺寸来。
        dpr = image.devicePixelRatioF() or 1.0
        logical_w = max(int(round(image.width() / dpr)), 1)
        logical_h = max(int(round(image.height() / dpr)), 1)
        paint_frame_to_painter(
            painter,
            logical_w,
            logical_h,
            track,
            t_ms,
            style,
            extra_tracks,
            duration_ms=duration_ms,
        )
    finally:
        painter.end()
    return image


def paint_frame_to_painter(
    painter: QPainter,
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> None:
    """把当前字幕帧直接绘制到已打开的 ``QPainter``。

    ``logical_w`` / ``logical_h`` 使用 Qt 逻辑像素；调用方负责先绘制背景。

    ``extra_tracks``：副字幕源（对标 N3 ``SourceLyricsInfos`` 多歌词文件，
    如コーラス轨）。每轨独立分页 / 分 lane / 计算显示窗口，依次叠绘到同一帧；
    标题 overlay 只随主轨绘制一次。
    """
    with layout_pass():
        if track is not None:
            _paint_track_to_painter(
                painter,
                logical_w,
                logical_h,
                track,
                t_ms,
                style,
                draw_title=True,
                duration_ms=duration_ms,
            )
        for extra in extra_tracks or ():
            _paint_track_to_painter(
                painter, logical_w, logical_h, extra, t_ms, style, draw_title=False
            )


def _paint_track_to_painter(
    painter: QPainter,
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    draw_title: bool,
    duration_ms: Optional[int] = None,
) -> None:
    track_t_ms, display_style, display_lines, signal_lines, title_opacity = (
        _resolve_visible_content(
            track,
            t_ms,
            style,
            duration_ms=duration_ms if draw_title else None,
            logical_w=logical_w,
            logical_h=logical_h,
        )
    )
    if not draw_title:
        title_opacity = 0.0
    if not display_lines and not signal_lines and title_opacity <= 0.0:
        return

    painter.save()
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _apply_viewport_transform(painter, logical_w, logical_h, display_style)
        # 竖排时 baselines 字典里存的是每 lane 的「列中心 x」，横排时存基线 y；
        # 含义由 style.vertical 区分，_paint_line_static 据此走对应几何。
        if display_style.vertical:
            baselines = _resolve_vertical_columns(logical_w, track, display_lines, display_style)
            line_layouts = {}
            per_line_axes = {
                id(display_line.line): _resolve_vertical_columns(
                    logical_w,
                    track,
                    [display_line],
                    _style_for_line(display_style, display_line.line),
                ).get(display_line.lane)
                for display_line in display_lines
            }
        else:
            baselines = (
                _resolve_display_baselines(logical_h, track, display_lines, display_style)
                if display_lines
                else {}
            )
            line_layouts = _resolve_sayatoo_line_layouts(
                logical_w,
                logical_h,
                track,
                display_lines,
                baselines,
                track_t_ms,
                display_style,
            )
            per_line_axes = {}
        layout_cache_sig = (
            _layout_cache_sig(track, display_style) if display_lines else None
        )
        track_offsets = resolved_page_offsets_for_style(
            logical_w, logical_h, track, display_style, t_ms=track_t_ms
        )
        line_offsets = {
            id(line): track_offsets.get(index, (0.0, 0.0))
            for index, line in enumerate(track.lines)
        }
        for display_line in display_lines:
            line_layout = line_layouts.get(id(display_line.line))
            has_role_labels = _line_has_role_labels(display_line.line)
            line_x = None
            if line_layout is not None and not has_role_labels:
                line_x = line_layout.text_x
            offset_x, offset_y = line_offsets.get(
                id(display_line.line), (0.0, 0.0)
            )
            painter.save()
            try:
                if offset_x or offset_y:
                    painter.translate(offset_x, offset_y)
                _paint_line(
                    painter,
                    logical_w,
                    logical_h,
                    track,
                    display_line.line,
                    track_t_ms,
                    display_style,
                    baseline_y=(
                        per_line_axes.get(id(display_line.line))
                        if display_style.vertical
                        else line_layout.baseline_y
                        if line_layout is not None
                        else baselines.get(
                            display_line.lane,
                            next(iter(baselines.values()), logical_h // 2),
                        )
                    ),
                    line_x=line_x,
                    lane=display_line.lane if display_style.dual_line_layout else None,
                    display_start_ms=display_line.display_start_ms,
                    display_end_ms=display_line.display_end_ms,
                    layout_cache_sig=layout_cache_sig,
                )
            finally:
                painter.restore()
        if not display_style.vertical and signal_lines:
            _paint_signal_lits(
                painter,
                logical_w,
                logical_h,
                track,
                signal_lines,
                baselines,
                track_t_ms,
                display_style,
                line_layouts=line_layouts,
                line_offsets=line_offsets,
            )
    finally:
        painter.restore()

    # 标题字幕 overlay（B7）：静态文字，画在屏幕坐标系（不随「视图」变换 / 行布局），
    # 在歌词之上独立绘制。外观由「标题」配色方案与布局引用解析。
    if title_opacity > 0.0 and style.title_overlay is not None:
        _paint_title_overlay(
            painter, logical_w, logical_h, track, style, title_opacity
        )


# ---------------------------------------------------------------------------
# 标题字幕 overlay（B7）
# ---------------------------------------------------------------------------


def _title_layout_source(style: Style, index: Optional[int]):
    """标题布局引用 → 几何来源（0 = 默认布局即 ``Style`` 自身；悬空返回 None）。"""
    if index is None:
        return None
    index = int(index)
    if index == 0:
        return style
    if 1 <= index <= len(style.layouts):
        return style.layouts[index - 1]
    return None


def resolve_title_overlay(style: Style) -> Optional[TitleOverlay]:
    """把「标题」配色方案与布局引用解析成有效 ``TitleOverlay``。

    字体/颜色来自 ``custom_style_schemes[TITLE_SCHEME_NAME]`` 与全局样式的合并
    结果（标题永不走字，取走字前配色）；位置、行距和字间距来自
    ``layout_index`` 引用的布局。方案或布局缺失（旧工程迁移前 / 引用悬空）
    时保留字段原值。
    """
    title = style.title_overlay
    if title is None:
        return None
    changes: dict[str, object] = {}
    if TITLE_SCHEME_NAME in style.custom_style_schemes:
        merged = _style_for_role(style, TITLE_SCHEME_NAME)
        colors = _effective_karaoke_colors(merged).before
        changes.update(
            font_family=merged.font_family,
            font_family_latin=merged.font_family_latin,
            font_size_px=int(merged.font_size_px),
            font_weight=int(merged.font_weight),
            italic=bool(merged.italic),
            letter_spacing_px=int(merged.letter_spacing_px),
            fill=colors.text,
            stroke=colors.stroke,
            stroke_width_px=int(merged.stroke_width_px),
            stroke2=colors.stroke2,
            stroke2_width_px=(
                int(merged.stroke2_width_px) if merged.stroke2_enabled else 0
            ),
            decoration_kind=merged.decoration_kind,
            glow_radius_px=int(merged.glow_before_radius_px),
            glow_concentration_level=int(merged.glow_concentration_level),
            shadow=colors.shadow,
            shadow_offset_x=int(merged.shadow_offset_x),
            shadow_offset_y=int(merged.shadow_offset_y),
        )
    source = _title_layout_source(style, title.layout_index)
    if source is not None:
        alignments = list(source.line_alignments) or ["left"]
        horizontal = alignments[0]
        vertical = source.line_y_position
        letter_spacing = getattr(source, "letter_spacing_px", None)
        if letter_spacing is None:
            letter_spacing = style.letter_spacing_px
        changes.update(
            anchor=(
                "center"
                if (vertical, horizontal) == ("center", "center")
                else f"{vertical}_{horizontal}"
            ),
            align=horizontal,
            offset_x=int(source.horizontal_margin_px),
            offset_y=int(source.line_y_margin_px),
            line_gap_px=int(source.line_gap_px),
            letter_spacing_px=int(letter_spacing),
        )
    if not changes:
        return title
    return replace(title, **changes)


def _resolve_title_role_overlay(
    style: Style, base: TitleOverlay, role_label: Optional[str]
) -> TitleOverlay:
    """标题字符角色 → 静态标题外观；缺失方案回退内置标题方案。"""
    if not role_label or role_label not in style.custom_style_schemes:
        return base
    merged = _style_for_role(style, role_label)
    colors = _effective_karaoke_colors(merged).before
    layout_source = _title_layout_source(style, base.layout_index)
    return replace(
        base,
        font_family=merged.font_family,
        font_family_latin=merged.font_family_latin,
        font_size_px=int(merged.font_size_px),
        font_weight=int(merged.font_weight),
        italic=bool(merged.italic),
        # 与普通歌词一致：角色方案决定字体/颜色，已应用布局的字符排版优先。
        letter_spacing_px=(
            int(base.letter_spacing_px)
            if layout_source is not None
            else int(merged.letter_spacing_px)
        ),
        fill=colors.text,
        stroke=colors.stroke,
        stroke_width_px=int(merged.stroke_width_px),
        stroke2=colors.stroke2,
        stroke2_width_px=(
            int(merged.stroke2_width_px) if merged.stroke2_enabled else 0
        ),
        decoration_kind=merged.decoration_kind,
        glow_radius_px=int(merged.glow_before_radius_px),
        glow_concentration_level=int(merged.glow_concentration_level),
        shadow=colors.shadow,
        shadow_offset_x=int(merged.shadow_offset_x),
        shadow_offset_y=int(merged.shadow_offset_y),
    )


_TITLE_SEPARATOR_CHARS = " \t/|・-–—~　"


def _resolve_title_text(title: TitleOverlay, track: TimingTrack) -> str:
    """模板 ``{title}`` / ``{artist}`` 用 ``@Title`` / ``@Artist`` 元数据替换。

    模板里没有占位符时（用户填了纯自定义文字）原样返回；含占位符时，缺失的
    title/artist 会让模板里的分隔符（``/`` 等）变孤立，按行清掉首尾分隔，整行只剩
    分隔符则清空——避免「无元数据时显示一个孤零零的 /」。
    """
    template = title.text_template or ""
    if "{title}" not in template and "{artist}" not in template:
        return template.strip("\n")
    meta_title = (track.meta.title or "").strip()
    meta_artist = (track.meta.artist or "").strip()
    text = template.replace("{title}", meta_title).replace("{artist}", meta_artist)
    lines = [raw.strip().strip(_TITLE_SEPARATOR_CHARS).strip() for raw in text.split("\n")]
    return "\n".join(lines).strip("\n")


def _title_show_window(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int]]:
    """返回标题可见区间（毫秒，项目播放时间轴）。

    ``duration_ms`` 是背景/音频/字幕共同决定的实际预览或导出时长。仅在旧调用方
    没有提供时才回退字幕轨时长，避免片尾标题错误地锚到最后一句歌词。
    """
    total = max(
        (
            int(duration_ms)
            if duration_ms is not None and int(duration_ms) > 0
            else track_duration_ms(track)
        ),
        0,
    )
    head_start = max(int(title.head_offset_ms), 0)
    duration = max(int(title.duration_ms), 0)
    tail_duration = max(
        int(title.tail_duration_ms)
        if title.tail_duration_ms is not None
        else duration,
        0,
    )
    tail_off = max(int(title.tail_offset_ms), 0)
    # N3 TitleShowTime.EndTime keeps a zero-offset tail visible through the
    # movie's inclusive endpoint by adding one 10 ms time-tag unit.
    tail_base_end = max(total - tail_off, 0)
    tail_end = tail_base_end + (10 if total > 0 and tail_off == 0 else 0)
    if title.show_mode == "whole":
        return [(0, tail_end)]
    if title.show_mode == "head":
        return [(head_start, head_start + duration)]
    if title.show_mode == "tail":
        return [(max(tail_base_end - tail_duration, 0), tail_end)]
    # 开始和片尾：两个独立片段，各自使用自己的显示时长。
    return [
        (head_start, head_start + duration),
        (max(tail_base_end - tail_duration, 0), tail_end),
    ]


def _title_show_specs(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int, int, int]]:
    """返回 ``(开始, 结束, 淡入, 淡出)``，允许首尾片段使用独立过渡。"""
    windows = _title_show_window(title, track, duration_ms=duration_ms)
    head_fade_in = max(int(title.fade_in_ms), 0)
    head_fade_out = max(int(title.fade_out_ms), 0)
    tail_fade_in = max(
        int(title.tail_fade_in_ms)
        if title.tail_fade_in_ms is not None
        else head_fade_in,
        0,
    )
    tail_fade_out = max(
        int(title.tail_fade_out_ms)
        if title.tail_fade_out_ms is not None
        else head_fade_out,
        0,
    )
    if title.show_mode == "tail":
        return [
            (begin, end, tail_fade_in, tail_fade_out)
            for begin, end in windows
        ]
    if title.show_mode == "head_tail" and len(windows) > 1:
        return [
            (*windows[0], head_fade_in, head_fade_out),
            (*windows[1], tail_fade_in, tail_fade_out),
        ]
    return [
        (begin, end, head_fade_in, head_fade_out)
        for begin, end in windows
    ]


def _title_overlay_opacity(
    title: Optional[TitleOverlay],
    track: TimingTrack,
    t_ms: int,
    *,
    duration_ms: Optional[int] = None,
) -> float:
    """标题在 ``t_ms`` 的不透明度（含淡入淡出）；不可见返回 0。"""
    if title is None or not title.enabled:
        return 0.0
    best = 0.0
    for begin, end, fade_in, fade_out in _title_show_specs(
        title, track, duration_ms=duration_ms
    ):
        if end <= begin or t_ms < begin or t_ms > end:
            continue
        alpha = 1.0
        if fade_in > 0 and t_ms < begin + fade_in:
            alpha = min(alpha, (t_ms - begin) / fade_in)
        if fade_out > 0 and t_ms > end - fade_out:
            alpha = min(alpha, (end - t_ms) / fade_out)
        best = max(best, max(0.0, min(1.0, alpha)))
    return best


def _build_title_font(title: TitleOverlay) -> QFont:
    font = QFont(
        resolve_qt_font_family(title.font_family), max(title.font_size_px, 1)
    )
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(_clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def _build_title_latin_font(title: TitleOverlay) -> QFont:
    family = title.font_family_latin or title.font_family
    font = QFont(resolve_qt_font_family(family), max(title.font_size_px, 1))
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(_clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def _title_block_origin(
    img_w: int,
    img_h: int,
    block_w: float,
    block_h: float,
    title: TitleOverlay,
    *,
    edge_px: float = 0.0,
) -> tuple[float, float]:
    """按锚点 9 宫格放置文字块，返回左上角 ``(x0, y_top)``。

    ``offset_x`` / ``offset_y`` 对贴边锚点是内边距，对居中锚点是附加位移。

    ``x0`` 是首字的笔尖原点，``block_w`` 是步进宽之和——两者都不含描边。N3 的
    字符盒把描边的一半算在盒内（``DrawCharInfo`` 宽 = 墨迹宽 + Edge、高 = 字号 +
    Edge），贴边锚点对齐的是盒边而不是墨迹，描边不会溢出余白。这里对贴边锚点补上
    ``edge_px / 2``，让左右余白与上下余白量到同一条边；竖向的那一半已经含在
    ``block_h`` 里（见 :func:`_n3_char_box_ascent`），无需另加。
    """
    anchor = title.anchor
    half_edge = max(float(edge_px), 0.0) / 2.0
    if anchor.endswith("left"):
        x0 = title.offset_x + half_edge
    elif anchor.endswith("right"):
        x0 = img_w - block_w - title.offset_x - half_edge
    else:  # center 列
        x0 = (img_w - block_w) / 2.0 + title.offset_x
    if anchor.startswith("top"):
        y_top = float(title.offset_y)
    elif anchor.startswith("bottom"):
        y_top = img_h - block_h - title.offset_y
    else:  # center 行
        y_top = (img_h - block_h) / 2.0 + title.offset_y
    return x0, y_top


def _paint_title_overlay(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    style: Style,
    opacity: float,
) -> None:
    title = resolve_title_overlay(style)
    if title is None:
        return
    layout = _layout_title_overlay(img_w, img_h, track, title, style=style)
    if layout is None:
        return
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=0, logical_w=img_w, logical_h=img_h),
        [_TitleOverlayLayer(layout, title, opacity)],
    )


def _layout_title_overlay(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    title: TitleOverlay,
    *,
    style: Optional[Style] = None,
) -> _TitleOverlayLayout | None:
    text = _resolve_title_text(title, track)
    lines = [line for line in text.split("\n")]
    if not any(line.strip() for line in lines):
        return None
    font = _build_title_font(title)
    metrics = QFontMetrics(font)
    latin_font = _build_title_latin_font(title)
    font_for = _make_title_font_for(title, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    glyph_rows: list[list[_TitleGlyphLayout]] = []
    widths: list[float] = []
    line_heights: list[float] = []
    line_ascents: list[float] = []
    max_edge = 0.0
    # 每一行的盒只由这一行实际用到的字形决定。基础标题方案只在整行没有字形
    # （空行）时兜底：否则给标题分配了角色方案后，基础方案的字号仍会顶着行盒
    # 走，改「标题」方案的字号会把已经用别的方案渲染的标题整体上下推。
    fallback_ascent = _n3_char_box_ascent(
        metrics, title.font_size_px, title.stroke_width_px
    )
    fallback_descent = _n3_char_box_descent(
        metrics, title.font_size_px, title.stroke_width_px
    )
    for row_index, text_line in enumerate(lines):
        glyphs: list[_TitleGlyphLayout] = []
        cursor = 0.0
        # N3 行盒：高 = 字号 + 描边，基线按字体 A:D 比例切分（见
        # _n3_char_box_ascent）。用它替代 Qt 原始 ascent/descent，标题的上余白才
        # 量到和左右余白同一条盒边——Qt metric 的 ascent 含 em 内部行距，大写字母
        # 上方那一段空白会让同样的 40px 看起来明显更高。
        max_ascent = 0.0
        max_descent = 0.0
        for char_index, char in enumerate(text_line):
            glyph_title = (
                _resolve_title_role_overlay(style, title, labels[row_index][char_index])
                if style is not None
                else title
            )
            glyph_jp_font = _build_title_font(glyph_title)
            glyph_latin_font = _build_title_latin_font(glyph_title)
            glyph_font_for = _make_title_font_for(
                glyph_title, glyph_jp_font, glyph_latin_font
            )
            glyph_font = (
                glyph_font_for(char) if glyph_font_for is not None else glyph_jp_font
            )
            glyph_metrics = QFontMetrics(glyph_font)
            advance = float(glyph_metrics.horizontalAdvance(char))
            glyphs.append(
                _TitleGlyphLayout(
                    text=char,
                    x=cursor,
                    advance=advance,
                    font=glyph_font,
                    metrics=glyph_metrics,
                    title=glyph_title,
                )
            )
            cursor += advance
            if char_index + 1 < len(text_line):
                cursor += int(glyph_title.letter_spacing_px)
            max_ascent = max(
                max_ascent,
                _n3_char_box_ascent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_descent = max(
                max_descent,
                _n3_char_box_descent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_edge = max(max_edge, float(max(glyph_title.stroke_width_px, 0)))
        if not glyphs:
            max_ascent = fallback_ascent
            max_descent = fallback_descent
        glyph_rows.append(glyphs)
        widths.append(cursor)
        line_ascents.append(max_ascent)
        line_heights.append(max_ascent + max_descent)
    block_w = max(widths) if widths else 0.0
    line_h = max(line_heights, default=metrics.height())
    gap = max(int(title.line_gap_px), 0)
    block_h = sum(line_heights) + gap * max(len(lines) - 1, 0)
    if block_w <= 0 or block_h <= 0:
        return None

    x0, y_top = _title_block_origin(
        img_w, img_h, block_w, block_h, title, edge_px=max_edge
    )
    return _TitleOverlayLayout(
        lines=lines,
        widths=widths,
        block_w=block_w,
        block_h=float(block_h),
        line_h=line_h,
        gap=gap,
        x0=x0,
        y_top=y_top,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        latin_metrics=latin_metrics,
        font_for=font_for,
        glyph_rows=glyph_rows,
        line_heights=line_heights,
        line_ascents=line_ascents,
    )


@dataclass(frozen=True)
class _TitleOverlayLayer:
    """Layer wrapper for the static title overlay block."""

    title_layout: _TitleOverlayLayout
    title: TitleOverlay
    opacity: float
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_TitleOverlayLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        return (
            *_title_overlay_layer_key(self.title_layout, self.title),
            _raster_scale_key(ctx.device_pixel_ratio),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = _build_title_overlay_layer(
            self.title_layout,
            self.title,
            device_pixel_ratio=ctx.device_pixel_ratio,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(float(self.title_layout.x0), float(self.title_layout.y_top)),
            opacity=max(0.0, min(1.0, self.opacity)),
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        pad = max(
            (_title_visual_padding(glyph.title) for row in self.title_layout.glyph_rows for glyph in row),
            default=_title_visual_padding(self.title),
        )
        return (
            int(math.floor(self.title_layout.y_top - pad)),
            int(math.ceil(self.title_layout.y_top + self.title_layout.block_h + pad)),
        )


def _title_overlay_layer_key(
    layout: _TitleOverlayLayout,
    title: TitleOverlay,
) -> tuple:
    return (
        tuple(layout.lines),
        tuple(round(width, 3) for width in layout.widths),
        round(layout.block_w, 3),
        round(layout.block_h, 3),
        round(layout.line_h, 3),
        layout.gap,
        title.align,
        layout.font.family(),
        layout.font.pixelSize(),
        int(layout.font.weight()),
        layout.font.italic(),
        layout.latin_font.family(),
        layout.latin_font.pixelSize(),
        int(layout.latin_font.weight()),
        layout.latin_font.italic(),
        title.letter_spacing_px,
        _fill_signature(title.fill),
        _fill_signature(title.stroke),
        title.stroke_width_px,
        _fill_signature(title.stroke2),
        title.stroke2_width_px,
        title.decoration_kind,
        title.glow_radius_px,
        title.glow_concentration_level,
        _fill_signature(title.shadow),
        title.shadow_offset_x,
        title.shadow_offset_y,
        tuple(
            (
                glyph.text,
                round(glyph.x, 3),
                round(glyph.advance, 3),
                glyph.font.family(),
                glyph.font.pixelSize(),
                int(glyph.font.weight()),
                glyph.font.italic(),
                _fill_signature(glyph.title.fill),
                _fill_signature(glyph.title.stroke),
                glyph.title.stroke_width_px,
                _fill_signature(glyph.title.stroke2),
                glyph.title.stroke2_width_px,
                glyph.title.decoration_kind,
                glyph.title.glow_radius_px,
                glyph.title.glow_concentration_level,
                _fill_signature(glyph.title.shadow),
                glyph.title.shadow_offset_x,
                glyph.title.shadow_offset_y,
            )
            for row in layout.glyph_rows
            for glyph in row
        ),
    )


def _build_title_overlay_layer(
    layout: _TitleOverlayLayout,
    title: TitleOverlay,
    *,
    device_pixel_ratio: float = 1.0,
) -> tuple[QImage, int, int]:
    glyph_titles = [glyph.title for row in layout.glyph_rows for glyph in row] or [title]
    extent = max(_title_visual_padding(item) for item in glyph_titles) + 4
    pad_left = max(max(0, -item.shadow_offset_x) for item in glyph_titles) + extent
    pad_right = max(max(0, item.shadow_offset_x) for item in glyph_titles) + extent
    pad_top = max(max(0, -item.shadow_offset_y) for item in glyph_titles) + extent
    pad_bottom = max(max(0, item.shadow_offset_y) for item in glyph_titles) + extent
    img_w = max(int(math.ceil(pad_left + layout.block_w + pad_right)), 1)
    img_h = max(int(math.ceil(pad_top + layout.block_h + pad_bottom)), 1)
    image = _make_raster_image(img_w, img_h, device_pixel_ratio)
    image.fill(0)

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        line_top = float(pad_top)
        for glyphs, width, line_height, line_ascent in zip(
            layout.glyph_rows,
            layout.widths,
            layout.line_heights,
            layout.line_ascents,
        ):
            if glyphs:
                if title.align == "center":
                    lx = pad_left + (layout.block_w - width) / 2.0
                elif title.align == "right":
                    lx = pad_left + (layout.block_w - width)
                else:
                    lx = float(pad_left)
                baseline = line_top + line_ascent
                run_start = 0
                while run_start < len(glyphs):
                    run_end = run_start + 1
                    run_title = glyphs[run_start].title
                    while (
                        run_end < len(glyphs)
                        and glyphs[run_end].title == run_title
                    ):
                        run_end += 1
                    run = glyphs[run_start:run_end]
                    path = QPainterPath()
                    for glyph in run:
                        path.addText(
                            float(lx + glyph.x), baseline, glyph.font, glyph.text
                        )
                    left = float(lx + run[0].x)
                    right = float(lx + run[-1].x + run[-1].advance)
                    ascent = max(glyph.metrics.ascent() for glyph in run)
                    descent = max(glyph.metrics.descent() for glyph in run)
                    rect = QRectF(
                        left,
                        float(baseline - ascent),
                        max(right - left, 1.0),
                        float(ascent + descent),
                    )
                    _paint_title_text_stack(p, path, rect, run_title)
                    run_start = run_end
            line_top += line_height + layout.gap
    finally:
        p.end()
    return image, -pad_left, -pad_top


def _raster_scale_key(device_pixel_ratio: float) -> int:
    return max(int(round(max(float(device_pixel_ratio or 1.0), 0.01) * 1000)), 1)


def _make_raster_image(logical_w: int, logical_h: int, device_pixel_ratio: float) -> QImage:
    dpr = max(float(device_pixel_ratio or 1.0), 0.01)
    physical_w = max(int(math.ceil(max(int(logical_w), 1) * dpr)), 1)
    physical_h = max(int(math.ceil(max(int(logical_h), 1) * dpr)), 1)
    image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    return image


def _make_title_font_for(title: TitleOverlay, jp_font: QFont, latin_font: QFont):
    if not title.font_family_latin or latin_font.family() == jp_font.family():
        return None

    def font_for(ch_text: str) -> QFont:
        return latin_font if (ch_text and ch_text.isascii()) else jp_font

    return font_for


def _title_line_path(
    line: str,
    font: QFont,
    x0: float,
    baseline: float,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
    spacing: int,
) -> QPainterPath:
    path = QPainterPath()
    cursor = float(x0)
    for ch in line:
        glyph_font = font_for(ch) if font_for is not None else font
        path.addText(cursor, float(baseline), glyph_font, ch)
        cursor += _char_advance(ch, metrics, latin_metrics, font_for) + spacing
    return path


def _paint_title_text_stack(
    painter: QPainter, path: QPainterPath, rect: QRectF, title: TitleOverlay
) -> None:
    """静态标题文字的装饰 + 二重描边 + 描边 + 填充（单态，不走字）。"""
    if title.decoration_kind == "glow":
        _paint_glow_path(
            painter,
            path,
            title.shadow,
            rect,
            max(int(title.glow_radius_px), 0),
            title.stroke_width_px,
            title.stroke2_width_px,
            concentration_level=title.glow_concentration_level,
        )
    elif (
        title.decoration_kind == "shadow"
        and (title.shadow_offset_x or title.shadow_offset_y)
    ):
        _paint_shadow_silhouette(
            painter,
            path,
            title.shadow,
            rect,
            title.shadow_offset_x,
            title.shadow_offset_y,
            title.stroke_width_px,
            title.stroke2_width_px,
        )
    if title.stroke2_width_px > 0:
        _paint_stroke_path(
            painter, path, title.stroke2, rect,
            _stroke2_pen_width(title.stroke_width_px, title.stroke2_width_px),
        )
    if title.stroke_width_px > 0:
        _paint_stroke_path(
            painter,
            path,
            title.stroke,
            rect,
            _stroke_pen_width(title.stroke_width_px),
            protect_body=_fill_is_alpha(title.fill),
        )
    _paint_fill_path(painter, path, title.fill, rect)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _effective_track_time_ms(track: TimingTrack, t_ms: int, style: Style) -> int:
    """Convert playback time to subtitle time after LRC and UI offsets.

    Positive offsets delay subtitles: at playback ``t_ms`` the renderer samples an
    earlier subtitle timestamp.
    """
    return t_ms - (track.meta.offset_ms + style.timing_offset_ms)


# 九宫格锚点在画布上的相对坐标（横向, 纵向），用于缩放 / 旋转的轴心。
_VIEWPORT_PIVOT_FRACTIONS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_center": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom_center": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}


def _apply_viewport_transform(
    painter: QPainter, logical_w: int, logical_h: int, style: Style
) -> None:
    """对整体字幕层套用 Sayatoo「视图」组的 2D 变换。

    位移直接平移；缩放与旋转围绕 ``viewport_align`` 指定的九宫格锚点。
    默认值（位移 0、缩放 100%、旋转 0）下不改动 painter 坐标系。
    """
    scale = max(style.viewport_scale_pct, 1) / 100.0
    angle = style.viewport_rotation_deg
    offset_x = style.viewport_offset_x
    offset_y = style.viewport_offset_y
    if offset_x == 0 and offset_y == 0 and scale == 1.0 and angle == 0:
        return
    frac_x, frac_y = _VIEWPORT_PIVOT_FRACTIONS.get(
        style.viewport_align, _VIEWPORT_PIVOT_FRACTIONS["center"]
    )
    pivot_x = logical_w * frac_x
    pivot_y = logical_h * frac_y
    if offset_x or offset_y:
        painter.translate(offset_x, offset_y)
    if scale != 1.0 or angle:
        painter.translate(pivot_x, pivot_y)
        if angle:
            painter.rotate(angle)
        if scale != 1.0:
            painter.scale(scale, scale)
        painter.translate(-pivot_x, -pivot_y)


def _subtitle_lines_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    track_t_ms: int,
    style: Style,
    display_lines: list[DisplayLine],
    signal_lines: list[DisplayLine],
) -> tuple[int, int] | None:
    """Aggregate migrated layer bounds for the lyric layer.

    ``None`` means the current frame uses a path whose visual extent still needs
    the older pixel-scan fallback.
    """
    if style.vertical or style.viewport_rotation_deg:
        return None

    baselines = (
        _resolve_display_baselines(logical_h, track, display_lines, style)
        if display_lines
        else {}
    )
    line_layouts = (
        _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            track_t_ms,
            style,
        )
        if display_lines
        else {}
    )
    layout_cache_sig = _layout_cache_sig(track, style) if display_lines else None
    track_offsets = resolved_page_offsets_for_style(
        logical_w, logical_h, track, style, t_ms=track_t_ms
    )
    line_offsets = {
        id(line): track_offsets.get(index, (0.0, 0.0))
        for index, line in enumerate(track.lines)
    }
    bounds: list[tuple[int, int]] = []
    for display_line in display_lines:
        line_bounds = _display_line_vertical_bounds(
            logical_w,
            logical_h,
            track,
            track_t_ms,
            style,
            display_line,
            baselines,
            line_layouts,
            layout_cache_sig=layout_cache_sig,
        )
        if line_bounds is None:
            return None
        _offset_x, offset_y = line_offsets.get(
            id(display_line.line), (0.0, 0.0)
        )
        bounds.append(
            (
                int(math.floor(line_bounds[0] + offset_y)),
                int(math.ceil(line_bounds[1] + offset_y)),
            )
        )
    if signal_lines:
        signal_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
            LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h),
            _signal_layer_stack(
                track,
                signal_lines,
                baselines,
                logical_w,
                logical_h,
                track_t_ms,
                style,
                line_layouts=line_layouts,
                line_offsets=line_offsets,
            ),
        )
        if signal_bounds is not None:
            bounds.append(signal_bounds)
    if not bounds:
        return None

    top = min(item[0] for item in bounds)
    bottom = max(item[1] for item in bounds)
    return _transform_vertical_bounds(top, bottom, logical_h, style)


def _display_line_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    track_t_ms: int,
    style: Style,
    display_line: DisplayLine,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    layout_cache_sig: tuple | None = None,
) -> tuple[int, int] | None:
    line = display_line.line
    line_style = _style_for_line_display_window(
        style,
        line,
        display_line.display_start_ms,
        display_line.display_end_ms,
    )
    animation = line_animation_state(
        line_style,
        t_ms=track_t_ms,
        display_start_ms=display_line.display_start_ms
        if display_line.display_start_ms is not None
        else _line_start_ms(line),
        display_end_ms=display_line.display_end_ms
        if display_line.display_end_ms is not None
        else _line_end_ms(line),
        lane=display_line.lane if line_style.dual_line_layout else None,
    )
    if animation.opacity <= 0.0:
        return None

    line_layout = line_layouts.get(id(display_line.line))
    has_role_labels = _line_has_role_labels(line)
    line_x = line_layout.text_x if line_layout is not None and not has_role_labels else None
    layout = _layout_line(
        track,
        line,
        line_style,
        logical_w,
        logical_h,
        baseline_y=line_layout.baseline_y if line_layout is not None else baselines[display_line.lane],
        line_x=line_x,
        lane=display_line.lane if line_style.dual_line_layout else None,
        cache_sig=layout_cache_sig,
    )
    if layout is None:
        return None

    transition = _line_char_transition_context(
        line_style,
        line,
        track_t_ms,
        display_line.display_start_ms,
        display_line.display_end_ms,
        len(line.chars),
        intervals=layout.intervals,
    )
    if transition is not None:
        if transition.effect == "utopia":
            ctx = LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h)
            line_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
                ctx,
                _utopia_transition_scope_layers(
                    layout,
                    line,
                    line_style,
                    track_t_ms,
                    transition,
                    logical_h,
                ),
            )
            if line_bounds is not None:
                dy = int(math.floor(animation.dy)) if animation.dy < 0 else int(math.ceil(animation.dy))
                return line_bounds[0] + dy, line_bounds[1] + dy
        return None

    ctx = LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h)
    layers = _line_layer_stack(layout, track_t_ms)
    if layout.active_rubies and layout.ruby_metrics is not None:
        layers.extend(_ruby_layer_stack(layout, line, track_t_ms, line_style))
    line_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(ctx, layers)
    if line_bounds is None:
        return None
    dy = int(math.floor(animation.dy)) if animation.dy < 0 else int(math.ceil(animation.dy))
    return line_bounds[0] + dy, line_bounds[1] + dy


def resolved_page_offset_windows_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> dict[int, tuple[tuple[int, int, float, float], ...]]:
    """Return one persistent per-line page translation window.

    Each tuple is ``(start_ms, end_ms, offset_x, offset_y)`` in the pre-viewport
    logical canvas.  Once a page is displaced, the same translation remains
    active until that page finishes.  The solver accepts only positions whose
    complete static ink envelope remains inside the canvas; it searches the
    authored anchor direction first, then the opposite direction, and falls
    back to the authored position when neither side can contain the page.
    """

    if style.allow_inter_page_line_overlap or not style.dual_line_layout:
        return {}
    cache_key = (
        max(int(logical_w), 1),
        max(int(logical_h), 1),
        _value_signature(track),
        _value_signature(style),
    )
    cached = _PAGE_PLACEMENT_CACHE.get(cache_key)
    if cached is not None:
        _PAGE_PLACEMENT_CACHE.move_to_end(cache_key)
        return {key: tuple(value) for key, value in cached.items()}

    display_lines = _display_lines_for_style(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    if not display_lines:
        return {}

    index_of = {id(line): index for index, line in enumerate(track.lines)}
    page_order: list[tuple[int, int]] = []
    page_bands: dict[tuple[int, int], list[LineVisualBand]] = {}
    page_lifetimes: dict[tuple[int, int], list[tuple[int, int]]] = {}
    page_styles: dict[tuple[int, int], Style] = {}
    line_to_page: dict[int, tuple[int, int]] = {}

    if style.vertical:
        baselines: dict[int, int] = {}
        line_layouts: dict[int, _SayatooLineLayout] = {}
        layout_cache_sig = None
    else:
        baselines = _resolve_display_baselines(
            logical_h, track, display_lines, style
        )
        # The map is keyed by TimingLine identity, not lane.  Several pages can
        # be visible at once and may legitimately reuse the same lane number.
        line_layouts = _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            0,
            style,
        )
        layout_cache_sig = _layout_cache_sig(track, style)

    for display_line in display_lines:
        track_index = index_of.get(id(display_line.line))
        if track_index is None:
            continue
        page_id = (int(display_line.section_index), int(display_line.page_index))
        if page_id not in page_bands:
            page_order.append(page_id)
            page_bands[page_id] = []
            page_lifetimes[page_id] = []
        line_style = _style_for_line(style, display_line.line)
        page_styles.setdefault(page_id, line_style)
        line_to_page[track_index] = page_id
        page_lifetimes[page_id].append(
            (
                int(display_line.display_start_ms),
                int(display_line.display_end_ms),
            )
        )
        if style.vertical:
            ink_rect = _display_line_vertical_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                line_style,
                # Glow may overlap between pages; collision avoidance only
                # protects solid glyph/ruby ink plus stroke/shadow.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            axis_anchor = _resolve_vertical_columns(
                logical_w, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            ink_rect = _display_line_horizontal_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                style,
                baselines,
                line_layouts,
                layout_cache_sig=layout_cache_sig,
                # Glow may overlap between pages; collision avoidance only
                # protects solid glyph/ruby ink plus stroke/shadow.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            line_layout = line_layouts.get(id(display_line.line))
            axis_anchor = (
                line_layout.baseline_y
                if line_layout is not None
                else baselines.get(display_line.lane)
            )
        if axis_bounds is None:
            continue
        assert cross_bounds is not None
        collision_start, collision_end = _display_line_static_collision_window(
            display_line,
            style,
        )
        entry_start = int(display_line.display_start_ms)
        if collision_end <= entry_start:
            continue
        page_bands[page_id].append(
            LineVisualBand(
                line_id=track_index,
                page_id=page_id,
                display_start_ms=collision_start,
                display_end_ms=collision_end,
                axis_min=float(axis_bounds[0]),
                axis_max=float(axis_bounds[1]),
                entry_start_ms=entry_start,
                axis_anchor=(
                    None if axis_anchor is None else float(axis_anchor)
                ),
                cross_min=float(cross_bounds[0]),
                cross_max=float(cross_bounds[1]),
            )
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            # Vertical columns originate on the right and naturally avoid
            # towards the left, independent of the within-column Y anchor.
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(page_bands[page_id]),
                gap_px=max(int(page_style.line_gap_px), 0),
                anchor=anchor,
                layout_key=_page_collision_layout_key(
                    page_style,
                    line_count=len(page_lifetimes.get(page_id, ())),
                    vertical=style.vertical,
                ),
            )
        )
    axis_offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )
    axis_windows: dict[tuple[int, int], tuple[AxisOffsetWindow, ...]] = {}
    for page_id in page_order:
        lifetimes = [
            (start, end)
            for start, end in page_lifetimes.get(page_id, ())
            if end > start
        ]
        axis_windows[page_id] = (
            (
                AxisOffsetWindow(
                    start_ms=min(start for start, _end in lifetimes),
                    end_ms=max(end for _start, end in lifetimes),
                    offset=float(axis_offsets.get(page_id, 0.0)),
                ),
            )
            if lifetimes
            else ()
        )
    resolved = {
        track_index: tuple(
            (
                int(window.start_ms),
                int(window.end_ms),
                float(window.offset) if style.vertical else 0.0,
                0.0 if style.vertical else float(window.offset),
            )
            for window in axis_windows.get(page_id, ())
        )
        for track_index, page_id in line_to_page.items()
    }
    _PAGE_PLACEMENT_CACHE[cache_key] = resolved
    _PAGE_PLACEMENT_CACHE.move_to_end(cache_key)
    while len(_PAGE_PLACEMENT_CACHE) > _PAGE_PLACEMENT_CACHE_MAX:
        _PAGE_PLACEMENT_CACHE.popitem(last=False)
    return {key: tuple(value) for key, value in resolved.items()}


def resolved_page_offsets_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    *,
    t_ms: int | None = None,
) -> dict[int, tuple[float, float]]:
    """Resolve the page translation active at ``t_ms`` for each track line.

    ``t_ms=None`` keeps the older inspection API useful by returning the first
    placement interval for each line.  Rendering consumers always pass the
    current display time.
    """

    windows = resolved_page_offset_windows_for_style(
        logical_w, logical_h, track, style
    )
    resolved: dict[int, tuple[float, float]] = {}
    for track_index, items in windows.items():
        selected: tuple[int, int, float, float] | None = None
        if t_ms is None:
            selected = items[0] if items else None
        else:
            selected = next(
                (
                    item
                    for item in items
                    if item[0] <= int(t_ms) < item[1]
                ),
                None,
            )
        if selected is not None:
            resolved[track_index] = (selected[2], selected[3])
    return resolved


def _display_line_vertical_envelope(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    style: Style,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    *,
    layout_cache_sig: tuple | None,
) -> tuple[int, int] | None:
    """Return the final static Y ink interval used for placement checks."""

    bounds = _display_line_horizontal_ink_rect(
        logical_w,
        logical_h,
        track,
        display_line,
        style,
        baselines,
        line_layouts,
        layout_cache_sig=layout_cache_sig,
    )
    if bounds is None:
        return None
    return bounds[1], bounds[3]


def _display_line_horizontal_ink_rect(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    style: Style,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    *,
    layout_cache_sig: tuple | None,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the final static horizontal-line ink rectangle.

    Entry/exit and per-character animation trajectories intentionally do not
    enlarge this box.  Main glyphs, Ruby and their visual decorations do.
    """

    line = display_line.line
    line_style = _style_for_line(style, display_line.line)
    line_layout = line_layouts.get(id(line))
    has_role_labels = _line_has_role_labels(line)
    line_x = (
        line_layout.text_x
        if line_layout is not None and not has_role_labels
        else None
    )
    baseline_y = (
        line_layout.baseline_y
        if line_layout is not None
        else baselines.get(display_line.lane, logical_h // 2)
    )
    lane = display_line.lane if line_style.dual_line_layout else None
    # 碰撞避让要反复收缩、反复重量，同一行的墨迹在一轮里会被问上十来遍（真实工程
    # 56 行问出 627 次）。结果只取决于下面这几项，排版区间内它们不变，缓存住。
    cache = getattr(_LAYOUT_PASS, "ink_rects", None)
    cache_key = None
    if cache is not None:
        cache_key = (
            logical_w,
            logical_h,
            id(track),
            id(line),
            id(line_style),
            baseline_y,
            line_x,
            lane,
            layout_cache_sig,
            include_glow,
        )
        if cache_key in cache:
            return cache[cache_key]
    layout = _layout_line(
        track,
        line,
        line_style,
        logical_w,
        logical_h,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        cache_sig=layout_cache_sig,
    )
    rect = None if layout is None else _line_static_ink_rect(
        layout, include_glow=include_glow
    )
    if cache_key is not None:
        cache[cache_key] = rect
        # 键里有 id()：对象被回收后地址会被复用，这里把它们按住。
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.styles.append(line_style)
    return rect


def _line_static_ink_rect(
    layout: _LineLayout,
    *,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the painted main/Ruby rectangle without layout line spacing."""

    bounds: list[tuple[float, float, float, float]] = []
    for run in _text_glyph_runs(layout.text_layout, layout.has_inline_styles):
        path = _glyph_run_path(run, layout.baseline_y)
        visual = _painted_path_ink_rect(
            path,
            run[0].style,
            ruby=False,
            include_glow=include_glow,
        )
        if visual is not None:
            bounds.append(visual)
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        rect = _bitmap_guide_target_rect(glyph, layout.baseline_y)
        if rect is not None and not rect.isEmpty():
            bounds.append(
                (
                    float(rect.left()),
                    float(rect.top()),
                    float(rect.right()),
                    float(rect.bottom()),
                )
            )

    for ruby_layout in layout.ruby_layouts:
        ruby_style = ruby_layout.style
        ruby_font = ruby_layout.font or layout.ruby_font
        ruby_metrics = ruby_layout.metrics or layout.ruby_metrics
        if ruby_metrics is None:
            continue
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(ruby_layout.ruby.reading)))
            if layout.rtl
            else ruby_layout.ruby.reading
        )
        path, _rect = _ruby_text_path_and_rect(
            reading,
            ruby_font,
            ruby_metrics,
            ruby_layout.x,
            ruby_layout.baseline_y,
            ruby_layout.target_width,
            ruby_style,
            base_text=ruby_layout.ruby.kanji,
        )
        visual = _painted_path_ink_rect(
            path,
            ruby_style,
            ruby=True,
            include_glow=include_glow,
        )
        if visual is not None:
            bounds.append(visual)

    if not bounds:
        return None
    return (
        int(math.floor(min(left for left, _top, _right, _bottom in bounds))),
        int(math.floor(min(top for _left, top, _right, _bottom in bounds))),
        int(math.ceil(max(right for _left, _top, right, _bottom in bounds))),
        int(math.ceil(max(bottom for _left, _top, _right, bottom in bounds))),
    )


def _line_static_vertical_ink_bounds(
    layout: _LineLayout,
    *,
    include_glow: bool = True,
) -> tuple[int, int] | None:
    """Return the static painted Y envelope without layout line spacing.

    Placement collisions use actual main-text/Ruby glyph geometry plus stroke,
    shadow and glow extents.  Font metric cells, ``line_gap_px`` and every
    entry/exit or per-character animation trajectory are deliberately absent.
    The solver adds the overlapped layout's line gap exactly once, after these
    per-line ink bounds have been measured.
    """

    bounds = _line_static_ink_rect(layout, include_glow=include_glow)
    if bounds is None:
        return None
    return bounds[1], bounds[3]


def _painted_path_ink_rect(
    path: QPainterPath,
    style: Style,
    *,
    ruby: bool,
    include_glow: bool = True,
) -> tuple[float, float, float, float] | None:
    """Return one path's static painted rectangle for both colour states."""

    rect = path.boundingRect()
    if rect.isEmpty():
        return None
    if ruby:
        stroke_width = _ruby_stroke_width(style)
        stroke2_width = _ruby_stroke2_width(style)
        decoration = _ruby_decoration_kind(style)
        shadow_dx = _ruby_shadow_dx(style)
        shadow_dy = _ruby_shadow_dy(style)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _ruby_glow_radius(style, after=after),
            )
            for after in (False, True)
        )
    else:
        stroke_width = max(int(style.stroke_width_px), 0)
        stroke2_width = _main_stroke2_width(style)
        decoration = style.decoration_kind
        shadow_dx = int(style.shadow_offset_x)
        shadow_dy = int(style.shadow_offset_y)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )

    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    extent = max(
        stroke_extent,
        glow_extent if include_glow and decoration == "glow" else 0,
    )
    left = float(rect.left()) - extent
    top = float(rect.top()) - extent
    right = float(rect.right()) + extent
    bottom = float(rect.bottom()) + extent
    if decoration == "shadow":
        left += min(shadow_dx, 0)
        right += max(shadow_dx, 0)
        top += min(shadow_dy, 0)
        bottom += max(shadow_dy, 0)
    return left, top, right, bottom


def _painted_path_vertical_bounds(
    path: QPainterPath,
    style: Style,
    *,
    ruby: bool,
    include_glow: bool = True,
) -> tuple[float, float] | None:
    """Return one path's painted static Y bounds for both karaoke colour states."""

    rect = path.boundingRect()
    if rect.isEmpty():
        return None
    if ruby:
        stroke_width = _ruby_stroke_width(style)
        stroke2_width = _ruby_stroke2_width(style)
        decoration = _ruby_decoration_kind(style)
        shadow_dy = _ruby_shadow_dy(style)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _ruby_glow_radius(style, after=after),
            )
            for after in (False, True)
        )
    else:
        stroke_width = max(int(style.stroke_width_px), 0)
        stroke2_width = _main_stroke2_width(style)
        decoration = style.decoration_kind
        shadow_dy = int(style.shadow_offset_y)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )

    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    extent = max(
        stroke_extent,
        glow_extent if include_glow and decoration == "glow" else 0,
    )
    top = float(rect.top()) - extent
    bottom = float(rect.bottom()) + extent
    if decoration == "shadow" and shadow_dy:
        top += min(shadow_dy, 0)
        bottom += max(shadow_dy, 0)
    return top, bottom


def _display_line_horizontal_envelope(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    line_style: Style,
) -> tuple[int, int] | None:
    """Return the final static X ink interval for a vertical lyric line."""

    bounds = _display_line_vertical_ink_rect(
        logical_w,
        logical_h,
        track,
        display_line,
        line_style,
    )
    if bounds is None:
        return None
    return bounds[0], bounds[2]


def _display_line_vertical_ink_rect(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    line_style: Style,
    *,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the final static vertical-line ink rectangle."""

    column = _resolve_vertical_columns(
        logical_w, track, [display_line], line_style
    ).get(display_line.lane)
    render_line = _line_with_guide_symbol(display_line.line)
    layout = _layout_vertical_line(
        track,
        render_line,
        line_style,
        logical_w,
        logical_h,
        column_x=column,
        source_line=display_line.line,
    )
    if layout is None:
        return None
    path_bounds = layout.text_path.boundingRect()
    left = min(float(layout.line_rect.left()), float(path_bounds.left()))
    top = min(float(layout.line_rect.top()), float(path_bounds.top()))
    right = max(float(layout.line_rect.right()), float(path_bounds.right()))
    bottom = max(float(layout.line_rect.bottom()), float(path_bounds.bottom()))
    if layout.active_rubies:
        ruby_metrics = QFontMetrics(_build_ruby_font(line_style))
        ruby_cell_w = _vertical_cell_width(ruby_metrics)
        right = max(
            right,
            float(
                layout.column_x
                + layout.cell_w / 2
                + int(line_style.ruby_gap_px)
                + ruby_cell_w
            ),
        )
    extent_x = _line_effect_extent(
        line_style,
        vertical_axis=False,
        include_glow=include_glow,
    )
    extent_y = _line_effect_extent(
        line_style,
        vertical_axis=True,
        include_glow=include_glow,
    )
    left -= extent_x
    right += extent_x
    top -= extent_y
    bottom += extent_y
    return (
        int(math.floor(left)),
        int(math.floor(top)),
        int(math.ceil(right)),
        int(math.ceil(bottom)),
    )


def _line_effect_extent(
    style: Style,
    *,
    vertical_axis: bool,
    include_glow: bool = True,
) -> int:
    stroke2 = _main_stroke2_width(style)
    extent = _visual_stroke_extent(style.stroke_width_px, stroke2)
    if include_glow and style.decoration_kind == "glow":
        extent = max(
            extent,
            _glow_extent(
                style.stroke_width_px,
                stroke2,
                max(
                    _glow_radius(style, after=False),
                    _glow_radius(style, after=True),
                ),
            ),
        )
    elif style.decoration_kind == "shadow":
        shadow = style.shadow_offset_y if vertical_axis else style.shadow_offset_x
        extent += abs(int(shadow))
    return max(int(extent), 0)


def _line_envelope_sample_times(
    display_line: DisplayLine, style: Style
) -> tuple[int, ...]:
    start = int(display_line.display_start_ms)
    end = max(int(display_line.display_end_ms), start + 1)
    sing_start = _line_start_ms(display_line.line)
    sing_end = _line_end_ms(display_line.line)
    candidates = {
        start,
        start + 1,
        start + max(int(style.entry_lead_ms), 0) // 2,
        start + max(int(style.entry_lead_ms), 0),
        sing_start,
        (sing_start + sing_end) // 2,
        max(sing_end - 1, start),
        end - max(int(style.exit_fade_ms), 0),
        end - max(int(style.exit_fade_ms), 0) // 2,
        end - 1,
    }
    return tuple(sorted(min(max(value, start), end - 1) for value in candidates))


def _transform_vertical_bounds(
    top: int,
    bottom: int,
    logical_h: int,
    style: Style,
) -> tuple[int, int]:
    scale = max(style.viewport_scale_pct, 1) / 100.0
    offset_y = style.viewport_offset_y
    if scale == 1.0 and offset_y == 0:
        return top, bottom
    _frac_x, frac_y = _VIEWPORT_PIVOT_FRACTIONS.get(
        style.viewport_align, _VIEWPORT_PIVOT_FRACTIONS["center"]
    )
    pivot_y = logical_h * frac_y
    mapped_top = offset_y + pivot_y + (top - pivot_y) * scale
    mapped_bottom = offset_y + pivot_y + (bottom - pivot_y) * scale
    return int(math.floor(mapped_top)), int(math.ceil(mapped_bottom))


def _resolve_sayatoo_line_layouts(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    t_ms: int,
    style: Style,
) -> dict[int, _SayatooLineLayout]:
    """Resolve row-local union bounds before applying row alignment.

    Sayatoo's CoreSuites aligns the complete ``LineDrawingData``.  Signal modules
    therefore contribute to the line width before ``row1/row2`` alignment is
    applied, instead of being painted later in screen coordinates.
    """
    # 每次要量整轨墨迹都会先解一遍行布局（真实工程一次 28ms），而碰撞避让一轮里
    # 要量好几遍。同样的入参在排版区间内答案不变，缓存住。
    cache = getattr(_LAYOUT_PASS, "sayatoo_layouts", None)
    cache_key = None
    if cache is not None:
        cache_key = (
            img_w,
            img_h,
            id(track),
            id(style),
            int(t_ms),
            tuple(
                (id(dl.line), dl.lane) for dl in display_lines
            ),
            tuple(sorted(baselines.items())),
        )
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    layouts: dict[int, _SayatooLineLayout] = {}
    signal_metrics = _signal_layout_metrics(style) if style.lit_enabled else None
    for display_line in display_lines:
        line = display_line.line
        if line.is_blank or not line.chars:
            continue
        line_style = _style_for_line(style, line)
        render_line = _line_with_guide_symbol(line)
        font = _build_font(line_style)
        metrics = QFontMetrics(font)
        latin_font = _build_latin_font(line_style)
        font_for = _make_font_for(line_style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        active_rubies = _active_rubies_for_line(track.rubies, line)
        ruby_metrics = QFontMetrics(_build_ruby_font(line_style)) if active_rubies else None
        if _line_has_role_labels(render_line):
            measure_layout = _build_role_text_layout(
                render_line, line_style, x0=0, baseline_y=0
            )
            char_widths, _measure_ranges = _role_char_geometry_by_index(
                render_line, measure_layout
            )
        else:
            char_widths = [
                (
                    _vector_glyph_width(
                        c.vector_glyph,
                        _style_for_role_in_layout(line_style, c.role_label),
                    )
                    if c.vector_glyph is not None
                    else _char_layout_width(
                        c.text, font, metrics, latin_metrics, font_for, line_style
                    )
                )
                for c in render_line.chars
            ]
        char_gaps, ruby_left, ruby_right = _ruby_char_gaps(
            render_line, char_widths, active_rubies, line_style
        )
        text_w = _line_text_width(char_widths, line_style) + sum(char_gaps)
        # 行盒左右不给描边留位（见 _line_total_width），只让 ruby 溢出撑开。
        left_ext = ruby_left
        right_ext = ruby_right
        text_line_w = max(int(round(text_w)) + left_ext + right_ext, 1)
        center_line = _line_center_override(track, line, line_style)
        signal_x: float | None = None
        if (
            signal_metrics is not None
            and _line_has_active_signal(line, t_ms, line_style)
        ):
            # Sayatoo CoreSuites aligns the *union* of the lyric text box and the
            # signal-module bounds (the LineDrawingData width), then applies
            # row1/row2 alignment to that union.  So an enabled guide cue widens
            # the line: under left/centre alignment the signal takes the row
            # anchor and the lyric text shifts right by the group width; under
            # right alignment the text stays put and the signal extends left.
            #
            # The union uses the indicator's *offset-free* span so that the
            # volume/lit X offset nudges only the indicator, not the text layout:
            # ``volume_offset_x`` therefore moves the bars (``signal_x``) while
            # ``text_x`` stays put, which is what the offset control should do.
            draw_left = _signal_local_x(signal_metrics, line_style)
            natural_left = draw_left - _signal_offset_x(line_style)
            natural_right = natural_left + signal_metrics.group_width
            union_left = min(-float(left_ext), natural_left)
            union_right = max(float(text_w) + right_ext, natural_right)
            union_w = max(int(round(union_right - union_left)), 1)
            union_x = _resolve_line_x_smart(
                img_w, union_w, track, line, line_style, display_line.lane,
                center_override=center_line,
            )
            text_x = float(union_x) - union_left
            signal_x = text_x + draw_left
        else:
            text_x = float(
                _resolve_line_x_smart(
                    img_w, text_line_w, track, line, line_style, display_line.lane,
                    center_override=center_line,
                )
                + left_ext
            )
        if int(getattr(line, "layout_index", 0) or 0) > 0:
            # 行引用了额外布局 → 垂直几何（锚点/余白/行距/行数）按该布局单独解析。
            baseline_y = _resolve_display_baselines(
                img_h, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            baseline_y = baselines.get(display_line.lane)
        if baseline_y is None:
            baseline_y = _resolve_baseline_y(metrics, img_h, line_style, ruby_metrics)
        resolved = _SayatooLineLayout(
            baseline_y=baseline_y,
            text_x=int(round(text_x)),
            line_style=line_style,
            metrics=metrics,
            total_w=text_w,
            signal_x=signal_x,
            signal_y=(
                _signal_lit_y(
                    baseline_y,
                    metrics,
                    signal_metrics.size,
                    line_style,
                    signal_metrics.stroke_extent,
                )
                if signal_metrics is not None and signal_x is not None
                else None
            ),
        )
        # Identity is authoritative for rendering because overlapping pages can
        # reuse a lane.  Keep the lane alias for older helpers/tests that inspect
        # one page at a time; the renderer itself never consumes that alias.
        layouts[id(display_line.line)] = resolved
        layouts[display_line.lane] = resolved
    if cache_key is not None:
        cache[cache_key] = layouts
        # 键里有 id()：对象被回收后地址会被复用，这里把它们按住。
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.styles.append(style)
        _LAYOUT_PASS.lines.extend(dl.line for dl in display_lines)
    return layouts


def _signal_layout_metrics(style: Style) -> _SignalLayoutMetrics:
    is_volume = style.lit_style == "volume"
    if is_volume:
        geometry = _volume_signal_geometry(style)
        count = geometry.count
        size = geometry.size
        tracking = geometry.column_spacing
        item_width = geometry.column_width
        stroke_extent = geometry.stroke_extent
        group_width = geometry.group_width
    else:
        count = max(1, min(int(style.lit_number), 8))
        size = max(int(style.lit_size), 1)
        tracking = max(int(style.lit_tracking), 0)
        item_width = size
        stroke_extent = _signal_stroke_extent(style, is_volume=False)
        group_width = count * size + max(count - 1, 0) * (size * 0.5 + tracking)
    return _SignalLayoutMetrics(
        count=count,
        size=size,
        item_width=item_width,
        tracking=tracking,
        stroke_extent=stroke_extent,
        group_width=float(group_width),
        is_volume=is_volume,
    )


def _line_has_active_signal(line: TimingLine, t_ms: int, style: Style) -> bool:
    duration = max(int(style.signals_duration_ms), 0)
    active_duration = max(duration - max(int(style.lit_waiting_time_ms), 0), 0)
    if active_duration <= 0:
        return False
    signal_end = _line_start_ms(line) + int(style.lit_time_offset_ms)
    display_end = _line_end_ms(line) + max(int(style.line_tail_ms), 0)
    return signal_end - active_duration <= t_ms <= display_end


def _signal_local_x(metrics: _SignalLayoutMetrics, style: Style) -> float:
    if metrics.is_volume:
        return float(style.volume_offset_x) - metrics.group_width
    return float(style.lit_offset_x)


def _signal_offset_x(style: Style) -> float:
    """User X offset for the active indicator (moves only the indicator)."""
    return float(style.volume_offset_x if style.lit_style == "volume" else style.lit_offset_x)


def _volume_signal_geometry(style: Style) -> _VolumeSignalGeometry:
    count = max(1, min(int(style.volume_column_count), 16))
    size = max(int(style.volume_size), 1)
    column_width = max(int(style.volume_column_width), 1)
    column_spacing = max(int(style.volume_column_spacing), 0)
    spacing = max(0, int(getattr(style, "volume_spacing", 0)))
    stroke_extent = _signal_stroke_extent(style, is_volume=True)
    pitch = float(column_width + column_spacing + 2 * stroke_extent)
    local_left = float(style.volume_offset_x) - stroke_extent
    group_width = float(count * pitch + spacing - column_spacing)

    ratio = max(float(style.volume_ratio), 0.01)
    base_factor = ratio
    depth_factor = 1.0
    if 1.0 < ratio:
        depth_factor = 1.0 / ratio
        base_factor = 1.0
    front_height = base_factor * size
    height_delta = (
        0.0
        if count < 2
        else ((depth_factor - base_factor) * size) / float(count - 1)
    )
    align_base_shift = 0.0
    align_delta_shift = 0.0
    align = int(style.volume_align)
    if align == 1:
        align_base_shift = (1.0 - base_factor) * size * 0.5
        align_delta_shift = -height_delta * 0.5
    elif align == 2:
        align_base_shift = (1.0 - base_factor) * size
        align_delta_shift = -height_delta

    return _VolumeSignalGeometry(
        count=count,
        size=size,
        column_width=column_width,
        column_spacing=column_spacing,
        spacing=spacing,
        stroke_extent=stroke_extent,
        local_left=local_left,
        group_width=group_width,
        pitch=pitch,
        front_height=front_height,
        height_delta=height_delta,
        align_base_shift=align_base_shift,
        align_delta_shift=align_delta_shift,
    )


def _volume_signal_column_rects(
    x: float,
    y: float,
    geometry: _VolumeSignalGeometry,
) -> list[QRectF]:
    return [
        QRectF(
            float(x + geometry.stroke_extent + index * geometry.pitch),
            float(
                y
                + geometry.stroke_extent
                + geometry.align_base_shift
                + index * geometry.align_delta_shift
            ),
            float(geometry.column_width),
            float(max(geometry.front_height + index * geometry.height_delta, 1.0)),
        )
        for index in range(geometry.count)
    ]


def _paint_signal_lits(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    t_ms: int,
    style: Style,
    *,
    line_layouts: dict[int, _SayatooLineLayout] | None = None,
    line_offsets: dict[int, tuple[float, float]] | None = None,
) -> None:
    """Paint Sayatoo-style ``SignalsLits`` guide cues.

    Sayatoo exposes this module as ``SignalsLits.sx`` with ``lit.*`` fields and
    ``signals.duration``. Nicokara LRC has no separate signal track, so each
    displayed lyric line emits one countdown cue before its first sung character.
    The cue is anchored to the lyric line, not to the viewport.
    """
    layers = _signal_layer_stack(
        track,
        display_lines,
        baselines,
        img_w,
        img_h,
        t_ms,
        style,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
    )
    if not layers:
        return
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=img_w, logical_h=img_h),
        layers,
    )


def _signal_layer_stack(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    img_w: int,
    img_h: int,
    t_ms: int,
    style: Style,
    *,
    line_layouts: dict[int, _SayatooLineLayout] | None = None,
    line_offsets: dict[int, tuple[float, float]] | None = None,
) -> list:
    if not style.lit_enabled:
        return []
    is_volume = style.lit_style == "volume"
    count = (
        max(1, min(int(style.volume_column_count), 16))
        if is_volume
        else max(1, min(int(style.lit_number), 8))
    )
    size = max(int(style.volume_size if is_volume else style.lit_size), 1)
    tracking = max(int(style.volume_column_spacing if is_volume else style.lit_tracking), 0)
    item_width = max(int(style.volume_column_width), 1) if is_volume else size
    stroke_extent = _signal_stroke_extent(style, is_volume=is_volume)
    groups = _signal_lit_groups(
        track,
        display_lines,
        baselines,
        img_w,
        img_h,
        t_ms,
        style,
        count,
        size,
        item_width,
        tracking,
        stroke_extent,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
    )
    if not groups:
        return []
    fill = _valid_color(style.lit_fill_color, "#0000FF")
    stroke = _valid_color(style.lit_stroke_color, "#FFFFFF")
    stroke_width = max(int(style.lit_stroke_width), 0)
    soften = max(int(style.lit_stroke_soften), 0)
    group_opacity = max(0, min(int(style.lit_opacity_pct), 100)) / 100.0
    edge_brightness = max(0, min(int(style.lit_edge_brightness_pct), 100)) / 100.0
    return [
        _SignalLitsLayer(
            group=group,
            style=style,
            count=count,
            size=size,
            tracking=tracking,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
            soften=soften,
            group_opacity=group_opacity,
            edge_brightness=edge_brightness,
            is_volume=is_volume,
            z_index=index,
        )
        for index, group in enumerate(groups)
    ]


@dataclass(frozen=True)
class _SignalLitsLayer:
    """Layer wrapper for one Sayatoo SignalsLits group."""

    group: _SignalLitGroup
    style: Style
    count: int
    size: int
    tracking: int
    fill: QColor
    stroke: QColor
    stroke_width: int
    soften: int
    group_opacity: float
    edge_brightness: float
    is_volume: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_SignalLitsLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("Signal layers are dynamic in the QPainter backend")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        if self.group_opacity <= 0.0:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.group_opacity)
            group = self.group
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * group.opacity)
                if self.is_volume:
                    _draw_volume_lit_group(painter, group, self.style)
                else:
                    _paint_shape_signal_group(painter, self)
            finally:
                painter.restore()
        finally:
            painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        if self.group_opacity <= 0.0 or self.group.opacity <= 0.0:
            return None
        if self.is_volume:
            return _volume_signal_vertical_bounds(self.group, self.style)
        return _shape_signal_vertical_bounds(self)


def _paint_shape_signal_group(painter: QPainter, layer: _SignalLitsLayer) -> None:
    group = layer.group
    for index in range(layer.count):
        if group.active_index is None or index > group.active_index:
            continue
        is_active = index == group.active_index
        dx = group.dx if is_active else 0.0
        dy = group.dy if is_active else 0.0
        x = group.x + dx + index * (layer.size * 1.5 + layer.tracking)
        rect = QRectF(x, group.y + dy, float(layer.size), float(layer.size))
        painter.save()
        try:
            if is_active:
                painter.setOpacity(painter.opacity() * group.active_opacity)
            _draw_lit_shape(
                painter,
                rect,
                layer.style,
                layer.fill,
                layer.stroke,
                layer.stroke_width,
                layer.soften,
                layer.edge_brightness if is_active else 0.0,
            )
        finally:
            painter.restore()


def _volume_signal_vertical_bounds(
    group: _SignalLitGroup,
    style: Style,
) -> tuple[int, int] | None:
    geometry = _volume_signal_geometry(style)
    rects = _volume_signal_column_rects(group.x, group.y, geometry)
    if not rects:
        return None
    pad = max(int(style.lit_stroke_width), 0) + 2
    top = min(rect.top() for rect in rects) - pad
    bottom = max(rect.bottom() for rect in rects) + pad
    return int(math.floor(top)), int(math.ceil(bottom))


def _shape_signal_vertical_bounds(layer: _SignalLitsLayer) -> tuple[int, int] | None:
    group = layer.group
    if group.active_index is None or group.active_index < 0:
        return None
    rects: list[QRectF] = []
    for index in range(layer.count):
        if index > group.active_index:
            continue
        is_active = index == group.active_index
        dx = group.dx if is_active else 0.0
        dy = group.dy if is_active else 0.0
        x = group.x + dx + index * (layer.size * 1.5 + layer.tracking)
        rect = QRectF(x, group.y + dy, float(layer.size), float(layer.size))
        rects.append(rect)
        if layer.style.lit_shadow:
            rects.append(
                rect.translated(
                    max(rect.width() * 0.08, 1.0),
                    max(rect.height() * 0.08, 1.0),
                )
            )
    if not rects:
        return None
    pad = _signal_stroke_extent(layer.style, is_volume=False) + 2
    top = min(rect.top() for rect in rects) - pad
    bottom = max(rect.bottom() for rect in rects) + pad
    return int(math.floor(top)), int(math.ceil(bottom))


def _signal_lit_groups(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    img_w: int,
    img_h: int,
    t_ms: int,
    style: Style,
    count: int,
    size: int,
    item_width: int,
    tracking: int,
    stroke_extent: float = 0.0,
    *,
    line_layouts: dict[int, _SayatooLineLayout] | None = None,
    line_offsets: dict[int, tuple[float, float]] | None = None,
) -> list[_SignalLitGroup]:
    duration = max(int(style.signals_duration_ms), 0)
    if duration <= 0:
        return []
    active_duration = max(duration - max(int(style.lit_waiting_time_ms), 0), 0)
    if active_duration <= 0:
        return []
    groups: list[_SignalLitGroup] = []
    time_offset = int(style.lit_time_offset_ms)
    if style.lit_style == "volume":
        group_width = _volume_signal_geometry(style).group_width
    else:
        group_width = count * size + max(count - 1, 0) * (size * 0.5 + tracking)
    for display_line in display_lines:
        line = display_line.line
        if line.is_blank or not line.chars:
            continue
        line_layout = (
            line_layouts.get(id(display_line.line))
            if line_layouts is not None
            else None
        )
        if line_layout is not None:
            line_style = line_layout.line_style
            metrics = line_layout.metrics
            total_w = line_layout.total_w
            baseline_y = line_layout.baseline_y
        else:
            line_style = _style_for_line(style, line)
            font = _build_font(line_style)
            metrics = QFontMetrics(font)
            latin_font = _build_latin_font(line_style)
            font_for = _make_font_for(line_style, font, latin_font)
            latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
            active_rubies = _active_rubies_for_line(track.rubies, line)
            ruby_metrics = QFontMetrics(_build_ruby_font(line_style)) if active_rubies else None
            render_line = _line_with_guide_symbol(line)
            char_widths = [
                (
                    _vector_glyph_width(
                        c.vector_glyph,
                        _style_for_role_in_layout(line_style, c.role_label),
                    )
                    if c.vector_glyph is not None
                    else _char_layout_width(
                        c.text, font, metrics, latin_metrics, font_for, line_style
                    )
                )
                for c in render_line.chars
            ]
            total_w = _line_text_width(char_widths, line_style)
            baseline_y = baselines.get(display_line.lane)
            if baseline_y is None:
                baseline_y = _resolve_baseline_y(metrics, img_h, line_style, ruby_metrics)
        if total_w <= 0:
            continue

        signal_end = _line_start_ms(line) + time_offset
        active_start = signal_end - active_duration
        display_end = display_line.display_end_ms
        if display_end is None:
            display_end = _line_end_ms(line) + max(int(line_style.line_tail_ms), 0)
        if not (active_start <= t_ms <= display_end):
            continue

        elapsed = max(t_ms - active_start, 0)
        if style.lit_style == "volume":
            elapsed = min(elapsed, max(active_duration - 1, 0))
        if style.lit_style == "volume":
            active_index, phase, opacity = _volume_signal_state(
                elapsed, active_duration, count, line_style
            )
            active_opacity, dx, dy = 1.0, 0.0, 0.0
        else:
            active_index, phase = _shape_active_index_and_phase(elapsed, active_duration, count)
            active_opacity, dx, dy = _lit_extinguish_transition_state(phase, line_style)
            opacity = 1.0

        x = (
            line_layout.signal_x
            if line_layout is not None and line_layout.signal_x is not None
            else _signal_lit_x(img_w, group_width, line_style, stroke_extent)
        )
        y = (
            line_layout.signal_y
            if line_layout is not None and line_layout.signal_y is not None
            else _signal_lit_y(baseline_y, metrics, size, line_style, stroke_extent)
        )
        offset_x, offset_y = (
            line_offsets.get(id(line), (0.0, 0.0))
            if line_offsets is not None
            else (0.0, 0.0)
        )
        x += offset_x
        y += offset_y
        groups.append(
            _SignalLitGroup(
                x=x,
                y=y,
                elapsed_ms=elapsed,
                duration_ms=active_duration,
                active_index=active_index,
                opacity=opacity,
                active_opacity=active_opacity,
                dx=dx,
                dy=dy,
                phase=phase,
            )
        )
    return groups


def _signal_lit_y(
    baseline_y: int,
    metrics: QFontMetrics,
    size: int,
    style: Style,
    stroke_extent: float = 0.0,
) -> float:
    if style.lit_style == "volume":
        # ``text_metric`` is the distance from the baseline up to the text's
        # visual mid-line. The volume group is centred on that mid-line, so the
        # term is subtracted (screen y grows downward): a positive metric lifts
        # the group above the baseline onto the characters. Adding it instead
        # dropped the whole group ~``text_metric`` below the baseline.
        text_metric = (metrics.height() * 0.5) - metrics.descent()
        return float(
            baseline_y
            + style.volume_offset_y
            - stroke_extent
            - size * 0.5
            - text_metric
        )

    return float(baseline_y + style.lit_offset_y - metrics.ascent() - size)


def _active_lit_indices(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    t_ms: int,
    style: Style,
    count: int,
) -> set[int]:
    is_volume = style.lit_style == "volume"
    groups = _signal_lit_groups(
        track,
        display_lines,
        {display_line.lane: 0 for display_line in display_lines},
        1920,
        1080,
        t_ms,
        style,
        count,
        max(int(style.volume_size if is_volume else style.lit_size), 1),
        max(int(style.volume_column_width if is_volume else style.lit_size), 1),
        max(int(style.volume_column_spacing if is_volume else style.lit_tracking), 0),
        _signal_stroke_extent(style, is_volume=is_volume),
    )
    active: set[int] = set()
    for group in groups:
        if group.opacity > 0 and group.active_index is not None and group.active_index >= 0:
            active.add(group.active_index)
    return active


def _signal_lit_x(
    img_w: int, group_width: int | float, style: Style, stroke_extent: float = 0.0
) -> float:
    """Fallback signal x used when no Sayatoo union layout is available.

    The normal horizontal paint path resolves ``LineDrawingData``-style union
    bounds in ``_resolve_sayatoo_line_layouts`` and passes ``signal_x`` through
    ``_SignalLitGroup``.  This helper only keeps direct low-level callers
    bounded inside the viewport.
    """
    offset_x = style.volume_offset_x if style.lit_style == "volume" else style.lit_offset_x
    x = float(style.horizontal_margin_px + offset_x)
    if style.lit_style == "volume":
        x -= stroke_extent
    return max(0.0, min(x, float(max(img_w - group_width, 0))))


def _shape_active_index_and_phase(
    elapsed: int, duration: int, count: int
) -> tuple[int, float]:
    if duration <= 0 or count <= 1:
        return 0, 1.0
    if elapsed >= duration:
        return -1, 1.0
    raw = ((duration - max(elapsed, 0)) * count) / duration
    active_index = max(0, min(count - 1, int(raw)))
    phase = raw - active_index
    return active_index, max(0.0, min(phase, 1.0))


def _volume_active_index_and_phase(
    elapsed: int, duration: int, count: int
) -> tuple[int, float]:
    if duration <= 0 or count <= 1:
        return 0, 1.0
    raw = (count * max(elapsed, 0)) / duration
    active_index = max(0, min(count - 1, int(raw)))
    phase = raw - active_index
    if active_index == count - 1 and elapsed >= duration:
        phase = 1.0
    return active_index, max(0.0, min(phase, 1.0))


def _volume_signal_state(
    elapsed: int, duration: int, count: int, style: Style
) -> tuple[int, float, float]:
    if duration <= 0:
        return -1, 0.0, 0.0
    times = max(int(style.volume_flash_times), 0)
    flash_ratio = max(float(style.volume_flash_duration_ratio), 0.0)
    if times <= 0 or flash_ratio <= 0.0:
        active_index, phase = _volume_active_index_and_phase(elapsed, duration, count)
        return active_index, phase, 1.0

    fill_duration = duration / (times * flash_ratio + 1.0)
    flash_duration = max(duration - fill_duration, 0.0)
    if elapsed < flash_duration:
        return -1, 0.0, _volume_flash_alpha(elapsed, int(max(flash_duration, 1.0)), style)

    fill_elapsed = int(max(elapsed - flash_duration, 0.0))
    active_index, phase = _volume_active_index_and_phase(fill_elapsed, int(max(fill_duration, 1.0)), count)
    return active_index, phase, 1.0


def _lit_transition_state(phase: float, style: Style) -> tuple[float, float, float]:
    mode = style.lit_transition_mode
    ratio = max(0, min(int(style.lit_transition_ratio_pct), 100)) / 100.0
    progress = 1.0 if ratio <= 0 else (phase - (1.0 - ratio)) / ratio
    progress = max(0.0, min(float(progress), 1.0))
    if mode == "fade":
        return progress, 0.0, 0.0
    if mode == "slide":
        distance = max(int(style.lit_transition_distance), 0) * (1.0 - progress)
        radians = math.radians(float(style.lit_transition_angle_deg))
        return progress, -math.cos(radians) * distance, -math.sin(radians) * distance
    return 1.0, 0.0, 0.0


def _lit_extinguish_transition_state(phase: float, style: Style) -> tuple[float, float, float]:
    opacity, dx, dy = _lit_transition_state(1.0 - phase, style)
    return 1.0 - opacity if style.lit_transition_mode == "fade" else opacity, dx, dy


def _draw_volume_lit_group(
    painter: QPainter,
    group: _SignalLitGroup,
    style: Style,
) -> None:
    fill = _valid_color(style.volume_fill_color, "#FFFFFF")
    stroke = _valid_color(style.volume_stroke_color, "#0000FF")
    overlay_fill = _valid_color(style.volume_overlay_fill_color, "#0000FF")
    overlay_stroke = _valid_color(style.volume_overlay_stroke_color, "#FFFFFF")
    stroke_width = max(int(style.lit_stroke_width), 0)
    geometry = _volume_signal_geometry(style)
    if group.opacity <= 0:
        return

    painter.save()
    try:
        painter.setOpacity(painter.opacity() * group.opacity)
        rects = _volume_signal_column_rects(group.x, group.y, geometry)
        active_index = group.active_index if group.active_index is not None else -1
        for index in range(active_index + 1, geometry.count):
            _draw_volume_column(painter, rects[index], fill, stroke, stroke_width)
        for index in range(0, active_index + 1):
            _draw_volume_column(painter, rects[index], overlay_fill, overlay_stroke, stroke_width)
    finally:
        painter.restore()


def _volume_flash_alpha(elapsed: int, duration: int, style: Style) -> float:
    if duration <= 0 or elapsed < 0:
        return 0.0
    times = max(int(style.volume_flash_times), 0)
    if times == 0:
        return 1.0
    per_flash = duration / times if times else 0.0
    if per_flash <= 0:
        return 1.0
    phase = (elapsed / per_flash) % 1.0
    phase *= 2.0
    if phase > 1.0:
        phase = 2.0 - phase
    transition = max(0.0, min(float(style.volume_transition_ratio_pct) / 100.0, 1.0))
    if transition <= 0:
        return 1.0 - (1.0 if (phase * 2.0 - 1.0) > 0.0 else 0.0)
    fade = ((phase * 3.0 - 1.0) * 0.67) / transition
    fade = max(0.0, min(fade, 1.0))
    return 1.0 - fade


def _signal_stroke_extent(style: Style, *, is_volume: bool) -> float:
    stroke_width = max(int(style.lit_stroke_width), 0)
    soften = 0 if is_volume else max(int(style.lit_stroke_soften), 0)
    return float(stroke_width + soften)


def _draw_volume_column(
    painter: QPainter,
    rect: QRectF,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
) -> None:
    painter.setBrush(QBrush(fill))
    if stroke_width > 0 and stroke.alpha() > 0:
        painter.setPen(QPen(stroke, stroke_width))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    radius = max(min(rect.width(), rect.height()) * 0.22, 1.0)
    painter.drawRoundedRect(rect, radius, radius)


def _draw_lit_shape(
    painter: QPainter,
    rect: QRectF,
    style: Style,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
    soften: int,
    edge_brightness: float,
) -> None:
    if style.lit_shadow:
        shadow = QColor("#000000")
        shadow.setAlphaF(0.35)
        shadow_rect = rect.translated(max(rect.width() * 0.08, 1.0), max(rect.height() * 0.08, 1.0))
        _draw_lit_shape_raw(painter, shadow_rect, style.lit_style, shadow, QColor("#00000000"), 0)
    if soften > 0 and stroke_width > 0:
        soft = QColor(stroke)
        soft.setAlphaF(0.28)
        _draw_lit_shape_raw(painter, rect, style.lit_style, fill, soft, stroke_width + soften)
    _draw_lit_shape_raw(painter, rect, style.lit_style, fill, stroke, stroke_width)
    if edge_brightness > 0:
        highlight = QColor("#FFFFFF")
        highlight.setAlphaF(min(edge_brightness * 0.55, 1.0))
        inset = rect.width() * 0.18
        hi = QRectF(
            rect.left() + inset,
            rect.top() + inset,
            rect.width() * 0.32,
            rect.height() * 0.32,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(highlight))
        painter.drawEllipse(hi)


def _draw_lit_shape_raw(
    painter: QPainter,
    rect: QRectF,
    lit_style: str,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
) -> None:
    painter.setBrush(QBrush(fill))
    if stroke_width > 0 and stroke.alpha() > 0:
        painter.setPen(QPen(stroke, stroke_width))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    if lit_style == "square":
        painter.drawRect(rect)
    elif lit_style == "rounded":
        radius = max(rect.width() * 0.22, 1.0)
        painter.drawRoundedRect(rect, radius, radius)
    else:
        painter.drawEllipse(rect)
def _build_font(style: Style) -> QFont:
    font = QFont(
        resolve_qt_font_family(style.font_family), max(style.font_size_px, 1)
    )
    # QFont 用 PointSize 时 size 是 pt；这里我们当 px 用，强制 setPixelSize
    font.setPixelSize(max(style.font_size_px, 1))
    font.setWeight(_clamp_weight(style.font_weight))
    font.setItalic(style.italic)
    return font


def _latin_font_size(style: Style) -> int:
    """英数轨字号；``None`` 跟随日文轨。"""
    value = style.latin_font_size_px
    return int(value) if value is not None and int(value) > 0 else int(style.font_size_px)


def _latin_font_weight(style: Style) -> int:
    value = style.latin_font_weight
    return int(value) if value is not None and int(value) > 0 else int(style.font_weight)


def _is_n3_latin_text(text: str) -> bool:
    """N3 英数页：ASCII U+0020..007E 加 Latin-1 字母 À..ÿ。"""
    return bool(text) and all(
        "\u0020" <= char <= "\u007e" or "\u00c0" <= char <= "\u00ff"
        for char in text
    )


def _main_script_stroke_style(style: Style, text: str) -> Style:
    """把当前字符对应字体槽的描边参数物化到 Painter 的通用字段。"""
    if _is_n3_latin_text(text):
        width = (
            style.stroke_width_px
            if style.latin_stroke_width_px is None
            or int(style.latin_stroke_width_px) <= 0
            else int(style.latin_stroke_width_px)
        )
        enabled = (
            style.stroke2_enabled
            if style.latin_stroke2_enabled is None
            else bool(style.latin_stroke2_enabled)
        )
        width2 = (
            style.stroke2_width_px
            if style.latin_stroke2_width_px is None
            or int(style.latin_stroke2_width_px) <= 0
            else int(style.latin_stroke2_width_px)
        )
    else:
        width = style.stroke_width_px
        enabled = style.stroke2_enabled
        width2 = style.stroke2_width_px
    effective_width2 = max(int(width2), 0) if enabled else 0
    if (
        int(style.stroke_width_px) == int(width)
        and int(style.stroke2_width_px) == effective_width2
    ):
        return style
    return replace(
        style,
        stroke_width_px=max(int(width), 0),
        stroke2_enabled=True,
        stroke2_width_px=effective_width2,
    )


def _main_stroke2_width(style: Style) -> int:
    return max(int(style.stroke2_width_px), 0) if style.stroke2_enabled else 0


def _build_latin_font(style: Style) -> QFont:
    """英数字体；未单独设置时各参数退回日文轨（行为与单字体一致）。"""
    family = style.font_family_latin or style.font_family
    size = max(_latin_font_size(style), 1)
    font = QFont(resolve_qt_font_family(family), size)
    font.setPixelSize(size)
    font.setWeight(_clamp_weight(_latin_font_weight(style)))
    font.setItalic(style.italic)
    return font


def _make_font_for(style: Style, jp_font: QFont, latin_font: QFont):
    """返回逐字符取字体的回调；无需分离时返回 ``None``（调用方走单字体老路径）。

    ``QPainterPath.addText`` 不遵循 ``setFamilies`` 的回退顺序，所以必须显式按
    字符挑字体：全 ASCII 的字符用英数字体，其余（假名/汉字/标点）用日文字体。
    英数轨的字号 / 字重覆盖也会使两套字体分离（family 相同亦然）。
    """
    if _font_signature(latin_font) == _font_signature(jp_font):
        return None

    def font_for(ch_text: str) -> QFont:
        return latin_font if _is_n3_latin_text(ch_text) else jp_font

    return font_for


def _char_advance(
    ch_text: str,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
) -> int:
    """单字符步进；英数字符用英数字体度量，其余用日文字体度量。

    ``QFontMetrics.horizontalAdvance`` 是 PyQt 绑定调用，**不释放 GIL 且会阻断
    GIL 轮换**——预览 worker 每次编辑要调它数千次，GUI 线程因此被饿住上百毫秒
    （实测:同样时长的纯 Python 负载只让另一个线程等 7ms，换成 PyQt 文字调用后
    涨到 184ms）。一次排版里同一个字符 + 同一套度量的结果是常量，缓存掉。
    """
    cache = getattr(_LAYOUT_PASS, "char_advances", None)
    if cache is None:
        if font_for is not None and _is_n3_latin_text(ch_text):
            return latin_metrics.horizontalAdvance(ch_text)
        return metrics.horizontalAdvance(ch_text)
    use_latin = font_for is not None and _is_n3_latin_text(ch_text)
    source = latin_metrics if use_latin else metrics
    cache_key = (ch_text, id(source))
    hit = cache.get(cache_key)
    if hit is None:
        hit = source.horizontalAdvance(ch_text)
        cache[cache_key] = hit
        # 键里有 id()：存住度量对象，避免回收后地址被复用。
        _LAYOUT_PASS.metrics.append(source)
    return hit


# 逐字 N3 度量（layout width / path left offset）是 (字符, 字体, 少数 style 标量) 的纯函数，
# 但每次都要 QPainterPath.addText + boundingRect + bearing，布局段每帧逐字调用非常贵。
# 按值键 memo：key 不含任何对象 id，track/style 被就地修改也不会取到脏值。
_CHAR_METRIC_CACHE: dict[tuple, tuple[int, float]] = {}
_CHAR_METRIC_CACHE_MAX = 16384


def _font_signature(font: QFont) -> tuple:
    return (font.family(), font.pixelSize(), int(font.weight()), font.italic())


def _char_metric_key(
    ch_text: str,
    glyph_font: QFont,
    advance: int,
    style: Style,
) -> tuple:
    # advance 进 key：它携带了 metrics（英数/日文字体选择）对结果的全部影响。
    return (
        ch_text,
        _font_signature(glyph_font),
        advance,
        bool(style.allow_biting),
        int(style.stroke_width_px),
        int(style.space_width_percent),
        int(style.font_size_px),
    )


def _truncate_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero, matching C# arithmetic."""
    if denominator == 0:
        return 0
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    return sign * (abs(numerator) // abs(denominator))


def _nicokara_layout_width(
    ink_width: int,
    advance: int,
    left_bearing: int,
    right_bearing: int,
    *,
    edge_size: int,
    allow_biting: bool,
) -> int:
    """Approximate NicokaraMaker3's per-glyph layout-box width."""
    advance = max(int(advance), 1)
    left = int(left_bearing)
    right = int(right_bearing)
    if not allow_biting:
        left = max(left, 0)
        right = max(right, 0)
    body_width = _truncate_div(
        max(int(ink_width), 0) * (left + advance + right),
        advance,
    )
    # NicokaraMaker3's DrawWidth includes EdgeSize (the first outline only).
    return max(body_width, 0) + max(int(edge_size), 0)


def _nicokara_char_geometry_left_offset(
    ink_width: int,
    advance: int,
    left_bearing: int,
    *,
    allow_biting: bool,
) -> int:
    """Approximate NicokaraMaker3's CharGeometryLeftOffset."""
    advance = max(int(advance), 1)
    left = int(left_bearing)
    if not allow_biting:
        left = max(left, 0)
    return _truncate_div(max(int(ink_width), 0) * left, advance)


def _char_layout_metrics(
    ch_text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
    style: Style,
) -> tuple[int, float]:
    """(layout width, path left offset) using NicokaraMaker3-like rules, memoized."""
    is_latin_glyph = font_for is not None and _is_n3_latin_text(ch_text)
    glyph_font = font_for(ch_text) if font_for is not None else font
    glyph_metrics = latin_metrics if is_latin_glyph else metrics
    font_size = glyph_font.pixelSize()
    if font_size <= 0:
        font_size = max(
            _latin_font_size(style) if is_latin_glyph else int(style.font_size_px), 1
        )
    space_percent = max(10, min(int(style.space_width_percent), 100))
    edge_size = max(int(style.stroke_width_px), 0)

    # Treat ASCII space explicitly.  Some headless Qt font backends expose a
    # tofu outline for it, while NicokaraMaker3 always applies SpaceWidth.
    if ch_text == " ":
        # A space has no path to outline.  Adding EdgeSize here made the visual
        # gap depend on the main-text stroke (20 px became 35 px in GO GHOST),
        # even though SpaceWidth is already an explicit percentage of FontSize.
        return font_size * space_percent // 100, 0.0

    advance = _char_advance(ch_text, metrics, latin_metrics, font_for)
    key = _char_metric_key(ch_text, glyph_font, advance, style)
    cached = _CHAR_METRIC_CACHE.get(key)
    if cached is not None:
        return cached

    path = QPainterPath()
    if ch_text:
        path.addText(0.0, 0.0, glyph_font, ch_text)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        body_width = font_size * space_percent * 25 // 100 // 10
        result = (max(body_width, 0) + edge_size, 0.0)
    else:
        try:
            width_left_bearing = glyph_metrics.leftBearing(ch_text)
            width_right_bearing = glyph_metrics.rightBearing(ch_text)
        except (TypeError, ValueError):
            # Multi-codepoint graphemes and non-BMP characters cannot always be
            # represented by QChar; derive equivalent bearings from the same path.
            width_left_bearing = int(bounds.left())
            width_right_bearing = int(advance - bounds.right())
        width = _nicokara_layout_width(
            int(bounds.width()),
            advance,
            width_left_bearing,
            width_right_bearing,
            edge_size=edge_size,
            allow_biting=bool(style.allow_biting),
        )
        try:
            offset_left_bearing = glyph_metrics.leftBearing(ch_text)
        except (TypeError, ValueError):
            offset_left_bearing = int(bounds.left())
        geometry_left = _nicokara_char_geometry_left_offset(
            int(bounds.width()),
            advance,
            offset_left_bearing,
            allow_biting=bool(style.allow_biting),
        )
        offset = (
            -float(bounds.left())
            + float(geometry_left)
            + max(int(style.stroke_width_px), 0) / 2.0
        )
        result = (width, offset)

    if len(_CHAR_METRIC_CACHE) >= _CHAR_METRIC_CACHE_MAX:
        _CHAR_METRIC_CACHE.clear()
    _CHAR_METRIC_CACHE[key] = result
    return result


def _char_path_left_offset(
    ch_text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
    style: Style,
) -> float:
    """Offset from the layout-box left edge to the QPainterPath text origin."""
    if not ch_text or ch_text.isspace():
        return 0.0
    return _char_layout_metrics(ch_text, font, metrics, latin_metrics, font_for, style)[1]


def _char_layout_width(
    ch_text: str,
    font: QFont,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
    style: Style,
) -> int:
    """Character step using NicokaraMaker3-like outline and spacing rules."""
    return _char_layout_metrics(ch_text, font, metrics, latin_metrics, font_for, style)[0]


def _letter_spacing(style: Style) -> int:
    return int(style.letter_spacing_px)


def _line_text_width(char_widths: list[int], style: Style) -> int:
    if not char_widths:
        return 0
    spacing = _letter_spacing(style)
    if style.layout_semantics == "n3_1074":
        return max(
            0,
            sum(max(int(width) + spacing, 0) for width in char_widths[:-1])
            + int(char_widths[-1]),
        )
    return max(0, sum(char_widths) + spacing * (len(char_widths) - 1))


def _display_line_compute_kwargs(style: Style) -> dict[str, object]:
    return {
        "lead_in_ms": style.line_lead_in_ms,
        "tail_ms": style.line_tail_ms,
        "lane_gap_ms": style.line_lane_gap_ms,
        "section_gap_ms": style.section_gap_ms,
        "sync_entry": style.sync_entry,
        "sync_ending": style.sync_ending,
        "section_ending_mode": style.section_ending_mode,
        "protect_ms": _effective_line_protect_ms(style),
        "lane_count": _lane_count(style),
        "row_count_of": _row_count_resolver(style),
        "bottom_align_of": _bottom_align_resolver(style),
        "vertical_position_of": _vertical_position_resolver(style),
        "auto_entry_reserve_ms_of": _auto_entry_reserve_resolver(style),
        "entry_animation_ms_of": _entry_animation_resolver(style),
        "exit_animation_ms_of": _exit_animation_resolver(style),
    }


def _default_collision_canvas(style: Style) -> tuple[int, int]:
    height = max(int(style.layout_reference_height), 1)
    return max(int(round(height * 16 / 9)), 1), height


def _measure_collision_bands(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
    *,
    time_window: str = "stable",
) -> list[tuple[int, tuple[int, int], LineVisualBand, float]]:
    """Measure ink bands without changing display-time behaviour."""

    if not display_lines:
        return []
    if style.vertical:
        baselines: dict[int, int] = {}
        line_layouts: dict[int, _SayatooLineLayout] = {}
        layout_cache_sig = None
    else:
        baselines = _resolve_display_baselines(
            logical_h, track, display_lines, style
        )
        line_layouts = _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            0,
            style,
        )
        layout_cache_sig = _layout_cache_sig(track, style)

    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]] = []
    for render_index, display_line in enumerate(display_lines):
        line_style = _style_for_line(style, display_line.line)
        if style.vertical:
            ink_rect = _display_line_vertical_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                line_style,
                # Glow may overlap between pages; collision avoidance only
                # protects solid glyph/ruby ink plus stroke/shadow.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            axis_anchor = _resolve_vertical_columns(
                logical_w, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            ink_rect = _display_line_horizontal_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                style,
                baselines,
                line_layouts,
                layout_cache_sig=layout_cache_sig,
                # Glow may overlap between pages; collision avoidance only
                # protects solid glyph/ruby ink plus stroke/shadow.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            line_layout = line_layouts.get(id(display_line.line))
            axis_anchor = (
                line_layout.baseline_y
                if line_layout is not None
                else baselines.get(display_line.lane)
            )
        if axis_bounds is None:
            continue
        assert cross_bounds is not None
        collision_start, collision_end = _display_line_collision_time_window(
            display_line, style, time_window=time_window
        )
        if collision_end <= collision_start:
            continue
        page_id = (
            int(display_line.section_index),
            int(display_line.page_index),
        )
        measured.append(
            (
                render_index,
                page_id,
                LineVisualBand(
                    line_id=render_index,
                    page_id=page_id,
                    display_start_ms=collision_start,
                    display_end_ms=collision_end,
                    axis_min=float(axis_bounds[0]),
                    axis_max=float(axis_bounds[1]),
                    entry_start_ms=int(display_line.display_start_ms),
                    axis_anchor=(
                        None if axis_anchor is None else float(axis_anchor)
                    ),
                    cross_min=float(cross_bounds[0]),
                    cross_max=float(cross_bounds[1]),
                ),
                max(float(line_style.line_gap_px), 0.0),
            )
        )
    return measured


def _pixel_collision_squeeze_pairs(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> tuple[tuple[int, int], ...]:
    """Return only line pairs with both stable-time and pixel-axis conflict."""

    measured = _measure_collision_bands(
        logical_w, logical_h, track, style, display_lines
    )
    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        for previous_index, previous_page, previous_band, _previous_gap in measured[
            :incoming_pos
        ]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            if not bands_require_separation(
                incoming_band, previous_band, 0.0
            ):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def _secondary_displacement_squeeze_pairs(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> tuple[tuple[int, int], ...]:
    """Return cascade dependencies created by rigid inter-page displacement.

    This is deliberately a discovery-only pass.  It never edits display
    windows or offsets; callers may feed the returned pairs through the
    existing measured-pair squeeze path, which preserves wipe bounds, manual
    overrides, animation reserves and page entry order.
    """

    measured = _measure_collision_bands(
        logical_w, logical_h, track, style, display_lines
    )
    if not measured:
        return ()

    page_order: list[tuple[int, int]] = []
    page_entries: dict[
        tuple[int, int], list[tuple[int, LineVisualBand, float]]
    ] = {}
    page_styles: dict[tuple[int, int], Style] = {}
    for render_index, page_id, band, gap in measured:
        if page_id not in page_entries:
            page_order.append(page_id)
            page_entries[page_id] = []
        page_entries[page_id].append((render_index, band, gap))
        page_styles.setdefault(
            page_id, _style_for_line(style, display_lines[render_index].line)
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(
                    band for _render_index, band, _gap in page_entries[page_id]
                ),
                gap_px=max(float(page_style.line_gap_px), 0.0),
                anchor=anchor,
            )
        )

    offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )
    if not any(float(offset) != 0.0 for offset in offsets.values()):
        return ()

    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        incoming_offset = float(offsets.get(incoming_page, 0.0))
        if incoming_offset == 0.0:
            continue
        for (
            previous_index,
            previous_page,
            previous_band,
            _previous_gap,
        ) in measured[:incoming_pos]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            previous_offset = float(offsets.get(previous_page, 0.0))
            if previous_offset == 0.0:
                continue
            # Authored-position conflicts already went through the primary
            # squeeze pass.  This pass only covers an incoming page that would
            # have been clear before an earlier page moved into its position.
            if bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            shifted_previous = previous_band.shifted(
                previous_offset
            )
            if not bands_require_separation(incoming_band, shifted_previous, 0.0):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def _measured_conflict_windows(
    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]],
) -> dict[tuple[int, int], tuple[int, int]]:
    """Return existing spatial-avoidance intervals for each colliding pair."""

    conflicts: dict[tuple[int, int], tuple[int, int]] = {}
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        for (
            previous_index,
            previous_page,
            previous_band,
            _previous_gap,
        ) in measured[:incoming_pos]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            if not bands_require_separation(
                incoming_band, previous_band, 0.0
            ):
                continue
            conflicts[(previous_index, incoming_index)] = (
                max(
                    int(previous_band.display_start_ms),
                    int(incoming_band.display_start_ms),
                ),
                min(
                    int(previous_band.display_end_ms),
                    int(incoming_band.display_end_ms),
                ),
            )
    return conflicts


def _retime_measured_collision_bands(
    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]],
    display_lines: list[DisplayLine],
    style: Style,
    changed_indices: tuple[int, ...],
    *,
    time_window: str = "stable",
) -> list[tuple[int, tuple[int, int], LineVisualBand, float]] | None:
    """Reuse measured X/Y rectangles when only display boundaries changed.

    ``None`` requests a full remeasurement when a changed line had no stable
    band before.  That rare case can gain one after synchronization; skipping
    it would miss a collision.
    """

    changed = set(changed_indices)
    measured_indices = {render_index for render_index, _page, _band, _gap in measured}
    if not changed.issubset(measured_indices):
        return None
    retimed: list[tuple[int, tuple[int, int], LineVisualBand, float]] = []
    for render_index, page_id, band, gap in measured:
        if render_index not in changed:
            retimed.append((render_index, page_id, band, gap))
            continue
        collision_start, collision_end = _display_line_collision_time_window(
            display_lines[render_index],
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        retimed.append(
            (
                render_index,
                page_id,
                replace(
                    band,
                    display_start_ms=int(collision_start),
                    display_end_ms=int(collision_end),
                    entry_start_ms=int(
                        display_lines[render_index].display_start_ms
                    ),
                ),
                gap,
            )
        )
    return retimed


def _sync_page_boundary_conflicts(
    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]],
    *,
    page_id: tuple[int, int],
    page_line_indices: tuple[int, ...],
    sync_line_indices: tuple[int, ...] | None = None,
    boundary: str,
    baseline_conflicts: dict[tuple[int, int], tuple[int, int]],
    time_gap_ms: int = 0,
) -> list[tuple[LineVisualBand, LineVisualBand]]:
    """Return sync collisions against every line in the relevant time direction.

    A page boundary synchronizes only that page's T lines.  For entry, every
    earlier timeline line is a read-only reference; for ending, every later
    timeline line is a read-only reference.  The reference need not belong to
    the immediately adjacent page.
    """

    conflicts: list[tuple[LineVisualBand, LineVisualBand]] = []
    required_time_gap = max(int(time_gap_ms), 0)
    page_indices = set(page_line_indices)
    active_indices = set(sync_line_indices or page_line_indices)
    first_page_index = min(page_line_indices)
    last_page_index = max(page_line_indices)
    current_bands = [
        (render_index, band)
        for render_index, measured_page, band, _gap in measured
        if measured_page == page_id
        and render_index in page_indices
        and render_index in active_indices
    ]
    reference_bands = sorted(
        [
            (render_index, band)
            for render_index, measured_page, band, _gap in measured
            if measured_page != page_id
            and (
                (boundary == "entry" and render_index < first_page_index)
                or (boundary == "ending" and render_index > last_page_index)
            )
        ],
        key=lambda item: item[0],
    )
    if boundary == "entry":
        prefix_latest_end: list[int] = []
        latest_end = -1
        for _reference_index, reference_band in reference_bands:
            latest_end = max(latest_end, int(reference_band.display_end_ms))
            prefix_latest_end.append(latest_end)
    else:
        suffix_earliest_start = [0] * len(reference_bands)
        earliest_start = 2**63 - 1
        for pos in range(len(reference_bands) - 1, -1, -1):
            earliest_start = min(
                earliest_start,
                int(reference_bands[pos][1].display_start_ms),
            )
            suffix_earliest_start[pos] = earliest_start

    for current_index, current_band in current_bands:
        positions = (
            range(len(reference_bands) - 1, -1, -1)
            if boundary == "entry"
            else range(len(reference_bands))
        )
        for pos in positions:
            if boundary == "entry":
                if (
                    prefix_latest_end[pos] + required_time_gap
                    <= current_band.display_start_ms
                ):
                    break
            elif suffix_earliest_start[pos] >= (
                current_band.display_end_ms + required_time_gap
            ):
                break
            reference_index, reference_band = reference_bands[pos]
            pair = (
                min(reference_index, current_index),
                max(reference_index, current_index),
            )
            if boundary == "entry":
                previous_band, incoming_band = reference_band, current_band
            else:
                previous_band, incoming_band = current_band, reference_band
            if (
                int(incoming_band.display_start_ms)
                - int(previous_band.display_end_ms)
                >= required_time_gap
            ):
                continue
            if not bands_require_separation(
                incoming_band, previous_band, 0.0
            ):
                continue
            overlap = (
                max(
                    int(previous_band.display_start_ms),
                    int(incoming_band.display_start_ms),
                ),
                min(
                    int(previous_band.display_end_ms),
                    int(incoming_band.display_end_ms),
                ),
            )
            baseline_overlap = baseline_conflicts.get(pair)
            if (
                overlap[0] < overlap[1]
                and
                baseline_overlap is not None
                and overlap[0] >= baseline_overlap[0]
                and overlap[1] <= baseline_overlap[1]
            ):
                # The ordinary schedule already needs spatial avoidance for
                # exactly this interval.  Synchronization may keep that
                # unavoidable interval, but must not start it earlier or keep
                # it alive longer.
                continue
            conflicts.append((previous_band, incoming_band))
    return conflicts


def _extend_page_display_boundary(
    display_lines: list[DisplayLine],
    indices: tuple[int, ...],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[DisplayLine]:
    changed = list(display_lines)
    for index in indices:
        item = changed[index]
        changed[index] = replace(
            item,
            display_start_ms=(
                item.display_start_ms
                if start_ms is None
                else min(int(item.display_start_ms), int(start_ms))
            ),
            display_end_ms=(
                item.display_end_ms
                if end_ms is None
                else max(int(item.display_end_ms), int(end_ms))
            ),
        )
    return changed


def _apply_constrained_page_sync(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> list[DisplayLine]:
    """Extend page sync boundaries without rewriting another page's schedule.

    Ordinary per-line collision compression is already complete when this
    function runs.  The automatic T lines *within one page* then extend toward
    the page's earliest entry / latest ending candidate.  Existing windows are
    never shortened: a colliding earlier line may stop a later T from reaching
    the earliest entry, and a colliding later line may stop an earlier T from
    reaching the latest ending.  Partial synchronization is valid.  Collision
    reference lines remain read-only.

    "Do not disturb the line that already entered / already exits" is part of
    what page sync means here, not part of avoidance, so this runs unchanged
    whether or not ``allow_inter_page_line_overlap`` is set.
    """

    if not display_lines or not (style.sync_entry or style.sync_ending):
        return display_lines
    baseline = list(display_lines)
    sync_gap_ms = max(int(style.line_lane_gap_ms), 0)
    measured = _measure_collision_bands(
        logical_w,
        logical_h,
        track,
        style,
        baseline,
        time_window="display",
    )
    page_order: list[tuple[int, int]] = []
    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(baseline):
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_indices:
            page_order.append(page_id)
            page_indices[page_id] = []
        page_indices[page_id].append(index)

    resolved = list(baseline)
    for page_id in page_order:
        indices = tuple(page_indices[page_id])
        if style.sync_entry:
            automatic = {
                index
                for index in indices
                if resolved[index].line.display_start_override_ms is None
            }
            if automatic:
                page_target = min(
                    int(resolved[index].display_start_ms) for index in indices
                )
                for page_pos, index in enumerate(indices):
                    if index not in automatic:
                        continue
                    latest = int(resolved[index].display_start_ms)
                    order_floor = (
                        max(
                            int(resolved[prior].display_start_ms)
                            for prior in indices[:page_pos]
                        )
                        if page_pos
                        else page_target
                    )
                    target = min(
                        max(int(page_target), int(order_floor)),
                        latest,
                    )
                    if target >= latest:
                        continue
                    entry_baseline_conflicts = _measured_conflict_windows(
                        measured
                    )
                    for _pass in range(len(display_lines) + 1):
                        candidate = _extend_page_display_boundary(
                            resolved, (index,), start_ms=target
                        )
                        candidate_measured = _retime_measured_collision_bands(
                            measured,
                            candidate,
                            style,
                            (index,),
                            time_window="display",
                        )
                        if candidate_measured is None:
                            candidate_measured = _measure_collision_bands(
                                logical_w,
                                logical_h,
                                track,
                                style,
                                candidate,
                                time_window="display",
                            )
                        conflicts = _sync_page_boundary_conflicts(
                            candidate_measured,
                            page_id=page_id,
                            page_line_indices=indices,
                            sync_line_indices=(index,),
                            boundary="entry",
                            baseline_conflicts=entry_baseline_conflicts,
                            time_gap_ms=sync_gap_ms,
                        )
                        if not conflicts or target >= latest:
                            resolved = candidate
                            measured = candidate_measured
                            break
                        delay = max(
                            previous.display_end_ms
                            + sync_gap_ms
                            - incoming.display_start_ms
                            for previous, incoming in conflicts
                        )
                        next_target = min(
                            target + max(int(delay), 1),
                            latest,
                        )
                        if next_target == target:
                            resolved = candidate
                            measured = candidate_measured
                            break
                        target = next_target

        if style.sync_ending:
            automatic = tuple(
                index
                for index in indices
                if resolved[index].line.display_end_override_ms is None
            )
            if automatic:
                page_target = max(
                    int(resolved[index].display_end_ms) for index in indices
                )
                for index in automatic:
                    earliest = int(resolved[index].display_end_ms)
                    target = max(int(page_target), earliest)
                    if target <= earliest:
                        continue
                    ending_baseline_conflicts = _measured_conflict_windows(
                        measured
                    )
                    for _pass in range(len(display_lines) + 1):
                        candidate = _extend_page_display_boundary(
                            resolved, (index,), end_ms=target
                        )
                        candidate_measured = _retime_measured_collision_bands(
                            measured,
                            candidate,
                            style,
                            (index,),
                            time_window="display",
                        )
                        if candidate_measured is None:
                            candidate_measured = _measure_collision_bands(
                                logical_w,
                                logical_h,
                                track,
                                style,
                                candidate,
                                time_window="display",
                            )
                        conflicts = _sync_page_boundary_conflicts(
                            candidate_measured,
                            page_id=page_id,
                            page_line_indices=indices,
                            sync_line_indices=(index,),
                            boundary="ending",
                            baseline_conflicts=ending_baseline_conflicts,
                            time_gap_ms=sync_gap_ms,
                        )
                        if not conflicts or target <= earliest:
                            resolved = candidate
                            measured = candidate_measured
                            break
                        retreat = max(
                            previous.display_end_ms
                            + sync_gap_ms
                            - incoming.display_start_ms
                            for previous, incoming in conflicts
                        )
                        next_target = max(
                            target - max(int(retreat), 1),
                            earliest,
                        )
                        if next_target == target:
                            resolved = candidate
                            measured = candidate_measured
                            break
                        target = next_target
    return resolved


def _apply_animation_time_guard(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
    *,
    enforce_inter_page_gap: bool,
) -> list[DisplayLine]:
    """Keep automatic line-action windows visible without eating wipe timing."""

    if not display_lines:
        return display_lines

    guarded = list(display_lines)
    changed = False
    entry_durations: list[int] = []
    exit_durations: list[int] = []
    line_starts: list[int] = []
    line_ends: list[int] = []
    for index, item in enumerate(guarded):
        entry_duration = _entry_animation_ms(style, item.line)
        exit_duration = _exit_animation_ms(style, item.line)
        entry_durations.append(entry_duration)
        exit_durations.append(exit_duration)
        line_start = _line_start_ms(item.line)
        line_end = _line_end_ms(item.line)
        line_starts.append(line_start)
        line_ends.append(line_end)

        start = int(item.display_start_ms)
        end = int(item.display_end_ms)
        if item.line.display_start_override_ms is None and entry_duration > 0:
            start = min(start, max(line_start - entry_duration, 0))
        if item.line.display_end_override_ms is None and exit_duration > 0:
            end = max(end, line_end + exit_duration)
        if start != item.display_start_ms or end != item.display_end_ms:
            guarded[index] = replace(
                item,
                display_start_ms=start,
                display_end_ms=max(start, end),
            )
            changed = True

    if not changed:
        return display_lines
    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return guarded if changed else display_lines

    required_gap = max(int(style.line_lane_gap_ms), 0)
    measured = _measure_collision_bands(
        logical_w,
        logical_h,
        track,
        style,
        guarded,
        time_window="display",
    )
    for _pass in range(max(len(guarded) * 2, 1)):
        adjusted = False
        changed_index: int | None = None
        for incoming_pos, (
            incoming_index,
            incoming_page,
            incoming_band,
            _incoming_gap,
        ) in enumerate(measured):
            incoming = guarded[incoming_index]
            for previous_index, previous_page, previous_band, _previous_gap in measured[
                :incoming_pos
            ]:
                if previous_page == incoming_page:
                    continue
                if not bands_require_separation(
                    incoming_band, previous_band, 0.0
                ):
                    continue
                required_start = (
                    int(previous_band.display_end_ms) + required_gap
                )
                if int(incoming_band.display_start_ms) >= required_start:
                    continue

                latest_entry_start = max(
                    line_starts[incoming_index] - entry_durations[incoming_index],
                    0,
                )
                if (
                    incoming.line.display_start_override_ms is None
                    and required_start <= latest_entry_start
                ):
                    new_start = max(
                        int(incoming.display_start_ms),
                        required_start,
                    )
                    if new_start != incoming.display_start_ms:
                        guarded[incoming_index] = replace(
                            incoming,
                            display_start_ms=new_start,
                        )
                        adjusted = True
                        changed_index = incoming_index
                        changed = True
                        break

                previous = guarded[previous_index]
                if previous.line.display_end_override_ms is not None:
                    continue
                latest_previous_end = (
                    int(incoming_band.display_start_ms) - required_gap
                )
                new_end = max(
                    line_ends[previous_index],
                    min(int(previous.display_end_ms), latest_previous_end),
                )
                if new_end != previous.display_end_ms:
                    guarded[previous_index] = replace(
                        previous,
                        display_end_ms=max(
                            int(previous.display_start_ms),
                            new_end,
                        ),
                    )
                    adjusted = True
                    changed_index = previous_index
                    changed = True
                    break
            if adjusted:
                break
        if not adjusted:
            break
        if changed_index is not None:
            retimed = _retime_measured_collision_bands(
                measured,
                guarded,
                style,
                (changed_index,),
                time_window="display",
            )
            measured = (
                retimed
                if retimed is not None
                else _measure_collision_bands(
                    logical_w,
                    logical_h,
                    track,
                    style,
                    guarded,
                    time_window="display",
                )
            )
    return guarded if changed else display_lines


def _display_lines_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    """Resolve display windows, squeezing only measured per-line collisions.

    Both modes run one timing pipeline.  ``allow_inter_page_line_overlap``
    decides only whether cross-page *avoidance* runs -- the measured squeeze and
    the inter-page animation gap.  Lead-in, tail, lane gap and page sync are user
    settings computed by the identical passes either way, so the two modes can
    only differ on windows that avoidance would actually have consumed.
    """

    kwargs = _display_line_compute_kwargs(style)
    avoid_collisions = not style.allow_inter_page_line_overlap
    base_kwargs = {
        **kwargs,
        "sync_entry": False,
        "sync_ending": False,
    }
    if logical_w is None or logical_h is None:
        default_w, default_h = _default_collision_canvas(style)
        logical_w = default_w if logical_w is None else logical_w
        logical_h = default_h if logical_h is None else logical_h
    logical_w = max(int(logical_w), 1)
    logical_h = max(int(logical_h), 1)
    cache_key = (
        logical_w,
        logical_h,
        id(track),
        _value_signature(track),
        _value_signature(style),
    )
    cached = _DISPLAY_LINES_CACHE.get(cache_key)
    if cached is not None:
        _DISPLAY_LINES_CACHE.move_to_end(cache_key)
        return list(cached[1])
    ideal = compute_display_lines(
        track,
        **base_kwargs,
        adjust_same_position=False,
        dynamic_single_page_reflow=True,
        independent_line_entry=True,
    )
    resolved = ideal
    if avoid_collisions:
        squeeze_pairs = _pixel_collision_squeeze_pairs(
            logical_w, logical_h, track, style, ideal
        )
        if squeeze_pairs:
            resolved = compute_display_lines(
                track,
                **base_kwargs,
                adjust_same_position=False,
                squeeze_pairs=squeeze_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
        secondary_pairs = _secondary_displacement_squeeze_pairs(
            logical_w, logical_h, track, style, resolved
        )
        if secondary_pairs:
            combined_pairs = tuple(
                dict.fromkeys((*squeeze_pairs, *secondary_pairs))
            )
            resolved = compute_display_lines(
                track,
                **base_kwargs,
                adjust_same_position=False,
                squeeze_pairs=combined_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
    resolved = _apply_constrained_page_sync(
        logical_w,
        logical_h,
        track,
        style,
        resolved,
    )
    resolved = _apply_animation_time_guard(
        logical_w,
        logical_h,
        track,
        style,
        resolved,
        enforce_inter_page_gap=avoid_collisions,
    )
    # Keep the track object alive with the cached display lines.  The key uses
    # ``id(track)`` to prevent equal-but-distinct tracks from sharing mutable
    # TimingLine references; retaining the owner also prevents CPython from
    # recycling that id while the cache entry exists.
    _DISPLAY_LINES_CACHE[cache_key] = (track, tuple(resolved))
    _DISPLAY_LINES_CACHE.move_to_end(cache_key)
    while len(_DISPLAY_LINES_CACHE) > _DISPLAY_LINES_CACHE_MAX:
        _DISPLAY_LINES_CACHE.popitem(last=False)
    return resolved


def _visible_lines_for_style(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    if style.dual_line_layout:
        return [
            item
            for item in _display_lines_for_style(
                track,
                style,
                logical_w=logical_w,
                logical_h=logical_h,
            )
            if item.display_start_ms <= t_ms < item.display_end_ms
        ]
    display_line = _single_visible_display_line(track, t_ms, style)
    if display_line is None:
        return []
    return [display_line]


def display_windows_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> dict[int, tuple[int, int]]:
    """全部可渲染行的显示窗口：``track.lines`` 索引 → (上屏, 消失) 毫秒。

    与预览/导出使用同一套布局参数（含逐行手动覆盖），供字幕轨道 UI
    展示与编辑句子的显示/隐藏时间。
    """
    windows: dict[int, tuple[int, int]] = {}
    if style.dual_line_layout:
        items = _display_lines_for_style(
            track,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        index_of = {id(line): i for i, line in enumerate(track.lines)}
        for item in items:
            index = index_of.get(id(item.line))
            if index is not None:
                windows[index] = (item.display_start_ms, item.display_end_ms)
        return windows
    lead = max(style.line_lead_in_ms, 0)
    tail = max(style.line_tail_ms, 0)
    for index, line in enumerate(track.lines):
        if line.is_blank or not line.chars:
            continue
        display_start = max(_line_start_ms(line) - lead, 0)
        display_end = _line_end_ms(line) + tail
        windows[index] = apply_display_overrides(line, display_start, display_end)
    return windows


def display_schedule_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> dict[int, tuple[int, int, int]]:
    """Return ``line index -> (lane, display start, display end)``.

    Native/GPU backends consume this resolved schedule so pagination, manual
    display overrides and lane protection remain owned by the Painter oracle.
    """
    if not style.dual_line_layout:
        return {
            index: (0, start, end)
            for index, (start, end) in display_windows_for_style(
                track,
                style,
                logical_w=logical_w,
                logical_h=logical_h,
            ).items()
        }
    items = _display_lines_for_style(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    return {
        index_of[id(item.line)]: (
            int(item.lane),
            int(item.display_start_ms),
            int(item.display_end_ms),
        )
        for item in items
        if id(item.line) in index_of
    }


def _single_visible_display_line(
    track: TimingTrack,
    t_ms: int,
    style: Style,
) -> DisplayLine | None:
    best_live: DisplayLine | None = None
    best_lead_or_tail: DisplayLine | None = None
    lead = max(style.line_lead_in_ms, 0)
    tail = max(style.line_tail_ms, 0)
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        sing_start = _line_start_ms(line)
        sing_end = _line_end_ms(line)
        display_start = max(sing_start - lead, 0)
        display_end = sing_end + tail
        display_start, display_end = apply_display_overrides(
            line, display_start, display_end
        )
        display_line = DisplayLine(
            line=line,
            lane=0,
            display_start_ms=display_start,
            display_end_ms=display_end,
        )
        if sing_start <= t_ms < sing_end:
            if best_live is None or sing_start >= _line_start_ms(best_live.line):
                best_live = display_line
        elif display_start <= t_ms < display_end:
            if best_lead_or_tail is None or sing_start >= _line_start_ms(best_lead_or_tail.line):
                best_lead_or_tail = display_line
    return best_live or best_lead_or_tail


def _effective_line_protect_ms(style: Style) -> int:
    """N3 ``WipeTimingSettingsModel.ProtectTime``。

    N3 不把入 / 退场动画时长算进保护时间——淡入淡出整段落在显示窗口内部，
    是从 PreTime / PostTime 里"吃"掉的，不额外撑窗口。
    """
    return protect_time_ms(
        style.line_lead_in_ms, style.line_tail_ms, style.line_protect_ms
    )


def _display_style_for_signal_window(style: Style) -> Style:
    if not style.lit_enabled or style.vertical:
        return style
    signal_lead = _signal_lead_in_ms(style)
    if signal_lead <= max(style.line_lead_in_ms, 0):
        return style
    return replace(style, line_lead_in_ms=signal_lead)


def _signal_lead_in_ms(style: Style) -> int:
    duration = max(int(style.signals_duration_ms), 0)
    if duration <= 0:
        return 0
    return max(
        0,
        duration + max(int(style.lit_waiting_time_ms), 0) - int(style.lit_time_offset_ms),
    )


def _signal_display_lines_for_style(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    if not style.lit_enabled or style.vertical:
        return []
    signal_lead = _signal_lead_in_ms(style)
    if signal_lead <= 0:
        return []
    signal_style = replace(style, line_lead_in_ms=max(style.line_lead_in_ms, signal_lead))
    return _visible_lines_for_style(
        track,
        t_ms,
        signal_style,
        logical_w=logical_w,
        logical_h=logical_h,
    )


def _build_ruby_font(style: Style) -> QFont:
    family = style.font_family if _ruby_uses_main_font(style) else (
        style.ruby_font_family or style.font_family
    )
    size = _ruby_font_size(style)
    weight = style.font_weight if _ruby_uses_main_font(style) else (
        style.ruby_font_weight
        if style.ruby_font_weight is not None and int(style.ruby_font_weight) > 0
        else style.font_weight
    )
    font = QFont(resolve_qt_font_family(family), size)
    font.setPixelSize(size)
    font.setWeight(_clamp_weight(int(weight)))
    font.setItalic(style.italic)
    return font


def _build_ruby_font_for_text(style: Style, reading: str) -> QFont:
    """Build the effective Japanese or Latin ruby font for one reading."""
    if not _is_n3_latin_text(reading):
        return _build_ruby_font(style)
    if _ruby_uses_main_font(style):
        family = style.font_family_latin or style.font_family
        weight = _latin_font_weight(style)
    else:
        family = (
            style.ruby_font_family_latin
            or style.ruby_font_family
            or style.font_family
        )
        weight = (
            int(style.ruby_latin_font_weight)
            if style.ruby_latin_font_weight is not None
            and int(style.ruby_latin_font_weight) > 0
            else int(style.ruby_font_weight)
            if style.ruby_font_weight is not None and int(style.ruby_font_weight) > 0
            else int(style.font_weight)
        )
    size = (
        int(style.ruby_latin_font_size_px)
        if style.ruby_latin_font_size_px is not None
        and int(style.ruby_latin_font_size_px) > 0
        else _ruby_font_size(style)
    )
    font = QFont(resolve_qt_font_family(family), max(size, 1))
    font.setPixelSize(max(size, 1))
    font.setWeight(_clamp_weight(weight))
    font.setItalic(style.italic)
    return font


def _ruby_uses_main_font(style: Style) -> bool:
    """旧工程显式保存了非默认注音字号时，视为已经解除跟随。"""
    return bool(style.ruby_font_follow_main) and all(
        value is None
        for value in (
            style.ruby_font_family,
            style.ruby_font_family_latin,
            style.ruby_font_weight,
            style.ruby_latin_font_size_px,
            style.ruby_latin_font_weight,
        )
    ) and int(style.ruby_font_size_px) == 45


def _ruby_font_size(style: Style) -> int:
    # 字体 fallback 只共享字体族/字重，注音字号始终使用自己的字段。
    # 否则全局默认 Style() 会把 45px 注音错误渲染成 100px 主文字。
    return max(int(style.ruby_font_size_px), 1)


def _clamp_weight(w: int) -> QFont.Weight:
    # QFont.Weight 在 PyQt6 是 IntEnum，可直接传 int；不过为了取最近档位更稳，
    # 映射到 Thin/Normal/Bold/Black 几档。
    if w <= 250:
        return QFont.Weight.Thin
    if w <= 350:
        return QFont.Weight.Light
    if w <= 450:
        return QFont.Weight.Normal
    if w <= 550:
        return QFont.Weight.Medium
    if w <= 650:
        return QFont.Weight.DemiBold
    if w <= 750:
        return QFont.Weight.Bold
    if w <= 850:
        return QFont.Weight.ExtraBold
    return QFont.Weight.Black


def _visual_text_padding(style: Style) -> int:
    return _visual_stroke_extent(style.stroke_width_px, _main_stroke2_width(style))


def _visual_stroke_extent(stroke_width: int, stroke2_width: int) -> int:
    return math.ceil((max(stroke_width, 0) + max(stroke2_width, 0)) / 2)


def _ruby_stroke_extent(style: Style) -> int:
    return _visual_stroke_extent(
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
    )


def _n3_char_box_ascent(metrics: QFontMetrics, font_size_px: int, stroke_width: int) -> float:
    """N3 字符盒的「基线以上」高度。

    N3 的字符/行盒（``DrawCharInfo.Height``）= **字号 + 描边宽**（edge2 不占位），
    基线把盒按字体 ascent:descent 比例分割（``CreateTransformedCharGeometryChar``：
    ``baseline = 盒底 - FontSize·D/(A+D) - Edge/2``）。即字体 metric 被归一化到
    字号高，没有 Qt metric 的 em 外头部空隙——这是 N3 注音贴得更近的根本原因。
    """
    ascent = max(metrics.ascent(), 0)
    descent = max(metrics.descent(), 0)
    total = max(ascent + descent, 1)
    return max(font_size_px, 1) * ascent / total + max(stroke_width, 0) / 2.0


def _n3_char_box_descent(metrics: QFontMetrics, font_size_px: int, stroke_width: int) -> float:
    """N3 字符盒的「基线以下」高度（含描边半宽）。见 :func:`_n3_char_box_ascent`。"""
    ascent = max(metrics.ascent(), 0)
    descent = max(metrics.descent(), 0)
    total = max(ascent + descent, 1)
    return max(font_size_px, 1) * descent / total + max(stroke_width, 0) / 2.0


def _ruby_vertical_extra(
    style: Style,
    ruby_metrics: QFontMetrics,
    *,
    font_size_px: int | None = None,
) -> int:
    """主文字上方为注音预留的高度（N3：间隔 + ruby 盒高 = 注音字号 + 注音描边宽）。

    间距可为负（ruby 咬进正文），但预留高度不能倒扣。``ruby_metrics`` 保留在签名里
    以兼容调用方（N3 盒高与 metric 无关）。
    """
    del ruby_metrics
    effective_size = (
        _ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    return max(
        int(round(
            int(style.ruby_gap_px)
            + effective_size
            + max(_ruby_stroke_width(style), 0)
        )),
        0,
    )


def _ruby_baseline_y(
    main_baseline_y: int,
    main_box_ascent: float,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    font_size_px: int | None = None,
) -> int:
    """N3 语义的注音基线：ruby 盒底 = 主行盒顶 − 歌詞とルビの間隔。

    ``main_box_ascent`` 为主行基线到主行盒顶的距离（:func:`_n3_char_box_ascent`）。
    ruby 基线在 ruby 盒底之上「字号归一化 descent + 描边半宽」处。
    """
    main_top = main_baseline_y - main_box_ascent
    effective_size = (
        _ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    return int(round(
        main_top
        - int(style.ruby_gap_px)
        - _n3_char_box_descent(
            ruby_metrics, effective_size, _ruby_stroke_width(style)
        )
    ))


def _stroke_pen_width(stroke_width: int) -> int:
    return max(stroke_width, 0)


def _stroke2_pen_width(stroke_width: int, stroke2_width: int) -> int:
    return max(stroke_width, 0) + max(stroke2_width, 0)


def _glow_pen_width(stroke_width: int, stroke2_width: int, glow_radius: int) -> int:
    if glow_radius <= 0:
        return 0
    base_width = _stroke2_pen_width(stroke_width, stroke2_width) if stroke2_width > 0 else _stroke_pen_width(stroke_width)
    return max(1, base_width + glow_radius)


def _glow_extent(stroke_width: int, stroke2_width: int, glow_radius: int) -> int:
    if glow_radius <= 0:
        return 0
    return math.ceil(_glow_pen_width(stroke_width, stroke2_width, glow_radius) / 2 + glow_radius * 3)


def _glow_blur_radii(radius: int, concentration_level: int) -> tuple[int, ...]:
    """N3 ``DrawOneLineDecorBlurMulti`` radii for low/medium/high density."""
    radius = max(int(radius), 0)
    level = normalize_glow_concentration_level(concentration_level)
    if radius == 0 or level < 0:
        return ()
    passes = level + 1
    return tuple(radius - (index * radius // passes) for index in range(passes))


def _glow_concentration_level(style: Style) -> int:
    return normalize_glow_concentration_level(style.glow_concentration_level)


def _glow_radius(style: Style, *, after: bool) -> int:
    if _glow_concentration_level(style) < 0:
        return 0
    value = style.glow_after_radius_px if after else style.glow_before_radius_px
    return max(int(value), 0)


def _ruby_stroke_width(style: Style) -> int:
    if style.ruby_stroke_width_px is not None:
        return max(int(style.ruby_stroke_width_px), 0)
    return _scaled_px(style.stroke_width_px, _ruby_scale(style))


def _ruby_stroke2_enabled(style: Style) -> bool:
    """注音描边 2 的开关；未设定表示跟随主文字。"""
    return (
        style.stroke2_enabled
        if style.ruby_stroke2_enabled is None
        else bool(style.ruby_stroke2_enabled)
    )


def _ruby_stroke2_width_value(style: Style) -> int:
    """注音描边 2 的宽度本身，不含开关判定。

    宽度与开关沿各自的链条独立继承（N3 ``FontFaceInfoModel`` 里 ``UseEdge2``
    与 ``EdgeSize2`` 就是分别回退的，见 ``n3_font_fallback``）。因此主文字关掉
    描边 2 不会抹掉注音继承的宽度值——属性面板显示的也正是这个未经开关裁剪的
    宽度。调用方负责在最后按开关裁剪一次。
    """
    if style.ruby_stroke2_width_px is not None:
        return max(int(style.ruby_stroke2_width_px), 0)
    return _scaled_px(style.stroke2_width_px, _ruby_scale(style))


def _ruby_stroke2_width(style: Style) -> int:
    return _ruby_stroke2_width_value(style) if _ruby_stroke2_enabled(style) else 0


def _ruby_script_stroke_style(style: Style, reading: str) -> Style:
    """把整段 ruby 读音所用字体槽的描边物化到 ruby 通用字段。"""
    if not _is_n3_latin_text(reading):
        return style
    width = (
        _ruby_stroke_width(style)
        if style.ruby_latin_stroke_width_px is None
        or int(style.ruby_latin_stroke_width_px) <= 0
        else max(int(style.ruby_latin_stroke_width_px), 0)
    )
    enabled = (
        _ruby_stroke2_enabled(style)
        if style.ruby_latin_stroke2_enabled is None
        else bool(style.ruby_latin_stroke2_enabled)
    )
    # 继承的宽度必须取未经开关裁剪的值：``_ruby_stroke2_width`` 会先按注音/主文字
    # 那一级的开关归零，于是英数槽自己显式打开描边 2 时也只能拿到 0。
    width2 = (
        _ruby_stroke2_width_value(style)
        if style.ruby_latin_stroke2_width_px is None
        or int(style.ruby_latin_stroke2_width_px) <= 0
        else max(int(style.ruby_latin_stroke2_width_px), 0)
    )
    return replace(
        style,
        ruby_stroke_width_px=width,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=width2 if enabled else 0,
    )


def _ruby_decoration_kind(style: Style) -> DecorationKind:
    value = style.ruby_decoration_kind
    return value if value in {"none", "shadow", "glow"} else style.decoration_kind


def _ruby_shadow_dx(style: Style) -> int:
    if _ruby_decoration_kind(style) != "shadow":
        return 0
    if style.ruby_shadow_offset_x is not None:
        return int(style.ruby_shadow_offset_x)
    return _scaled_signed_px(style.shadow_offset_x, _ruby_scale(style))


def _ruby_shadow_dy(style: Style) -> int:
    if _ruby_decoration_kind(style) != "shadow":
        return 0
    if style.ruby_shadow_offset_y is not None:
        return int(style.ruby_shadow_offset_y)
    return _scaled_signed_px(style.shadow_offset_y, _ruby_scale(style))


def _ruby_glow_radius(style: Style, *, after: bool) -> int:
    if _ruby_glow_concentration_level(style) < 0:
        return 0
    value = style.ruby_glow_after_radius_px if after else style.ruby_glow_before_radius_px
    if value is None and style.ruby_glow_radius_px is not None:
        value = style.ruby_glow_radius_px
    if value is not None:
        return max(int(value), 0)
    return _scaled_glow_radius(style, _ruby_scale(style), after=after)


def _ruby_glow_concentration_level(style: Style) -> int:
    value = style.ruby_glow_concentration_level
    if value is None:
        return _glow_concentration_level(style)
    return normalize_glow_concentration_level(value)


def _ruby_paint_style(style: Style) -> Style:
    decoration = _ruby_decoration_kind(style)
    concentration = _ruby_glow_concentration_level(style)
    if (
        decoration == style.decoration_kind
        and concentration == _glow_concentration_level(style)
    ):
        return style
    return replace(
        style,
        decoration_kind=decoration,
        glow_concentration_level=concentration,
    )


def _text_visual_padding(style: Style, *, after: bool) -> int:
    stroke2_width = _main_stroke2_width(style)
    pad = _visual_stroke_extent(style.stroke_width_px, stroke2_width)
    if style.decoration_kind == "glow":
        pad = max(
            pad,
            _glow_extent(
                style.stroke_width_px,
                stroke2_width,
                _glow_radius(style, after=after),
            ),
        )
    elif style.decoration_kind == "shadow":
        # 阴影是含描边的整字剪影：足迹 = 描边半宽 + 偏移。
        pad = pad + abs(style.shadow_offset_y)
    return max(pad, 2)


def _ruby_visual_padding(style: Style, *, after: bool) -> int:
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    pad = _visual_stroke_extent(stroke_width, stroke2_width)
    if _ruby_decoration_kind(style) == "glow":
        pad = max(
            pad,
            _glow_extent(
                stroke_width,
                stroke2_width,
                _ruby_glow_radius(style, after=after),
            ),
        )
    else:
        pad = pad + abs(_ruby_shadow_dy(style))
    return max(pad, 2)


def _title_visual_padding(title: TitleOverlay) -> int:
    pad = _visual_stroke_extent(title.stroke_width_px, title.stroke2_width_px)
    if title.decoration_kind == "glow":
        pad = max(
            pad,
            _glow_extent(
                title.stroke_width_px,
                title.stroke2_width_px,
                max(int(title.glow_radius_px), 0),
            ),
        )
    elif title.decoration_kind == "shadow":
        pad = pad + abs(title.shadow_offset_y)
    return max(pad, 2)


def _scaled_glow_radius(style: Style, scale: float, *, after: bool) -> int:
    return _scaled_px(_glow_radius(style, after=after), scale)


def _resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = max(style.line_y_margin_px, 0)
    if style.layout_semantics == "n3_1074":
        main_h, main_ascent, main_descent, ruby_extra = _fixed_line_geometry(style)
        if pos == "top":
            return margin + ruby_extra + main_ascent
        if pos == "center":
            return (img_h - main_h) // 2 + main_ascent
        return img_h - margin - main_descent
    pad = _visual_text_padding(style)
    ruby_extra = 0
    if ruby_metrics is not None:
        ruby_extra = _ruby_vertical_extra(style, ruby_metrics)
    if pos == "top":
        return margin + ruby_extra + pad + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + metrics.ascent()
    # bottom（默认）
    return img_h - margin - pad - metrics.descent()


def _fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = _build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    ruby_extra = _ruby_vertical_extra(style, ruby_metrics)
    if style.layout_semantics == "n3_1074":
        # N3 DrawCharInfo.Height is font size + the first edge only.  Keep the
        # product's larger guide/role glyphs out of the shared lane box; their
        # visual geometry and ruby-anchor participation remain independent.
        font_size = max(int(style.font_size_px), 1)
        edge = max(int(style.stroke_width_px), 0)
        main_h = font_size + edge
        metric_total = max(metrics.ascent() + metrics.descent(), 1)
        main_descent = font_size * max(metrics.descent(), 0) // metric_total + edge // 2
        main_descent = min(max(main_descent, 0), main_h)
        main_ascent = main_h - main_descent
        # N3 anchors top/middle/bottom against the main DrawLine box. Ruby is
        # positioned above DrawTop afterwards and may extend into the margin.
        return main_h, main_ascent, main_descent, 0
    pad = _visual_text_padding(style)
    main_h = metrics.ascent() + metrics.descent() + pad * 2
    return main_h, metrics.ascent() + pad, metrics.descent() + pad, ruby_extra


def _resolve_display_baselines(
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    if not style.dual_line_layout:
        font = _build_font(style)
        metrics = QFontMetrics(font)
        line = display_lines[0].line if display_lines else None
        ruby_metrics = (
            QFontMetrics(_build_ruby_font(style))
            if line is not None and _active_rubies_for_line(track.rubies, line)
            else None
        )
        baseline = _resolve_baseline_y(metrics, img_h, style, ruby_metrics)
        if style.line_horizontal_layout == "per_row":
            baseline += style.row1_offset_y
        return {0: baseline}

    main_h, main_ascent, main_descent, ruby_extra = _fixed_line_geometry(style)
    gap = int(style.line_gap_px)
    margin = max(style.line_y_margin_px, 0)
    lanes = _lane_count(style)
    step = main_h + gap

    if style.line_y_position == "top":
        first_baseline = margin + ruby_extra + main_ascent
    elif style.line_y_position == "center":
        total_h = main_h * lanes + gap * (lanes - 1)
        first_baseline = (img_h - total_h) // 2 + main_ascent
    else:
        last_baseline = img_h - margin - main_descent
        first_baseline = last_baseline - step * (lanes - 1)
    baselines = {lane: first_baseline + step * lane for lane in range(lanes)}
    if style.line_horizontal_layout == "per_row":
        # per_row 是 Sayatoo 双行遗留：Y 偏移只定义了前两行。
        if 0 in baselines:
            baselines[0] += style.row1_offset_y
        if 1 in baselines:
            baselines[1] += style.row2_offset_y
    return baselines


# ---------------------------------------------------------------------------
# 竖排（縦書き）
# ---------------------------------------------------------------------------

_VERTICAL_REFERENCE_CHAR = "永"  # 「永」全角参照字，估列宽

# UTR#50：竖排时需旋转 90° 的字符（长音、破折号、波浪、横向括号、横箭头）。
_VERTICAL_ROTATE_CHARS = set(
    "ーｰ"  # ー ｰ 长音符
    "—―‐‑‒–"  # — ― ‐ ‑ ‒ – 各种连字符/破折号
    "〜～"  # 〜 ～ 波浪
    "→←"  # → ← 横向箭头
    "（）()"  # （ ） ( )
    "「」『』"  # 「 」 『 』
    "【】〔〕"  # 【 】 〔 〕
    "［］｛｝"  # ［ ］ ｛ ｝
    "〈〉《》"  # 〈 〉 《 》
    "[]{}<>"  # [ ] { } < >
)

# 竖排时移到字格右上角的标点（直立、不旋转）。
_VERTICAL_CORNER_PUNCT = set("、。，．")  # 、 。 ， ．

# 竖排时向右上偏移的小书き假名（直立）。
_VERTICAL_SMALL_KANA = set(
    "ぁぃぅぇぉっゃゅょゎ"  # ぁぃぅぇぉっゃゅょゎ
    "ァィゥェォッャュョヮ"  # ァィゥェォッャュョヮ
    "ヵヶ"  # ヵヶ
)


def _vertical_orientation(ch: str) -> str:
    """UTR#50 简化朝向：``"R"`` 需旋转 90°，``"U"`` 直立。"""
    return "R" if ch in _VERTICAL_ROTATE_CHARS else "U"


def _vertical_glyph_offset(ch: str, cell_w: int, cell_h: int) -> tuple[float, float]:
    """直立字形在字格内的位移（标点/小假名靠右上）。"""
    if ch in _VERTICAL_CORNER_PUNCT:
        return (cell_w * 0.28, -cell_h * 0.28)
    if ch in _VERTICAL_SMALL_KANA:
        return (cell_w * 0.10, -cell_h * 0.10)
    return (0.0, 0.0)


def _vertical_glyph_path(
    ch_text: str,
    font: QFont,
    metrics: QFontMetrics,
    column_x: int,
    cell_top: int,
    cell_w: int,
    cell_h: int,
    ascent: int,
    *,
    vector_glyph=None,
) -> QPainterPath:
    """单个竖排字形的 path：旋转类绕字格中心转 90°，其余直立（标点/小假名偏移）。"""
    if vector_glyph is not None:
        if _guide_symbol_is_bitmap(vector_glyph):
            return QPainterPath()
        path = scaled_guide_symbol_path(
            vector_glyph,
            pixel_size=max(int(font.pixelSize()), 1),
            left=float(column_x - max(int(font.pixelSize()), 1) / 2),
            baseline_y=float(cell_top + ascent),
        )
        bounds = path.boundingRect()
        return QTransform.fromTranslate(
            float(column_x) - bounds.center().x(),
            float(cell_top + cell_h / 2) - bounds.center().y(),
        ).map(path)
    advance = metrics.horizontalAdvance(ch_text)
    baseline = cell_top + ascent
    glyph_x = column_x - advance / 2
    path = QPainterPath()
    if _vertical_orientation(ch_text) == "R":
        path.addText(float(glyph_x), float(baseline), font, ch_text)
        center_x = float(column_x)
        center_y = float(cell_top + cell_h / 2)
        transform = QTransform()
        transform.translate(center_x, center_y)
        transform.rotate(90)
        transform.translate(-center_x, -center_y)
        return transform.map(path)
    dx, dy = _vertical_glyph_offset(ch_text, cell_w, cell_h)
    path.addText(float(glyph_x + dx), float(baseline + dy), font, ch_text)
    return path


def _vertical_cell_width(metrics: QFontMetrics) -> int:
    """竖排列宽 = 一个全角字的步进（字形列内居中用）。"""
    width = metrics.horizontalAdvance(_VERTICAL_REFERENCE_CHAR)
    if width <= 0:
        width = metrics.height()
    return max(width, 1)


def _resolve_vertical_columns(
    img_w: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    """每 lane 的列中心 x。lane 0 = 右列（当前句），lane 1 = 左列（下一句）。

    竖排文字流向右→左：当前句在最右，列向左排。列宽用全角参照字估算，
    列间距复用 ``line_gap_px``，右列距右边缘复用 ``line_y_margin_px``。
    """
    metrics = QFontMetrics(_build_font(style))
    cell_w = _vertical_cell_width(metrics)
    margin = max(style.line_y_margin_px, 0)
    gap = max(style.line_gap_px, 0)  # 竖排列距不允许负值（列重叠无意义）
    ruby_w = _vertical_ruby_allowance(track, style)
    # 右列：列右侧留出 ruby 宽度（ruby 排在基字右边）。列数随 lane 数扩展，
    # lane k 在 lane k-1 左侧一列；行级布局的页行数可能超过全局行数，按可见行补足。
    right_center = img_w - margin - ruby_w - cell_w / 2
    max_lane = max((item.lane for item in display_lines), default=0)
    columns: dict[int, int] = {}
    for lane in range(max(_lane_count(style), max_lane + 1)):
        columns[lane] = int(round(right_center - lane * (cell_w + ruby_w + gap)))
    return columns


def _vertical_ruby_allowance(track: TimingTrack, style: Style) -> int:
    """竖排时基字右侧为 ruby 预留的水平宽度（无 ruby 则 0）。"""
    if not track.rubies:
        return 0
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    return max(ruby_metrics.height() + int(style.ruby_gap_px), 0)


def _resolve_vertical_top(img_h: int, block_h: int, style: Style) -> int:
    """竖排列的纵向起点 y（列整体上/中/下锚定，复用 line_y_position）。"""
    margin = max(style.line_y_margin_px, 0)
    pos = style.line_y_position
    if pos == "top":
        return margin
    if pos == "center":
        return max((img_h - block_h) // 2, 0)
    return img_h - margin - block_h  # bottom（默认）


def _build_baked_path_stack(
    path: QPainterPath,
    rect: QRectF,
    state: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
) -> tuple[QImage, int, int] | None:
    """把一次 :func:`_paint_text_layer_stack` 烘焙成透明 QImage（整数对齐 → 贴出像素一致）。

    返回 ``(image, ox, oy)``：``ox/oy`` 为整数 blit 偏移，``drawImage(QPointF(ox,oy), image)``
    时字形落回原坐标，与直绘逐像素一致（pad/偏移均取整、blit 偏移为整数 → 不重采样）。
    """
    is_glow = style.decoration_kind == "glow"
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    glow_extra = _glow_extent(stroke_width, stroke2_width, glow_radius) if is_glow else 0
    extent = max(stroke_extent, glow_extra, 0) + 4
    pad_left = max(0, -shadow_dx) + extent
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    pbr = path.boundingRect()
    if pbr.isEmpty():
        return None
    left_i = math.floor(pbr.left())
    top_i = math.floor(pbr.top())
    right_i = math.ceil(pbr.right())
    bottom_i = math.ceil(pbr.bottom())
    img_w = max((right_i - left_i) + pad_left + pad_right, 1)
    img_h = max((bottom_i - top_i) + pad_top + pad_bottom, 1)
    ox = left_i - pad_left
    oy = top_i - pad_top

    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        p.translate(-ox, -oy)
        _paint_text_layer_stack(
            p, path, rect, state, style,
            stroke_width=stroke_width, stroke2_width=stroke2_width,
            shadow_dx=shadow_dx, shadow_dy=shadow_dy, glow_radius=glow_radius,
        )
    finally:
        p.end()
    return image, ox, oy


@dataclass(frozen=True)
class _BakedPathStackLayer:
    """通用「烘焙 path 栈」层：把一次 ``_paint_text_layer_stack`` 烘焙成位图缓存，逐帧
    只 blit + 可选 clip 带。竖排主文本 / 竖排 ruby 共用（其几何已是 QPainterPath + clip）。"""

    path: QPainterPath
    rect: QRectF
    state: KaraokeColorState
    style: Style
    cache_key: tuple
    stroke_width: int
    stroke2_width: int
    shadow_dx: int
    shadow_dy: int
    glow_radius: int
    clip_rect: QRectF | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_BakedPathStackLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        return self.cache_key

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        built = _build_baked_path_stack(
            self.path, self.rect, self.state, self.style,
            stroke_width=self.stroke_width, stroke2_width=self.stroke2_width,
            shadow_dx=self.shadow_dx, shadow_dy=self.shadow_dy, glow_radius=self.glow_radius,
        )
        if built is None:
            return BakedLayer(image=QImage(), offset=QPointF())
        image, ox, oy = built
        return BakedLayer(image=image, offset=QPointF(float(ox), float(oy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(top_left=QPointF(0.0, 0.0), clip_rect=self.clip_rect)

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        pbr = self.path.boundingRect()
        if pbr.isEmpty():
            return None
        is_glow = self.style.decoration_kind == "glow"
        extent = max(
            _visual_stroke_extent(self.stroke_width, self.stroke2_width),
            _glow_extent(self.stroke_width, self.stroke2_width, self.glow_radius) if is_glow else 0,
            abs(self.shadow_dy), 0,
        ) + 4
        top = int(math.floor(pbr.top())) - extent
        bottom = int(math.ceil(pbr.bottom())) + extent
        if self.clip_rect is not None:
            top = max(top, int(math.floor(self.clip_rect.top())))
            bottom = min(bottom, int(math.ceil(self.clip_rect.bottom())))
        if bottom < top:
            return None
        return top, bottom


def _paint_line_vertical(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    column_x: int | None,
    lane: int | None = None,
) -> None:
    """竖排单列渲染：字符上→下堆叠、卡拉ok 扫光上→下。

    默认走 :func:`_paint_line_vertical_layers`（整条路径迁入 LayerCompositor + bake 缓存，
    与横排一致）；``KROK_SUBTITLE_VERTICAL_LAYER=0`` 回退到 :func:`_paint_line_vertical_direct`
    逐帧直绘（亦作像素一致性 oracle）。两条路径像素一致。
    """
    source_line = line
    line = _line_with_guide_symbol(line)
    layout = _layout_vertical_line(
        track, line, style, img_w, img_h,
        column_x=column_x, source_line=source_line,
    )
    if layout is None:
        return
    if _vertical_layer_enabled():
        _paint_line_vertical_layers(painter, layout, line, t_ms, style)
    else:
        _paint_line_vertical_direct(painter, layout, line, t_ms, style)


def _paint_line_vertical_direct(
    painter: QPainter,
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> None:
    """竖排逐帧直绘（旧路径，A/B oracle + env 回退）。"""
    stroke2_width = _main_stroke2_width(style)
    band = _vertical_fill_band(
        layout.cells,
        layout.intervals,
        t_ms,
        line=line,
        active_rubies=layout.active_rubies,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    # 「未唱」层。N3 硬分割：发光存在且已唱层会覆盖已唱带时，未唱层整体裁到
    # 扫光线之下（见 _vertical_before_clip_rect 注释）。
    before_clip = None
    if (
        band is not None
        and style.decoration_kind == "glow"
        and _glow_radius(style, after=False) > 0
    ):
        before_clip = _vertical_before_clip_rect(
            layout.column_x,
            layout.cell_w,
            band[1],
            _vertical_before_clip_pad(
                style.stroke_width_px,
                stroke2_width,
                _glow_radius(style, after=False),
                style.shadow_offset_x,
                style.shadow_offset_y,
            ),
        )
    painter.save()
    try:
        if before_clip is not None:
            painter.setClipRect(before_clip)
        _paint_text_layer_stack(
            painter,
            layout.text_path,
            layout.line_rect,
            layout.colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
        )
    finally:
        painter.restore()

    # 「已唱」层：纵向裁剪带 [y_top, scan]
    if band is not None:
        y0, y_scan = band
        pad = _vertical_after_clip_pad(style)
        painter.save()
        try:
            painter.setClipRect(
                _vertical_after_clip_rect(layout.column_x, layout.cell_w, y0, y_scan, pad)
            )
            _paint_text_layer_stack(
                painter,
                layout.text_path,
                layout.line_rect,
                layout.colors.after,
                style,
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=True),
            )
        finally:
            painter.restore()

    # 注音：排在基字列右侧、上→下扫光
    if layout.active_rubies:
        ruby_font = _build_ruby_font(style)
        _paint_rubies_vertical(
            painter,
            ruby_font,
            QFontMetrics(ruby_font),
            line,
            layout.intervals,
            layout.cells,
            layout.column_x,
            layout.cell_w,
            t_ms,
            layout.active_rubies,
            style,
        )


def _vertical_after_clip_pad(style: Style) -> int:
    stroke2_width = _main_stroke2_width(style)
    stroke_extent = _visual_stroke_extent(style.stroke_width_px, stroke2_width)
    return max(
        stroke_extent,
        _glow_extent(style.stroke_width_px, stroke2_width, _glow_radius(style, after=True))
        if style.decoration_kind == "glow"
        else 0,
        stroke_extent + abs(style.shadow_offset_x),
        stroke_extent + abs(style.shadow_offset_y),
        2,
    )


def _vertical_after_clip_rect(
    column_x: int, cell_w: int, y0: int, y_scan: int, pad: int
) -> QRectF:
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y0 - pad),
        float(cell_w + pad * 2),
        float((y_scan - y0) + pad),
    )


def _vertical_before_clip_pad(
    stroke_width: int,
    stroke2_width: int,
    before_glow_radius: int,
    shadow_dx: int,
    shadow_dy: int,
) -> int:
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    return max(
        stroke_extent,
        _glow_extent(stroke_width, stroke2_width, before_glow_radius),
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )


def _vertical_before_clip_rect(
    column_x: float, cell_w: float, y_scan: float, pad: int
) -> QRectF:
    """未唱层的互补裁剪带：扫光线以下（竖排扫光上→下）。

    N3 硬分割：竖排发光烘在整层位图内、无法单独跳过已唱发光，所以只要未唱
    发光存在就把未唱层整体裁到扫光线之下，已唱带交给已唱层。前后发光相同时
    两层位图逐像素相同，互补裁剪恰好还原整条 halo（不再是此前的双份叠加）。
    """
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y_scan),
        float(cell_w + pad * 2),
        1_000_000.0,
    )


def _paint_line_vertical_layers(
    painter: QPainter,
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> None:
    """竖排经 LayerCompositor 绘制：主文本/ruby 的 before/after 烘焙成位图缓存，逐帧
    只 blit + 纵向扫光带 clip。与 :func:`_paint_line_vertical_direct` 像素一致。"""
    layers = _vertical_layer_stack(layout, line, t_ms, style)
    if not layers:
        return
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter, LayerContext(t_ms=t_ms, logical_w=0, logical_h=0), layers
    )


def _vertical_main_path_sig(line: TimingLine, style: Style, layout: _VerticalLineLayout) -> tuple:
    return (
        "vmain",
        tuple(ch.text for ch in line.chars),
        tuple(_value_signature(ch.vector_glyph) for ch in line.chars),
        style.font_family,
        style.font_family_latin,
        style.font_size_px,
        _latin_font_size(style),
        int(style.font_weight),
        _latin_font_weight(style),
        style.italic,
        layout.column_x,
        layout.y_top,
        layout.cell_w,
        layout.cell_h,
        layout.ascent,
    )


def _baked_stack_key(
    path_sig: tuple,
    rect: QRectF,
    state: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
    after: bool,
) -> tuple:
    return (
        path_sig,
        int(round(rect.left())),
        int(round(rect.top())),
        int(round(rect.width())),
        int(round(rect.height())),
        _karaoke_state_signature(state),
        style.decoration_kind,
        stroke_width,
        stroke2_width,
        shadow_dx,
        shadow_dy,
        glow_radius,
        after,
    )


def _vertical_layer_stack(
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> list:
    layers: list = []
    stroke2_width = _main_stroke2_width(style)
    main_sig = _vertical_main_path_sig(line, style, layout)
    band = _vertical_fill_band(
        layout.cells,
        layout.intervals,
        t_ms,
        line=line,
        active_rubies=layout.active_rubies,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    # N3 硬分割：与 _paint_line_vertical_direct 同口径的未唱层互补裁剪。
    before_clip = None
    if (
        band is not None
        and style.decoration_kind == "glow"
        and _glow_radius(style, after=False) > 0
    ):
        before_clip = _vertical_before_clip_rect(
            layout.column_x,
            layout.cell_w,
            band[1],
            _vertical_before_clip_pad(
                style.stroke_width_px,
                stroke2_width,
                _glow_radius(style, after=False),
                style.shadow_offset_x,
                style.shadow_offset_y,
            ),
        )
    layers.append(
        _BakedPathStackLayer(
            path=layout.text_path,
            rect=layout.line_rect,
            state=layout.colors.before,
            style=style,
            cache_key=_baked_stack_key(
                main_sig, layout.line_rect, layout.colors.before, style,
                stroke_width=style.stroke_width_px, stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x, shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=False), after=False,
            ),
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
            clip_rect=before_clip,
            z_index=0,
        )
    )
    if band is not None:
        y0, y_scan = band
        pad = _vertical_after_clip_pad(style)
        layers.append(
            _BakedPathStackLayer(
                path=layout.text_path,
                rect=layout.line_rect,
                state=layout.colors.after,
                style=style,
                cache_key=_baked_stack_key(
                    main_sig, layout.line_rect, layout.colors.after, style,
                    stroke_width=style.stroke_width_px, stroke2_width=stroke2_width,
                    shadow_dx=style.shadow_offset_x, shadow_dy=style.shadow_offset_y,
                    glow_radius=_glow_radius(style, after=True), after=True,
                ),
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=True),
                clip_rect=_vertical_after_clip_rect(layout.column_x, layout.cell_w, y0, y_scan, pad),
                z_index=1,
            )
        )
    if layout.active_rubies:
        layers.extend(_vertical_ruby_layers(layout, line, t_ms, style))
    return layers


def _vertical_ruby_path_and_wipe(
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    ruby_x: int,
    ruby_cell_w: int,
    ruby_ascent: int,
    base_top: int,
    span_h: int,
) -> tuple[QPainterPath, tuple[_RubyWipeSegment, ...], float, float, tuple[str, ...]]:
    timed_units = _ruby_visual_units_and_intervals(ruby)
    if not timed_units:
        return QPainterPath(), (), float(base_top), float(base_top), ()
    count = len(timed_units)
    ruby_path = QPainterPath()
    segments: list[_RubyWipeSegment] = []
    ink_bounds: list[tuple[float, float]] = []
    for unit_index, (unit, (start_ms, end_ms)) in enumerate(timed_units):
        slot_top = base_top + span_h * unit_index / count
        slot_h = span_h / count
        unit_path = _vertical_glyph_path(
            unit,
            ruby_font,
            ruby_metrics,
            ruby_x,
            int(round(slot_top)),
            ruby_cell_w,
            max(int(round(slot_h)), 1),
            ruby_ascent,
        )
        ruby_path.addPath(unit_path)
        ink = unit_path.boundingRect()
        if ink.isEmpty():
            ink_top = float(slot_top)
            ink_bottom = float(slot_top + slot_h)
        else:
            ink_top = float(ink.top())
            ink_bottom = float(ink.bottom())
        segments.append(
            _RubyWipeSegment(
                int(start_ms), max(int(start_ms), int(end_ms)), ink_top, ink_bottom
            )
        )
        ink_bounds.append((ink_top, ink_bottom))
    return (
        ruby_path,
        tuple(segments),
        min(top for top, _bottom in ink_bounds),
        max(bottom for _top, bottom in ink_bounds),
        tuple(unit for unit, _interval in timed_units),
    )


def _vertical_ruby_layers(
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> list:
    cells = layout.cells
    if not cells:
        return []
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font)
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    before_glow_radius = _ruby_glow_radius(style, after=False)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    colors = _effective_ruby_karaoke_colors(style)
    ruby_cell_w = _vertical_cell_width(ruby_metrics)
    ruby_ascent = ruby_metrics.ascent()
    ruby_x = int(
        round(layout.column_x + layout.cell_w / 2 + int(style.ruby_gap_px) + ruby_cell_w / 2)
    )
    ruby_font_sig = (
        ruby_font.family(), ruby_font.pixelSize(), int(ruby_font.weight()), ruby_font.italic(),
    )

    layers: list = []
    z = 2
    for ruby in layout.active_rubies:
        indices = [i for i in _ruby_target_indices(ruby, line, layout.intervals) if 0 <= i < len(cells)]
        if not indices:
            continue
        base_top = cells[min(indices)][0]
        base_bottom = cells[max(indices)][1]
        span_h = base_bottom - base_top
        ruby_path, wipe_segments, wipe_top, _wipe_bottom, units = _vertical_ruby_path_and_wipe(
            ruby, ruby_font, ruby_metrics, ruby_x, ruby_cell_w, ruby_ascent,
            base_top, span_h,
        )
        if not wipe_segments:
            continue
        count = len(units)
        ruby_rect = QRectF(
            float(ruby_x - ruby_cell_w / 2), float(base_top), float(ruby_cell_w), float(span_h),
        )
        ruby_sig = (
            "vruby", ruby.kanji, ruby.reading, tuple(units), ruby_font_sig,
            ruby_x, base_top, span_h, count,
        )
        visible, complete, scan_y = _ruby_segment_wipe_state(
            wipe_segments, ruby.pos_end_ms, t_ms
        )
        # N3 硬分割：注音未唱层裁到扫光线之下；唱完后已唱层不再裁剪、整读音
        # 由已唱层负责，未唱层直接省略（发光才有差异，body 本就被覆盖）。
        glow_split = (
            _ruby_decoration_kind(style) == "glow" and before_glow_radius > 0
        )
        before_clip = None
        if glow_split and visible and not complete:
            before_clip = _vertical_before_clip_rect(
                ruby_x,
                ruby_cell_w,
                scan_y,
                _vertical_before_clip_pad(
                    stroke_width, stroke2_width, before_glow_radius, shadow_dx, shadow_dy
                ),
            )
        if not (glow_split and visible and complete):
            layers.append(
                _BakedPathStackLayer(
                    path=ruby_path, rect=ruby_rect, state=colors.before, style=paint_style,
                    cache_key=_baked_stack_key(
                        ruby_sig, ruby_rect, colors.before, paint_style,
                        stroke_width=stroke_width, stroke2_width=stroke2_width,
                        shadow_dx=shadow_dx, shadow_dy=shadow_dy,
                        glow_radius=before_glow_radius, after=False,
                    ),
                    stroke_width=stroke_width, stroke2_width=stroke2_width,
                    shadow_dx=shadow_dx, shadow_dy=shadow_dy, glow_radius=before_glow_radius,
                    clip_rect=before_clip, z_index=z,
                )
            )
        z += 1
        if not visible:
            continue
        stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
        pad = max(
            stroke_extent,
            _glow_extent(stroke_width, stroke2_width, after_glow_radius) if _ruby_decoration_kind(style) == "glow" else 0,
            stroke_extent + abs(shadow_dx), stroke_extent + abs(shadow_dy), 2,
        )
        clip = None if complete else QRectF(
            float(ruby_x - ruby_cell_w / 2 - pad), float(wipe_top - pad),
            float(ruby_cell_w + pad * 2), float(max(scan_y - wipe_top, 0.0) + pad),
        )
        layers.append(
            _BakedPathStackLayer(
                path=ruby_path, rect=ruby_rect, state=colors.after, style=paint_style,
                cache_key=_baked_stack_key(
                    ruby_sig, ruby_rect, colors.after, paint_style,
                    stroke_width=stroke_width, stroke2_width=stroke2_width,
                    shadow_dx=shadow_dx, shadow_dy=shadow_dy,
                    glow_radius=after_glow_radius, after=True,
                ),
                stroke_width=stroke_width, stroke2_width=stroke2_width,
                shadow_dx=shadow_dx, shadow_dy=shadow_dy, glow_radius=after_glow_radius,
                clip_rect=clip, z_index=z,
            )
        )
        z += 1
    return layers


def _layout_vertical_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    column_x: int | None,
    source_line: TimingLine | None = None,
) -> _VerticalLineLayout | None:
    """layout 段：算竖排行的列几何 / 字符格 / 字形路径（不依赖 t_ms）。"""
    chars = line.chars
    if not chars:
        return None
    font = _build_font(style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(style)
    font_for = _make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    cell_w = _vertical_cell_width(metrics)
    cell_h = metrics.height()
    ascent = metrics.ascent()

    resolved_column_x = (
        column_x
        if column_x is not None
        else int(round(img_w - max(style.line_y_margin_px, 0) - cell_w / 2))
    )
    block_h = cell_h * len(chars)
    y_top = _resolve_vertical_top(img_h, block_h, style)
    intervals = compute_char_intervals(line)
    colors = _effective_karaoke_colors(style)

    text_path = QPainterPath()
    cells: list[tuple[int, int]] = []
    for index, ch in enumerate(chars):
        cell_top = y_top + index * cell_h
        cells.append((cell_top, cell_top + cell_h))
        glyph_font = font_for(ch.text) if font_for is not None else font
        glyph_metrics = (
            latin_metrics
            if (font_for is not None and ch.text and ch.text.isascii())
            else metrics
        )
        text_path.addPath(
            _vertical_glyph_path(
                ch.text,
                glyph_font,
                glyph_metrics,
                resolved_column_x,
                cell_top,
                cell_w,
                cell_h,
                ascent,
                vector_glyph=ch.vector_glyph,
            )
        )

    line_rect = QRectF(
        float(resolved_column_x - cell_w / 2),
        float(y_top),
        float(cell_w),
        float(block_h),
    )
    return _VerticalLineLayout(
        font=font,
        metrics=metrics,
        cell_w=cell_w,
        cell_h=cell_h,
        ascent=ascent,
        column_x=resolved_column_x,
        y_top=y_top,
        block_h=block_h,
        intervals=intervals,
        cells=cells,
        line_rect=line_rect,
        text_path=text_path,
        colors=colors,
        active_rubies=_active_rubies_for_line(track.rubies, source_line or line),
    )


def _paint_rubies_vertical(
    painter: QPainter,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    cells: list[tuple[int, int]],
    base_column_x: int,
    cell_w: int,
    t_ms: int,
    rubies: list[RubyAnnotation],
    style: Style,
) -> None:
    """竖排注音：读音字形竖向堆叠在基字列右侧，覆盖基字纵向区间，上→下扫光。"""
    if not cells:
        return
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    before_glow_radius = _ruby_glow_radius(style, after=False)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    colors = _effective_ruby_karaoke_colors(style)
    ruby_cell_w = _vertical_cell_width(ruby_metrics)
    ruby_ascent = ruby_metrics.ascent()
    ruby_x = int(
        round(base_column_x + cell_w / 2 + int(style.ruby_gap_px) + ruby_cell_w / 2)
    )

    painter.setFont(ruby_font)
    for ruby in rubies:
        indices = [
            index
            for index in _ruby_target_indices(ruby, line, intervals)
            if 0 <= index < len(cells)
        ]
        if not indices:
            continue
        base_top = cells[min(indices)][0]
        base_bottom = cells[max(indices)][1]
        span_h = base_bottom - base_top
        ruby_path, wipe_segments, wipe_top, _wipe_bottom, _units = _vertical_ruby_path_and_wipe(
            ruby, ruby_font, ruby_metrics, ruby_x, ruby_cell_w, ruby_ascent,
            base_top, span_h,
        )
        if not wipe_segments:
            continue

        ruby_rect = QRectF(
            float(ruby_x - ruby_cell_w / 2),
            float(base_top),
            float(ruby_cell_w),
            float(span_h),
        )
        visible, complete, scan_y = _ruby_segment_wipe_state(
            wipe_segments, ruby.pos_end_ms, t_ms
        )
        # N3 硬分割：与 _vertical_ruby_layers 同口径（direct 是像素一致 oracle）。
        glow_split = (
            _ruby_decoration_kind(style) == "glow" and before_glow_radius > 0
        )
        if not (glow_split and visible and complete):
            painter.save()
            try:
                if glow_split and visible and not complete:
                    painter.setClipRect(
                        _vertical_before_clip_rect(
                            ruby_x,
                            ruby_cell_w,
                            scan_y,
                            _vertical_before_clip_pad(
                                stroke_width,
                                stroke2_width,
                                before_glow_radius,
                                shadow_dx,
                                shadow_dy,
                            ),
                        )
                    )
                _paint_text_layer_stack(
                    painter,
                    ruby_path,
                    ruby_rect,
                    colors.before,
                    paint_style,
                    stroke_width=stroke_width,
                    stroke2_width=stroke2_width,
                    shadow_dx=shadow_dx,
                    shadow_dy=shadow_dy,
                    glow_radius=before_glow_radius,
                )
            finally:
                painter.restore()

        if not visible:
            continue
        stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
        pad = max(
            stroke_extent,
            _glow_extent(stroke_width, stroke2_width, after_glow_radius)
            if _ruby_decoration_kind(style) == "glow"
            else 0,
            stroke_extent + abs(shadow_dx),
            stroke_extent + abs(shadow_dy),
            2,
        )
        painter.save()
        try:
            if not complete:
                painter.setClipRect(
                    QRectF(
                        float(ruby_x - ruby_cell_w / 2 - pad),
                        float(wipe_top - pad),
                        float(ruby_cell_w + pad * 2),
                        float(max(scan_y - wipe_top, 0.0) + pad),
                    )
                )
            _paint_text_layer_stack(
                painter,
                ruby_path,
                ruby_rect,
                colors.after,
                paint_style,
                stroke_width=stroke_width,
                stroke2_width=stroke2_width,
                shadow_dx=shadow_dx,
                shadow_dy=shadow_dy,
                glow_radius=after_glow_radius,
            )
        finally:
            painter.restore()


def _vertical_fill_band(
    cells: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    t_ms: int,
    *,
    line: TimingLine | None = None,
    active_rubies: list[RubyAnnotation] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> tuple[int, int] | None:
    """竖排已唱区 ``(y_top, y_scan)``：扫光从首字符顶向下推进；空带返回 None。"""
    if not cells:
        return None
    y_top = cells[0][0]
    scan = float(y_top)
    ruby_groups = (
        _resolve_char_ruby_groups(active_rubies, line, intervals)
        if ruby_main_progress_mode == "reading_units"
        and line is not None
        and active_rubies
        else None
    )
    for index, ((cell_top, cell_bottom), (start, end)) in enumerate(
        zip(cells, intervals)
    ):
        ratio = (
            _character_fill_ratio(
                line,
                intervals,
                cells,
                active_rubies or [],
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=ruby_main_progress_mode,
            )
            if ruby_groups is not None and line is not None
            else char_fill_ratio(start, end, t_ms)
        )
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            scan = cell_bottom
            continue
        scan = cell_top + (cell_bottom - cell_top) * ratio
        break
    if scan <= y_top:
        return None
    return y_top, int(round(scan))


def _paint_line(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    layout_cache_sig: tuple | None = None,
) -> None:
    style = _style_for_line_display_window(
        style,
        line,
        display_start_ms,
        display_end_ms,
    )
    animation = line_animation_state(
        style,
        t_ms=t_ms,
        display_start_ms=display_start_ms if display_start_ms is not None else _line_start_ms(line),
        display_end_ms=display_end_ms if display_end_ms is not None else _line_end_ms(line),
        lane=lane,
    )
    if animation.opacity <= 0.0:
        return

    def draw(target: QPainter) -> None:
        target.save()
        try:
            if animation.dx or animation.dy:
                target.translate(animation.dx, animation.dy)
            _paint_line_static(
                target,
                img_w,
                img_h,
                track,
                line,
                t_ms,
                style,
                baseline_y=baseline_y,
                line_x=line_x,
                lane=lane,
                display_start_ms=display_start_ms,
                display_end_ms=display_end_ms,
                layout_cache_sig=layout_cache_sig,
            )
        finally:
            target.restore()

    if animation.opacity < 1.0:
        _paint_through_opacity_layer(painter, animation.opacity, draw)
        return
    draw(painter)


def _paint_through_opacity_layer(
    painter: QPainter,
    opacity: float,
    draw,
) -> None:
    """Compose ``draw`` at full opacity, then blend the result once.

    Mirrors N3's ``LineFade`` → ``PushOpacityLayer`` (Direct2D
    ``PushLayer(LayerParameters{Opacity})``).  Setting the opacity on the painter
    instead applies it to every single draw call, so a glyph body stops covering
    its own edge, glow and shadow: those lower layers show through the
    semi-transparent body and the line appears to change colour mid-animation.

    The scratch buffer matches the target's device resolution and carries the
    same world transform, so the rasterization is identical to drawing straight
    onto the target -- only the final alpha blend differs.
    """

    device = painter.device()
    physical_w = max(int(device.width()), 1)
    physical_h = max(int(device.height()), 1)
    buffer = _opacity_layer_buffer(physical_w, physical_h)
    if buffer is None:
        # Nothing sane to compose into; keep drawing rather than dropping the
        # line, accepting the legacy per-call blending for this frame.
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            draw(painter)
        finally:
            painter.restore()
        return
    buffer.setDevicePixelRatio(_device_pixel_ratio(device))
    buffer.fill(0)
    inner = QPainter(buffer)
    try:
        inner.setRenderHints(painter.renderHints())
        inner.setTransform(painter.transform())
        draw(inner)
    finally:
        inner.end()
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        painter.resetTransform()
        painter.drawImage(QPointF(0.0, 0.0), buffer)
    finally:
        painter.restore()


def _device_pixel_ratio(device) -> float:
    getter = getattr(device, "devicePixelRatioF", None)
    if getter is None:
        getter = getattr(device, "devicePixelRatio", None)
    try:
        return max(float(getter()), 0.01) if getter is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _opacity_layer_buffer(physical_w: int, physical_h: int) -> QImage | None:
    """Return a reusable scratch image, avoiding a per-frame allocation.

    Entry/exit animations run for a few hundred milliseconds at a time, so one
    cached buffer per size per thread keeps the steady state allocation-free.
    """

    cache = getattr(_OPACITY_LAYER_LOCAL, "buffers", None)
    if cache is None:
        cache = {}
        _OPACITY_LAYER_LOCAL.buffers = cache
    key = (physical_w, physical_h)
    buffer = cache.get(key)
    if buffer is None:
        buffer = QImage(
            physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied
        )
        if buffer.isNull():
            return None
        if len(cache) >= 4:
            cache.clear()
        cache[key] = buffer
    return buffer


def _paint_line_static(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    layout_cache_sig: tuple | None = None,
) -> None:
    if style.vertical:
        _paint_line_vertical(
            painter,
            img_w,
            img_h,
            track,
            line,
            t_ms,
            style,
            column_x=baseline_y,
            lane=lane,
        )
        return
    # layout 段（纯几何，不依赖 t_ms）：算字符几何 / 基线 / fill_segments。
    layout = _layout_line(
        track, line, style, img_w, img_h,
        baseline_y=baseline_y, line_x=line_x, lane=lane,
        cache_sig=layout_cache_sig,
    )
    if layout is None:
        return
    render_line = layout.render_line or line
    # animation 段（依赖 t_ms）：逐字入退场上下文。
    transition = _line_char_transition_context(
        style, render_line, t_ms, display_start_ms, display_end_ms, len(render_line.chars),
        intervals=layout.intervals,
    )
    def paint_ruby_glow_under_main() -> None:
        if (
            transition is not None
            or not layout.active_rubies
            or layout.ruby_metrics is None
        ):
            return
        _paint_ruby_glow_layers(
            painter,
            list(layout.ruby_layouts),
            layout.ruby_font,
            layout.ruby_metrics,
            t_ms,
            style,
            layout.rtl,
        )

    def paint_rubies_on_top() -> None:
        if not layout.active_rubies or layout.ruby_metrics is None:
            return
        # N3 renders main text decoration first, then ruby on top.  Painting
        # ruby before the main glyphs lets a large main glow bleed over the
        # reading stroke/fill, which makes ruby look submerged.
        _paint_rubies(
            painter, layout.ruby_font, layout.ruby_metrics, render_line,
            layout.intervals, layout.char_x_ranges, layout.baseline_y,
            t_ms, layout.active_rubies, style, transition,
            main_ascent_px=layout.text_layout.ascent if layout.has_inline_styles else None,
            text_layout=layout.text_layout,
            draw_glow=transition is not None,
            precomputed_layouts=layout.ruby_layouts,
        )

    if transition is not None:
        if transition.effect in ("char_fade", "char_drip", "spin_flip"):
            # A1/A2（§9.7）：逐字入退场 → 走 LayerCompositor 烘焙缓存，不再每帧
            # _paint_char_karaoke_stack 重栅（含 glow 复用）。普通行/分色行同路。
            # char_fade 仅 opacity（无损）；char_drip / spin_flip 加 N3 对应的
            # skew（spin_flip 还带 scale）残差（D2 软化可接受）。
            _TEXT_RUN_COMPOSITOR.paint_ordered(
                painter,
                LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
                _char_transition_layer_stack(layout, t_ms, transition, max(len(render_line.chars), 1)),
            )
            paint_rubies_on_top()
            return
        if layout.has_inline_styles:
            _paint_role_line_with_character_transition(
                painter, render_line, layout.text_layout, layout.char_x_ranges, layout.intervals,
                layout.active_rubies, layout.baseline_y, t_ms, transition, style,
                rtl=layout.rtl, ink_x_ranges=layout.ink_x_ranges,
                fill_segments=layout.fill_segments,
            )
        else:
            _paint_line_with_character_transition(
                painter, render_line, layout.char_widths, layout.char_x_ranges, layout.intervals,
                layout.active_rubies, layout.font, layout.baseline_y, layout.metrics,
                style, layout.colors, layout.line_rect, t_ms, transition,
                rtl=layout.rtl, font_for=layout.font_for, ink_x_ranges=layout.ink_x_ranges,
                glyphs_by_index=_role_glyphs_by_index(render_line, layout.text_layout),
                fill_rect=_n3_main_fill_rect(
                    layout.text_layout, layout.baseline_y
                ),
                fill_segments=layout.fill_segments,
            )
        paint_rubies_on_top()
        return

    # paint 段：消费 layout。默认 blit 未唱层 + 已唱层；测试/调试可回退同 layout 直绘。
    paint_ruby_glow_under_main()
    if _horizontal_layer_enabled():
        _paint_line_layers(painter, layout, t_ms)
    else:
        _paint_line_direct(painter, layout, t_ms)
    paint_rubies_on_top()


def _track_line_index(track: TimingTrack, line: TimingLine) -> int:
    """行在轨道中的下标；找不到返回 ``-1``。

    每次行布局查缓存都要它，而线性扫描是 O(行数)——在长曲目上又是一处
    O(行数²)。排版区间内行表不会变，整轨建一次索引即可。
    """
    cache = getattr(_LAYOUT_PASS, "line_indices", None)
    if cache is None:
        for index, item in enumerate(track.lines):
            if item is line:
                return index
        return -1
    index_map = cache.get(id(track))
    if index_map is None:
        index_map = {id(item): index for index, item in enumerate(track.lines)}
        cache[id(track)] = index_map
        # 键里有 id()：存住 track，避免回收后地址被复用。
        _LAYOUT_PASS.tracks.append(track)
    return index_map.get(id(line), -1)


def _layout_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    cache_sig: tuple | None = None,
) -> _LineLayout | None:
    if cache_sig is None:
        return _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
        )
    line_index = _track_line_index(track, line)
    if line_index < 0:
        return _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
        )
    key = (
        cache_sig,
        line_index,
        _line_layout_signature(line),
        img_w,
        img_h,
        baseline_y,
        line_x,
        lane,
    )
    return _LINE_LAYOUT_CACHE.get_or_build(
        key,
        lambda: _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
        ),
    )


def _layout_line_uncached(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
) -> _LineLayout | None:
    render_line = _line_with_guide_symbol(line)
    if _line_has_role_labels(render_line):
        return _layout_role_line(
            track, render_line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
            source_line=line,
        )
    return _layout_plain_line(
        track, render_line, style, img_w, img_h,
        baseline_y=baseline_y, line_x=line_x, lane=lane,
        source_line=line,
    )


def _layout_plain_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
) -> _LineLayout:
    """layout 段：算普通行的纯几何 + 字体资源（不依赖 t_ms，可缓存）。"""
    font = _build_font(style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(style)
    font_for = _make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    source_line = source_line or line
    active_rubies = _active_rubies_for_line(track.rubies, source_line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    # 整行宽度 → 水平居中起点（英数字符用英数字体的步进）。
    # 演唱计时用原始字宽；ruby 避让间隙只改几何。
    char_widths = [
        (
            _vector_glyph_width(c.vector_glyph, style)
            if c.vector_glyph is not None
            else _char_layout_width(c.text, font, metrics, latin_metrics, font_for, style)
        )
        for c in line.chars
    ]
    intervals = compute_char_intervals(line, char_widths)
    char_gaps, ruby_left_ext, ruby_right_ext = _ruby_char_gaps(
        line, char_widths, active_rubies, style, intervals
    )
    total_w = _line_text_width(char_widths, style) + sum(char_gaps)
    # N3 anchors the logical DrawLineLeft/Right.  Secondary stroke, glow and
    # shadow may extend outside the requested horizontal margin.
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    center_override = _line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and _line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else _resolve_line_x_smart(
            img_w, total_w, track, source_line, style, lane,
            center_override=False,
        )
        if n3_main_center
        else _resolve_line_x_smart(
            img_w, total_w + left_ext + right_ext, track, source_line, style, lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else _resolve_baseline_y(metrics, img_h, style, ruby_metrics)
    )
    rtl = style.right_to_left
    char_lefts = _char_left_positions(
        char_widths,
        x0,
        rtl,
        _letter_spacing(style),
        char_gaps=char_gaps,
        n3_no_backtracking=style.layout_semantics == "n3_1074",
    )
    char_x_ranges: list[tuple[int, int]] = [
        (left, left + w) for left, w in zip(char_lefts, char_widths)
    ]
    text_layout = _build_text_layout(
        line, style, x0=x0, baseline_y=y, inline_styles=False, char_gaps=char_gaps
    )
    ink_x_ranges = _role_char_ink_ranges_by_index(line, text_layout, char_x_ranges)
    wipe_x_ranges = _n3_char_wipe_ranges_by_index(
        line, text_layout, char_x_ranges, ink_x_ranges
    )
    fill_segments = _karaoke_fill_segments(
        char_widths, intervals, ink_x_ranges, active_rubies, line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    line_rect = QRectF(
        float(x0), float(y - metrics.ascent()), float(total_w), float(metrics.height()),
    )
    colors = _effective_karaoke_colors(style)
    ruby_layouts = tuple(
        _layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if ruby_metrics is not None
        else ()
    )
    return _LineLayout(
        text_layout=text_layout,
        font=font, metrics=metrics, latin_font=latin_font, font_for=font_for,
        active_rubies=active_rubies, ruby_font=ruby_font, ruby_metrics=ruby_metrics,
        char_widths=char_widths, total_w=total_w, x0=x0, baseline_y=y,
        intervals=intervals, char_lefts=char_lefts, char_x_ranges=char_x_ranges,
        fill_segments=fill_segments, line_rect=line_rect, colors=colors, rtl=rtl,
        has_inline_styles=False, ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )


def _char_left_positions(
    char_widths: list[int],
    base_x: int,
    rtl: bool,
    letter_spacing_px: int = 0,
    char_gaps: list[int] | None = None,
    n3_no_backtracking: bool = False,
) -> list[int]:
    """每个字符左缘的 x 坐标。``rtl`` 时第一个字符排在最右、依次向左。

    ``char_gaps[i]`` = 字符 i 前插入的 ruby 避让间隙（仅 LTR，见
    :func:`_ruby_char_gaps`）。
    """
    lefts: list[int] = []
    total_w = sum(char_widths) + letter_spacing_px * max(len(char_widths) - 1, 0)
    if rtl:
        cursor = base_x + total_w
        for w in char_widths:
            cursor -= w
            lefts.append(cursor)
            advance = w + letter_spacing_px
            cursor -= max(advance, 0) - w if n3_no_backtracking else letter_spacing_px
    else:
        cursor = base_x
        for index, w in enumerate(char_widths):
            if char_gaps is not None and index < len(char_gaps):
                cursor += char_gaps[index]
            lefts.append(cursor)
            advance = w + letter_spacing_px
            cursor += max(advance, 0) if n3_no_backtracking else advance
    return lefts


_SUBTITLE_SCHEME_STYLE_FIELDS: tuple[str, ...] = (
    "font_family",
    "font_family_latin",
    "font_size_px",
    "letter_spacing_px",
    "space_width_percent",
    "latin_font_size_px",
    "latin_font_weight",
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "allow_biting",
    "font_weight",
    "italic",
    "affects_ruby_anchor",
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "stroke_width_px",
    "stroke2_enabled",
    "stroke2_width_px",
    "decoration_kind",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "ruby_font_size_px",
    "ruby_font_family",
    "ruby_font_family_latin",
    "ruby_font_weight",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_font_follow_main",
    "ruby_color",
    "ruby_gap_px",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
    "ruby_decoration_kind",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_colors_follow_main",
    "ruby_horizontal_gradient_with_main",
    "karaoke_colors",
    "ruby_karaoke_colors",
)


def _style_scheme_changes(scheme: SubtitleStyleScheme) -> dict[str, object]:
    return {
        field: value
        for field in _SUBTITLE_SCHEME_STYLE_FIELDS
        if (value := getattr(scheme, field)) is not None
    }


def _style_for_role(style: Style, role_label: str | None) -> Style:
    if not role_label:
        return style
    scheme = style.custom_style_schemes.get(role_label)
    if scheme is None:
        return style
    changes = _style_scheme_changes(scheme)
    if scheme.n3_font_inheritance:
        # These None values mean fallback to this scheme's Japanese/root slot,
        # not inheritance from the outer global scheme.
        changes.update(
            {field: getattr(scheme, field) for field in N3_FONT_INHERITANCE_FIELDS}
        )
    has_legacy_color_changes = any(
        getattr(scheme, field) is not None
        for field in (
            "base_color",
            "fill_color",
            "fill_gradient_enabled",
            "fill_gradient_start_color",
            "fill_gradient_end_color",
            "fill_gradient_angle_deg",
            "stroke_color",
            "shadow_color",
        )
    )
    if scheme.karaoke_colors is None and has_legacy_color_changes:
        changes["karaoke_colors"] = None
    if scheme.ruby_karaoke_colors is None and (
        scheme.karaoke_colors is not None or has_legacy_color_changes
    ):
        changes["ruby_karaoke_colors"] = None
    if not changes:
        return style
    return replace(style, **changes)


def _style_for_role_in_layout(style: Style, role_label: str | None) -> Style:
    """Apply role visuals while keeping the active layout's character spacing."""
    role_style = _style_for_role(style, role_label)
    return replace(
        role_style,
        **{name: getattr(style, name) for name in LYRICS_LAYOUT_CHAR_FIELDS},
    )


def _line_has_role_labels(line: TimingLine) -> bool:
    return any(bool(ch.role_label) for ch in line.chars)


def _line_with_guide_symbol(line: TimingLine) -> TimingLine:
    """Return a painter-only line with prefix and inline guide replacements applied."""
    if not line.chars:
        return line
    symbol = line.guide_symbol
    replacement_count = guide_symbol_replacement_count(line, symbol)
    chars = list(line.chars)
    inline_changed = False
    for index, inline_symbol in line.inline_guide_symbols.items():
        if (
            isinstance(index, int)
            and 0 <= index < len(chars)
            and isinstance(inline_symbol, GuideSymbol)
            and _guide_symbol_has_visual(inline_symbol)
        ):
            chars[index] = replace(
                chars[index], text="\uFFFC", vector_glyph=inline_symbol
            )
            inline_changed = True
    render_line = (
        replace(line, chars=chars, inline_guide_symbols={})
        if inline_changed
        else line
    )
    symbol = render_line.guide_symbol
    if symbol is None or not _guide_symbol_has_visual(symbol):
        return render_line
    if symbol.replacement_prefix:
        if replacement_count == 0:
            return render_line
        labels = guide_symbol_role_labels(symbol)
        guides = [
            TimingChar(
                text="\uFFFC",
                start_ms=int(source.start_ms),
                pause_release_ms=source.pause_release_ms,
                explicit_start=source.explicit_start,
                explicit_end=source.explicit_end,
                role_label=(
                    labels[index] if index < len(labels) else source.role_label
                ),
                vector_glyph=symbol,
            )
            for index, source in enumerate(render_line.chars[:replacement_count])
        ]
        return replace(
            render_line,
            chars=[*guides, *render_line.chars[replacement_count:]],
            guide_symbol=None,
            inline_guide_symbols={},
        )
    first_start = int(render_line.chars[0].start_ms)
    interval = max(int(symbol.duration_ms), 0)
    labels = guide_symbol_role_labels(symbol)
    guides = [
        TimingChar(
            text="\uFFFC",
            start_ms=(
                first_start
                if symbol.prefix_timing == "anchored"
                else first_start - interval * (len(labels) - index)
            ),
            role_label=label,
            vector_glyph=symbol,
        )
        for index, label in enumerate(labels)
    ]
    return replace(
        render_line,
        chars=[*guides, *render_line.chars],
        guide_symbol=None,
        inline_guide_symbols={},
    )


def _guide_symbol_has_visual(symbol: GuideSymbol) -> bool:
    return bool(symbol.path_commands) or (
        symbol.kind == "bitmap" and bool(symbol.bitmap_before_path)
    )


def _guide_symbol_is_bitmap(symbol: object | None) -> bool:
    return isinstance(symbol, GuideSymbol) and symbol.kind == "bitmap" and bool(
        symbol.bitmap_before_path
    )


def _bitmap_guide_is_no_wipe(symbol: object | None) -> bool:
    return _guide_symbol_is_bitmap(symbol) and not bool(
        getattr(symbol, "bitmap_after_path", None)
    )


def _bitmap_guide_image(path: str | None) -> QImage | None:
    if not path:
        return None
    signature = _image_file_signature(path)
    if signature is None:
        return None
    with _IMAGE_FILL_LOCK:
        cached = _BITMAP_GUIDE_CACHE.get(signature)
        if cached is not None:
            _BITMAP_GUIDE_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        return None
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    with _IMAGE_FILL_LOCK:
        existing = _BITMAP_GUIDE_CACHE.get(signature)
        if existing is not None:
            _BITMAP_GUIDE_CACHE.move_to_end(signature)
            return existing
        _BITMAP_GUIDE_CACHE[signature] = image
        while len(_BITMAP_GUIDE_CACHE) > _BITMAP_GUIDE_CACHE_MAX:
            _BITMAP_GUIDE_CACHE.popitem(last=False)
    return image


def _bitmap_guide_content_size(symbol: GuideSymbol, style: Style) -> tuple[int, int]:
    image = _bitmap_guide_image(symbol.bitmap_before_path)
    if image is None:
        image = _bitmap_guide_image(symbol.bitmap_after_path)
    if image is None or image.isNull():
        return 1, max(int(style.font_size_px), 1)
    if symbol.bitmap_fix_size:
        return max(int(image.width()), 1), max(int(image.height()), 1)
    target_h = max(
        max(int(style.font_size_px), 1) * max(int(symbol.bitmap_zoom_percent), 1) // 100,
        1,
    )
    target_w = max(int(round(image.width() * target_h / max(image.height(), 1))), 1)
    return target_w, target_h


def _bitmap_guide_anchor_descent(glyph: _GlyphLayout) -> int:
    style = glyph.style
    if style.layout_semantics == "n3_1074":
        return _fixed_line_geometry(style)[2]
    return max(int(glyph.metrics.descent()), 0)


def _vector_glyph_width(symbol, style: Style) -> int:
    if _guide_symbol_is_bitmap(symbol):
        content_w, _ = _bitmap_guide_content_size(symbol, style)
        return (
            content_w
            + int(symbol.bitmap_margin_left_px)
            + int(symbol.bitmap_margin_right_px)
        )
    return max(
        int(
            round(
                max(int(style.font_size_px), 1)
                * max(float(symbol.advance_width), 0.0)
                / max(int(symbol.units_per_em), 1)
            )
        ),
        1,
    )


def _glyph_path(glyph: _GlyphLayout, baseline_y: int) -> QPainterPath:
    if glyph.vector_glyph is not None:
        if _guide_symbol_is_bitmap(glyph.vector_glyph):
            return QPainterPath()
        return scaled_guide_symbol_path(
            glyph.vector_glyph,
            pixel_size=max(int(glyph.font.pixelSize()), 1),
            left=float(glyph.left),
            baseline_y=float(baseline_y),
        )
    path = QPainterPath()
    path.addText(
        float(glyph.left + glyph.path_offset_x),
        float(baseline_y),
        glyph.font,
        glyph.text,
    )
    return path


def _build_text_layout(
    line: TimingLine,
    style: Style,
    *,
    x0: int,
    baseline_y: int,
    inline_styles: bool,
    char_gaps: list[int] | None = None,
) -> _TextLayout:
    rtl = style.right_to_left
    measured: list[
        tuple[
            int,
            str,
            str | None,
            Style,
            Style,
            QFont,
            QFontMetrics,
            int,
            int,
            float,
            object | None,
        ]
    ] = []
    total_w = 0
    max_ascent = 0
    max_descent = 0
    plain_font = _build_font(style) if not inline_styles else None
    plain_metrics = QFontMetrics(plain_font) if plain_font is not None else None
    plain_latin_font = _build_latin_font(style) if not inline_styles else None
    plain_font_for = (
        _make_font_for(style, plain_font, plain_latin_font)
        if plain_font is not None and plain_latin_font is not None
        else None
    )
    plain_latin_metrics = (
        QFontMetrics(plain_latin_font)
        if plain_font_for is not None and plain_latin_font is not None
        else plain_metrics
    )
    inline_resource_cache: dict[
        str | None,
        tuple[Style, str | None, QFont, QFontMetrics, object, QFontMetrics],
    ] = {}
    for index, ch in enumerate(line.chars):
        if inline_styles:
            cached = inline_resource_cache.get(ch.role_label)
            if cached is None:
                role_style = _style_for_role_in_layout(style, ch.role_label)
                role_label = ch.role_label
                font = _build_font(role_style)
                metrics = QFontMetrics(font)
                latin_font = _build_latin_font(role_style)
                font_for = _make_font_for(role_style, font, latin_font)
                latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
                cached = (role_style, role_label, font, metrics, font_for, latin_metrics)
                inline_resource_cache[ch.role_label] = cached
            role_style, role_label, font, metrics, font_for, latin_metrics = cached
        else:
            role_style = style
            role_label = None
            font = plain_font
            metrics = plain_metrics
            font_for = plain_font_for
            latin_metrics = plain_latin_metrics
            if font is None or metrics is None or latin_metrics is None:
                continue
        is_guide = ch.vector_glyph is not None
        glyph_style = role_style if is_guide else _main_script_stroke_style(role_style, ch.text)
        glyph_font = font if is_guide else (font_for(ch.text) if font_for is not None else font)
        glyph_metrics = (
            latin_metrics
            if not is_guide and font_for is not None and _is_n3_latin_text(ch.text)
            else metrics
        )
        width = (
            _vector_glyph_width(ch.vector_glyph, role_style)
            if is_guide
            else _char_layout_width(
                ch.text, font, metrics, latin_metrics, font_for, glyph_style,
            )
        )
        spacing_after = _letter_spacing(role_style) if index < len(line.chars) - 1 else 0
        measured.append(
            (
                index,
                ch.text,
                role_label,
                glyph_style,
                role_style,
                glyph_font,
                glyph_metrics,
                width,
                spacing_after,
                0.0
                if is_guide
                else _char_path_left_offset(
                    ch.text, font, metrics, latin_metrics, font_for, glyph_style,
                ),
                ch.vector_glyph,
            )
        )
        advance = width + spacing_after
        total_w += (
            max(advance, 0)
            if style.layout_semantics == "n3_1074" and spacing_after
            else advance
        )
        # 空白字符无墨水，不参与行高（N3 按墨水轮廓求行盒）。否则半角空格走
        # 英数字体时，其超高 metrics（如 Comic Sans）会把注音顶离正文。
        if ch.text.strip():
            max_ascent = max(max_ascent, glyph_metrics.ascent())
            max_descent = max(max_descent, glyph_metrics.descent())

    if measured and max_ascent == 0 and max_descent == 0:
        # 整行都是空白：退回第一个字符的 metrics，保证行高非零。
        fallback_metrics = measured[0][6]
        max_ascent = fallback_metrics.ascent()
        max_descent = fallback_metrics.descent()

    gap_total = (
        sum(char_gaps[: len(line.chars)])
        if char_gaps is not None and not rtl
        else 0
    )
    layout_total_w = total_w + gap_total
    glyphs: list[_GlyphLayout] = []
    if rtl:
        cursor = x0 + layout_total_w
        for index, text, role_label, glyph_style, brush_style, glyph_font, metrics, width, spacing_after, path_offset_x, vector_glyph in measured:
            cursor -= width
            glyphs.append(
                _GlyphLayout(
                    index=index,
                    text=text,
                    role_label=role_label,
                    style=glyph_style,
                    font=glyph_font,
                    metrics=metrics,
                    left=cursor,
                    width=width,
                    path_offset_x=path_offset_x,
                    brush_style=brush_style,
                    vector_glyph=vector_glyph,
                )
            )
            advance = width + spacing_after
            cursor -= (
                max(advance, 0) - width
                if style.layout_semantics == "n3_1074" and spacing_after
                else spacing_after
            )
    else:
        cursor = x0
        for index, text, role_label, glyph_style, brush_style, glyph_font, metrics, width, spacing_after, path_offset_x, vector_glyph in measured:
            if char_gaps is not None and index < len(char_gaps):
                cursor += char_gaps[index]
            glyphs.append(
                _GlyphLayout(
                    index=index,
                    text=text,
                    role_label=role_label,
                    style=glyph_style,
                    font=glyph_font,
                    metrics=metrics,
                    left=cursor,
                    width=width,
                    path_offset_x=path_offset_x,
                    brush_style=brush_style,
                    vector_glyph=vector_glyph,
                )
            )
            advance = width + spacing_after
            cursor += (
                max(advance, 0)
                if style.layout_semantics == "n3_1074" and spacing_after
                else advance
            )

    height = max_ascent + max_descent
    line_rect = QRectF(
        float(x0),
        float(baseline_y - max_ascent),
        float(max(layout_total_w, 0)),
        float(max(height, 1)),
    )
    return _TextLayout(
        glyphs=glyphs,
        total_width=max(layout_total_w, 0),
        ascent=max_ascent,
        descent=max_descent,
        height=max(height, 1),
        line_rect=line_rect,
    )


def _build_role_text_layout(
    line: TimingLine,
    style: Style,
    *,
    x0: int,
    baseline_y: int,
    char_gaps: list[int] | None = None,
) -> _TextLayout:
    return _build_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=baseline_y,
        inline_styles=True,
        char_gaps=char_gaps,
    )


def _role_visual_text_padding(layout: _TextLayout) -> int:
    if not layout.glyphs:
        return 0
    return max(_visual_text_padding(glyph.style) for glyph in layout.glyphs)


def _resolve_role_baseline_y(
    layout: _TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pos = style.line_y_position
    margin = max(style.line_y_margin_px, 0)
    pad = _role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    if pos == "top":
        return margin + ruby_extra + pad + layout.ascent
    if pos == "center":
        block_h = layout.height + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + layout.ascent
    return img_h - margin - pad - layout.descent


def _clamp_role_baseline_y(
    baseline_y: int,
    layout: _TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pad = _role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    min_y = ruby_extra + pad + layout.ascent
    max_y = img_h - pad - layout.descent
    if max_y < min_y:
        return min_y
    return max(min_y, min(max_y, baseline_y))


def _glyph_run_signature(glyph: _GlyphLayout) -> tuple:
    colors = _effective_karaoke_colors(glyph.style)
    return (
        _karaoke_state_signature(colors.before),
        _karaoke_state_signature(colors.after),
        glyph.style.shadow_offset_x,
        glyph.style.shadow_offset_y,
        glyph.style.stroke_width_px,
        glyph.style.stroke2_width_px,
        glyph.style.decoration_kind,
        _glow_radius(glyph.style, after=False),
        _glow_radius(glyph.style, after=True),
        _glow_concentration_level(glyph.style),
    )


def _glyph_runs(layout: _TextLayout) -> list[list[_GlyphLayout]]:
    runs: list[list[_GlyphLayout]] = []
    current: list[_GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for glyph in layout.glyphs:
        style_id = id(glyph.style)
        signature = signature_cache.get(style_id)
        if signature is None:
            signature = _glyph_run_signature(glyph)
            signature_cache[style_id] = signature
        if not current or signature == current_signature:
            current.append(glyph)
            current_signature = signature
            continue
        runs.append(current)
        current = [glyph]
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def _glyph_is_bitmap_guide(glyph: _GlyphLayout) -> bool:
    return _guide_symbol_is_bitmap(glyph.vector_glyph)


def _text_glyph_runs(
    layout: _TextLayout, has_inline_styles: bool
) -> list[list[_GlyphLayout]]:
    runs = [layout.glyphs] if not has_inline_styles else _glyph_runs(layout)
    result: list[list[_GlyphLayout]] = []
    for run in runs:
        text_run = [glyph for glyph in run if not _glyph_is_bitmap_guide(glyph)]
        if text_run:
            result.append(text_run)
    return result


def _bitmap_guide_glyphs(layout: _TextLayout) -> list[_GlyphLayout]:
    return [glyph for glyph in layout.glyphs if _glyph_is_bitmap_guide(glyph)]


def _glyph_run_path(glyphs: list[_GlyphLayout], baseline_y: int) -> QPainterPath:
    path = QPainterPath()
    for glyph in glyphs:
        path.addPath(_glyph_path(glyph, baseline_y))
    return path


def _glyph_run_rect(glyphs: list[_GlyphLayout], baseline_y: int) -> QRectF:
    left = min(glyph.left for glyph in glyphs)
    right = max(glyph.left + glyph.width for glyph in glyphs)
    ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    descent = max(glyph.metrics.descent() for glyph in glyphs)
    return QRectF(
        float(left),
        float(baseline_y - ascent),
        float(max(right - left, 1)),
        float(max(ascent + descent, 1)),
    )


def _n3_main_fill_rect(layout: _TextLayout, baseline_y: int) -> QRectF:
    """Return N3's shared vertical brush area for one main-text line.

    ``DrawLineInfo.DrawTop/DrawBottom`` use the tallest ``FontSize + EdgeSize``
    character box.  ``SetMultiColorAreas`` then moves both gradient endpoints
    inward by half of ``EdgeSize + EdgeSize2`` from the *first* character's
    font slot.  All divisions in N3 are integer divisions.  The resulting
    rectangle is shared by every character and visual layer in the line; it is
    not the individual glyph ink/advance box.
    """
    glyphs = layout.glyphs
    if not glyphs:
        return QRectF(layout.line_rect)

    first = glyphs[0]
    font_size = max(int(first.font.pixelSize()), 1)
    metric_total = max(first.metrics.ascent() + first.metrics.descent(), 1)
    descent = font_size * max(first.metrics.descent(), 0) // metric_total
    brush_style = first.brush_style or first.style
    draw_edge = max(int(first.style.stroke_width_px), 0)
    anchor_edge = max(int(brush_style.stroke_width_px), 0)
    anchor_edge2 = _main_stroke2_width(brush_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_height = max(
        max(int(glyph.font.pixelSize()), 1)
        + max(int(glyph.style.stroke_width_px), 0)
        for glyph in glyphs
    )
    draw_top = draw_bottom - float(draw_height)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(layout.line_rect.left()),
        top,
        float(max(layout.line_rect.width(), 1.0)),
        float(max(bottom - top, 1.0)),
    )


def _layout_role_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
) -> _LineLayout | None:
    """layout 段：算分色行的纯几何（逐段多字体）+ 基线 + fill_segments（不依赖 t_ms）。"""
    has_shared_baseline = baseline_y is not None
    source_line = source_line or line
    active_rubies = _active_rubies_for_line(track.rubies, source_line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None
    measure_layout = _build_role_text_layout(line, style, x0=0, baseline_y=0)
    if not measure_layout.glyphs:
        return None
    char_widths, _measure_ranges = _role_char_geometry_by_index(line, measure_layout)
    intervals = compute_char_intervals(line, char_widths)
    ruby_extra = _role_ruby_vertical_extra(
        line, active_rubies, intervals, style
    )
    char_gaps, ruby_left_ext, ruby_right_ext = _ruby_char_gaps(
        line, char_widths, active_rubies, style, intervals
    )
    # 行盒左右不给描边留位（见 _line_total_width），只让 ruby 溢出撑开。
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    total_w = measure_layout.total_width + sum(char_gaps)
    center_override = _line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and _line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else _resolve_line_x_smart(
            img_w, total_w, track, source_line, style, lane,
            center_override=False,
        )
        if n3_main_center
        else _resolve_line_x_smart(
            img_w,
            total_w + left_ext + right_ext,
            track,
            source_line,
            style,
            lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else _resolve_role_baseline_y(measure_layout, img_h, style, ruby_extra)
    )
    # A display lane owns its baseline.  Clamping that shared value against a
    # large inline role/guide glyph would move only this line and mutate the
    # configured inter-line gap.  Self-positioned diagnostic callers still get
    # the historical canvas clamp.
    if not has_shared_baseline:
        y = _clamp_role_baseline_y(y, measure_layout, img_h, style, ruby_extra)
    text_layout = _build_role_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        char_gaps=char_gaps,
    )
    char_widths, char_x_ranges = _role_char_geometry_by_index(line, text_layout)
    intervals = compute_char_intervals(line, char_widths)
    ink_x_ranges = _role_char_ink_ranges_by_index(line, text_layout, char_x_ranges)
    wipe_x_ranges = _n3_char_wipe_ranges_by_index(
        line, text_layout, char_x_ranges, ink_x_ranges
    )
    fill_segments = _karaoke_fill_segments(
        char_widths, intervals, ink_x_ranges, active_rubies, line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    ruby_layouts = tuple(
        _layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if ruby_metrics is not None
        else ()
    )
    return _LineLayout(
        text_layout=text_layout, active_rubies=active_rubies,
        font=text_layout.glyphs[0].font, metrics=text_layout.glyphs[0].metrics,
        latin_font=_build_latin_font(style), font_for=None,
        ruby_font=ruby_font, ruby_metrics=ruby_metrics,
        char_widths=char_widths, total_w=text_layout.total_width,
        x0=int(text_layout.line_rect.left()), baseline_y=y,
        intervals=intervals,
        char_lefts=[rng[0] for rng in char_x_ranges],
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments, line_rect=text_layout.line_rect,
        colors=_effective_karaoke_colors(style), rtl=style.right_to_left,
        has_inline_styles=True, ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )


def _paint_line_layers(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
) -> None:
    """paint 段：消费 :class:`_LineLayout`，逐 run blit 未唱层 + 已唱层。"""
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _line_layer_stack(layout, t_ms),
    )


def _paint_line_direct(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
) -> None:
    """Vector oracle for horizontal static lines, sharing the baked path layout."""
    runs = _text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    y = layout.baseline_y
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    combined_glow_runs = [
        run for run in runs if _glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}

    # N3 builds both glow colours from outline sources clipped at WipeLeft,
    # blurs that combined decoration, and only then paints body/edges.
    for run in combined_glow_runs:
        _paint_glyph_run_combined_glow(
            painter,
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
    for run in runs:
        if id(run) in combined_run_ids:
            continue
        if not _glyph_run_needs_before_glow_split(run):
            continue
        before_band = _fill_clip_band_for_glyphs(
            layout.fill_segments, run, t_ms, layout.rtl
        )
        complete = _run_fill_complete(
            layout.fill_segments, {glyph.index for glyph in run}, t_ms
        )
        _paint_glyph_run_before_glow_direct(
            painter,
            run,
            y,
            before_band,
            rtl=layout.rtl,
            complete=complete,
            fill_rect=fill_rect,
        )

    for run in runs:
        if id(run) in combined_run_ids:
            continue
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = _run_fill_complete(
                layout.fill_segments, {glyph.index}, t_ms
            )
            following_band = _n3_following_wipe_band(
                layout.fill_segments, {glyph.index}, t_ms, layout.rtl
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if _glyph_run_needs_after_glow(glyph_run):
                _paint_glyph_run_after_glow_direct(
                    painter,
                    glyph_run,
                    y,
                    glyph_band,
                    rtl=layout.rtl,
                    complete=glyph_released,
                    fill_rect=fill_rect,
                )

    for run in runs:
        split_glow = _glyph_run_needs_before_glow_split(run)
        if not split_glow:
            _paint_glyph_run_direct(
                painter, run, y, after=False, fill_rect=fill_rect
            )
            continue
        before_band = _fill_clip_band_for_glyphs(
            layout.fill_segments, run, t_ms, layout.rtl
        )
        complete = _run_fill_complete(
            layout.fill_segments, {glyph.index for glyph in run}, t_ms
        )
        if complete:
            continue
        if before_band is None:
            _paint_glyph_run_direct(
                painter,
                run,
                y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
            continue
        painter.save()
        try:
            painter.setClipRect(
                _horizontal_before_clip_rect(before_band, layout.rtl)
            )
            _paint_glyph_run_direct(
                painter,
                run,
                y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
        finally:
            painter.restore()

    _paint_bitmap_guide_glyphs(painter, layout, t_ms, after=False)

    for run in runs:
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = _run_fill_complete(
                layout.fill_segments, {glyph.index}, t_ms
            )
            following_band = _n3_following_wipe_band(
                layout.fill_segments, {glyph.index}, t_ms, layout.rtl
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if glyph_released:
                _paint_glyph_run_direct(
                    painter, glyph_run, y, after=True, fill_rect=fill_rect
                )
                continue
            painter.save()
            try:
                painter.setClipRect(_horizontal_after_clip_rect(glyph_band, layout.rtl))
                _paint_glyph_run_direct(
                    painter, glyph_run, y, after=True, fill_rect=fill_rect
                )
            finally:
                painter.restore()

    _paint_bitmap_guide_glyphs(painter, layout, t_ms, after=True)


def _line_layer_stack(layout: _LineLayout, t_ms: int) -> list:
    runs = _text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    y = layout.baseline_y
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    combined_glow_runs = [
        run for run in runs if _glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}
    combined_glow_layers = [
        _GlyphRunSplitGlowLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in combined_glow_runs
    ]
    before_glow_layers = [
        _GlyphRunBeforeGlowLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in runs
        if id(run) not in combined_run_ids
        and _glyph_run_needs_before_glow_split(run)
    ]
    before_layers = [
        _GlyphRunLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            fill_rect=fill_rect,
        )
        for run in runs
    ]
    bitmap_before_layers = [
        _BitmapGuideLayer(
            glyph,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            z_index=len(runs) * 2,
        )
        for glyph in _bitmap_guide_glyphs(layout.text_layout)
    ]
    after_glow_layers = []
    after_body_layers = []
    bitmap_after_layers = []
    z_index = len(runs) * 2 + len(bitmap_before_layers)
    for run in runs:
        combined_glow = id(run) in combined_run_ids
        for glyph in run:
            glyph_run = [glyph]
            after_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if after_band is None or glyph.text.isspace():
                continue
            if not combined_glow and _glyph_run_needs_after_glow(glyph_run):
                after_glow_layers.append(
                    _GlyphRunAfterGlowLayer(
                        glyph_run,
                        y,
                        layout.fill_segments,
                        t_ms,
                        layout.rtl,
                        clip_band=after_band,
                        z_index=z_index,
                        fill_rect=fill_rect,
                    )
                )
                z_index += 1
            after_body_layers.append(
                _GlyphRunLayer(
                    glyph_run,
                    y,
                    layout.fill_segments,
                    t_ms,
                    layout.rtl,
                    after=True,
                    clip_band=after_band,
                    z_index=z_index,
                    fill_rect=fill_rect,
                )
            )
            z_index += 1
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        bitmap_after_layers.append(
            _BitmapGuideLayer(
                glyph,
                y,
                layout.fill_segments,
                t_ms,
                layout.rtl,
                after=True,
                z_index=z_index,
            )
        )
        z_index += 1
    # N3 composites the blurred decoration below every body/edge layer.
    return (
        combined_glow_layers
        + before_glow_layers
        + after_glow_layers
        + before_layers
        + bitmap_before_layers
        + after_body_layers
        + bitmap_after_layers
    )


def _horizontal_after_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    band_left, band_right = band
    if rtl:
        return QRectF(float(band_left), -1_000_000.0, 1_000_000.0, 2_000_000.0)
    return QRectF(-1_000_000.0, -1_000_000.0, float(band_right) + 1_000_000.0, 2_000_000.0)


def _horizontal_before_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    """Keep the before layer only on the unsung side of the wipe front."""
    band_left, band_right = band
    if rtl:
        return QRectF(
            -1_000_000.0,
            -1_000_000.0,
            float(band_left) + 1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        float(band_right),
        -1_000_000.0,
        1_000_000.0,
        2_000_000.0,
    )


def _bitmap_guide_target_rect(glyph: _GlyphLayout, baseline_y: int) -> QRectF | None:
    symbol = glyph.vector_glyph
    if not _guide_symbol_is_bitmap(symbol):
        return None
    width, height = _bitmap_guide_content_size(symbol, glyph.style)
    left = float(glyph.left + int(symbol.bitmap_margin_left_px))
    bottom = (
        baseline_y
        + _bitmap_guide_anchor_descent(glyph)
        - int(symbol.bitmap_margin_bottom_px)
    )
    top = float(bottom - height)
    return QRectF(left, top, float(max(width, 1)), float(max(height, 1)))


def _paint_bitmap_guide_glyph(
    painter: QPainter,
    glyph: _GlyphLayout,
    baseline_y: int,
    *,
    after: bool,
    band: tuple[int, int] | None,
    rtl: bool,
) -> None:
    symbol = glyph.vector_glyph
    if not _guide_symbol_is_bitmap(symbol):
        return
    image_path = (
        symbol.bitmap_after_path
        if after and symbol.bitmap_after_path
        else symbol.bitmap_before_path
    )
    image = _bitmap_guide_image(image_path)
    rect = _bitmap_guide_target_rect(glyph, baseline_y)
    if image is None or rect is None or image.isNull():
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if after and band is not None:
            painter.setClipRect(_horizontal_after_clip_rect(band, rtl))
        painter.drawImage(rect, image)
    finally:
        painter.restore()


def _bitmap_guide_band_for_segments(
    fill_segments: list[_FillSegment],
    glyph: _GlyphLayout,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    band = _fill_clip_band_for_glyphs(
        fill_segments, [glyph], t_ms, rtl
    )
    if band is None:
        band = _fill_clip_band(fill_segments, t_ms, rtl)
    following_band = _n3_following_wipe_band(
        fill_segments, {glyph.index}, t_ms, rtl
    )
    if following_band is not None:
        return following_band
    return band


def _bitmap_guide_band_for_glyph(
    layout: _LineLayout,
    glyph: _GlyphLayout,
    t_ms: int,
) -> tuple[int, int] | None:
    return _bitmap_guide_band_for_segments(
        layout.fill_segments, glyph, t_ms, layout.rtl
    )


def _paint_bitmap_guide_glyphs(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
    *,
    after: bool,
) -> None:
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        band = _bitmap_guide_band_for_glyph(layout, glyph, t_ms)
        if after and band is None:
            continue
        _paint_bitmap_guide_glyph(
            painter,
            glyph,
            layout.baseline_y,
            after=after,
            band=band,
            rtl=layout.rtl,
        )


def _paint_bitmap_guide_transition_glyph(
    painter: QPainter,
    glyph: _GlyphLayout,
    fill_segments: list[_FillSegment],
    baseline_y: int,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    t_ms: int,
    transition: _LineCharTransition,
    style: Style,
    *,
    rtl: bool,
) -> None:
    if not _glyph_is_bitmap_guide(glyph):
        return
    following_done_ms = (
        _utopia_following_done_time(line, intervals, index, style)
        if transition.effect == "utopia"
        else None
    )
    char_start_ms = intervals[index][0] if index < len(intervals) else glyph.index
    char_end_ms = intervals[index][1] if index < len(intervals) else char_start_ms
    opacity = _transition_char_state(
        style,
        transition,
        index,
        max(len(line.chars), 1),
        char_start_ms=char_start_ms,
        char_end_ms=char_end_ms,
        t_ms=t_ms,
        frame_height=painter.device().height(),
        following_done_ms=following_done_ms,
    )[0]
    if opacity <= 0.0:
        return
    band = _bitmap_guide_band_for_segments(fill_segments, glyph, t_ms, rtl)
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        _paint_bitmap_guide_glyph(
            painter, glyph, baseline_y, after=False, band=band, rtl=rtl
        )
        if band is not None:
            _paint_bitmap_guide_glyph(
                painter, glyph, baseline_y, after=True, band=band, rtl=rtl
            )
    finally:
        painter.restore()


@dataclass(frozen=True)
class _BitmapGuideLayer:
    glyph: _GlyphLayout
    baseline_y: int
    fill_segments: list
    t_ms: int
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_BitmapGuideLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        return BakedLayer(image=QImage(), offset=QPointF())

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        band = _bitmap_guide_band_for_segments(
            self.fill_segments, self.glyph, self.t_ms, self.rtl
        )
        if self.after and band is None:
            return
        _paint_bitmap_guide_glyph(
            painter,
            self.glyph,
            self.baseline_y,
            after=self.after,
            band=band,
            rtl=self.rtl,
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _bitmap_guide_target_rect(self.glyph, self.baseline_y)
        if rect is None:
            return None
        return int(math.floor(rect.top())), int(math.ceil(rect.bottom()))


def _paint_glyph_run_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    draw_glow: bool | None = None,
) -> None:
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    state = colors.after if after else colors.before
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    if draw_glow is None:
        draw_glow = not (after and role_style.decoration_kind == "glow")
    _paint_text_layer_stack(
        painter,
        path,
        rect,
        state,
        role_style,
        stroke_width=role_style.stroke_width_px,
        stroke2_width=role_style.stroke2_width_px,
        shadow_dx=role_style.shadow_offset_x,
        shadow_dy=role_style.shadow_offset_y,
        glow_radius=_glow_radius(role_style, after=after),
        draw_glow=draw_glow,
        fill_rect=fill_rect,
    )


def _after_glow_loose_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF:
    """已唱发光的宽松裁切矩形（理由见 ``_paint_char_karaoke_stack`` 内注释）。

    N3 的 WipeLeft 用字形轮廓加一半一重描边计算锋面，不包含二重描边或 glow/blur
    半径；N3 随后才对已裁剪的描边源做 blur。这里的 after-glow 是已经预先 blur 好的
    位图，所以走字中的前缘必须严格停在扫光线，避免把未唱侧的 glow 位图切进来形成
    粗亮竖边。``glow_pad`` 只用于尾缘、上下和唱完后的边缘释放。
    """
    band_left, band_right = band
    glow_pad_f = float(glow_pad)
    left = float(band_left) - (0.0 if rtl and not complete else glow_pad_f)
    right = float(band_right) + (glow_pad_f if rtl or complete else 0.0)
    return QRectF(
        left,
        rect.top() - glow_pad,
        right - left,
        rect.height() + glow_pad * 2,
    )


def _paint_glyph_run_after_glow_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    pad = _glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        _glow_radius(role_style, after=True),
    )
    _paint_glow_path(
        painter,
        path,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        _glow_radius(role_style, after=True),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=_after_glow_source_clip_rect(band, rect, pad, rtl, complete),
        concentration_level=_glow_concentration_level(role_style),
    )


def _paint_glyph_run_before_glow_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int] | None,
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """Paint N3's before-glow by clipping the stroke source before blur."""
    if complete:
        return
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    pad = _glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        _glow_radius(role_style, after=False),
    )
    if band is not None and _glow_cache_enabled():
        front = float(band[0] if rtl else band[1])
        _paint_cached_run_glow_source_wipe(
            painter,
            path,
            rect,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=False,
            front=front,
            rtl=rtl,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    _paint_glow_path(
        painter,
        path,
        colors.before.shadow,
        fill_rect if fill_rect is not None else rect,
        _glow_radius(role_style, after=False),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=(
            _before_glow_source_clip_rect(band, rect, pad, rtl)
            if band is not None
            else None
        ),
        concentration_level=_glow_concentration_level(role_style),
    )


def _paint_full_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    fill_rect: QRectF | None,
) -> None:
    """Paint an entire source-clipped glow after geometry transforms."""
    before_radius = _glow_radius(role_style, after=False)
    after_radius = _glow_radius(role_style, after=True)
    stroke2_width = _main_stroke2_width(role_style)
    if before_radius > 0 and before_radius == after_radius:
        pad = _glow_extent(
            role_style.stroke_width_px, stroke2_width, before_radius
        )
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if rtl:
            before_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
        _paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            before_radius,
            role_style.stroke_width_px,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=_glow_concentration_level(role_style),
        )
        return

    for after in (False, True):
        radius = _glow_radius(role_style, after=after)
        pad = _glow_extent(
            role_style.stroke_width_px, stroke2_width, radius
        )
        source_is_right = rtl == after
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if source_is_right
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        state = colors.after if after else colors.before
        _paint_glow_path(
            painter,
            path,
            state.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            source_clip=source_clip,
            concentration_level=_glow_concentration_level(role_style),
        )


def _paint_cached_run_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> None:
    """Blur only the moving front strip and reuse cached full glow elsewhere.

    Source-clipped blur differs from the full cached halo only within one
    Gaussian support radius of ``front``.  N3's source-before-blur result is
    therefore reproduced by rasterising that narrow strip dynamically and
    clipping the cached full halo to the unaffected far side.
    """
    radius = _glow_radius(role_style, after=after)
    if radius <= 0:
        return
    state = colors.after if after else colors.before
    stroke2_width = _main_stroke2_width(role_style)
    pad = _glow_extent(role_style.stroke_width_px, stroke2_width, radius)
    top = rect.top() - pad
    height = rect.height() + pad * 2
    source_is_right = rtl == after
    if source_is_right:
        source_clip = QRectF(front, top, 1_000_000.0, height)
        baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        source_clip = QRectF(
            -1_000_000.0,
            top,
            front + 1_000_000.0,
            height,
        )
        baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    _paint_glow_path(
        painter,
        path,
        state.shadow,
        fill_rect if fill_rect is not None else rect,
        radius,
        role_style.stroke_width_px,
        stroke2_width,
        source_clip=source_clip,
        concentration_level=_glow_concentration_level(role_style),
        target_clip=strip_clip,
    )
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
    finally:
        painter.restore()


def _paint_cached_run_split_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> bool:
    """N3 fast path: one dynamic blur for both colours plus cached far halos."""
    before_radius = _glow_radius(role_style, after=False)
    after_radius = _glow_radius(role_style, after=True)
    if before_radius <= 0 or before_radius != after_radius:
        return False
    stroke2_width = _main_stroke2_width(role_style)
    pad = _glow_extent(
        role_style.stroke_width_px, stroke2_width, before_radius
    )
    top = rect.top() - pad
    height = rect.height() + pad * 2
    if rtl:
        before_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        after_source_clip = QRectF(front, top, 1_000_000.0, height)
        before_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        before_source_clip = QRectF(front, top, 1_000_000.0, height)
        after_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        before_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    _paint_split_glow_path(
        painter,
        path,
        colors.before.shadow,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        before_radius,
        role_style.stroke_width_px,
        stroke2_width,
        before_source_clip=before_source_clip,
        after_source_clip=after_source_clip,
        concentration_level=_glow_concentration_level(role_style),
        target_clip=strip_clip,
    )
    for after, clip in (
        (False, before_baked_clip),
        (True, after_baked_clip),
    ):
        painter.save()
        try:
            painter.setClipRect(clip)
            _blit_cached_run_glow(
                painter,
                glyphs,
                baseline_y,
                role_style,
                colors,
                after=after,
                transform=transform,
                fill_rect=fill_rect,
            )
        finally:
            painter.restore()
    return True


def _glyph_run_can_combine_split_glow(glyphs: list[_GlyphLayout]) -> bool:
    if not _glyph_run_needs_before_glow_split(glyphs):
        return False
    style = glyphs[0].style
    before_radius = _glow_radius(style, after=False)
    return before_radius > 0 and before_radius == _glow_radius(style, after=True)


def _paint_glyph_run_combined_glow(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    fill_segments: list[_FillSegment],
    t_ms: int,
    rtl: bool,
    *,
    fill_rect: QRectF | None,
) -> None:
    """Paint one static run's N3 before/after decoration with one blur."""
    style = glyphs[0].style
    colors = _effective_karaoke_colors(style)
    indices = {glyph.index for glyph in glyphs}
    band = _fill_clip_band_for_glyphs(fill_segments, glyphs, t_ms, rtl)
    complete = _run_fill_complete(fill_segments, indices, t_ms)
    if band is None:
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=False,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    if complete:
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=True,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    front = float(band[0] if rtl else band[1])
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    _paint_cached_run_split_glow_source_wipe(
        painter,
        path,
        rect,
        glyphs,
        baseline_y,
        style,
        colors,
        front=front,
        rtl=rtl,
        transform=None,
        fill_rect=fill_rect,
    )


def _afterglow_strip_enabled() -> bool:
    """走字中 after-glow 只逐帧模糊扫光前沿窄带（默认开）。

    ``KROK_SUBTITLE_AFTERGLOW_STRIP=0`` 退回整行逐帧
    ``_paint_glyph_run_after_glow_direct``（A/B 像素 oracle / 紧急回退用）。
    """
    return os.environ.get("KROK_SUBTITLE_AFTERGLOW_STRIP", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _paint_glyph_run_after_glow_wipe(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """走字中的已唱发光：前沿窄带逐帧模糊 + 其余贴整段烘焙位图。

    N3 语义要求「先按扫光线裁源、再模糊」让前沿保持柔和，因此该层无法整层烘焙。
    但 blur(裁切源) 与 blur(完整源) 只在扫光线 ±支撑半径（``_glow_extent``，≥3×radius）
    内不同：seam（前沿 - pad）之前两者逐像素一致 → 直接贴 ``_RUN_GLOW_CACHE`` 里
    整段 after-glow 烘焙；seam 之后仅对 2×pad 宽的窄带做逐帧 stroke+blur。模糊成本
    随画布面积线性，长行收益一个数量级。"""
    role_style = glyphs[0].style
    if complete or not _afterglow_strip_enabled() or not _glow_cache_enabled():
        _paint_glyph_run_after_glow_direct(
            painter,
            glyphs,
            baseline_y,
            band,
            rtl=rtl,
            complete=complete,
            fill_rect=fill_rect,
        )
        return
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    radius = _glow_radius(role_style, after=True)
    pad = _glow_extent(role_style.stroke_width_px, role_style.stroke2_width_px, radius)
    band_left, band_right = band
    front = float(band_left) if rtl else float(band_right)
    # 前沿窄带 [front-pad, front+pad]；seam 在其已唱侧边缘。
    if rtl:
        seam = front + pad
        strip_clip = QRectF(front - pad, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(seam, -1_000_000.0, 1_000_000.0, 2_000_000.0)
    else:
        seam = front - pad
        strip_clip = QRectF(seam, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(-1_000_000.0, -1_000_000.0, seam + 1_000_000.0, 2_000_000.0)
    painter.save()
    try:
        painter.setClipRect(strip_clip)
        _paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            source_clip=_after_glow_source_clip_rect(band, rect, pad, rtl, complete),
            concentration_level=_glow_concentration_level(role_style),
            target_clip=strip_clip,
        )
    finally:
        painter.restore()
    baked = _get_or_build_run_glow(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _after_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF | None:
    """Source clip for N3-style glow wiping.

    N3 clips the edge source by ``WipeLeft`` and then blurs the resulting
    work bitmap.  Returning this as ``source_clip`` keeps the visible glow front
    soft; clipping the already-blurred bitmap would create a hard vertical edge.
    """
    if complete:
        return None
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(float(band_left), top, 1_000_000.0, height)
    return QRectF(-1_000_000.0, top, float(band_right) + 1_000_000.0, height)


def _before_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
) -> QRectF:
    """Unsing side of N3's source geometry, before Gaussian blur."""
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(-1_000_000.0, top, float(band_left) + 1_000_000.0, height)
    return QRectF(float(band_right), top, 1_000_000.0, height)


def _spin_flip_char_transform(
    glyph: _GlyphLayout,
    baseline_y: int,
    transition: _LineCharTransition,
    opacity: float,
) -> QTransform | None:
    """A2：spin_flip 逐字的 scale(opacity)+skew 残差变换，绕字心枢轴。

    复用 ``_character_transform``（与旧 ``_apply_character_transform`` 同一构造、
    几何完全一致），把枢轴烘焙进矩阵；compositor 把它作为残差套在烘焙位图上
    （bitmap-transform，短窗口软化可接受，见 §9.7 D2）。返回恒等时给 ``None``。
    """
    direction = 1.0 if transition.phase == "exit" else -1.0
    skew_y = direction * _spin_flip_skew(opacity)
    center_x = glyph.left + glyph.width / 2
    center_y = baseline_y - glyph.metrics.ascent() + glyph.metrics.height() / 2
    transform = _character_transform(
        center_x=center_x,
        center_y=center_y,
        scale_x=opacity,
        scale_y=opacity,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def _char_drip_char_transform(
    glyph: _GlyphLayout,
    baseline_y: int,
    transition: _LineCharTransition,
    progress: float,
) -> QTransform | None:
    """N3 ``CharDrip``: shear around the glyph's right-bottom/right-top corner.

    N3 uses ``(drawWidth, 0)`` for intro and ``(drawWidth, -height)`` for
    outro in glyph-local coordinates.  Unlike ``CharFadeInFadeOut`` it uses
    the inherited opacity value only as transform progress; visible geometry
    itself remains opaque once progress is non-zero.
    """
    direction = 1.0 if transition.phase == "entry" else -1.0
    skew_y = direction * _spin_flip_skew(progress)
    pivot_x = glyph.left + glyph.width
    pivot_y = (
        baseline_y
        if transition.phase == "entry"
        else baseline_y - glyph.metrics.height()
    )
    transform = _character_transform(
        center_x=pivot_x,
        center_y=pivot_y,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def _char_transition_layer_stack(
    layout: _LineLayout,
    t_ms: int,
    transition: _LineCharTransition,
    char_count: int,
) -> list:
    """A1/A2（§9.7）：逐字入退场走 LayerCompositor。

    每个 glyph 复用静态路径的 ``_GlyphRunLayer`` / ``_GlyphRunAfterGlowLayer``
    烘焙缓存（直立烘焙一次、跨帧复用），逐帧只补该字的残差：
    - **char_fade**：仅淡入/淡出 opacity（无损）；
    - **char_drip**：按 N3 从字的右下/右上枢轴纵向剪切，几何保持不透明；
    - **spin_flip**：opacity + scale(opacity)+skew 残差变换（绕字心枢轴，
      bitmap-transform 软化可接受，§9.7 D2）。
    glow 也因此并入烘焙缓存、不再每帧重算高斯。与旧逐帧
    ``_paint_char_karaoke_stack`` 路径同口径：逐字独立栈、按 glyph 顺序交错绘制
    （后字覆盖前字），扫光带取整行 ``fill_segments``（与静态路径同一来源），
    同一字的 before/after/glow 三层套同一残差变换。
    适用于普通行与分色行（per-glyph ``style``/``metrics`` 已携带角色样式）。
    """
    y = layout.baseline_y
    rtl = layout.rtl
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    is_spin = transition.effect == "spin_flip"
    is_drip = transition.effect == "char_drip"
    before_glow_layers: list = []
    after_glow_layers: list = []
    body_layers: list = []
    z = 0
    for glyph in layout.text_layout.glyphs:
        progress = _char_fade_opacity(transition, glyph.index, char_count, t_ms=t_ms)
        if progress <= 0.0:
            continue
        opacity = 1.0 if is_drip else progress
        if is_spin:
            transform = _spin_flip_char_transform(glyph, y, transition, progress)
        elif is_drip:
            transform = _char_drip_char_transform(glyph, y, transition, progress)
        else:
            transform = None
        run = [glyph]
        if _glyph_run_needs_before_glow_split(run):
            before_glow_layers.append(
                _GlyphRunBeforeGlowLayer(
                    run, y, layout.fill_segments, t_ms, rtl,
                    z_index=z, fade_opacity=opacity, transform=transform,
                    fill_rect=fill_rect,
                )
            )
        body_layers.append(
            _GlyphRunLayer(
                run, y, layout.fill_segments, t_ms, rtl,
                after=False, z_index=z, fade_opacity=opacity, transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
        after_band = _fill_clip_band_for_glyphs(layout.fill_segments, run, t_ms, rtl)
        if after_band is None:
            continue
        if _glyph_run_needs_after_glow(run):
            after_glow_layers.append(
                _GlyphRunAfterGlowLayer(
                    run, y, layout.fill_segments, t_ms, rtl,
                    clip_band=after_band, z_index=z, fade_opacity=opacity, transform=transform,
                    fill_rect=fill_rect,
                )
            )
            z += 1
        body_layers.append(
            _GlyphRunLayer(
                run, y, layout.fill_segments, t_ms, rtl,
                after=True, clip_band=after_band, z_index=z, fade_opacity=opacity, transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
    return before_glow_layers + after_glow_layers + body_layers


@dataclass(frozen=True)
class _GlyphRunLayer:
    """Layer wrapper for a horizontal text glyph run body."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    after: bool
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        state = colors.after if self.after else colors.before
        return (
            _glyph_run_layer_key(self.glyphs, role_style, colors, after=self.after),
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=_karaoke_state_uses_image(state),
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_layer(
            self.glyphs,
            role_style,
            colors,
            after=self.after,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        clip_rect = None
        role_style = self.glyphs[0].style
        # N3 硬分割：前后发光不同（颜色或半径）时，未唱层（含发光）整体裁到
        # 扫光线未唱侧，已唱侧由已唱层 + 已唱发光层负责；相同时未唱层整行铺满、
        # 已唱发光层被跳过（结果等价）。
        if (
            not self.after
            and _glow_radius(role_style, after=False) > 0
            and _karaoke_glow_states_differ(
                role_style, _effective_karaoke_colors(role_style)
            )
        ):
            band = _fill_clip_band_for_glyphs(
                self.fill_segments, self.glyphs, self.t_ms, self.rtl
            )
            if band is not None:
                if _run_fill_complete(
                    self.fill_segments,
                    {glyph.index for glyph in self.glyphs},
                    self.t_ms,
                ):
                    return LayerAnimation(opacity=0.0)
                clip_rect = _horizontal_before_clip_rect(band, self.rtl)
        elif self.after:
            indices = {glyph.index for glyph in self.glyphs}
            following_band = _n3_following_wipe_band(
                self.fill_segments, indices, self.t_ms, self.rtl
            )
            band = following_band or self.clip_band or _fill_clip_band(
                self.fill_segments, self.t_ms, self.rtl
            )
            if band is None:
                return LayerAnimation(opacity=0.0)
            band_left, band_right = band
            if (
                _run_fill_complete(self.fill_segments, indices, self.t_ms)
                and following_band is None
            ):
                # 唱完后不裁切：带缘停在墨水边界，再裁会把行缘的描边/阴影硬截掉。
                clip_rect = None
            elif self.rtl:
                clip_rect = QRectF(float(band_left), -1_000_000.0, 1_000_000.0, 2_000_000.0)
            else:
                clip_rect = QRectF(-1_000_000.0, -1_000_000.0, float(band_right) + 1_000_000.0, 2_000_000.0)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        pad = _text_visual_padding(self.glyphs[0].style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunBeforeGlowLayer:
    """N3 before-glow: split the outline source at WipeLeft, then blur it."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunBeforeGlowLayer":
        return self

    def _state(self) -> tuple[tuple[int, int] | None, bool]:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or _fill_clip_band_for_glyphs(
            self.fill_segments, self.glyphs, self.t_ms, self.rtl
        )
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        return band, complete

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        band, complete = self._state()
        if complete or band is not None:
            return None
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        return (
            _glyph_run_layer_key(self.glyphs, role_style, colors, after=False),
            "before-glow",
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.before.shadow.mode == "image",
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_glow_layer(
            self.glyphs,
            role_style,
            colors,
            after=False,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        band, complete = self._state()
        if complete:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.fade_opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            _paint_glyph_run_before_glow_direct(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=False,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=False),
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunSplitGlowLayer:
    """Combined before/after decoration for equal-radius N3 glow wipes."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunSplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("combined split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        _paint_glyph_run_combined_glow(
            painter,
            self.glyphs,
            self.baseline_y,
            self.fill_segments,
            self.t_ms,
            self.rtl,
            fill_rect=self.fill_rect,
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        style = self.glyphs[0].style
        pad = max(
            _glow_extent(
                style.stroke_width_px,
                style.stroke2_width_px,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunAfterGlowLayer:
    """Layer wrapper for the after-glow bitmap of a horizontal glyph run."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunAfterGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        if role_style.decoration_kind != "glow":
            return None
        before_radius = _glow_radius(role_style, after=False)
        after_radius = _glow_radius(role_style, after=True)
        if after_radius == 0:
            return None
        need_after_glow = (
            _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
            or before_radius != after_radius
        )
        band = self.clip_band or _fill_clip_band(self.fill_segments, self.t_ms, self.rtl)
        if not need_after_glow or band is None:
            return None
        indices = {glyph.index for glyph in self.glyphs}
        if not _run_fill_complete(
            self.fill_segments, indices, self.t_ms
        ) or _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        ) is not None:
            return None
        return (
            _glyph_run_after_glow_key(self.glyphs, role_style, colors),
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.after.shadow.mode == "image",
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_after_glow_layer(
            self.glyphs,
            role_style,
            colors,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or self.clip_band or _fill_clip_band(
            self.fill_segments, self.t_ms, self.rtl
        )
        if band is None:
            return LayerAnimation(opacity=0.0)
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=True),
        )
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        clip_rect = None if complete else _after_glow_loose_clip_rect(
            band,
            rect,
            pad,
            self.rtl,
            complete,
        )
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or self.clip_band or _fill_clip_band(
            self.fill_segments, self.t_ms, self.rtl
        )
        if band is None:
            return
        opacity = max(0.0, min(float(self.fade_opacity), 1.0))
        if opacity <= 0.0:
            return
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            _paint_glyph_run_after_glow_wipe(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=complete,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=True),
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _ScopeBoundsLayer:
    """Bounds-only layer used while a dynamic effect is not yet fully layerized."""

    rect: QRectF
    scope_id: Hashable
    z_index: int = 0
    scope: str = SCOPE_GROUP

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_ScopeBoundsLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> Hashable | None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("bounds-only layers are never baked")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(clip_rect=self.rect)

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        return int(math.floor(self.rect.top())), int(math.ceil(self.rect.bottom()))


def _utopia_transition_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    """Return conservative group-scope bounds for the existing utopia dynamic path."""
    if transition.effect != "utopia":
        return []
    layers = _utopia_main_scope_layers(layout, line, style, t_ms, transition, frame_height)
    if layout.active_rubies and layout.ruby_metrics is not None:
        layers.extend(
            _utopia_ruby_scope_layers(layout, line, style, t_ms, transition, frame_height)
        )
    return layers


def _utopia_main_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    glyphs_by_index = _role_glyphs_by_index(line, layout.text_layout)
    count = max(len(line.chars), 1)
    layers: list[_ScopeBoundsLayer] = []
    handled_indices: set[int] = set()
    ruby_groups = _resolve_char_ruby_groups(layout.active_rubies, line, layout.intervals)
    for index in range(len(line.chars)):
        if index in handled_indices:
            continue
        if index >= len(layout.intervals) or index >= len(layout.char_x_ranges):
            continue
        if index >= len(glyphs_by_index) or glyphs_by_index[index] is None:
            continue
        group = _utopia_main_group_for_index(layout.active_rubies, line, layout.intervals, index, groups=ruby_groups)
        group_ruby: RubyAnnotation | None = None
        group_scope_indices: list[int] | None = None
        group_done_ms: int | None = None
        if group is not None:
            group_scope_indices, group_ruby = group
            group_done_ms = _utopia_following_done_time(
                line, layout.intervals, group_scope_indices[-1], style
            )
            group_exiting = t_ms > group_done_ms
            if group_exiting and index != group_scope_indices[0]:
                continue
            if group_exiting:
                indices = [
                    i
                    for i in group_scope_indices
                    if i < len(layout.intervals)
                    and i < len(layout.char_x_ranges)
                    and i < len(glyphs_by_index)
                    and glyphs_by_index[i] is not None
                ]
                handled_indices.update(indices[1:])
            else:
                indices = [index]
        else:
            indices = [index]
            group_scope_indices = indices

        if not indices:
            continue
        first_index = indices[0]
        last_index = indices[-1]
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, layout.intervals, last_index, style)
        )
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            layout.intervals,
            layout.char_x_ranges,
            ruby_groups,
            first_index,
            style,
            fallback_start=layout.intervals[first_index][0],
            fallback_end=layout.intervals[last_index][1],
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=frame_height,
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue
        group_glyphs = [glyphs_by_index[i] for i in indices if glyphs_by_index[i] is not None]
        if not group_glyphs:
            continue
        left = min(layout.char_x_ranges[i][0] for i in indices)
        right = max(layout.char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        group_rect = _glyph_run_rect(group_glyphs, layout.baseline_y)
        transform = _character_transform(
            center_x=left + width / 2,
            center_y=group_rect.top() + group_rect.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=left,
            scale_origin_y=layout.baseline_y,
        )
        rect = transform.map(_glyph_run_path(group_glyphs, layout.baseline_y)).boundingRect()
        pad = max(
            _text_visual_padding(glyph.style, after=False) for glyph in group_glyphs
        )
        pad = max(
            pad,
            max(_text_visual_padding(glyph.style, after=True) for glyph in group_glyphs),
        )
        layers.append(
            _ScopeBoundsLayer(
                _inflate_rect(rect, pad),
                _utopia_scope_id(line, group_scope_indices, group_ruby, "main"),
                z_index=index,
            )
        )
    return layers


def _utopia_ruby_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    if layout.ruby_metrics is None:
        return []
    layers: list[_ScopeBoundsLayer] = []
    for index, ruby_layout in enumerate(layout.ruby_layouts):
        if not ruby_layout.indices:
            continue
        target_ruby_font = ruby_layout.font or layout.ruby_font
        target_ruby_metrics = ruby_layout.metrics or layout.ruby_metrics
        rect = _utopia_ruby_scope_rect(
            ruby_layout,
            target_ruby_font,
            target_ruby_metrics,
            line,
            layout.intervals,
            layout.rtl,
            ruby_layout.style,
            t_ms,
            transition,
            frame_height,
        )
        if rect is None:
            continue
        pad = max(
            _ruby_visual_padding(ruby_layout.style, after=False),
            _ruby_visual_padding(ruby_layout.style, after=True),
        )
        layers.append(
            _ScopeBoundsLayer(
                _inflate_rect(rect, pad),
                _utopia_scope_id(line, ruby_layout.indices, ruby_layout.ruby, "ruby"),
                z_index=10_000 + index,
            )
        )
    return layers


def _utopia_ruby_scope_rect(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    rtl: bool,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> QRectF | None:
    first_index = min(layout.indices)
    last_index = max(layout.indices)
    if first_index >= len(intervals) or last_index >= len(intervals):
        return None
    following_done_ms = _utopia_following_done_time(line, intervals, last_index, style)
    ruby_groups = _resolve_char_ruby_groups([layout.ruby], line, intervals)
    char_x_ranges = [(layout.x, layout.x + layout.target_width) for _index in line.chars]
    char_start, char_end = _utopia_wipe_window_for_index(
        line,
        intervals,
        char_x_ranges,
        ruby_groups,
        first_index,
        style,
        fallback_start=intervals[first_index][0],
        fallback_end=intervals[last_index][1],
    )
    opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
        style,
        transition,
        first_index,
        max(len(line.chars), 1),
        char_start_ms=char_start,
        char_end_ms=char_end,
        t_ms=t_ms,
        frame_height=frame_height,
        following_done_ms=following_done_ms,
    )
    if opacity <= 0.0:
        return None
    group_exiting = len(layout.indices) > 1 and t_ms > following_done_ms
    if group_exiting:
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
            if rtl
            else layout.ruby.reading
        )
        path, _ = _ruby_text_path_and_rect(
            reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        transform = _character_transform(
            center_x=layout.x + layout.reading_width / 2,
            center_y=layout.baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=layout.x,
            scale_origin_y=layout.baseline_y,
        )
        return transform.map(path).boundingRect()

    visual_units = _ruby_utopia_reading_units_and_intervals(layout.ruby)
    if rtl:
        visual_units = list(reversed(visual_units))
    units = [unit for unit, _interval in visual_units]
    unit_intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(unit_intervals):
        path, _ = _ruby_text_path_and_rect(
            layout.ruby.reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        return path.boundingRect()

    rect: QRectF | None = None
    for (unit, unit_x, unit_width), (start_ms, end_ms) in zip(
        _ruby_layout_units(
            units,
            ruby_metrics,
            layout.x,
            layout.target_width,
            style=style,
            base_text=layout.ruby.kanji,
        ),
        unit_intervals,
    ):
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            max(len(line.chars), 1),
            char_start_ms=start_ms,
            char_end_ms=end_ms,
            t_ms=t_ms,
            frame_height=frame_height,
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue
        path = QPainterPath()
        path.addText(float(unit_x), float(layout.baseline_y), ruby_font, unit)
        transform = _character_transform(
            center_x=unit_x + unit_width / 2,
            center_y=layout.baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=unit_x,
            scale_origin_y=layout.baseline_y,
        )
        unit_rect = transform.map(path).boundingRect()
        rect = unit_rect if rect is None else rect.united(unit_rect)
    return rect


def _utopia_scope_id(
    line: TimingLine,
    indices: list[int],
    ruby: RubyAnnotation | None,
    kind: str,
) -> tuple:
    return (
        "utopia",
        kind,
        _line_start_ms(line),
        _line_end_ms(line),
        tuple(indices),
        ruby.kanji if ruby is not None else "",
        ruby.reading if ruby is not None else "",
    )


def _inflate_rect(rect: QRectF, pad: int | float) -> QRectF:
    pad_f = float(max(pad, 0))
    return rect.adjusted(-pad_f, -pad_f, pad_f, pad_f)


def _glyph_run_layer_key(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
) -> tuple:
    """run 层缓存 key：run 内逐字形（文本/字体/相对 x/宽）+ 角色样式签名 + 状态。

    扫光带不进 key（blit 时半平面 clip 处理）；run 绝对位置不进 key（blit offset 复位）。
    """
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            _value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    state = colors.after if after else colors.before
    return (
        glyph_sig,
        _karaoke_state_signature(state),
        role_style.shadow_offset_x,
        role_style.shadow_offset_y,
        role_style.stroke_width_px,
        _main_stroke2_width(role_style),
        role_style.decoration_kind,
        _glow_radius(role_style, after=False),
        _glow_concentration_level(role_style),
        after,
    )


def _relative_fill_rect_signature(
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    fill_rect: QRectF | None,
    *,
    global_anchor: bool = False,
) -> tuple[float, float, float, float] | None:
    """Return the brush coordinates that affect a cached glyph run."""
    run_left = min(glyph.left for glyph in glyphs)
    if global_anchor:
        if fill_rect is None:
            return (
                round(float(run_left), 3),
                round(float(baseline_y), 3),
                0.0,
                0.0,
            )
        return (
            round(float(fill_rect.left()), 3),
            round(float(fill_rect.top()), 3),
            round(float(fill_rect.width()), 3),
            round(float(fill_rect.height()), 3),
        )
    if fill_rect is None:
        return None
    return (
        round(float(fill_rect.left()) - run_left, 3),
        round(float(fill_rect.top()) - baseline_y, 3),
        round(float(fill_rect.width()), 3),
        round(float(fill_rect.height()), 3),
    )


def _karaoke_state_uses_image(state: KaraokeColorState) -> bool:
    return any(
        fill.mode == "image"
        for fill in (state.text, state.stroke, state.stroke2, state.shadow)
    )


def _glyph_run_after_glow_key(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
) -> tuple:
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            _value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    return (
        "after_glow",
        glyph_sig,
        _fill_signature(colors.after.shadow),
        role_style.stroke_width_px,
        _main_stroke2_width(role_style),
        _glow_radius(role_style, after=True),
        _glow_concentration_level(role_style),
        role_style.decoration_kind,
    )


def _karaoke_glow_states_differ(style: Style, colors: KaraokeColors) -> bool:
    """前后发光状态（颜色签名 + 半径）是否不同。

    N3 在 ``WipeLeft`` 两侧互补裁剪前后描边源，再对合成源做 blur；因此锋线
    只硬分割字形墨水/描边，模糊后的两色 halo 可以跨线混合。状态相同时无需
    拆源，整字画一次未唱发光即可。
    """
    if style.decoration_kind != "glow":
        return False
    return (
        _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
        or _glow_radius(style, after=False) != _glow_radius(style, after=True)
    )


def _glyph_run_needs_after_glow(glyphs: list[_GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if _glow_radius(role_style, after=True) == 0:
        return False
    return _karaoke_glow_states_differ(role_style, _effective_karaoke_colors(role_style))


def _glyph_run_needs_before_glow_split(glyphs: list[_GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if _glow_radius(role_style, after=False) == 0:
        return False
    return _karaoke_glow_states_differ(
        role_style, _effective_karaoke_colors(role_style)
    )


def _ruby_glow_states_differ(style: Style) -> bool:
    """注音前后发光状态是否不同（语义同主字的裁源后模糊判据）。"""
    if _ruby_decoration_kind(style) != "glow":
        return False
    colors = _effective_ruby_karaoke_colors(style)
    return (
        _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
        or _ruby_glow_radius(style, after=False) != _ruby_glow_radius(style, after=True)
    )


def _ruby_glow_can_combine_split(style: Style) -> bool:
    """Whether one source bitmap can represent both ruby glow colours."""
    if not _ruby_glow_states_differ(style):
        return False
    before_radius = _ruby_glow_radius(style, after=False)
    return before_radius > 0 and before_radius == _ruby_glow_radius(style, after=True)


def _build_glyph_run_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    supersample: float = 1.0,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """把一个角色 run 的某状态烘焙成透明 QImage。

    ``after=False``（未唱层）：glow(before) 或 阴影(before) + stroke2 + stroke + 底色。
    ``after=True``（已唱主体）：阴影(after，仅非 glow) + stroke2 + stroke + 底色，
    **不含 glow 模糊**（已唱 glow 由 :class:`_GlyphRunAfterGlowLayer` 单独烘焙）。

    run 内逐字形可有不同字体/字号，故按 glyph 各自的 ``font`` 排版。返回 ``(image, dx, dy)``，
    blit 时画在 ``(run_left + dx, baseline_y + dy)``。
    """
    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    stroke2_width = _main_stroke2_width(role_style)

    is_glow = role_style.decoration_kind == "glow"
    bake_glow = (
        is_glow
        and not after
        and not _karaoke_glow_states_differ(role_style, colors)
    )
    has_shadow = (
        role_style.decoration_kind == "shadow"
        and bool(role_style.shadow_color)
        and bool(role_style.shadow_offset_x or role_style.shadow_offset_y)
    )

    stroke_extent = _visual_stroke_extent(
        role_style.stroke_width_px, stroke2_width
    )
    glow_extra = (
        _glow_extent(
            role_style.stroke_width_px,
            stroke2_width,
            _glow_radius(role_style, after=False),
        )
        if bake_glow
        else 0
    )
    extent = max(stroke_extent, glow_extra, 0) + 4
    shadow_dx = role_style.shadow_offset_x if has_shadow else 0
    shadow_dy = role_style.shadow_offset_y if has_shadow else 0
    pad_left = max(0, -shadow_dx) + extent
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    img_w = max(pad_left + run_w + pad_right, 1)
    img_h = max(pad_top + run_h + pad_bottom, 1)

    # supersample：把同一份「自然坐标」绘制逻辑渲染进 S× 分辨率位图（``p.scale(S,S)``），
    # 调用方再以 1/S 缩放贴出 → utopia 入场放大相位不糊。offset 仍以自然坐标返回。
    s = max(float(supersample), 1.0)
    image = QImage(
        max(int(round(img_w * s)), 1),
        max(int(round(img_h * s)), 1),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(0)

    target_origin_x = float(run_left - pad_left)
    target_origin_y = float(baseline_y - run_ascent - pad_top)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    p = QPainter(image)
    try:
        if s != 1.0:
            p.scale(s, s)
        p.translate(-target_origin_x, -target_origin_y)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        # 1) glow（仅未唱层）/ 阴影（仅非 glow）
        if bake_glow:
            _paint_glow_path(
                p,
                path,
                state.shadow,
                brush_rect,
                _glow_radius(role_style, after=False),
                role_style.stroke_width_px,
                stroke2_width,
                concentration_level=_glow_concentration_level(role_style),
            )
        elif has_shadow:
            _paint_shadow_silhouette(
                p,
                path,
                state.shadow,
                brush_rect,
                role_style.shadow_offset_x,
                role_style.shadow_offset_y,
                role_style.stroke_width_px,
                stroke2_width,
            )
        # 2) stroke2
        if stroke2_width > 0:
            _paint_stroke_path(
                p,
                path,
                state.stroke2,
                brush_rect,
                _stroke2_pen_width(role_style.stroke_width_px, stroke2_width),
            )
        # 3) stroke
        if role_style.stroke_color and role_style.stroke_width_px > 0:
            _paint_stroke_path(
                p,
                path,
                state.stroke,
                brush_rect,
                _stroke_pen_width(role_style.stroke_width_px),
                protect_body=_fill_is_alpha(state.text),
            )
        # 4) 底色文字
        _paint_fill_path(p, path, state.text, brush_rect)
    finally:
        p.end()

    offset_x = -pad_left
    offset_y = -(pad_top + run_ascent)
    return (image, offset_x, offset_y)


def _build_glyph_run_glow_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped glow image (before/after state) for a glyph run."""
    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    radius = _glow_radius(role_style, after=after)
    stroke2_width = _main_stroke2_width(role_style)
    extent = _glow_extent(
        role_style.stroke_width_px, stroke2_width, radius
    ) + 4

    img_w = max(extent + run_w + extent, 1)
    img_h = max(extent + run_h + extent, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    target_origin_x = float(run_left - extent)
    target_origin_y = float(baseline_y - run_ascent - extent)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    p = QPainter(image)
    try:
        p.translate(-target_origin_x, -target_origin_y)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_glow_path(
            p,
            path,
            state.shadow,
            brush_rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            concentration_level=_glow_concentration_level(role_style),
        )
    finally:
        p.end()

    offset_x = -extent
    offset_y = -(extent + run_ascent)
    return (image, offset_x, offset_y)


def _build_glyph_run_after_glow_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped after-glow image for a glyph run."""
    return _build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )


def _get_or_build_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    """A3：按上正 glyph 身份缓存 glow 烘焙位图（before/after 各一条）。"""
    key = (
        _glyph_run_layer_key(glyphs, role_style, colors, after=after),
        "glow",
        after,
        _relative_fill_rect_signature(
            glyphs,
            baseline_y,
            fill_rect,
            global_anchor=(colors.after if after else colors.before).shadow.mode
            == "image",
        ),
    )
    return _RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            colors,
            after=after,
            fill_rect=fill_rect,
            baseline_y=baseline_y,
        ),
    )


def _baked_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    image, dx, dy = _build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=after,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))


def _get_or_build_run_glow_mask(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    *,
    after: bool,
) -> BakedLayer:
    """Cache an opaque-white glow alpha mask for spatial brush fills."""
    mask_fill = PaintFill(mode="solid", color="#FFFFFF")
    mask_state = KaraokeColorState(shadow=mask_fill)
    mask_colors = KaraokeColors(before=mask_state, after=mask_state)
    key = (
        _glyph_run_layer_key(
            glyphs, role_style, mask_colors, after=after
        ),
        "glow-mask",
        after,
    )
    return _RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            mask_colors,
            after=after,
        ),
    )


def _blit_tinted_run_glow_mask(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    fill: PaintFill,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF,
) -> None:
    """Transform a cached glow mask, then colour it in fixed line space."""
    baked = _get_or_build_run_glow_mask(
        glyphs, role_style, after=after
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(
        float(run_left) + baked.offset.x(),
        float(baseline_y) + baked.offset.y(),
    )
    source_rect = QRectF(
        anchor.x(),
        anchor.y(),
        float(baked.image.width()),
        float(baked.image.height()),
    )
    effective_transform = transform or QTransform()
    mapped = effective_transform.mapRect(source_rect)
    left = math.floor(mapped.left())
    top = math.floor(mapped.top())
    right = math.ceil(mapped.right())
    bottom = math.ceil(mapped.bottom())
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    tinted = QImage(
        width, height, QImage.Format.Format_ARGB32_Premultiplied
    )
    tinted.fill(0)

    mask_painter = QPainter(tinted)
    try:
        mask_painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True
        )
        local_transform = QTransform(effective_transform)
        local_transform *= QTransform.fromTranslate(-float(left), -float(top))
        mask_painter.setTransform(local_transform)
        mask_painter.drawImage(anchor, baked.image)
        mask_painter.resetTransform()
        mask_painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        local_fill_rect = fill_rect.translated(-float(left), -float(top))
        mask_painter.fillRect(
            QRectF(0.0, 0.0, float(width), float(height)),
            _brush_for_fill(fill, local_fill_rect),
        )
    finally:
        mask_painter.end()
    painter.drawImage(QPointF(float(left), float(top)), tinted)


def _blit_cached_run_glow(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None = None,
) -> None:
    """A3：在 ``transform`` 下贴出缓存的上正 glow 位图（替代逐帧 ``_paint_glow_path``）。

    glow 在上正坐标烘焙、自然 anchor ``(run_left+dx, baseline_y+dy)`` 贴出；``transform``
    把它送到与逐帧矢量 body 相同的变换位置。调用方在贴前已设好设备空间 clip（扫光带），
    本函数 ``setTransform(combine=True)`` 不影响该 clip（Qt clip 存于设备坐标）。
    ``fill_rect`` is the shared line brush area used by N3 gradients and
    MilleFeuille fills.
    """
    if _glow_radius(role_style, after=after) == 0:
        return
    state = colors.after if after else colors.before
    if (
        state.shadow.mode != "solid"
        and fill_rect is not None
        and transform is not None
        and not transform.isIdentity()
    ):
        _blit_tinted_run_glow_mask(
            painter,
            glyphs,
            baseline_y,
            role_style,
            state.shadow,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
        return
    baked = _get_or_build_run_glow(
        glyphs, role_style, colors, after=after,
        fill_rect=fill_rect, baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if transform is not None:
            painter.setTransform(transform, combine=True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _paint_role_line_with_character_transition(
    painter: QPainter,
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    baseline_y: int,
    t_ms: int,
    transition: _LineCharTransition,
    style: Style,
    *,
    rtl: bool = False,
    ink_x_ranges: list[tuple[int, int]] | None = None,
    fill_segments: list[_FillSegment] | None = None,
) -> None:
    # 走字 ratio 按墨水边界算（与静态路径一致）；缺省回退 advance 框。
    fill_ranges = ink_x_ranges if ink_x_ranges is not None else char_x_ranges
    fill_rect = _n3_main_fill_rect(layout, baseline_y)
    glyphs_by_index = _role_glyphs_by_index(line, layout)
    count = max(len(line.chars), 1)
    ruby_groups = _resolve_char_ruby_groups(active_rubies, line, intervals)
    for index in range(len(line.chars)):
        if index >= len(intervals) or index >= len(char_x_ranges):
            continue
        layout_glyph = glyphs_by_index[index]
        if layout_glyph is None:
            continue
        if _glyph_is_bitmap_guide(layout_glyph):
            _paint_bitmap_guide_transition_glyph(
                painter,
                layout_glyph,
                fill_segments or [],
                baseline_y,
                line,
                intervals,
                index,
                t_ms,
                transition,
                style,
                rtl=rtl,
            )
            continue

        group = _utopia_main_group_for_index(active_rubies, line, intervals, index, groups=ruby_groups) if transition.effect == "utopia" else None
        group_done_ms: int | None = None
        if group is not None:
            group_indices, _group_ruby = group
            group_done_ms = _utopia_following_done_time(line, intervals, group_indices[-1], style)
        indices = [index]
        group_ruby = None

        if not indices:
            continue
        left = min(char_x_ranges[i][0] for i in indices)
        right = max(char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        first_index = indices[0]
        last_index = indices[-1]
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            intervals,
            fill_ranges,
            ruby_groups,
            index,
            style,
            fallback_start=intervals[first_index][0],
            fallback_end=intervals[last_index][1],
        )
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, intervals, last_index, style)
            if transition.effect == "utopia"
            else None
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue

        group_glyphs = [glyphs_by_index[i] for i in indices if glyphs_by_index[i] is not None]
        group_rect = _glyph_run_rect(group_glyphs, baseline_y)
        group_center_x = left + width / 2
        group_center_y = group_rect.top() + group_rect.height() / 2
        group_transform = QTransform()
        group_clip_rect: QRectF | None = None
        paint_left = left
        paint_width = width
        if transition.effect == "utopia":
            group_transform = _character_transform(
                center_x=group_center_x,
                center_y=group_center_y,
                dx=dx,
                dy=dy,
                rotation=rotation,
                scale_x=scale_x,
                scale_y=scale_y,
                skew_y=skew_y,
                scale_origin_x=left,
                scale_origin_y=baseline_y,
            )
            group_path = _glyph_run_path(group_glyphs, baseline_y)
            transformed_group_path = group_transform.map(group_path)
            group_clip_rect = transformed_group_path.boundingRect()
            paint_left, paint_width = _n3_transformed_wipe_span(
                transformed_group_path,
                group_glyphs[0].style.stroke_width_px,
            )

        # utopia 退场阶段整词早已唱完：强制 ratio=1.0，避免对已旋转/翻转的字形再按设备空间
        # 水平带裁切已唱层而把部分着色裁掉（详见 _paint_line_with_character_transition 同处注释）。
        in_utopia_exit = (
            transition.effect == "utopia"
            and style.exit_anim == "utopia"
            and following_done_ms is not None
            and t_ms > following_done_ms
        )
        if in_utopia_exit:
            ratio = 1.0
        elif group_ruby is not None:
            ratio = _main_text_ruby_progress_ratio(
                group_ruby, t_ms, mode=style.ruby_main_progress_mode
            )
        else:
            ratio = _character_fill_ratio(
                line,
                intervals,
                fill_ranges,
                active_rubies,
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=style.ruby_main_progress_mode,
            )
        for run in _glyph_runs_for_indices(glyphs_by_index, indices):
            role_style = run[0].style
            colors = _effective_karaoke_colors(role_style)
            run_path = _glyph_run_path(run, baseline_y)
            run_rect = _glyph_run_rect(run, baseline_y)
            run_metrics = max(run, key=lambda glyph: glyph.metrics.ascent() + glyph.metrics.descent()).metrics
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                paint_path = run_path
                paint_rect = run_rect
                clip_rect = group_clip_rect
                if transition.effect == "utopia":
                    paint_path = group_transform.map(run_path)
                    paint_rect = paint_path.boundingRect()
                else:
                    _apply_character_transform(
                        painter,
                        center_x=group_center_x,
                        center_y=group_center_y,
                        dx=dx,
                        dy=dy,
                        rotation=rotation,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        skew_y=skew_y,
                    )
                    clip_rect = None
                use_glow_cache = transition.effect == "utopia" and _glow_cache_enabled()
                _paint_char_karaoke_stack(
                    painter,
                    paint_path,
                    paint_rect,
                    char_x=paint_left,
                    char_width=paint_width,
                    baseline_y=baseline_y,
                    metrics=run_metrics,
                    colors=colors,
                    style=role_style,
                    ratio=ratio,
                    rtl=rtl,
                    clip_rect=clip_rect,
                    glow_run=run if use_glow_cache else None,
                    glow_transform=group_transform if use_glow_cache else None,
                    geometry_transform=(
                        group_transform if transition.effect == "utopia" else None
                    ),
                    fill_rect=fill_rect,
                )
            finally:
                painter.restore()


def _role_glyphs_by_index(
    line: TimingLine,
    layout: _TextLayout,
) -> list[_GlyphLayout | None]:
    glyphs: list[_GlyphLayout | None] = [None for _ in line.chars]
    for glyph in layout.glyphs:
        if 0 <= glyph.index < len(glyphs):
            glyphs[glyph.index] = glyph
    return glyphs


def _glyph_runs_for_indices(
    glyphs_by_index: list[_GlyphLayout | None],
    indices: list[int],
) -> list[list[_GlyphLayout]]:
    runs: list[list[_GlyphLayout]] = []
    current: list[_GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for index in indices:
        if not (0 <= index < len(glyphs_by_index)):
            continue
        glyph = glyphs_by_index[index]
        if glyph is None:
            continue
        style_id = id(glyph.style)
        signature = signature_cache.get(style_id)
        if signature is None:
            signature = _glyph_run_signature(glyph)
            signature_cache[style_id] = signature
        if current and signature != current_signature:
            runs.append(current)
            current = []
        current.append(glyph)
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def _role_char_geometry_by_index(
    line: TimingLine,
    layout: _TextLayout,
) -> tuple[list[int], list[tuple[int, int]]]:
    widths = [0 for _ in line.chars]
    ranges = [(0, 0) for _ in line.chars]
    for glyph in layout.glyphs:
        if 0 <= glyph.index < len(line.chars):
            widths[glyph.index] = glyph.width
            ranges[glyph.index] = (glyph.left, glyph.left + glyph.width)
    return widths, ranges


def _role_char_ink_ranges_by_index(
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """分色行各字符的墨水边界（逐 glyph 用各自字体），用于走字扫光。

    缺失/空白字符回退为 advance 框左缘的零宽 ``(left, left)``，与
    :func:`_char_ink_x_ranges` 同口径（见其 docstring）。
    """
    ranges: list[tuple[int, int]] = [(left, left) for left, _ in char_x_ranges]
    for glyph in layout.glyphs:
        if not (0 <= glyph.index < len(ranges)):
            continue
        text = glyph.text
        left = glyph.left
        if not text or text.isspace():
            ranges[glyph.index] = (left, left)
            continue
        path = _glyph_path(glyph, 0)
        br = path.boundingRect()
        if br.isEmpty():
            ranges[glyph.index] = (left, left)
        else:
            ranges[glyph.index] = (int(math.floor(br.left())), int(math.ceil(br.right())))
    return ranges


def _n3_char_wipe_ranges_by_index(
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
    ink_x_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return N3 ``WipeLeft`` bounds for each main-text glyph.

    N3 interpolates across the transformed glyph geometry expanded by half of
    the primary edge size.  The character advance box is only layout geometry;
    using it as the wipe range spends singing time in transparent side bearings
    and creates a visible pause at otherwise contiguous character timestamps.
    Empty glyphs (notably timed spaces) deliberately keep zero-width geometry.
    """
    ranges = list(ink_x_ranges)
    for glyph in layout.glyphs:
        if not (0 <= glyph.index < len(ranges)):
            continue
        ink_left, ink_right = ranges[glyph.index]
        if not glyph.text or glyph.text.isspace() or ink_right <= ink_left:
            ranges[glyph.index] = (char_x_ranges[glyph.index][0],) * 2
            continue
        # N3 truncates the scaled EdgeSize first, then performs integer / 2.
        edge_half = max(int(glyph.style.stroke_width_px), 0) // 2
        ranges[glyph.index] = (ink_left - edge_half, ink_right + edge_half)
    return ranges


def _n3_transformed_wipe_span(
    path: QPainterPath,
    stroke_width: int,
) -> tuple[int, int]:
    """Return N3 WipeLeft's transformed ink bounds plus half primary edge."""
    bounds = path.boundingRect()
    edge_half = max(int(stroke_width), 0) // 2
    left = int(math.floor(bounds.left())) - edge_half
    right = int(math.ceil(bounds.right())) + edge_half
    return left, max(right - left, 1)


def _line_text_path(
    line: TimingLine,
    char_widths: list[int],
    font: QFont,
    x: int,
    y: int,
    char_lefts: list[int] | None = None,
    font_for=None,
    char_path_offsets: list[float] | None = None,
) -> QPainterPath:
    path = QPainterPath()
    if char_lefts is None:
        char_lefts = _char_left_positions(char_widths, x, False)
    if char_path_offsets is None:
        char_path_offsets = [0.0 for _ in char_lefts]
    for ch, left, path_offset_x in zip(line.chars, char_lefts, char_path_offsets):
        glyph_font = font_for(ch.text) if font_for is not None else font
        path.addText(float(left + path_offset_x), float(y), glyph_font, ch.text)
    return path


def _line_char_transition_context(
    style: Style,
    line: TimingLine,
    t_ms: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    char_count: int,
    *,
    intervals: list[tuple[int, int]] | None = None,
) -> _LineCharTransition | None:
    if char_count <= 0:
        return None
    start = display_start_ms if display_start_ms is not None else _line_start_ms(line)
    end = display_end_ms if display_end_ms is not None else _line_end_ms(line)

    if style.exit_anim in {"char_fade", "char_drip", "spin_flip"} and style.exit_fade_ms > 0:
        exit_start = max(_line_end_ms(line), end - _CHAR_FADE_INTRO_DELAY_MS - _CHAR_FADE_OUT_TIME_MS)
        if t_ms >= exit_start:
            return _LineCharTransition(
                phase="exit",
                effect=style.exit_anim,
                progress=1.0,
                start_ms=exit_start,
                end_ms=end,
            )

    if style.entry_anim in {"char_fade", "char_drip", "spin_flip"} and style.entry_lead_ms > 0:
        entry_end = start + _CHAR_FADE_INTRO_DELAY_MS + _CHAR_FADE_IN_TIME_MS
        if t_ms <= entry_end:
            return _LineCharTransition(
                phase="entry",
                effect=style.entry_anim,
                progress=1.0,
                start_ms=start,
                end_ms=entry_end,
            )

    if (
        style.entry_anim == "utopia"
        or style.exit_anim == "utopia"
        or effective_karaoke_animation(style) == "utopia"
    ):
        intervals = intervals if intervals is not None else compute_char_intervals(line)
        # Utopia 的入场、唱中弹跳和退场原本只在各自的活动窗口切进逐字符
        # 矢量路径，其余时刻回到整行静态路径。两条路径在发光叠加与边缘
        # 抗锯齿上存在细微差异，切换瞬间即使配色完全相同也会产生色闪。
        # 行可见期间始终保留 Utopia 上下文；但若另一侧正在执行逐字
        # 入/退场，上面的活动过渡必须优先，避免 Utopia 吞掉其效果。
        if start <= t_ms <= end:
            return _LineCharTransition(
                phase="utopia",
                effect="utopia",
                progress=1.0,
                start_ms=start,
                end_ms=end,
            )
    return None


def _paint_line_with_character_transition(
    painter: QPainter,
    line: TimingLine,
    char_widths: list[int],
    char_x_ranges: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    font: QFont,
    baseline_y: int,
    metrics: QFontMetrics,
    style: Style,
    colors: KaraokeColors,
    line_rect: QRectF,
    t_ms: int,
    transition: _LineCharTransition,
    rtl: bool = False,
    font_for=None,
    ink_x_ranges: list[tuple[int, int]] | None = None,
    glyphs_by_index: list[_GlyphLayout | None] | None = None,
    fill_rect: QRectF | None = None,
    fill_segments: list[_FillSegment] | None = None,
) -> None:
    # 走字 ratio 按墨水边界算（与静态路径一致）；缺省回退 advance 框。
    fill_ranges = ink_x_ranges if ink_x_ranges is not None else char_x_ranges
    count = max(len(line.chars), 1)
    ruby_groups = _resolve_char_ruby_groups(active_rubies, line, intervals)
    if glyphs_by_index is None:
        glyphs_by_index = [None for _ in line.chars]
    for index, (ch, width) in enumerate(zip(line.chars, char_widths)):
        if index >= len(intervals) or index >= len(char_x_ranges):
            continue
        layout_glyph = (
            glyphs_by_index[index] if index < len(glyphs_by_index) else None
        )
        if layout_glyph is not None and _glyph_is_bitmap_guide(layout_glyph):
            _paint_bitmap_guide_transition_glyph(
                painter,
                layout_glyph,
                fill_segments or [],
                baseline_y,
                line,
                intervals,
                index,
                t_ms,
                transition,
                style,
                rtl=rtl,
            )
            continue
        group = _utopia_main_group_for_index(active_rubies, line, intervals, index, groups=ruby_groups) if transition.effect == "utopia" else None
        group_done_ms: int | None = None
        if group is not None:
            group_indices, _group_ruby = group
            group_done_ms = _utopia_following_done_time(line, intervals, group_indices[-1], style)
        indices = [index]
        group_ruby = None

        left = min(char_x_ranges[i][0] for i in indices)
        right = max(char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        first_index = indices[0]
        last_index = indices[-1]
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            intervals,
            fill_ranges,
            ruby_groups,
            index,
            style,
            fallback_start=intervals[first_index][0],
            fallback_end=intervals[last_index][1],
        )
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, intervals, last_index, style)
            if transition.effect == "utopia"
            else None
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue

        # utopia 退场阶段整词早已唱完：强制 fill_ratio=1.0。否则 _paint_char_karaoke_stack 会按
        # 设备空间的水平带裁切「已唱(after)层」，而退场时字形已被旋转/翻转（rotation 最大 -180°、
        # x_flip），水平带与字形朝向脱钩，会把部分笔画的着色裁掉（着色被褪掉一部分的 bug）。
        # 退场时卡拉ok扫光本无意义，整词应作为「已唱」整体淡出/旋出。
        in_utopia_exit = (
            transition.effect == "utopia"
            and style.exit_anim == "utopia"
            and following_done_ms is not None
            and t_ms > following_done_ms
        )
        if in_utopia_exit:
            fill_ratio = 1.0
        elif group_ruby is not None:
            fill_ratio = _main_text_ruby_progress_ratio(
                group_ruby, t_ms, mode=style.ruby_main_progress_mode
            )
        else:
            fill_ratio = _character_fill_ratio(
                line,
                intervals,
                fill_ranges,
                active_rubies,
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=style.ruby_main_progress_mode,
            )

        path = QPainterPath()
        for char_index in indices:
            layout_glyph = glyphs_by_index[char_index] if char_index < len(glyphs_by_index) else None
            glyph = line.chars[char_index]
            if layout_glyph is not None:
                path.addPath(_glyph_path(layout_glyph, baseline_y))
                continue
            glyph_font = layout_glyph.font if layout_glyph is not None else (font_for(glyph.text) if font_for is not None else font)
            glyph_left = layout_glyph.left if layout_glyph is not None else char_x_ranges[char_index][0]
            path_offset_x = layout_glyph.path_offset_x if layout_glyph is not None else 0.0
            if glyph.vector_glyph is not None:
                path.addPath(
                    scaled_guide_symbol_path(
                        glyph.vector_glyph,
                        pixel_size=max(int(glyph_font.pixelSize()), 1),
                        left=float(glyph_left),
                        baseline_y=float(baseline_y),
                    )
                )
            else:
                path.addText(float(glyph_left + path_offset_x), float(baseline_y), glyph_font, glyph.text)
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            paint_path = path
            paint_rect = line_rect
            paint_left = left
            paint_width = width
            paint_clip_rect: QRectF | None = None
            glow_run: list[_GlyphLayout] | None = None
            glow_transform: QTransform | None = None
            geometry_transform: QTransform | None = None
            if transition.effect == "utopia":
                transform = _character_transform(
                    center_x=left + width / 2,
                    center_y=baseline_y - metrics.ascent() + metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                    scale_origin_x=left,
                    scale_origin_y=baseline_y,
                )
                paint_path = transform.map(path)
                paint_rect = paint_path.boundingRect()
                paint_left, paint_width = _n3_transformed_wipe_span(
                    paint_path, style.stroke_width_px
                )
                paint_clip_rect = paint_rect
                geometry_transform = transform
                # 上正 glyph 列表：bake 路径与 A3 glow 缓存共用。
                group_glyphs = []
                for ci in indices:
                    layout_glyph = glyphs_by_index[ci] if ci < len(glyphs_by_index) else None
                    if layout_glyph is not None:
                        group_glyphs.append(layout_glyph)
                        continue
                    group_glyphs.append(
                        _GlyphLayout(
                            index=ci,
                            text=line.chars[ci].text,
                            role_label=None,
                            style=style,
                            font=(font_for(line.chars[ci].text) if font_for is not None else font),
                            metrics=metrics,
                            left=char_x_ranges[ci][0],
                            width=char_x_ranges[ci][1] - char_x_ranges[ci][0],
                            vector_glyph=line.chars[ci].vector_glyph,
                        )
                    )
                if _glow_cache_enabled():
                    glow_run = group_glyphs
                    glow_transform = transform
            else:
                _apply_character_transform(
                    painter,
                    center_x=left + width / 2,
                    center_y=baseline_y - metrics.ascent() + metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                )
            _paint_char_karaoke_stack(
                painter,
                paint_path,
                paint_rect,
                char_x=paint_left,
                char_width=paint_width,
                baseline_y=baseline_y,
                metrics=metrics,
                colors=colors,
                style=style,
                ratio=fill_ratio,
                rtl=rtl,
                clip_rect=paint_clip_rect,
                glow_run=glow_run,
                glow_transform=glow_transform,
                geometry_transform=geometry_transform,
                fill_rect=fill_rect,
            )
        finally:
            painter.restore()


def _utopia_main_group_for_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
) -> tuple[list[int], RubyAnnotation] | None:
    # groups 由 _resolve_char_ruby_groups 预建（每行一次）；缺省回退逐字查找。
    if groups is not None:
        entry = groups.get(index)
        if entry is None:
            return None
        raw_indices, ruby = entry
    else:
        ruby = _ruby_for_char_index(rubies, line, intervals, index)
        if ruby is None:
            return None
        raw_indices = _ruby_target_indices(ruby, line, intervals)
    indices = [candidate for candidate in raw_indices if 0 <= candidate < len(line.chars)]
    if len(indices) <= 1:
        return None
    return indices, ruby


def _utopia_wipe_window_for_index(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    groups: dict[int, tuple[list[int], RubyAnnotation]],
    index: int,
    style: Style,
    *,
    fallback_start: int,
    fallback_end: int,
) -> tuple[int, int]:
    if effective_karaoke_animation(style) != "utopia":
        return fallback_start, fallback_end
    entry = groups.get(index)
    if entry is None:
        return fallback_start, fallback_end
    raw_indices, ruby = entry
    if _is_utopia_group_marker(ruby):
        return fallback_start, fallback_end
    indices = [candidate for candidate in raw_indices if 0 <= candidate < len(char_x_ranges)]
    if index not in indices or _ruby_main_uses_base_timing(line, indices):
        return fallback_start, fallback_end

    effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
    if (
        style.ruby_main_progress_mode == "reading_units"
        and _ruby_visual_units_and_intervals(effective_ruby)
    ):
        base_index = indices.index(index)
        return _ruby_main_text_slot_times(effective_ruby, base_index, len(indices))

    group_left = min(char_x_ranges[candidate][0] for candidate in indices)
    group_right = max(char_x_ranges[candidate][1] for candidate in indices)
    if group_right <= group_left:
        return fallback_start, fallback_end
    char_left, char_right = char_x_ranges[index]
    group_width = group_right - group_left
    start_ratio = (char_left - group_left) / group_width
    end_ratio = (char_right - group_left) / group_width
    start = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        start_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="right",
    )
    end = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        end_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="left",
    )
    return start, max(start, end)


def _transition_char_state(
    style: Style,
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    char_start_ms: int | None = None,
    char_end_ms: int | None = None,
    t_ms: int | None = None,
    frame_height: int | None = None,
    following_done_ms: int | None = None,
) -> tuple[float, float, float, float, float, float, float]:
    if transition.effect == "utopia" and transition.phase == "utopia":
        if (
            style.entry_anim == "utopia"
            and t_ms is not None
            and transition.start_ms is not None
            and t_ms <= transition.start_ms + _UTOPIA_INTRO_TIME_MS
        ):
            intro_transition = _LineCharTransition(
                phase="entry",
                effect="utopia",
                progress=_clamped_ratio(t_ms - transition.start_ms, _UTOPIA_INTRO_TIME_MS),
                start_ms=transition.start_ms,
                end_ms=transition.start_ms + _UTOPIA_INTRO_TIME_MS,
            )
            return _transition_char_state(
                style,
                intro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            style.exit_anim == "utopia"
            and t_ms is not None
            and following_done_ms is not None
            and t_ms > following_done_ms
        ):
            outro_transition = _LineCharTransition(phase="exit", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                outro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            effective_karaoke_animation(style) == "utopia"
            and t_ms is not None
            and char_start_ms is not None
            and char_end_ms is not None
            and _is_utopia_wiping(t_ms, char_start_ms, char_end_ms)
        ):
            wipe_transition = _LineCharTransition(phase="wipe", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                wipe_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "utopia" and transition.phase == "entry":
        if t_ms is None or transition.start_ms is None:
            local = _staggered_char_progress(transition.progress, index, count)
            opacity = min(max(local, 0.0), 1.0)
            return opacity, 0.0, 0.0, 0.0, opacity, opacity, 0.0
        delay = _utopia_intro_delay_step(count) * index
        elapsed = t_ms - transition.start_ms - delay
        if elapsed < 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        opacity = min(elapsed / _UTOPIA_INTRO_ENLARGE_MS, 1.0)
        if elapsed < _UTOPIA_INTRO_ENLARGE_MS:
            scale = _UTOPIA_INTRO_OVER_RATIO * elapsed / _UTOPIA_INTRO_ENLARGE_MS
        elif elapsed < _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS:
            remaining = _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS - elapsed
            scale = 1.0 + (_UTOPIA_INTRO_OVER_RATIO - 1.0) * remaining / _UTOPIA_INTRO_CONDENSE_MS
        else:
            scale = 1.0
        return opacity, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.phase == "exit" and transition.effect == "utopia":
        if t_ms is None:
            local = transition.progress
        else:
            done_ms = following_done_ms if following_done_ms is not None else char_end_ms
            if done_ms is None:
                local = transition.progress
            else:
                local = (t_ms - done_ms) / _UTOPIA_FADE_OUT_TIME_MS
        local = min(max(local, 0.0), 1.0)
        opacity = max(0.0, 1.0 - local)
        shrink = 1.0 - local
        height = frame_height if frame_height and frame_height > 0 else 1080
        amp = height / 15.0
        if local <= 0.5:
            x_travel = math.sin(math.pi * local) * amp
        else:
            x_travel = amp + math.sin((local - 0.5) * math.pi) * amp
        y_travel = math.sin(math.pi * local / 2.0) * amp
        x_flip = math.cos(math.pi * local)
        rotation = -180.0 * local
        return opacity, -x_travel, y_travel, rotation, shrink * x_flip, shrink, 0.0

    if transition.phase == "wipe" and transition.effect == "utopia":
        if char_start_ms is None or char_end_ms is None or t_ms is None:
            return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
        scale = _utopia_wipe_scale(t_ms, char_start_ms, char_end_ms)
        return 1.0, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.effect in {"char_fade", "char_drip", "spin_flip"}:
        progress = _char_fade_opacity(
            transition,
            index,
            count,
            t_ms=t_ms,
        )
        if transition.effect == "spin_flip":
            direction = 1.0 if transition.phase == "exit" else -1.0
            skew_y = direction * _spin_flip_skew(progress)
            return progress, 0.0, 0.0, 0.0, progress, progress, skew_y
        if transition.effect == "char_drip":
            direction = 1.0 if transition.phase == "entry" else -1.0
            skew_y = direction * _spin_flip_skew(progress)
            return float(progress > 0.0), 0.0, 0.0, 0.0, 1.0, 1.0, skew_y
        return progress, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    local = _staggered_char_progress(transition.progress, index, count)
    eased = 1.0 - (1.0 - local) * (1.0 - local)
    if transition.phase == "entry":
        opacity = 0.22 + 0.78 * eased
        return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    opacity = 1.0 - eased
    if transition.effect == "utopia":
        return 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
    return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0


def _apply_character_transform(
    painter: QPainter,
    *,
    center_x: float,
    center_y: float,
    dx: float,
    dy: float,
    rotation: float,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    skew_y: float = 0.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> None:
    transform = _character_transform(
        center_x=center_x,
        center_y=center_y,
        dx=dx,
        dy=dy,
        rotation=rotation,
        scale_x=scale_x,
        scale_y=scale_y,
        skew_y=skew_y,
        scale_origin_x=scale_origin_x,
        scale_origin_y=scale_origin_y,
    )
    if transform.isIdentity():
        return
    painter.setTransform(transform, combine=True)


def _character_transform(
    *,
    center_x: float,
    center_y: float,
    dx: float = 0.0,
    dy: float = 0.0,
    rotation: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    skew_y: float = 0.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> QTransform:
    transform = QTransform()
    if not dx and not dy and not rotation and scale_x == 1.0 and scale_y == 1.0 and not skew_y:
        return transform
    if scale_origin_x is not None and scale_origin_y is not None:
        transform.translate(scale_origin_x + dx, scale_origin_y + dy)
        if skew_y:
            transform.shear(0.0, skew_y)
        if scale_x != 1.0 or scale_y != 1.0:
            transform.scale(scale_x, scale_y)
        transform.translate(center_x - scale_origin_x, center_y - scale_origin_y)
        if rotation:
            transform.rotate(rotation)
        transform.translate(-center_x, -center_y)
        return transform
    transform.translate(center_x + dx, center_y + dy)
    if rotation:
        transform.rotate(rotation)
    if skew_y:
        transform.shear(0.0, skew_y)
    if scale_x != 1.0 or scale_y != 1.0:
        transform.scale(scale_x, scale_y)
    transform.translate(-center_x, -center_y)
    return transform


def _utopia_intro_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _UTOPIA_INTRO_DELAY_MS // (count - 1)


def _is_utopia_wiping(t_ms: int, char_start_ms: int, char_end_ms: int) -> bool:
    return char_start_ms < t_ms < char_end_ms and char_start_ms != char_end_ms


def _utopia_wipe_scale(t_ms: int, char_start_ms: int, char_end_ms: int) -> float:
    if not _is_utopia_wiping(t_ms, char_start_ms, char_end_ms):
        return 1.0
    over_ms = min(int((char_end_ms - char_start_ms) * _UTOPIA_WIPE_OVER_TIME_RATIO), _UTOPIA_WIPE_OVER_TIME_LIMIT_MS)
    if over_ms <= 0:
        return 1.0
    peak_ms = char_start_ms + over_ms
    if t_ms <= peak_ms:
        progress = (t_ms - char_start_ms) / over_ms
    else:
        release_ms = max(char_end_ms - peak_ms, 1)
        progress = (char_end_ms - t_ms) / release_ms
    return 1.0 + (_UTOPIA_WIPE_OVER_RATIO - 1.0) * min(max(progress, 0.0), 1.0)


def _utopia_following_done_time(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    style: Style,
) -> int:
    if not intervals:
        return _line_end_ms(line)
    index = min(max(index, 0), len(intervals) - 1)
    current_end = intervals[index][1]
    next_index = _next_valid_char_index(line, index + 1)
    if next_index is not None and next_index < len(intervals):
        next_end = intervals[next_index][1]
        if current_end <= next_end:
            return next_end
    return current_end + _utopia_tail_delay_ms(style)


def _next_valid_char_index(line: TimingLine, start_index: int) -> int | None:
    for index in range(start_index, len(line.chars)):
        text = line.chars[index].text
        if text and not text.isspace():
            return index
    return None


def _utopia_tail_delay_ms(style: Style) -> int:
    return max(0, style.line_tail_ms - _UTOPIA_FADE_OUT_TIME_MS)


def _char_fade_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _CHAR_FADE_INTRO_DELAY_MS // (count - 1)


def _char_fade_opacity(
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
) -> float:
    if t_ms is None:
        return transition.progress
    if transition.phase == "entry":
        start_ms = (transition.start_ms or 0) + _char_fade_delay_step(count) * index
        return _clamped_ratio(t_ms - start_ms, _CHAR_FADE_IN_TIME_MS)
    if transition.phase == "exit":
        end_ms = (transition.end_ms or t_ms) - _char_fade_delay_step(count) * (count - index - 1)
        if t_ms > end_ms:
            return 0.0
        if t_ms < end_ms - _CHAR_FADE_OUT_TIME_MS:
            return 1.0
        return _clamped_ratio(end_ms - t_ms, _CHAR_FADE_OUT_TIME_MS)
    return 1.0


def _spin_flip_skew(opacity: float) -> float:
    opacity = max(0.0, min(1.0, opacity))
    if opacity <= 0.0:
        return 0.0
    angle = (math.pi / 2.0) * (1.0 - opacity)
    return math.tan(min(angle, math.radians(89.0)))


def _paint_char_karaoke_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    char_x: int,
    char_width: int,
    baseline_y: int,
    metrics: QFontMetrics,
    colors: KaraokeColors,
    style: Style,
    ratio: float,
    rtl: bool = False,
    clip_rect: QRectF | None = None,
    glow_run: list[_GlyphLayout] | None = None,
    glow_transform: QTransform | None = None,
    geometry_transform: QTransform | None = None,
    fill_rect: QRectF | None = None,
) -> None:
    # A3（§9.7）：``glow_run`` 给定（utopia 路径）时，glow 走上正烘焙缓存 + 变换 blit，
    # 不再每帧 _paint_glow_path 重算高斯；body 仍逐帧矢量（锐利）。``glow_run`` 为 None
    # 时退回原逐帧 glow 路径（保留旧行为，可回退）。
    def _use_cached_glow(after: bool) -> bool:
        del after
        # N3 transforms the glyph geometry before drawing the shared
        # decoration source and applying Gaussian blur.  Reusing an already
        # blurred upright bitmap would also scale/rotate the blur kernel,
        # producing a visibly different Utopia outline during entry, wipe and
        # outro.  Keep the cache for identity frames; transformed frames must
        # blur the transformed source.
        return (
            glow_run is not None
            and style.decoration_kind == "glow"
            and (
                geometry_transform is None
                or geometry_transform.isIdentity()
            )
        )
    stroke2_width = _main_stroke2_width(style)

    def _blit_glow(after: bool) -> None:
        _blit_cached_run_glow(
            painter, glow_run, baseline_y, style, colors,
            after=after, transform=glow_transform, fill_rect=fill_rect,
        )

    if ratio <= 0.0:
        use_cached_glow = _use_cached_glow(after=False)
        if use_cached_glow:
            _blit_glow(after=False)
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
            draw_glow=not use_cached_glow,
            fill_rect=fill_rect,
        )
        return

    if ratio < 1.0:
        clip_bounds = clip_rect if clip_rect is not None else QRectF(
            float(char_x),
            float(baseline_y - metrics.ascent()),
            float(char_width),
            float(metrics.height()),
        )
        glow_states_differ = _karaoke_glow_states_differ(style, colors)
        use_cached_before_glow = (
            _use_cached_glow(after=False) and not glow_states_differ
        )
        front = char_x + char_width * (1.0 - ratio if rtl else ratio)
        if glow_states_differ:
            transformed_geometry = (
                geometry_transform is not None
                and not geometry_transform.isIdentity()
            )
            if transformed_geometry:
                _paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
            elif glow_run is not None and _glow_cache_enabled():
                combined = _paint_cached_run_split_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    glow_run,
                    baseline_y,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    transform=glow_transform,
                    fill_rect=fill_rect,
                )
                if not combined:
                    for after in (False, True):
                        _paint_cached_run_glow_source_wipe(
                            painter,
                            path,
                            clip_bounds,
                            glow_run,
                            baseline_y,
                            style,
                            colors,
                            after=after,
                            front=front,
                            rtl=rtl,
                            transform=glow_transform,
                            fill_rect=fill_rect,
                        )
            else:
                _paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
        elif use_cached_before_glow:
            _blit_glow(after=False)
        utopia_shadow_split = (
            geometry_transform is not None
            and style.decoration_kind == "shadow"
            and bool(style.shadow_offset_x or style.shadow_offset_y)
        )
        if utopia_shadow_split:
            shadow_front = front + style.shadow_offset_x
            shadow_states_differ = (
                _fill_signature(colors.before.shadow)
                != _fill_signature(colors.after.shadow)
            )
            if not shadow_states_differ:
                _paint_shadow_silhouette(
                    painter,
                    path,
                    colors.before.shadow,
                    fill_rect if fill_rect is not None else rect,
                    style.shadow_offset_x,
                    style.shadow_offset_y,
                    style.stroke_width_px,
                    stroke2_width,
                )
            else:
                for after in (False, True):
                    source_is_right = rtl == after
                    output_clip = (
                        QRectF(
                            shadow_front,
                            -1_000_000.0,
                            1_000_000.0,
                            2_000_000.0,
                        )
                        if source_is_right
                        else QRectF(
                            -1_000_000.0,
                            -1_000_000.0,
                            shadow_front + 1_000_000.0,
                            2_000_000.0,
                        )
                    )
                    painter.save()
                    try:
                        painter.setClipRect(output_clip)
                        state = colors.after if after else colors.before
                        _paint_shadow_silhouette(
                            painter,
                            path,
                            state.shadow,
                            fill_rect if fill_rect is not None else rect,
                            style.shadow_offset_x,
                            style.shadow_offset_y,
                            style.stroke_width_px,
                            stroke2_width,
                        )
                    finally:
                        painter.restore()
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
            draw_glow=not use_cached_before_glow and not glow_states_differ,
            fill_rect=fill_rect,
            draw_shadow=not utopia_shadow_split,
        )
        stroke_pad = _visual_text_padding(style)
        # RTL：单字内扫光从右向左，已唱区贴字符右缘。
        clip_x = char_x + (char_width * (1.0 - ratio) if rtl else 0.0)
        # 已唱描边 + 填充：保持卡拉ok 走字的硬边（按字框紧裁），发光已单独画过。
        painter.save()
        try:
            painter.setClipRect(
                QRectF(
                    float(clip_x - stroke_pad),
                    float(clip_bounds.top() - stroke_pad),
                    float(char_width * ratio + stroke_pad),
                    float(clip_bounds.height() + stroke_pad * 2),
                )
            )
            _paint_text_layer_stack(
                painter,
                path,
                rect,
                colors.after,
                style,
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=True),
                draw_glow=False,
                fill_rect=fill_rect,
                draw_shadow=not utopia_shadow_split,
            )
        finally:
            painter.restore()
        return

    use_cached_glow = _use_cached_glow(after=True)
    if use_cached_glow:
        _blit_glow(after=True)
    _paint_text_layer_stack(
        painter,
        path,
        rect,
        colors.after,
        style,
        stroke_width=style.stroke_width_px,
        stroke2_width=stroke2_width,
        shadow_dx=style.shadow_offset_x,
        shadow_dy=style.shadow_offset_y,
        glow_radius=_glow_radius(style, after=True),
        draw_glow=not use_cached_glow,
        fill_rect=fill_rect,
    )


def _staggered_char_progress(progress: float, index: int, count: int) -> float:
    if count <= 1:
        return progress
    span = 0.68
    window = 1.0 - span
    offset = (index / max(count - 1, 1)) * span
    return max(0.0, min(1.0, (progress - offset) / window))


def _clamped_ratio(elapsed_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed_ms / duration_ms))


def _paint_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
) -> None:
    painter.fillPath(path, _brush_for_fill(fill, rect))


def _fill_brush_rect(
    fill: PaintFill,
    rect: QRectF,
    horizontal_rect: QRectF | None,
) -> QRectF:
    """Use the shared ruby/main box only for horizontal gradients."""
    if fill.mode == "gradient_horizontal" and horizontal_rect is not None:
        return horizontal_rect
    return rect


def _paint_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    *,
    protect_body: bool = False,
) -> None:
    brush = _brush_for_fill(fill, rect)
    pen_width = max(width, 1)
    if protect_body:
        stroker = QPainterPathStroker()
        stroker.setWidth(float(pen_width))
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        outline = stroker.createStroke(path).subtracted(path)
        painter.fillPath(outline, brush)
        return
    pen = QPen(brush, pen_width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.strokePath(path, pen)


def _paint_shadow_silhouette(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    dx: int,
    dy: int,
    stroke_width: int,
    stroke2_width: int,
) -> None:
    """N3 式阴影：整字**剪影**（含描边外缘）平移绘制。

    N3 的 DrawOneLineDecorShadow 把 edge2+edge+body 整行画进 work bitmap 再整体
    平移 blit——阴影轮廓因此比正文描边外缘还大。若只平移文字本体路径，偏移小于
    描边半宽时阴影会被正文描边完全盖住（「几乎看不到阴影」）。"""
    shadow_path = QTransform().translate(dx, dy).map(path)
    shadow_rect = rect.translated(dx, dy)
    pen_width = (
        _stroke2_pen_width(stroke_width, stroke2_width)
        if stroke2_width > 0
        else _stroke_pen_width(stroke_width)
    )
    if pen_width > 0:
        _paint_stroke_path(painter, shadow_path, fill, shadow_rect, pen_width)
    _paint_fill_path(painter, shadow_path, fill, shadow_rect)


def _paint_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    source_clip: QRectF | None = None,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
) -> None:
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = _glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = _glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        # 调用方只消费 target_clip 内的输出：把 stroke/blur 画布水平裁到
        # target ± pad（pad ≥ 模糊支撑半径），窄带模糊代替整行模糊。裁剪量取整，
        # 保留 layer_rect 原有的小数相位——drawImage 的亚像素重采样必须与整行
        # 路径逐位一致，否则扫光前沿的陡坡会产生半像素偏移。
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(layer_rect.left() + math.floor(needed_left - layer_rect.left()))
        if needed_right < layer_rect.right():
            layer_rect.setRight(layer_rect.right() - math.floor(layer_rect.right() - needed_right))
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)

    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    p = QPainter(source)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        if source_clip is not None:
            p.setClipRect(source_clip.translated(-layer_rect.left(), -layer_rect.top()))
        _paint_stroke_path(p, local_path, fill, local_rect, width)
    finally:
        p.end()

    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in _glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, _blur_image(source, blur_radius))
    finally:
        painter.restore()


def _paint_split_glow_path(
    painter: QPainter,
    path: QPainterPath,
    before_fill: PaintFill,
    after_fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    *,
    before_source_clip: QRectF,
    after_source_clip: QRectF,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
) -> None:
    """Paint both WipeLeft source colours into one bitmap, then blur once."""
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = _glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = _glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(
                layer_rect.left() + math.floor(needed_left - layer_rect.left())
            )
        if needed_right < layer_rect.right():
            layer_rect.setRight(
                layer_rect.right()
                - math.floor(layer_rect.right() - needed_right)
            )
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    local_horizontal_rect = (
        horizontal_fill_rect.translated(-layer_rect.left(), -layer_rect.top())
        if horizontal_fill_rect is not None
        else None
    )
    source_painter = QPainter(source)
    try:
        source_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        for fill, clip in (
            (before_fill, before_source_clip),
            (after_fill, after_source_clip),
        ):
            source_painter.save()
            try:
                source_painter.setClipRect(
                    clip.translated(-layer_rect.left(), -layer_rect.top())
                )
                _paint_stroke_path(
                    source_painter,
                    local_path,
                    fill,
                    _fill_brush_rect(fill, local_rect, local_horizontal_rect),
                    width,
                )
            finally:
                source_painter.restore()
    finally:
        source_painter.end()
    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in _glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, _blur_image(source, blur_radius))
    finally:
        painter.restore()


def _n3_gaussian_kernel_1d(standard_deviation: float) -> np.ndarray:
    """Return N3/Direct2D's normalized Gaussian kernel for one axis."""
    sigma = max(float(standard_deviation), 1.0)
    support_radius = math.ceil(sigma * 3.0)
    offsets = np.arange(-support_radius, support_radius + 1, dtype=np.float64)
    kernel = np.exp(-(offsets * offsets) / (2.0 * sigma * sigma))
    return kernel / kernel.sum()


def _blur_image(source: QImage, radius: int) -> QImage:
    """Approximate N3's default Direct2D ``Balanced`` Gaussian blur.

    N3 assigns ``DecorSize`` directly to Direct2D's ``StandardDeviation``.
    In the default ``Balanced`` optimization mode, Direct2D pre-scales the
    input before filtering at larger radii, then restores it with filtered
    sampling.  A half-size pass reproduces the radius-10 response used by N3
    projects within one 8-bit alpha value; smaller radii retain the direct
    Gaussian path.  Qt's QGraphicsBlurEffect cannot be used because it applies
    an unrelated exponential blur.
    """
    sigma = max(float(radius), 1.0)
    if sigma < 8.0 or source.width() < 2 or source.height() < 2:
        return _gaussian_blur_image(source, sigma)

    reduced = source.scaled(
        max(source.width() // 2, 1),
        max(source.height() // 2, 1),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    blurred = _gaussian_blur_image(reduced, sigma / 2.0)
    return blurred.scaled(
        source.size(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _gaussian_blur_image(source: QImage, standard_deviation: float) -> QImage:
    """Apply a separable ``3 * sigma`` Gaussian with a transparent border."""
    image = source.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image

    source_bits = image.constBits()
    source_bits.setsize(image.sizeInBytes())
    source_rows = np.frombuffer(source_bits, dtype=np.uint8).reshape(
        height, image.bytesPerLine()
    )
    pixels = source_rows[:, : width * 4].reshape(height, width, 4).astype(np.float32)

    kernel = _n3_gaussian_kernel_1d(standard_deviation).astype(np.float32)
    support_radius = len(kernel) // 2
    horizontal = np.pad(
        pixels,
        ((0, 0), (support_radius, support_radius), (0, 0)),
        mode="constant",
    )
    horizontal_windows = np.lib.stride_tricks.sliding_window_view(
        horizontal, len(kernel), axis=1
    )
    horizontal_blur = np.einsum(
        "...k,k->...", horizontal_windows, kernel, optimize=True
    )
    vertical = np.pad(
        horizontal_blur,
        ((support_radius, support_radius), (0, 0), (0, 0)),
        mode="constant",
    )
    vertical_windows = np.lib.stride_tricks.sliding_window_view(
        vertical, len(kernel), axis=0
    )
    blurred = np.einsum("...k,k->...", vertical_windows, kernel, optimize=True)
    quantized = np.clip(np.rint(blurred), 0, 255).astype(np.uint8)

    result = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    result.fill(0)
    result_bits = result.bits()
    result_bits.setsize(result.sizeInBytes())
    result_rows = np.frombuffer(result_bits, dtype=np.uint8).reshape(
        height, result.bytesPerLine()
    )
    result_rows[:, : width * 4] = quantized.reshape(height, width * 4)
    return result


def _paint_after_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, None, fill_segments, y, metrics, t_ms, rtl
    )


def _paint_after_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, width, fill_segments, y, metrics, t_ms, rtl
    )


def _paint_after_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    stroke_width: int | None,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    # 卡拉ok填色是连续扫光，已唱字符总是连续从一侧开始；把 N 个相邻 char
    # clip 合并成单 clip rect → 整 line path 只画一次，不再 N 次重复绘制。
    band = _fill_clip_band(fill_segments, t_ms, rtl)
    if band is None:
        return
    clip = _horizontal_after_path_clip_rect(
        fill_segments, y, metrics, t_ms, rtl, stroke_width
    )
    if clip is None:
        return
    painter.save()
    try:
        painter.setClipRect(clip)
        if stroke_width is None:
            _paint_fill_path(painter, path, fill, rect)
        else:
            _paint_stroke_path(painter, path, fill, rect, stroke_width)
    finally:
        painter.restore()


def _horizontal_after_path_clip_rect(
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool,
    stroke_width: int | None,
) -> QRectF | None:
    band = _fill_clip_band(fill_segments, t_ms, rtl)
    if band is None:
        return None
    fill_start, fill_end = band
    stroke_pad = 0 if stroke_width is None else math.ceil(stroke_width / 2)
    return QRectF(
        float(fill_start - stroke_pad),
        float(y - metrics.ascent() - stroke_pad),
        float((fill_end - fill_start) + stroke_pad),
        float(metrics.height() + stroke_pad * 2),
    )


def _legacy_fill_extent_end(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    t_ms: int,
) -> int:
    """Return rightmost x of the karaoke-filled extent at ``t_ms``.

    卡拉ok填色按字符顺序左→右推进，给定 ``t_ms`` 时一定形如
    "前 k 个字符全填 + 第 k+1 个字符部分填 + 之后全空"。本函数返回填色
    末端的 x 坐标；与 ``x0`` 相等表示当前没有字符被填到（直接早退）。
    """
    fill_end = x0
    cursor_x = x0
    for w, (cs, ce) in zip(char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            cursor_x += w
            fill_end = cursor_x
            continue
        # 部分填色——也是最后一个被填到的字符
        fill_end = cursor_x + int(round(w * ratio))
        break
    return fill_end


def _char_ink_x_ranges(
    texts: list[str],
    fonts: list[QFont],
    char_lefts: list[int],
    char_path_offsets: list[float] | None = None,
) -> list[tuple[int, int]]:
    """每个字符的墨水水平边界（绝对坐标 ``(ink_left, ink_right)``）。

    走字（卡拉ok 扫光）严格按字形**墨水**推进，而非按 advance 框。advance 含字形
    左右两侧的 side bearing 与字间空隙，纯按 advance 走会让扫光锋面与字形墨水错位
    （字头偏慢——锋面停在左侧空白上墨水迟迟不染；字尾悬空——墨水早已染满而锋面还在
    右侧空白里推进）。这里用 ``QPainterPath.addText`` 的矢量包围盒取墨水边界：与实际
    ``fillPath`` 绘制同源、与 DPR/点阵 strike 无关。空白字符无墨水 → 零宽 ``(left, left)``。
    与 SUG ``karaoke_preview.py`` 的 ``_ink_bounds``（``tightBoundingRect``）同口径。
    """
    if char_path_offsets is None:
        char_path_offsets = [0.0 for _ in char_lefts]
    ranges: list[tuple[int, int]] = []
    for text, font, left, path_offset_x in zip(texts, fonts, char_lefts, char_path_offsets):
        if not text or text.isspace():
            ranges.append((left, left))
            continue
        path = QPainterPath()
        path.addText(float(left + path_offset_x), 0.0, font, text)
        br = path.boundingRect()
        if br.isEmpty():
            ranges.append((left, left))
        else:
            ranges.append((int(math.floor(br.left())), int(math.ceil(br.right()))))
    return ranges


def _karaoke_fill_segments(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    ink_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    line: TimingLine,
    *,
    release_x_ranges: list[tuple[int, int]] | None = None,
    layout_x_ranges: list[tuple[int, int]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> list[_FillSegment]:
    """构造走字分段。``ink_x_ranges`` 为各字符的墨水边界（非 advance 框），
    扫光锋面据此推进，确保不扫过字形两侧的透明空白（见 :func:`_char_ink_x_ranges`）。"""
    segments: list[_FillSegment] = []
    release_x_ranges = release_x_ranges or ink_x_ranges
    layout_x_ranges = layout_x_ranges or release_x_ranges
    index = 0
    while index < len(char_widths):
        if index < len(line.chars) and _bitmap_guide_is_no_wipe(
            line.chars[index].vector_glyph
        ):
            index += 1
            continue
        ruby = _ruby_for_char_index(active_rubies, line, intervals, index)
        ruby_indices = (
            _ruby_target_indices(ruby, line, intervals) if ruby is not None else []
        )
        # SUG uses a pause-only ruby over a linked English phrase as a
        # non-rendering group marker.  Utopia must still consume that ruby to
        # drop the whole phrase together, but it is not pronunciation data and
        # must not replace the phrase's real per-syllable TimingChar clock with
        # one linear start-to-end wipe.
        if (
            ruby is None
            or _is_utopia_group_marker(ruby)
            or _ruby_main_uses_base_timing(line, ruby_indices)
        ):
            left, right = ink_x_ranges[index]
            release_left, release_right = release_x_ranges[index]
            layout_left, layout_right = layout_x_ranges[index]
            start, end = intervals[index]
            segments.append(
                _FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    start_ms=start,
                    end_ms=end,
                    indices=(index,),
                )
            )
            index += 1
            continue

        indices = [i for i in ruby_indices if 0 <= i < len(ink_x_ranges)]
        if not indices:
            left, right = ink_x_ranges[index]
            release_left, release_right = release_x_ranges[index]
            layout_left, layout_right = layout_x_ranges[index]
            start, end = intervals[index]
            segments.append(
                _FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    start_ms=start,
                    end_ms=end,
                    indices=(index,),
                )
            )
            index += 1
            continue

        effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        reading_unit_mode = (
            ruby_main_progress_mode == "reading_units"
            and bool(_ruby_visual_units_and_intervals(effective_ruby))
        )
        if reading_unit_mode:
            base_count = len(indices)
            for base_index, target_index in enumerate(indices):
                left, right = ink_x_ranges[target_index]
                release_left, release_right = release_x_ranges[target_index]
                layout_left, layout_right = layout_x_ranges[target_index]
                slot_start, slot_end = _ruby_main_text_slot_times(
                    effective_ruby, base_index, base_count
                )
                segments.append(
                    _FillSegment(
                        left=left,
                        right=right,
                        release_left=release_left,
                        release_right=release_right,
                        layout_left=layout_left,
                        layout_right=layout_right,
                        start_ms=slot_start,
                        end_ms=slot_end,
                        ruby=effective_ruby,
                        indices=(target_index,),
                        ruby_base_index=base_index,
                        ruby_base_count=base_count,
                    )
                )
        else:
            left = min(ink_x_ranges[i][0] for i in indices)
            right = max(ink_x_ranges[i][1] for i in indices)
            release_left = min(release_x_ranges[i][0] for i in indices)
            release_right = max(release_x_ranges[i][1] for i in indices)
            layout_left = min(layout_x_ranges[i][0] for i in indices)
            layout_right = max(layout_x_ranges[i][1] for i in indices)
            segments.append(
                _FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    ruby=effective_ruby,
                    indices=tuple(indices),
                )
            )
        index = max(indices) + 1
    return _adjust_fill_release_edges(segments)


def _adjust_fill_release_edges(segments: list[_FillSegment]) -> list[_FillSegment]:
    """Apply N3 ``AdjustWipeEnd`` at overlapping character boxes.

    N3 calculates the adjusted *position ratio* in DrawLeft/DrawRight layout-box
    coordinates, then applies that ratio to the transformed ink geometry used by
    WipeLeft.  Bearings and the primary edge therefore affect the visible end
    point without changing the overlap decision itself.
    """
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        release_left = current.release_left if current.release_left is not None else current.left
        release_right = current.release_right if current.release_right is not None else current.right
        layout_left = current.layout_left if current.layout_left is not None else release_left
        layout_right = current.layout_right if current.layout_right is not None else release_right
        following_left = (
            following.layout_left
            if following.layout_left is not None
            else (following.release_left if following.release_left is not None else following.left)
        )
        following_right = (
            following.layout_right
            if following.layout_right is not None
            else (following.release_right if following.release_right is not None else following.right)
        )
        layout_width = max(layout_right - layout_left + 1, 1)
        if layout_left <= following_left:
            if layout_right >= following_left:
                pose = max(0.0, min(1.0, (following_left - layout_left) / layout_width))
                adjusted[index] = replace(
                    current,
                    release_right=release_left + (release_right - release_left) * pose,
                )
        elif layout_left <= following_right:
            pose = max(0.0, min(1.0, (layout_right - following_right) / layout_width))
            adjusted[index] = replace(
                current,
                release_left=release_right - (release_right - release_left) * pose,
            )
    return adjusted


def _ruby_for_char_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
) -> RubyAnnotation | None:
    for ruby in rubies:
        if index in _ruby_target_indices(ruby, line, intervals):
            return ruby
    return None


def _ruby_target_indices(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> list[int]:
    time_indices = _ruby_time_indices(ruby, intervals)
    if ruby.kanji:
        return _find_ruby_text_indices(ruby.kanji, line, preferred_indices=time_indices)
    return time_indices


def _resolve_char_ruby_groups(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> dict[int, tuple[list[int], RubyAnnotation]]:
    """预解析 ``char index -> (该字所属 ruby 的 target indices, ruby)``，每行算一次。

    等价于逐字调用 ``_ruby_for_char_index`` + ``_ruby_target_indices``，但这些查找是
    **布局静态**（不依赖 ``t_ms``，只取决于 rubies/line/intervals）。原本在 transition
    逐字逐帧循环里反复重算（实测每帧数百次 ``_find_ruby_text_span``/``_text_span_indices``），
    在此一次性建表。``setdefault`` 实现「rubies 顺序中首个命中者胜」，与 ``_ruby_for_char_index``
    一致；消费方（``_utopia_main_group_for_index`` / ``_character_fill_ratio``）各自对返回的
    indices 施加自己的范围过滤，故行为逐像素不变。
    """
    groups: dict[int, tuple[list[int], RubyAnnotation]] = {}
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        for index in indices:
            groups.setdefault(index, (indices, ruby))
    return groups


def _ruby_main_uses_base_timing(
    line: TimingLine,
    indices: list[int],
) -> bool:
    """是否保留 ruby 目标正文已有的逐字时间边界。

    ``DrawDataGenerator.SetOneLineWipe`` 只在 ruby 组内部没有显式正文边界时调用
    ``RubyTimesToKanjiTimes``。组首的 begin 和组尾的 end 不算内部边界；任一后续正文
    字符有显式 begin，或任一非末正文字符有显式 end，整组都改用正文自己的时钟。
    """
    valid = [index for index in indices if 0 <= index < len(line.chars)]
    if len(valid) <= 1:
        return False
    last_offset = len(valid) - 1
    for offset, index in enumerate(valid):
        char = line.chars[index]
        if offset > 0 and char.explicit_start:
            return True
        if offset < last_offset and char.explicit_end:
            return True
    return False


def _ruby_time_indices(
    ruby: RubyAnnotation,
    intervals: list[tuple[int, int]],
) -> list[int]:
    return [
        index
        for index, (start, end) in enumerate(intervals)
        if start < ruby.pos_end_ms and end > ruby.pos_start_ms
    ]


def _effective_ruby_for_target(
    ruby: RubyAnnotation,
    indices: list[int],
    intervals: list[tuple[int, int]],
) -> RubyAnnotation:
    """把 ruby 的 wipe 时钟对齐到目标字符区间（收窄方向）。

    基字区间可因呼吸停顿 / 多字块再分配而比导出的 ``pos`` 区间**短**，此时
    以基字区间为准（``fix: honor subtitle pause timing`` 的场景）；但基字
    interval 的末端是「下一字开始 / 行末」，比 ruby 自身唱完时刻**晚**时不能
    采用，否则每条 ruby 会拖到间隙 / 行尾才走完。取交集：结束时刻用两者中
    更早的有效值。
    """
    valid_indices = [index for index in indices if 0 <= index < len(intervals)]
    if not valid_indices:
        return ruby
    start = min(intervals[index][0] for index in valid_indices)
    end = max(intervals[index][1] for index in valid_indices)
    if ruby.pos_end_ms > ruby.pos_start_ms and start < ruby.pos_end_ms < end:
        end = ruby.pos_end_ms
    if start == ruby.pos_start_ms and end == ruby.pos_end_ms:
        return ruby
    target_duration = max(end - start, 0)
    reading_part_ms = [max(0, min(target_duration, rel_ms)) for rel_ms in ruby.reading_part_ms]
    return replace(
        ruby,
        pos_start_ms=start,
        pos_end_ms=end,
        reading_part_ms=reading_part_ms,
    )


def _offset_fill_segments(segments: list[_FillSegment], dx: int) -> list[_FillSegment]:
    if dx == 0:
        return segments
    return [
        _FillSegment(
            left=segment.left + dx,
            right=segment.right + dx,
            release_left=(
                segment.release_left + dx if segment.release_left is not None else None
            ),
            release_right=(
                segment.release_right + dx if segment.release_right is not None else None
            ),
            layout_left=(
                segment.layout_left + dx if segment.layout_left is not None else None
            ),
            layout_right=(
                segment.layout_right + dx if segment.layout_right is not None else None
            ),
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            ruby=segment.ruby,
            indices=segment.indices,
        )
        for segment in segments
    ]


def _fill_extent_start(segments: list[_FillSegment]) -> float | None:
    if not segments:
        return None
    first = segments[0]
    return first.release_left if first.release_left is not None else first.left


def _segment_wipe_edges(segment: _FillSegment) -> tuple[float, float]:
    """Return the N3 drawing edges used by the moving wipe front.

    ``left`` / ``right`` keep the glyph-ink bounds for layout and ruby mapping,
    while ``release_*`` are the full DrawLeft/DrawRight-style bounds (including
    the primary edge).  NicoKaraMaker3 interpolates across those full drawing
    bounds for the *whole* character interval; ``AdjustWipeEnd`` only changes
    the destination edge before interpolation.  Characters without drawable
    ink (spaces) deliberately remain zero-width: their timing consumes time
    without moving the visible front.  Falling back to ink bounds keeps
    synthetic/legacy segments compatible.
    """
    if segment.right <= segment.left:
        return segment.left, segment.left
    left = (
        segment.release_left if segment.release_left is not None else segment.left
    )
    right = (
        segment.release_right if segment.release_right is not None else segment.right
    )
    return left, max(left, right)


def _segment_wipe_times(segment: _FillSegment) -> tuple[int, int]:
    """Return the effective N3 wipe window for one main-text segment."""
    if segment.ruby_base_index is not None:
        return int(segment.start_ms), int(segment.end_ms)
    if segment.ruby is not None:
        return int(segment.ruby.pos_start_ms), int(segment.ruby.pos_end_ms)
    return int(segment.start_ms), int(segment.end_ms)


def _segment_wipe_band_at(
    segment: _FillSegment,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int]:
    """Return one segment's wipe band, including its zero-progress boundary."""
    wipe_left, wipe_right = _segment_wipe_edges(segment)
    ratio = _segment_fill_ratio(segment, t_ms)
    if rtl:
        boundary = wipe_right - int(round((wipe_right - wipe_left) * ratio))
        return boundary, wipe_right
    boundary = wipe_left + int(round((wipe_right - wipe_left) * ratio))
    return wipe_left, boundary


def _n3_following_wipe_band(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """Keep a completed glyph on N3's shared front until its successor ends.

    N3 continues treating the preceding character as ``IsWiping`` while the
    following character is active.  At the exact hand-off it still uses the
    preceding segment's adjusted endpoint; afterwards it reuses the following
    segment's moving boundary.  This is what releases overlapping outlines
    continuously instead of fully opening each glyph at its own end.

    N3's implementation is left-to-right.  Keep this compatibility behavior
    scoped away from the application's independent RTL extension.
    """
    if rtl or not indices:
        return None
    positions = [
        position
        for position, segment in enumerate(segments)
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    if not positions:
        return None
    current_position = max(positions)
    if current_position >= len(segments) - 1:
        return None
    current = segments[current_position]
    following = segments[current_position + 1]
    # N3 cannot reuse a following space/no-wipe glyph because it has no
    # transformed geometry.  Its WipeLeft fallback leaves the completed glyph
    # fully released instead of holding its outline/glow through the pause.
    if following.right <= following.left:
        return None
    current_start, current_end = _segment_wipe_times(current)
    _following_start, following_end = _segment_wipe_times(following)
    if not (
        current_start < t_ms < following_end
        and current_start != following_end
        and _segment_fill_ratio(current, t_ms) >= 1.0
    ):
        return None
    if t_ms <= current_end:
        return _segment_wipe_band_at(current, t_ms, rtl=False)
    return _segment_wipe_band_at(following, t_ms, rtl=False)


def _fill_extent_end(
    segments: list[_FillSegment],
    t_ms: int,
) -> float:
    """Return the current right edge of the continuous karaoke scan.

    Motion follows the N3 drawing bounds for the complete interval.  In
    particular, the destination is the AdjustWipeEnd-clamped DrawRight from
    the first partial frame onward; switching from ink ``right`` to DrawRight
    only on the completion frame causes a visible one-frame jump.
    """
    if not segments:
        return 0
    fill_end, _ = _segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = _segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = _segment_wipe_edges(segment)
        if ratio >= 1.0:
            fill_end = max(fill_end, wipe_right)
            continue
        fill_end = max(
            fill_end,
            wipe_left + int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return fill_end


def _fill_extent_left(segments: list[_FillSegment], t_ms: int) -> float:
    """RTL：返回已唱区的左缘 x（扫光从右向左推进时的移动边）。

    句中停顿的间隙中点推进与 :func:`_fill_extent_end` 镜像。
    """
    if not segments:
        return 0
    _, scanline = _segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = _segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = _segment_wipe_edges(segment)
        if ratio >= 1.0:
            scanline = min(scanline, wipe_left)
            continue
        scanline = min(
            scanline,
            wipe_right - int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return scanline


def _fill_clip_band(
    segments: list[_FillSegment],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """已唱区水平裁剪带 ``(left, right)``；空带返回 ``None``。

    LTR：左缘固定在首字符左缘，右缘随扫光右移；
    RTL：右缘固定在首字符（最右）右缘，左缘随扫光左移。
    """
    if not segments:
        return None
    if rtl:
        left = _fill_extent_left(segments, t_ms)
        right = max(_segment_wipe_edges(segment)[1] for segment in segments)
    else:
        left = _fill_extent_start(segments)
        right = _fill_extent_end(segments, t_ms)
    if left is None or right is None or right <= left:
        return None
    return left, right


def _fill_clip_band_for_indices(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    if not indices:
        return _fill_clip_band(segments, t_ms, rtl)
    scoped = [
        segment
        for segment in segments
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    while scoped and (
        scoped[0].right <= scoped[0].left
        or _segment_fill_ratio(scoped[0], t_ms) <= 0.0
    ):
        scoped = scoped[1:]
    return _fill_clip_band(scoped, t_ms, rtl)


def _fill_clip_band_for_glyphs(
    segments: list[_FillSegment],
    glyphs: list[_GlyphLayout],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    return _fill_clip_band_for_indices(
        segments,
        {glyph.index for glyph in glyphs},
        t_ms,
        rtl,
    )


def _run_fill_complete(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
) -> bool:
    """run 覆盖的走字分段是否已全部唱完（扫光线已越过 run 前缘）。

    唱完后已唱层不再需要在扫光线处裁切，行缘的发光/描边可完整外扩。
    """
    if indices:
        scoped = [
            segment
            for segment in segments
            if segment.indices and any(index in indices for index in segment.indices)
        ]
    else:
        scoped = segments
    return bool(scoped) and all(
        _segment_fill_ratio(segment, t_ms) >= 1.0 for segment in scoped
    )


def _segment_fill_ratio(segment: _FillSegment, t_ms: int) -> float:
    if segment.ruby is None:
        return char_fill_ratio(segment.start_ms, segment.end_ms, t_ms)
    if segment.ruby_base_index is not None:
        progress = _main_text_ruby_progress_ratio(
            segment.ruby, t_ms, mode="reading_units"
        )
        return max(
            0.0,
            min(
                1.0,
                progress * max(segment.ruby_base_count, 1)
                - segment.ruby_base_index,
            ),
        )
    return _main_text_ruby_progress_ratio(segment.ruby, t_ms)


def _character_fill_ratio(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    index: int,
    t_ms: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> float:
    # groups 由 _resolve_char_ruby_groups 预建（每行一次）；缺省回退逐字查找。
    if groups is not None:
        entry = groups.get(index)
        ruby = entry[1] if entry is not None else None
        raw_indices = entry[0] if entry is not None else None
    else:
        ruby = _ruby_for_char_index(active_rubies, line, intervals, index)
        raw_indices = _ruby_target_indices(ruby, line, intervals) if ruby is not None else None
    # Keep SUG's pause-only linked-phrase marker available to
    # ``_utopia_main_group_for_index`` while letting the main-text wipe follow
    # the underlying syllable/character intervals.
    if ruby is not None and _is_utopia_group_marker(ruby):
        ruby = None
        raw_indices = None
    if ruby is not None:
        indices = [
            candidate
            for candidate in raw_indices
            if 0 <= candidate < len(char_x_ranges)
        ]
        if indices and not _ruby_main_uses_base_timing(line, indices):
            effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
            if (
                ruby_main_progress_mode == "reading_units"
                and _ruby_visual_units_and_intervals(effective_ruby)
            ):
                base_index = indices.index(index)
                progress = _main_text_ruby_progress_ratio(
                    effective_ruby, t_ms, mode="reading_units"
                )
                return max(
                    0.0,
                    min(1.0, progress * len(indices) - base_index),
                )
            group_left = min(char_x_ranges[candidate][0] for candidate in indices)
            group_right = max(char_x_ranges[candidate][1] for candidate in indices)
            fill_end = group_left + (group_right - group_left) * _main_text_ruby_progress_ratio(
                effective_ruby, t_ms
            )
            char_left, char_right = char_x_ranges[index]
            width = max(char_right - char_left, 1)
            return max(0.0, min(1.0, (fill_end - char_left) / width))
    if index >= len(intervals):
        return 0.0
    start, end = intervals[index]
    return char_fill_ratio(start, end, t_ms)


def _is_utopia_group_marker(ruby: RubyAnnotation) -> bool:
    """Return whether ``ruby`` is SUG's linked-phrase-only pause marker."""
    return ruby.reading.strip() in {"", "^"} and all(
        not part.strip() or part.strip() == "^" for part in ruby.reading_parts
    )


def _brush_for_fill(fill: PaintFill, rect: QRectF) -> QBrush:
    if fill.mode == "image" and fill.image_path:
        brush = _cached_image_brush(fill.image_path, fill.image_scale_pct)
        if brush is not None:
            return brush

    if fill.mode == "gradient_horizontal":
        return _linear_gradient_brush(fill, rect, 0)
    if fill.mode == "gradient_vertical":
        return _linear_gradient_brush(fill, rect, 90)
    if fill.mode == "split_vertical":
        return _split_vertical_brush(fill, rect)
    return QBrush(_valid_color(fill.color, "#FFFFFF"))


def _fill_is_alpha(fill: PaintFill) -> bool:
    """Return whether N3 protects the glyph body from its primary edge."""
    if fill.mode == "image":
        # N3 treats bitmap brushes as alpha-capable unconditionally.  This is
        # intentional even for opaque or temporarily missing image files.
        return True
    if fill.mode in {"gradient_horizontal", "gradient_vertical"}:
        colors = [color for _position, color in _gradient_stops(fill)]
    elif fill.mode == "split_vertical":
        colors = [color for _position, color in _split_gradient_stops(fill)]
    else:
        colors = [fill.color]
    return any(_valid_color(color, fill.color).alpha() < 255 for color in colors)


def _cached_image_brush(path: str, scale_pct: int) -> QBrush | None:
    signature = _image_file_signature(path)
    if signature is None:
        return None
    scale = max(1, min(int(scale_pct), 1000))
    brush_key = (*signature, scale)
    with _IMAGE_FILL_LOCK:
        brush = _IMAGE_BRUSH_CACHE.get(brush_key)
        if brush is not None:
            _IMAGE_BRUSH_CACHE.move_to_end(brush_key)
            return QBrush(brush)

    image = _cached_fill_image(signature)
    if image is None or image.isNull():
        return None
    brush = QBrush(image)
    brush_scale = scale / 100.0
    # N3 uses a Direct2D bitmap brush with Wrap/Wrap extension and applies
    # BitmapScale directly.  QBrush texture patterns wrap in both directions
    # as well; using the same direct transform keeps 200% visually twice as
    # large instead of twice as dense.  No translation is applied here: the
    # texture is anchored at the render target origin, not at each lyric line.
    brush.setTransform(QTransform().scale(brush_scale, brush_scale))

    with _IMAGE_FILL_LOCK:
        _IMAGE_BRUSH_CACHE[brush_key] = brush
        while len(_IMAGE_BRUSH_CACHE) > _IMAGE_FILL_CACHE_MAX:
            _IMAGE_BRUSH_CACHE.popitem(last=False)
    return QBrush(brush)


def _anchor_texture_brush(brush: QBrush, rect: QRectF) -> QBrush:
    anchored = QBrush(brush)
    transform = QTransform(anchored.transform())
    transform.translate(rect.left(), rect.top())
    anchored.setTransform(transform)
    return anchored


def _cached_fill_image(signature: tuple[str, int, int]) -> QImage | None:
    with _IMAGE_FILL_LOCK:
        cached = _IMAGE_FILL_CACHE.get(signature)
        if cached is not None:
            _IMAGE_FILL_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        return None
    with _IMAGE_FILL_LOCK:
        _IMAGE_FILL_CACHE[signature] = image
        while len(_IMAGE_FILL_CACHE) > _IMAGE_FILL_CACHE_MAX:
            _IMAGE_FILL_CACHE.popitem(last=False)
    return image


def _image_file_signature(path: str) -> tuple[str, int, int] | None:
    try:
        normalized = os.path.abspath(os.path.normpath(path))
        stat = os.stat(normalized)
    except OSError:
        return None
    return normalized, int(stat.st_mtime_ns), int(stat.st_size)


def _linear_gradient_brush(fill: PaintFill, rect: QRectF, angle_deg: int) -> QBrush:
    angle = math.radians(angle_deg % 360)
    dx = math.cos(angle)
    dy = math.sin(angle)
    projection = abs(rect.width() * dx) + abs(rect.height() * dy)
    if projection <= 0:
        projection = max(rect.width(), rect.height(), 1.0)
    half = projection / 2.0
    center = rect.center()
    start = QPointF(center.x() - dx * half, center.y() - dy * half)
    end = QPointF(center.x() + dx * half, center.y() + dy * half)

    gradient = QLinearGradient(start, end)
    for position, color in _gradient_stops(fill):
        gradient.setColorAt(position / 100.0, _valid_color(color, fill.color))
    return QBrush(gradient)


def _split_vertical_brush(fill: PaintFill, rect: QRectF) -> QBrush:
    """Return an exact hard-band texture, cached by height and stop values.

    Qt collapses duplicate-position ``QGradientStop`` entries, so a linear
    gradient cannot represent N3 MilleFeuille without a visible transition.
    A one-pixel-wide texture keeps every boundary exact; the cached base brush
    is only translated per glyph/run and is never regenerated per frame.
    """
    stops = _split_gradient_stops(fill)
    height = max(int(math.ceil(rect.height())), 1)
    stop_key = tuple(
        (position, _valid_color(color, fill.color).rgba())
        for position, color in stops
    )
    key = (height, stop_key)
    with _IMAGE_FILL_LOCK:
        base = _HARD_BAND_BRUSH_CACHE.get(key)
        if base is not None:
            _HARD_BAND_BRUSH_CACHE.move_to_end(key)
        else:
            image = QImage(1, height, QImage.Format.Format_ARGB32_Premultiplied)
            band_index = 0
            for y in range(height):
                position = (y + 0.5) * 100.0 / height
                while (
                    band_index + 1 < len(stops)
                    and stops[band_index + 1][0] <= position
                ):
                    band_index += 1
                image.setPixelColor(
                    0, y, _valid_color(stops[band_index][1], fill.color)
                )
            base = QBrush(image)
            _HARD_BAND_BRUSH_CACHE[key] = base
            while len(_HARD_BAND_BRUSH_CACHE) > _HARD_BAND_BRUSH_CACHE_MAX:
                _HARD_BAND_BRUSH_CACHE.popitem(last=False)
    return _anchor_texture_brush(base, rect)


def _split_gradient_stops(fill: PaintFill) -> list[tuple[float, str]]:
    raw = list(fill.split_stops)
    if len(raw) < 2:
        raw = [
            (0, fill.split_top_color),
            (fill.split_position_pct, fill.split_bottom_color),
            (100, fill.split_bottom_color),
        ]
    stops = sorted(
        (
            _gradient_stop_position(position),
            color,
        )
        for position, color in raw
    )
    if stops[0][0] > 0:
        stops.insert(0, (0, stops[0][1]))
    if stops[-1][0] < 100:
        stops.append((100, stops[-1][1]))
    return stops


# ---------------------------------------------------------------------------
# Before-layer 缓存：构建 / 查询
# ---------------------------------------------------------------------------


def _fill_signature(fill: PaintFill) -> tuple:
    return (
        fill.mode,
        fill.color,
        fill.start_color,
        fill.end_color,
        tuple(_gradient_stops(fill)),
        fill.split_top_color,
        fill.split_bottom_color,
        fill.split_position_pct,
        tuple(fill.split_stops),
        fill.image_path,
        fill.image_scale_pct,
    )


def _karaoke_state_signature(state: KaraokeColorState) -> tuple:
    return (
        _fill_signature(state.text),
        _fill_signature(state.stroke),
        _fill_signature(state.stroke2),
        _fill_signature(state.shadow),
    )


def _effective_karaoke_colors(style: Style) -> KaraokeColors:
    if style.karaoke_colors is not None:
        return style.karaoke_colors

    before = KaraokeColorState(
        text=_solid_fill(style.base_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    after_text = _legacy_after_text_fill(style)
    after = KaraokeColorState(
        text=after_text,
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


def _legacy_after_text_fill(style: Style) -> PaintFill:
    if not style.fill_gradient_enabled:
        return _solid_fill(style.fill_color)
    mode = "gradient_vertical" if style.fill_gradient_angle_deg in {90, 270} else "gradient_horizontal"
    return PaintFill(
        mode=mode,
        color=style.fill_color,
        start_color=style.fill_gradient_start_color,
        end_color=style.fill_gradient_end_color,
        gradient_stops=[
            (0, style.fill_gradient_start_color),
            (100, style.fill_gradient_end_color),
        ],
        split_top_color=style.fill_gradient_start_color,
        split_bottom_color=style.fill_gradient_end_color,
    )


def _solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def _gradient_stop_position(value: object) -> float:
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        position = 0.0
    if not math.isfinite(position):
        position = 0.0
    return max(0.0, min(100.0, position))


def _gradient_stops(fill: PaintFill) -> list[tuple[float, str]]:
    raw = fill.gradient_stops or [(0, fill.start_color), (100, fill.end_color)]
    normalized: list[tuple[float, str]] = []
    for position, color in raw:
        normalized.append((_gradient_stop_position(position), color))
    normalized.sort(key=lambda item: item[0])
    positions = {position for position, _color in normalized}
    if 0 not in positions:
        normalized.insert(0, (0, fill.start_color))
    if 100 not in positions:
        normalized.append((100, fill.end_color))
    return normalized


def _valid_color(value: str, fallback: str) -> QColor:
    color = QColor(value)
    if color.isValid():
        return color
    fallback_color = QColor(fallback)
    return fallback_color if fallback_color.isValid() else QColor("#FF5A6F")


def _line_center_override(track: TimingTrack, line: TimingLine, style: Style) -> bool:
    """N3 SmartHorizon：仅页内只有一行时强制居中。

    仅在默认「上左下右」双行布局下生效；center 布局本就居中，per_row 是
    用户手动逐行控制，不覆盖。``ParagraphBreak`` 只结束当前页/段落，并不
    令多行页的最后一行单独居中；N3 的准确条件是 ``topLineIndex ==
    bottomLineIndex``。``smart_horizontal == "none"`` 时关闭此覆盖。
    """
    if style.smart_horizontal == "none":
        return False
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return False
    page = _renderable_page_lines(track, line, style)
    return page is not None and len(page) == 1


def _lane_count(style: Style) -> int:
    """多行显示的行数 = 每行对齐列表长度；单行模式恒为 1。"""
    if not style.dual_line_layout:
        return 1
    return max(len(style.line_alignments), 1)


def _lane_alignment(
    style: Style, lane: int | None, page_line_count: int | None = None
) -> str:
    """lane（0 = 页内最上行）对应的水平对齐（N3 ``CalcHorizontalAlignment``）。

    取值方向跟着上下配置走：Top / Middle 从对齐列表**开头**往下数，Bottom 从
    **末尾**往回数。满页时两者等价，段末短页（页内行数 < 列表长度）不等价——
    Bottom 锚定的短页取列表末尾那几项，例如 3 行布局 ``[左, 中, 右]`` 里的两行页
    是「中 + 右」而不是「左 + 中」。``page_line_count`` 为 ``None`` 时按满页处理。
    越界钳到端项（对应 N3 两个分支各自的 while 上下限）。
    """
    alignments = style.line_alignments or ["left"]
    count = len(alignments)
    index = 0 if lane is None else max(int(lane), 0)
    if (
        page_line_count is not None
        and style.line_y_position == "bottom"
        and 0 < int(page_line_count) < count
    ):
        index = max(count - int(page_line_count) + index, 0)
    return alignments[min(index, count - 1)]


def _line_lane_alignment(
    track: TimingTrack, line: TimingLine, style: Style, lane: int | None
) -> str:
    """某一行的水平对齐；Bottom 短页会改取对齐列表末尾（见 ``_lane_alignment``）。"""
    if not _bottom_short_page_alignment(style):
        return _lane_alignment(style, lane)
    page = _renderable_page_lines(track, line, style)
    return _lane_alignment(style, lane, len(page) if page else None)


def _bottom_short_page_alignment(style: Style) -> bool:
    """Bottom 锚定 + 多行对齐列表：短页会改取对齐列表的末尾项。"""
    return (
        style.dual_line_layout
        and style.line_horizontal_layout == "asymmetric"
        and style.line_y_position == "bottom"
        and len(style.line_alignments or []) > 1
    )


def _layout_page_lines(
    track: TimingTrack, line: TimingLine, style: Style
) -> list[tuple[TimingLine, int]] | None:
    """水平布局要用到的页成员；SmartHorizon 与 Bottom 短页对齐都不需要时返回 None。

    两个特性共用同一趟页定位，避免多算一次 ``assign_lanes``。
    """
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return None
    needs_smart = style.smart_horizontal != "none" and not style.vertical
    if not needs_smart and not _bottom_short_page_alignment(style):
        return None
    return _renderable_page_lines(track, line, style)


def _resolve_line_x(
    img_w: int,
    total_w: int,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
    page_line_count: int | None = None,
) -> int:
    if center_override:
        return (img_w - total_w) // 2
    if style.line_horizontal_layout == "per_row":
        align, offset_x, _ = _row_layout_params(style, lane)
        return _aligned_x0(img_w, total_w, align) + offset_x
    if style.line_horizontal_layout == "center":
        return (img_w - total_w) // 2
    if style.dual_line_layout and lane is not None:
        align = _lane_alignment(style, lane, page_line_count)
        margin = max(style.horizontal_margin_px, 0)
        if align == "left":
            return margin
        if align == "right":
            return img_w - margin - total_w
        return (img_w - total_w) // 2
    return (img_w - total_w) // 2


def _aligned_x0(img_w: int, total_w: int, align: str) -> int:
    """根据水平锚点返回行左边缘 x0：left=贴左，center=居中，right=贴右。"""
    if align == "center":
        return (img_w - total_w) // 2
    if align == "right":
        return img_w - total_w
    return 0


def _row_layout_params(style: Style, lane: int | None) -> tuple[str, int, int]:
    """逐行布局参数 (对齐, offset_x, offset_y)。lane 1 取第二行，其余取第一行。"""
    if lane == 1:
        return style.row2_align, style.row2_offset_x, style.row2_offset_y
    return style.row1_align, style.row1_offset_x, style.row1_offset_y


def _ruby_char_gaps(
    line: TimingLine,
    char_widths: list[int],
    rubies: list[RubyAnnotation],
    style: Style,
    intervals: list[tuple[int, int]] | None = None,
) -> tuple[list[int], int, int]:
    """相邻 ruby 避让及行缘溢出。排版过程中每行会被问十来次，缓存到区间内。

    带注音的曲目里这是最后一块明显的重复计算：六个调用点各自从头量一遍这行的
    注音间隔。合成轨道没有注音，之前几轮的基准照不出来。
    """
    cache = getattr(_LAYOUT_PASS, "ruby_gaps", None)
    if cache is None:
        return _ruby_char_gaps_uncached(line, char_widths, rubies, style, intervals)
    # 按每条注音的身份取键，不是列表的身份：调用方拿到的 rubies 多半是
    # _active_rubies_for_line 现做的一份新列表，用 id(rubies) 会每次都落空。
    cache_key = (
        id(line),
        tuple(char_widths),
        tuple(id(ruby) for ruby in rubies),
        id(style),
        None if intervals is None else tuple(intervals),
    )
    hit = cache.get(cache_key)
    if hit is None:
        gaps, left, right = _ruby_char_gaps_uncached(
            line, char_widths, rubies, style, intervals
        )
        hit = (tuple(gaps), left, right)
        cache[cache_key] = hit
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.ruby_lists.append(list(rubies))
        _LAYOUT_PASS.styles.append(style)
    # 调用方会就地改这个列表，每次给一份新的。
    return list(hit[0]), hit[1], hit[2]


def _ruby_char_gaps_uncached(
    line: TimingLine,
    char_widths: list[int],
    rubies: list[RubyAnnotation],
    style: Style,
    intervals: list[tuple[int, int]] | None = None,
) -> tuple[list[int], int, int]:
    """相邻 ruby 避让（N3 无条件规则）及行缘溢出。

    返回 ``(每字符前插入的间隙列表, 行首左溢出, 行末右溢出)``：

    - 相邻两条 ruby 的视觉排布缘间距 < ``RubyInterval`` 时，在当前 ruby 首字符
      **之前**插入差值间隙——等价 N3「从当前正文字符开始整行向右移动」。
      间隙不加宽任何字符框，因此不会反过来加宽前一条 ruby 的标注范围；
    - 溢出 = ruby 排布缘超出正文行盒左/右边界的像素（≥ 0），供行锚定并入
      行盒（N3 ``DrawLineLeft/Right`` 语义）。

    演唱计时用**原始**字宽（间隙只影响几何）。竖排 / RTL 几何互为镜像，
    不做推移。每条 ruby 按其目标角色的注音字体与字号独立测量。
    """
    zero = [0] * len(char_widths)
    if not rubies or not line.chars or style.vertical or style.right_to_left:
        return zero, 0, 0
    if intervals is None:
        intervals = compute_char_intervals(line, char_widths)
    spacing = _letter_spacing(style)
    interval = _ruby_interval_px(style)

    entries: list[tuple[int, int, RubyAnnotation, RubyAnnotation, Style]] = []
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        ruby_style = _ruby_script_stroke_style(
            _ruby_style_for_target_indices(style, line, indices),
            paint_ruby.reading,
        )
        entries.append(
            (
                min(indices),
                max(indices),
                paint_ruby,
                ruby,
                ruby_style,
            )
        )
    if not entries:
        return zero, 0, 0
    entries.sort(key=lambda item: item[0])

    gaps = [0] * len(char_widths)

    def char_span(first: int, last: int) -> tuple[float, float]:
        lefts = _char_left_positions(
            char_widths,
            0,
            False,
            spacing,
            char_gaps=gaps,
            n3_no_backtracking=style.layout_semantics == "n3_1074",
        )
        return float(lefts[first]), float(lefts[last] + char_widths[last])

    prev_right: float | None = None
    min_ruby_left = 0.0
    max_ruby_right = 0.0
    for first, last, paint_ruby, ruby, ruby_style in entries:
        span_left, span_right = char_span(first, min(last, len(char_widths) - 1))
        target_w = max(span_right - span_left, 1.0)
        ruby_metrics = QFontMetrics(
            _build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        )
        ruby_left, ruby_right = _ruby_layout_draw_bounds(
            _ruby_utopia_visual_units(paint_ruby.reading),
            ruby_metrics,
            span_left,
            target_w,
            style=ruby_style,
            base_text=ruby.kanji,
        )
        # DrawWidth already includes N3's primary edge.  Adding another stroke
        # extent here double-counts the outline and over-expands the main text.
        if prev_right is not None and first > 0:
            deficit = (prev_right + interval) - ruby_left
            if deficit > 0:
                push = int(math.ceil(deficit))
                gaps[first] += push
                ruby_left += push
                ruby_right += push
        prev_right = ruby_right
        min_ruby_left = min(min_ruby_left, ruby_left)
        max_ruby_right = max(max_ruby_right, ruby_right)

    if style.layout_semantics == "n3_1074":
        lefts = _char_left_positions(
            char_widths,
            0,
            False,
            spacing,
            char_gaps=gaps,
            n3_no_backtracking=True,
        )
        text_w = float(lefts[-1] + char_widths[-1])
    else:
        text_w = float(
            sum(char_widths) + spacing * max(len(char_widths) - 1, 0) + sum(gaps)
        )
    left_ext = max(0, int(math.ceil(-min_ruby_left)))
    right_ext = max(0, int(math.ceil(max_ruby_right - text_w)))
    return gaps, left_ext, right_ext


def _line_total_width(
    line: TimingLine,
    style: Style,
    rubies: list[RubyAnnotation] | None = None,
) -> int:
    """行盒宽度（给了 rubies 时含 ruby 推移间隙与行缘溢出）。

    与绘制路径同一套测量，供 SmartHorizon 页宽与余白警告使用。

    对齐 N3 ``DrawLineInfo.DrawLineLeft/DrawLineRight``：行盒就是字形几何的左右
    边界，左边不留任何描边余量，右边那一次描边已经含在末字 ``DrawWidth`` 里
    （我们的字步进同样已经含首层描边）。次级描边、发光、阴影允许溢出到左右余白
    之外——N3 就是这么画的，只有余白警告会提示。

    这里以前在 ``legacy`` 语义下额外左右各加 ``ceil((描边 + 次级描边) / 2)``，
    于是行宽比 N3 宽一整个描边宽，落点与 SmartHorizon 的阈值 / slack 判定都跟着
    偏；现在两种语义同口径。
    """
    source_line = line
    line = _line_with_guide_symbol(line)
    if _line_has_role_labels(line):
        role_layout = _build_role_text_layout(line, style, x0=0, baseline_y=0)
        char_widths, _ranges = _role_char_geometry_by_index(line, role_layout)
        text_width = role_layout.total_width
    else:
        font = _build_font(style)
        metrics = QFontMetrics(font)
        latin_font = _build_latin_font(style)
        font_for = _make_font_for(style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        char_widths = [
            (
                _vector_glyph_width(
                    char.vector_glyph,
                    _style_for_role_in_layout(style, char.role_label),
                )
                if char.vector_glyph is not None
                else _char_layout_width(
                    char.text, font, metrics, latin_metrics, font_for, style
                )
            )
            for char in line.chars
        ]
        text_width = _line_text_width(char_widths, style)
    left_ext = right_ext = 0
    gap_total = 0
    if rubies:
        active = _active_rubies_for_line(rubies, source_line)
        if active:
            gaps, ruby_left, ruby_right = _ruby_char_gaps(
                line, char_widths, active, style
            )
            gap_total = sum(gaps)
            left_ext = ruby_left
            right_ext = ruby_right
    return max(
        int(
            round(
                text_width
                + gap_total
                + left_ext
                + right_ext
            )
        ),
        1,
    )


def _page_lines_style_key(style: Style) -> tuple:
    """样式里唯一能移动分页的那几项。

    字号、颜色、描边这些改了也不会换页，所以不进键——否则改个颜色就全线失效。
    逐行布局会派生出一堆 ``replace()`` 出来的 Style 实例，按值取键才能让它们共享
    同一份分页结果。
    """
    return (
        bool(style.dual_line_layout),
        len(style.line_alignments or ()),
        int(style.section_gap_ms),
        tuple(len(layout.line_alignments or ()) for layout in style.layouts),
    )


def _renderable_page_map(
    track: TimingTrack, style: Style
) -> dict[int, tuple[tuple[TimingLine, int], ...]]:
    """``id(行) -> 同页 (行, lane) 元组``，整轨算一次。

    以前每问一行就把全轨重跑一遍 ``assign_lanes``，而排版是逐行问的——一次
    IR 构建能跑上千次，是编辑卡顿里最大的一块。

    只在 :func:`layout_pass` 内缓存：一次排版过程中轨道不会被改，用 ``id(track)``
    做键就够，且是 O(1)。按内容算签名反而更慢——签名本身是 O(行数)，乘上每行十几
    次调用比重算还贵（实测 400 行从 5.2s 涨到 10.1s）。
    """
    cache = getattr(_LAYOUT_PASS, "page_maps", None)
    cache_key = (id(track), _page_lines_style_key(style)) if cache is not None else None
    if cache is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    render_lines = [item for item in track.lines if not item.is_blank and item.chars]
    lanes, page_starts, page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    page_map: dict[int, tuple[tuple[TimingLine, int], ...]] = {}
    for index, item in enumerate(render_lines):
        page_start = page_starts[index]
        page_end = min(page_start + page_rows[index], len(render_lines))
        page_map[id(item)] = tuple(
            (render_lines[i], lanes[i]) for i in range(page_start, page_end)
        )
    if cache is not None:
        # track 一并存住：键里有 id()，对象被回收后地址会被复用。
        cache[cache_key] = page_map
        _LAYOUT_PASS.tracks.append(track)
    return page_map


def _renderable_page_lines(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
) -> list[tuple[TimingLine, int]] | None:
    """N3「页」的近似：页划分与 timeline 的 lane 分配一致（页首行布局定行数）。

    返回同页 ``(行, lane)`` 列表（含自身）；行不在 track 中时返回 ``None``。
    """
    page = _renderable_page_map(track, style).get(id(line))
    return None if page is None else list(page)


def _smart_horizontal_dx(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool,
    page: list[tuple[TimingLine, int]] | None = None,
) -> int:
    """SmartHorizon 二次水平修正（逆向 N3 ``SetOneLineX``）。

    仅作用于 ``asymmetric`` 多行布局：``center_position`` 逐行判断短行是否
    从画面中心附近开始/结束；``equal_margins`` 以页为单位，页内同时存在
    Left 与 Right 行且有空隙时，把空隙对半分给 Left/Right 行。Center 行
    （含被居中覆盖的行）不修正。

    ``page`` 是调用方已经定好的页成员（``_layout_page_lines``），省一趟页定位。
    """
    mode = style.smart_horizontal
    if mode == "none" or style.vertical or center_override:
        return 0
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return 0
    if page is None:
        page = _renderable_page_lines(track, line, style)
    page_rows = len(page) if page else None
    own_align = _lane_alignment(style, lane, page_rows)
    if own_align == "center":
        return 0
    margin = max(style.horizontal_margin_px, 0)
    font = _n3_smart_font_size(line, style)
    base_x = _resolve_line_x(
        img_w, total_w, style, lane, center_override=False, page_line_count=page_rows
    )
    if page is not None and len(page) <= 1:
        # 单行页：SmartHorizon != None 时整行居中。
        return (img_w - total_w) // 2 - base_x

    if mode == "center_position":
        threshold = img_w // 2 + font // 2 - total_w
        if threshold <= margin:
            return 0
        if own_align == "right":
            return (img_w // 2 - font // 2) - base_x
        return threshold - base_x

    # equal_margins：按页内 Left / Center / Right 各自最大宽度计算空隙。
    if page is None:
        return 0
    if page:
        page_head, _page_head_lane = page[0]
        font = _n3_smart_font_size(
            page_head, _style_for_line(style, page_head)
        )
    max_widths = {"left": 0, "center": 0, "right": 0}
    for page_line, page_lane in page:
        page_style = _style_for_line(style, page_line)
        if _line_center_override(track, page_line, page_style):
            align = "center"
        else:
            align = _lane_alignment(page_style, page_lane, page_rows)
        width = (
            total_w
            if page_line is line
            else _line_total_width(page_line, page_style, track.rubies)
        )
        max_widths[align] = max(max_widths[align], width)
    if max_widths["left"] == 0 or max_widths["right"] == 0:
        return 0
    slack = (
        img_w
        - margin * 2
        - max_widths["left"]
        - max_widths["center"]
        - max_widths["right"]
        + font
    )
    if slack <= 0:
        return 0
    return -(slack // 2) if own_align == "right" else slack // 2


def _n3_smart_font_size(line: TimingLine, style: Style) -> int:
    """Font-size term used by N3's SmartHorizon formulas.

    N3 reads the Japanese font slot belonging to the first rendered character,
    even when that character is assigned a non-default color/font scheme.
    N3 has only one layout semantics, so this holds for ``legacy`` styles too.
    """
    render_line = _line_with_guide_symbol(line)
    if not render_line.chars:
        return max(int(style.font_size_px), 1)
    first_style = _style_for_role_in_layout(
        style, render_line.chars[0].role_label
    )
    return max(int(first_style.font_size_px), 1)


def _resolve_line_x_smart(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
) -> int:
    page = _layout_page_lines(track, line, style)
    x = _resolve_line_x(
        img_w,
        total_w,
        style,
        lane,
        center_override=center_override,
        page_line_count=len(page) if page else None,
    )
    return x + _smart_horizontal_dx(
        img_w,
        total_w,
        track,
        line,
        style,
        lane,
        center_override=center_override,
        page=page,
    )


def apply_layout_to_page(
    track: TimingTrack,
    style: Style,
    track_line_index: int,
    layout_index: int,
) -> list[int]:
    """把 ``layout_index`` 应用到指定行所在页的所有行（N3 页级联动）。

    ``track_line_index`` 是 ``track.lines`` 索引；返回被改写的 ``track.lines``
    索引列表（空 = 该行不可渲染或未变化）。页按应用前的布局分配计算。
    """
    if track.page_plan is not None:
        resolved = resolve_page_plan(track, style)
        resolved_line = resolved.line_for_track_index(track_line_index)
        if resolved_line is None:
            return []
        page = resolved.pages[resolved_line.global_page_index]
        layout_id = layout_id_for_index(style, int(layout_index))
        if layout_capacity(style, layout_id) < page.line_count:
            return []
        changed = set_pages_layout(
            track, style, [resolved_line.global_page_index], layout_id
        )
        return list(page.track_line_indices) if changed else []

    render_positions = [
        i for i, line in enumerate(track.lines) if not line.is_blank and line.chars
    ]
    render_lines = [track.lines[i] for i in render_positions]
    try:
        render_index = render_positions.index(track_line_index)
    except ValueError:
        return []
    _lanes, page_starts, page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    page_start = page_starts[render_index]
    page_end = min(page_start + page_rows[render_index], len(render_lines))
    affected: list[int] = []
    for i in range(page_start, page_end):
        line = render_lines[i]
        if line.layout_index != layout_index:
            line.layout_index = layout_index
            affected.append(render_positions[i])
    return affected


def assign_layout_to_all(
    track: TimingTrack, layout_index: int, style: Optional[Style] = None
) -> bool:
    """所有可渲染行统一应用同一布局（N3 UnificationLayoutSelector）。"""
    if track.page_plan is not None:
        if style is None:
            return False
        resolved = resolve_page_plan(track, style)
        layout_id = layout_id_for_index(style, int(layout_index))
        if any(
            layout_capacity(style, layout_id) < page.line_count
            for page in resolved.pages
        ):
            return False
        return set_pages_layout(
            track,
            style,
            [page.global_page_index for page in resolved.pages],
            layout_id,
        )
    changed = False
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        if line.layout_index != layout_index:
            line.layout_index = layout_index
            changed = True
    return changed


def auto_assign_layouts_by_page(track: TimingTrack, style: Style) -> bool:
    """按页内行数自动选布局（N3 ``LinesLayoutSelector``）。

    每页取实际歌词行数 N，
    在「默认布局 + 额外布局」中找第一个行数 == N 的；找不到按 N-1, N-2…
    递减再找；仍找不到用默认布局。返回是否有行被改写。
    """
    if track.page_plan is not None:
        return use_default_layouts(track, style)

    row_counts = [max(len(style.line_alignments), 1)] + [
        max(len(layout.line_alignments), 1) for layout in style.layouts
    ]
    pick_cache: dict[int, int] = {}

    def pick(page_lines: int) -> int:
        if page_lines in pick_cache:
            return pick_cache[page_lines]
        choice = 0
        for want in range(page_lines, 0, -1):
            found = next(
                (i for i, count in enumerate(row_counts) if count == want), None
            )
            if found is not None:
                choice = found
                break
        pick_cache[page_lines] = choice
        return choice

    render_lines = [line for line in track.lines if not line.is_blank and line.chars]
    if not render_lines:
        return False
    _lanes, page_starts, _page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    pages: dict[int, list[TimingLine]] = {}
    for line, page_start in zip(render_lines, page_starts):
        pages.setdefault(page_start, []).append(line)

    changed = False
    for page in pages.values():
        index = pick(len(page))
        for line in page:
            if line.layout_index != index:
                line.layout_index = index
                changed = True
    return changed


@dataclass(frozen=True)
class LayoutMarginWarning:
    """一条歌词行的左右余白检查结果（对齐 N3 预览警告语义）。"""

    line_index: int
    """``track.lines`` 中的索引。"""
    text: str
    level: str
    """``"overflow"`` = 字幕溢出画面（N3 Warning）；``"margin"`` = 左右余白无法确保
    （N3 Information）。"""
    left: int
    right: int


def check_layout_margins(
    track: TimingTrack,
    style: Style,
    img_w: int,
) -> list[LayoutMarginWarning]:
    """检查每行主文字的左右边界，返回溢出/侵入余白的行。

    只做主文字外框（N3 ``DrawLineLeft/Right`` 口径，不含描边溢出）的静态估算，
    不含 ruby 左右溢出与指示灯加宽；足以提示用户调小字号或余白。竖排模式左右
    边界语义不同，不检查。
    """
    if style.vertical or not track.lines:
        return []
    if style.dual_line_layout:
        display_lines = _display_lines_for_style(
            track,
            style,
            logical_w=img_w,
        )
    else:
        display_lines = [
            DisplayLine(line=line, lane=0, display_start_ms=0, display_end_ms=0)
            for line in track.lines
            if not line.is_blank and line.chars
        ]
    line_indices = {id(line): index for index, line in enumerate(track.lines)}
    warnings: list[LayoutMarginWarning] = []
    for display_line in display_lines:
        line = display_line.line
        if not line.chars:
            continue
        line_style = _style_for_line(style, line)
        margin_left = max(line_style.horizontal_margin_px, 0)
        margin_right = max(line_style.horizontal_margin_px, 0)
        total_w = _line_total_width(line, line_style, track.rubies)
        lane = display_line.lane if line_style.dual_line_layout else None
        x0 = _resolve_line_x_smart(
            img_w,
            total_w,
            track,
            line,
            line_style,
            lane,
            center_override=_line_center_override(track, line, line_style),
        )
        left = x0
        right = x0 + total_w
        if left < 0 or right > img_w:
            level = "overflow"
        elif left < margin_left or right > img_w - margin_right:
            level = "margin"
        else:
            continue
        warnings.append(
            LayoutMarginWarning(
                line_index=line_indices.get(id(line), -1),
                text="".join(ch.text for ch in line_visible_chars(line)),
                level=level,
                left=left,
                right=right,
            )
        )
    return warnings


def _line_start_ms(line: TimingLine) -> int:
    return timing_line_start_ms(line)


def _line_end_ms(line: TimingLine) -> int:
    if line.end_ms is not None:
        return line.end_ms
    return line.chars[-1].start_ms + 1000 if line.chars else 0


def _layout_style_for_line(style: Style, line: TimingLine) -> Style:
    """把行引用的布局定义套到 style 上（0 = 默认布局，原样返回）。"""
    index = int(getattr(line, "layout_index", 0) or 0)
    if index <= 0 or index > len(style.layouts):
        return style
    layout = style.layouts[index - 1]
    changes = {
        name: value
        for name in LYRICS_LAYOUT_FIELDS
        if (value := getattr(layout, name)) is not None
    }
    return replace(style, **changes)


def _row_count_resolver(style: Style):
    """timeline ``row_count_of`` 回调：按行布局返回该行所在页的行数。"""
    if not style.layouts:
        return None  # 没有额外布局 → 全部页用全局行数，走快路径
    return lambda line: _lane_count(_layout_style_for_line(style, line))


def _bottom_align_resolver(style: Style):
    if style.vertical:
        return None
    return lambda line: _layout_style_for_line(style, line).line_y_position == "bottom"


def _vertical_position_resolver(style: Style):
    if style.vertical:
        return None
    return lambda line: _layout_style_for_line(style, line).line_y_position


def _style_for_line(style: Style, line: TimingLine) -> Style:
    """行的有效样式（布局引用 + 歌手覆盖 + 逐行动画）。

    ``dataclasses.replace`` 要把 ``Style`` 的两百多个字段整份重建一遍，而这个函数
    每行会被问上好几次、每次至少两趟 replace。结果只取决于下面这几项，同一条轨道
    里绝大多数行都落在同一个键上，所以在排版区间内缓存住。
    """
    cache = getattr(_LAYOUT_PASS, "line_styles", None)
    if cache is None:
        return _style_for_line_uncached(style, line)
    override = line.animation_override
    cache_key = (
        id(style),
        int(getattr(line, "layout_index", 0) or 0),
        line.singer_id,
        None
        if override is None
        else (
            override.entry_anim,
            int(override.entry_duration_ms),
            override.exit_anim,
            int(override.exit_duration_ms),
        ),
    )
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    resolved = _style_for_line_uncached(style, line)
    cache[cache_key] = resolved
    # style 存住：键里有 id()，对象被回收后地址会被复用。
    _LAYOUT_PASS.styles.append(style)
    return resolved


def _style_for_line_uncached(style: Style, line: TimingLine) -> Style:
    layout_style = _layout_style_for_line(style, line)
    if line.singer_id is not None:
        scheme = style.singer_style_overrides.get(line.singer_id)
        if scheme is not None:
            changes = _style_scheme_changes(scheme)
            if changes:
                style = replace(style, **changes)
    style = replace(
        style,
        **{name: getattr(layout_style, name) for name in LYRICS_LAYOUT_FIELDS},
    )
    return style_with_line_animation(style, line)


def _auto_entry_reserve_ms(style: Style, line: TimingLine) -> int:
    """Return the automatic pre-wipe reserve for this line's entry animation.

    A user-configured animation shorter than the automatic 250 ms floor is
    already an explicit choice, so the resolver preserves that shorter value
    instead of lengthening it.
    """

    line_style = _style_for_line(style, line)
    duration = max(int(line_style.entry_lead_ms), 0)
    if line_style.entry_anim == "none" or duration <= 0:
        return 0
    return min(duration, MIN_AUTO_ENTRY_ANIMATION_MS)


def _auto_entry_reserve_resolver(style: Style):
    return lambda line: _auto_entry_reserve_ms(style, line)


def _entry_animation_ms(style: Style, line: TimingLine) -> int:
    line_style = _style_for_line(style, line)
    if line_style.entry_anim == "none":
        return 0
    return max(int(line_style.entry_lead_ms), 0)


def _exit_animation_ms(style: Style, line: TimingLine) -> int:
    line_style = _style_for_line(style, line)
    if line_style.exit_anim == "none":
        return 0
    return max(int(line_style.exit_fade_ms), 0)


def _entry_animation_resolver(style: Style):
    return lambda line: _entry_animation_ms(style, line)


def _exit_animation_resolver(style: Style):
    return lambda line: _exit_animation_ms(style, line)


def _page_collision_layout_key(
    page_style: Style,
    *,
    line_count: int,
    vertical: bool,
) -> tuple:
    """Return the rendered page-layout identity used by animation collisions."""

    return (
        bool(vertical),
        max(int(line_count), 0),
        tuple(
            (name, _value_signature(getattr(page_style, name)))
            for name in LYRICS_LAYOUT_FIELDS
        ),
    )


def _display_line_static_collision_window(
    display_line: DisplayLine,
    style: Style,
) -> tuple[int, int]:
    """Return the non-animation interval used for page placement collisions."""

    line_style = _style_for_line_display_window(
        style,
        display_line.line,
        display_line.display_start_ms,
        display_line.display_end_ms,
    )
    start = int(display_line.display_start_ms)
    end = int(display_line.display_end_ms)
    if line_style.entry_anim != "none":
        start += max(int(line_style.entry_lead_ms), 0)
    if line_style.exit_anim != "none":
        end -= max(int(line_style.exit_fade_ms), 0)
    return start, max(start, end)


def _display_line_collision_time_window(
    display_line: DisplayLine,
    style: Style,
    *,
    time_window: str,
) -> tuple[int, int]:
    if time_window == "display":
        start = int(display_line.display_start_ms)
        end = int(display_line.display_end_ms)
        return start, max(start, end)
    if time_window != "stable":
        raise ValueError(f"Unsupported collision time window: {time_window}")
    return _display_line_static_collision_window(display_line, style)


def _style_for_line_display_window(
    style: Style,
    line: TimingLine,
    display_start_ms: int | None,
    display_end_ms: int | None,
) -> Style:
    """Clamp rendered animation durations to the resolved outside-wipe margins.

    Automatic show-time compression may consume configured entry/exit animation
    time.  Unless the configured animation or display override is already
    shorter, the entry window retains the 250 ms automatic minimum; exit may
    collapse to zero.  Manual display overrides are already reflected in the
    resolved window and therefore remain authoritative.
    """

    line_style = _style_for_line(style, line)
    start = (
        _line_start_ms(line)
        if display_start_ms is None
        else int(display_start_ms)
    )
    end = _line_end_ms(line) if display_end_ms is None else int(display_end_ms)
    entry_available = max(_line_start_ms(line) - start, 0)
    exit_available = max(end - _line_end_ms(line), 0)
    return replace(
        line_style,
        entry_lead_ms=min(max(int(line_style.entry_lead_ms), 0), entry_available),
        exit_fade_ms=min(max(int(line_style.exit_fade_ms), 0), exit_available),
    )


def resolved_char_intervals_for_line(
    line: TimingLine, style: Style
) -> list[tuple[int, int]]:
    """Resolve Painter's final per-character timing intervals for Render IR.

    Shared source spans are weighted by the same effective glyph advances used
    by horizontal layout.  Vertical Painter intentionally keeps the parser's
    existing intervals because its fixed cells do not run the width-weighted
    shared-span path.
    """
    line_style = _style_for_line(style, line)
    line = _line_with_guide_symbol(line)
    if line_style.vertical:
        return compute_char_intervals(line)
    if _line_has_role_labels(line):
        measure_layout = _build_role_text_layout(
            line, line_style, x0=0, baseline_y=0
        )
        char_widths, _ranges = _role_char_geometry_by_index(line, measure_layout)
    else:
        font = _build_font(line_style)
        metrics = QFontMetrics(font)
        latin_font = _build_latin_font(line_style)
        font_for = _make_font_for(line_style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        char_widths = [
            (
                _vector_glyph_width(ch.vector_glyph, line_style)
                if ch.vector_glyph is not None
                else _char_layout_width(
                    ch.text,
                    font,
                    metrics,
                    latin_metrics,
                    font_for,
                    line_style,
                )
            )
            for ch in line.chars
        ]
    return compute_char_intervals(line, char_widths)


def resolved_guide_anchor_bounds_for_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
) -> tuple[float, float] | None:
    """Return Painter's source-line horizontal anchor box for guide glyphs.

    Sayatoo row layout measures the unmodified source line before Painter
    inserts/replaces guide glyphs.  The rendered vector glyphs therefore grow
    from that source text origin.  Role-run and vertical lines take their own
    rendered geometry path and do not use this pre-resolved anchor.
    """
    line_style = _style_for_line(style, line)
    guide_symbols = [line.guide_symbol, *line.inline_guide_symbols.values()]
    if (
        line_style.vertical
        or _line_has_role_labels(line)
        or (line.guide_symbol is None and not line.inline_guide_symbols)
        or any(_guide_symbol_is_bitmap(symbol) for symbol in guide_symbols)
    ):
        return None
    font = _build_font(line_style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(line_style)
    font_for = _make_font_for(line_style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    char_widths = [
        _char_layout_width(
            ch.text, font, metrics, latin_metrics, font_for, line_style
        )
        for ch in line.chars
    ]
    active_rubies = _active_rubies_for_line(track.rubies, line)
    char_gaps, ruby_left, ruby_right = _ruby_char_gaps(
        line, char_widths, active_rubies, line_style
    )
    # 行盒左右不给描边留位（见 _line_total_width），只让 ruby 溢出撑开。
    left_ext = ruby_left
    right_ext = ruby_right
    text_width = _line_text_width(char_widths, line_style) + sum(char_gaps)
    return -left_ext, int(round(text_width)) + right_ext


def _active_rubies_for_line(
    rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[RubyAnnotation]:
    """这一行生效的注音。排版过程中每行会被问十来次，缓存到区间内。"""
    cache = getattr(_LAYOUT_PASS, "active_rubies", None)
    if cache is None:
        return _active_rubies_for_line_uncached(rubies, line)
    cache_key = (id(rubies), id(line))
    hit = cache.get(cache_key)
    if hit is None:
        hit = tuple(_active_rubies_for_line_uncached(rubies, line))
        cache[cache_key] = hit
        # 键里有 id()：存住这两个对象，避免回收后地址被复用。
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.ruby_lists.append(rubies)
    # 调用方会就地改返回的列表，每次给一份新的。
    return list(hit)


def _active_rubies_for_line_uncached(
    rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[RubyAnnotation]:
    if not rubies or not line.chars:
        return []
    line_start = line.chars[0].start_ms
    line_end = line.end_ms if line.end_ms is not None else line.chars[-1].start_ms
    intervals = compute_char_intervals(line)
    return [
        ruby
        for ruby in rubies
        if ruby.reading
        and (
            _ruby_has_global_position(ruby)
            or (
                ruby.pos_end_ms > line_start
                and ruby.pos_start_ms < line_end
                and _timed_ruby_matches_line(ruby, line, intervals)
            )
        )
    ]


def _ruby_has_global_position(ruby: RubyAnnotation) -> bool:
    return ruby.pos_start_ms == 0 and ruby.pos_end_ms == 0


def _timed_ruby_matches_line(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> bool:
    time_indices = _ruby_time_indices(ruby, intervals)
    if not time_indices:
        return False
    if not ruby.kanji:
        return True
    preferred = set(time_indices)
    text = "".join(ch.text for ch in line.chars)
    pos = text.find(ruby.kanji)
    while pos >= 0:
        indices = _text_span_indices((pos, pos + len(ruby.kanji)), line)
        if preferred.intersection(indices):
            return True
        pos = text.find(ruby.kanji, pos + 1)
    return False


def _paint_rubies(
    painter: QPainter,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    t_ms: int,
    rubies: list[RubyAnnotation],
    style: Style,
    transition: _LineCharTransition | None = None,
    main_ascent_px: int | None = None,
    text_layout: _TextLayout | None = None,
    draw_glow: bool = True,
    precomputed_layouts: tuple[_RubyLayout, ...] | None = None,
) -> None:
    rtl = style.right_to_left
    painter.save()
    try:
        painter.setFont(ruby_font)
        layouts = list(precomputed_layouts) if precomputed_layouts is not None else _layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            main_baseline_y,
            rubies,
            style,
            main_ascent_px=main_ascent_px,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if transition is None:
            _paint_ruby_layers(
                painter,
                layouts,
                ruby_font,
                ruby_metrics,
                t_ms,
                style,
                rtl,
                draw_glow=draw_glow,
            )
            return
        for layout in layouts:
            ruby_style = layout.style
            target_ruby_font = layout.font or ruby_font
            target_ruby_metrics = layout.metrics or ruby_metrics
            indices = layout.indices
            paint_ruby = layout.ruby
            x = layout.x
            ruby_baseline_y = layout.baseline_y
            target_width = layout.target_width
            reading_w = layout.reading_width
            opacity, dx, dy, rotation, scale_x, scale_y, skew_y = 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
            if transition is not None:
                first_index = min(indices)
                last_index = max(indices)
                following_done_ms = (
                    _utopia_following_done_time(line, intervals, last_index, style)
                    if transition.effect == "utopia"
                    else None
                )
                opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
                    style,
                    transition,
                    first_index,
                    max(len(line.chars), 1),
                    char_start_ms=intervals[first_index][0],
                    char_end_ms=intervals[last_index][1],
                    t_ms=t_ms,
                    frame_height=painter.device().height(),
                    following_done_ms=following_done_ms,
                )
            if opacity <= 0.0:
                continue
            painter.save()
            try:
                use_utopia_origin = transition is not None and transition.effect == "utopia"
                if use_utopia_origin:
                    _paint_ruby_text_units_with_transition(
                        painter,
                        paint_ruby,
                        target_ruby_font,
                        target_ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        ruby_style,
                        transition,
                        first_index,
                        max(len(line.chars), 1),
                        following_done_ms,
                        rtl,
                        target_width=target_width,
                        gradient_rect=layout.gradient_rect,
                        horizontal_gradient_rect=layout.horizontal_gradient_rect,
                    )
                else:
                    painter.setOpacity(painter.opacity() * opacity)
                    is_char_drip = transition.effect == "char_drip"
                    if is_char_drip:
                        center_x = x + reading_w
                        center_y = (
                            ruby_baseline_y
                            if transition.phase == "entry"
                            else ruby_baseline_y - target_ruby_metrics.height()
                        )
                    else:
                        center_x = x + reading_w / 2
                        center_y = (
                            ruby_baseline_y
                            - target_ruby_metrics.ascent()
                            + target_ruby_metrics.height() / 2
                        )
                    _apply_character_transform(
                        painter,
                        center_x=center_x,
                        center_y=center_y,
                        dx=dx,
                        dy=dy,
                        rotation=rotation,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        skew_y=skew_y,
                    )
                    _paint_ruby_text(
                        painter,
                        paint_ruby,
                        target_ruby_font,
                        target_ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        ruby_style,
                        rtl,
                        target_width=target_width,
                        gradient_rect=layout.gradient_rect,
                        horizontal_gradient_rect=layout.horizontal_gradient_rect,
                        wipe_layout=layout,
                    )
            finally:
                painter.restore()
    finally:
        painter.restore()


def _layout_rubies(
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    rubies: list[RubyAnnotation],
    style: Style,
    *,
    main_ascent_px: int | None = None,
    text_layout: _TextLayout | None = None,
    ruby_font: QFont | None = None,
) -> list[_RubyLayout]:
    """layout 段：算横排 ruby 的目标字符范围、基线与排布宽度。"""
    if not rubies:
        return []
    main_box_ascent: Optional[float] = None
    if main_ascent_px is not None and text_layout is not None and text_layout.glyphs:
        # N3 行盒顶 = 参与注音高度计算的字符盒顶最高者；空白无墨水不算。
        height_glyphs = [
            glyph
            for glyph in text_layout.glyphs
            if glyph.text.strip() and glyph.style.affects_ruby_anchor
        ]
        if not height_glyphs:
            # If every visible glyph opted out, keep ruby attached to its own
            # base characters instead of falling back to an unrelated global
            # font metric.  This also gives an all-decoration line a safe floor.
            ruby_target_indices = {
                index
                for ruby in rubies
                for index in _ruby_target_indices(ruby, line, intervals)
            }
            height_glyphs = [
                glyph
                for glyph in text_layout.glyphs
                if glyph.text.strip() and glyph.index in ruby_target_indices
            ]
        candidates = [
            _n3_char_box_ascent(
                glyph.metrics, glyph.style.font_size_px, glyph.style.stroke_width_px
            )
            for glyph in height_glyphs
        ]
        if candidates:
            main_box_ascent = max(candidates)
    if main_box_ascent is None:
        main_box_ascent = _n3_char_box_ascent(
            QFontMetrics(_build_font(style)), style.font_size_px, style.stroke_width_px
        )
    layouts: list[_RubyLayout] = []
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        target_range = _ruby_target_x_range(ruby, line, intervals, char_x_ranges)
        if target_range is None:
            continue
        ruby_brush_style = _ruby_style_for_target_indices(style, line, indices)
        ruby_style = _ruby_script_stroke_style(
            ruby_brush_style, paint_ruby.reading
        )
        target_ruby_font = _build_ruby_font_for_text(
            ruby_style, paint_ruby.reading
        )
        target_ruby_metrics = QFontMetrics(target_ruby_font)
        target_ruby_size = max(target_ruby_font.pixelSize(), 1)
        ruby_baseline_y = _ruby_baseline_y(
            main_baseline_y,
            main_box_ascent,
            target_ruby_metrics,
            ruby_style,
            font_size_px=target_ruby_size,
        )
        left, right = target_range
        target_width = max(right - left, 1)
        gradient_rect = _n3_ruby_fill_rect(
            left,
            target_width,
            ruby_baseline_y,
            target_ruby_metrics,
            ruby_style,
            brush_style=ruby_brush_style,
            font_size_px=target_ruby_size,
        )
        reading_width = _ruby_layout_width(
            paint_ruby.reading,
            target_ruby_metrics,
            target_width,
            style=ruby_style,
            base_text=paint_ruby.kanji,
        )
        wipe_segments, wipe_left, wipe_right, geometry_signature = _ruby_wipe_geometry(
            paint_ruby,
            target_ruby_font,
            target_ruby_metrics,
            left,
            ruby_baseline_y,
            target_width,
            ruby_style,
            rtl=style.right_to_left,
        )
        layouts.append(
            _RubyLayout(
                ruby=paint_ruby,
                indices=indices,
                style=ruby_style,
                x=left,
                baseline_y=ruby_baseline_y,
                target_width=target_width,
                reading_width=reading_width,
                gradient_rect=gradient_rect,
                wipe_segments=wipe_segments,
                wipe_left=wipe_left,
                wipe_right=wipe_right,
                geometry_signature=geometry_signature,
                font=target_ruby_font,
                metrics=target_ruby_metrics,
            )
        )
    if text_layout is not None and layouts:
        main_rect = _n3_main_fill_rect(text_layout, main_baseline_y)
        top = min(
            float(main_rect.top()),
            *(float(layout.gradient_rect.top()) for layout in layouts),
        )
        bottom = max(
            float(main_rect.bottom()),
            *(float(layout.gradient_rect.bottom()) for layout in layouts),
        )
        shared_rect = QRectF(
            float(main_rect.left()),
            top,
            float(max(main_rect.width(), 1.0)),
            float(max(bottom - top, 1.0)),
        )
        layouts = [
            replace(
                layout,
                horizontal_gradient_rect=(
                    shared_rect
                    if layout.style.ruby_horizontal_gradient_with_main
                    else None
                ),
            )
            for layout in layouts
        ]
    return layouts


def _ruby_wipe_geometry(
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    target_width: int,
    style: Style,
    *,
    rtl: bool,
) -> tuple[tuple[_RubyWipeSegment, ...], float, float, tuple]:
    """Build N3-style timed glyph geometry independently from the layout box.

    The target-width box is still used for Center/EqualSpace placement.  Wipe
    fronts follow each visible glyph's primary-stroke drawing bounds, matching
    N3 ``WipeLeft`` (geometry bounds expanded by half ``EdgeSize``).  Using the
    fill-only ink bounds makes the next ruby glyph's left half-stroke visible at
    ratio zero and leaves the previous half-stroke unfinished until the exact
    boundary frame, which reads as a jump.  The layout box's centered leading /
    trailing blank area still does not consume singing time.
    """
    logical_units = _ruby_visual_units_and_intervals(ruby)
    if not logical_units:
        return (), float(x), float(x), ()
    visual_units = list(reversed(logical_units)) if rtl else logical_units
    unit_layouts = _ruby_layout_units(
        [unit for unit, _interval in visual_units],
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=ruby.kanji,
    )
    segments: list[_RubyWipeSegment] = []
    signature: list[tuple] = []
    bounds: list[tuple[float, float]] = []
    # N3 performs integer ``EdgeSize / 2`` before converting to float.
    edge_half = float(max(int(_ruby_stroke_width(style)), 0) // 2)
    for (unit, interval), (_draw_unit, unit_x, unit_width) in zip(
        visual_units, unit_layouts
    ):
        path = QPainterPath()
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
        ink = path.boundingRect()
        if ink.isEmpty():
            ink_left = float(unit_x)
            ink_right = float(unit_x + max(unit_width, 0.0))
        else:
            ink_left = float(ink.left())
            ink_right = float(ink.right())
        if ink_right < ink_left:
            ink_left, ink_right = ink_right, ink_left
        draw_left = ink_left - edge_half
        draw_right = ink_right + edge_half
        start_ms, end_ms = interval
        segments.append(
            _RubyWipeSegment(
                int(start_ms),
                max(int(start_ms), int(end_ms)),
                draw_right if rtl else draw_left,
                draw_left if rtl else draw_right,
            )
        )
        bounds.append((draw_left, draw_right))
        signature.append(
            (
                unit,
                round(float(unit_x) - float(x), 3),
                round(float(unit_width), 3),
                round(ink_left - float(x), 3),
                round(ink_right - float(x), 3),
            )
        )
    if not bounds:
        return (), float(x), float(x), tuple(signature)
    segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        overlaps = (
            current.axis_end <= following.axis_start
            if rtl
            else current.axis_end >= following.axis_start
        )
        if overlaps:
            adjusted[index] = replace(current, axis_end=following.axis_start)
    return (
        tuple(adjusted),
        min(left for left, _right in bounds),
        max(right for _left, right in bounds),
        tuple(signature),
    )


def _ruby_style_for_target_indices(
    style: Style,
    line: TimingLine,
    indices: list[int],
) -> Style:
    for index in indices:
        if 0 <= index < len(line.chars):
            role_label = line.chars[index].role_label
            if role_label:
                return _style_for_role_in_layout(style, role_label)
    return style


def _role_ruby_vertical_extra(
    line: TimingLine,
    rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]],
    style: Style,
) -> int:
    """Reserve enough vertical space for the largest role-specific ruby."""
    extra = 0
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        ruby_style = _ruby_script_stroke_style(
            _ruby_style_for_target_indices(style, line, indices),
            paint_ruby.reading,
        )
        font = _build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        metrics = QFontMetrics(font)
        extra = max(
            extra,
            _ruby_vertical_extra(
                ruby_style,
                metrics,
                font_size_px=max(font.pixelSize(), 1),
            ),
        )
    return extra


def _n3_ruby_fill_rect(
    left: int,
    width: int,
    baseline_y: int,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    brush_style: Style | None = None,
    font_size_px: int | None = None,
) -> QRectF:
    """Return the ruby ``DrawLineInfo`` gradient area used by N3."""
    font_size = (
        _ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    metric_total = max(ruby_metrics.ascent() + ruby_metrics.descent(), 1)
    descent = font_size * max(ruby_metrics.descent(), 0) // metric_total
    draw_edge = _ruby_stroke_width(style)
    anchor_style = brush_style or style
    anchor_edge = _ruby_stroke_width(anchor_style)
    anchor_edge2 = _ruby_stroke2_width(anchor_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_top = draw_bottom - float(font_size + draw_edge)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(left),
        top,
        float(max(width, 1)),
        float(max(bottom - top, 1.0)),
    )


def _paint_ruby_layers(
    painter: QPainter,
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    *,
    draw_glow: bool = True,
) -> None:
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _ruby_text_layers(
            layouts, ruby_font, ruby_metrics, t_ms, style, rtl, draw_glow=draw_glow
        ),
    )


def _paint_ruby_glow_layers(
    painter: QPainter,
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
) -> None:
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _ruby_glow_layers(layouts, ruby_font, ruby_metrics, t_ms, style, rtl),
    )


def _ruby_text_layers(
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    *,
    draw_glow: bool = True,
) -> list:
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        layers.append(
            _RubyTextLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
                draw_glow=draw_glow,
            )
        )
        layers.append(
            _RubyTextLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
                draw_glow=draw_glow,
            )
        )
    return layers


def _ruby_glow_layers(
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
) -> list:
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        if _ruby_glow_can_combine_split(layout.style):
            layers.append(
                _RubySplitGlowLayer(
                    layout,
                    target_ruby_font,
                    target_ruby_metrics,
                    t_ms,
                    layout.style,
                    rtl,
                    z_index=index,
                )
            )
            continue
        layers.append(
            _RubyGlowLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
            )
        )
        layers.append(
            _RubyGlowLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
            )
        )
    return layers


def _ruby_layer_stack(
    layout: _LineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> list:
    if layout.ruby_metrics is None:
        return []
    return _ruby_text_layers(
        list(layout.ruby_layouts),
        layout.ruby_font,
        layout.ruby_metrics,
        t_ms,
        style,
        layout.rtl,
    )


@dataclass(frozen=True)
class _RubySplitGlowLayer:
    """Combined before/after ruby glow with a cached moving-front strip."""

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubySplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        # The layer moves every frame, but its two full halos are cached below.
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("combined ruby split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        visible, complete, front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if not visible:
            _blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=False,
            )
            return
        if complete:
            _blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=True,
            )
            return

        reading = (
            "".join(
                reversed(_ruby_utopia_visual_units(self.ruby_layout.ruby.reading))
            )
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = _ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = _ruby_glow_radius(self.style, after=False)
        stroke_width = _ruby_stroke_width(self.style)
        stroke2_width = _ruby_stroke2_width(self.style)
        pad = _glow_extent(stroke_width, stroke2_width, radius)
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if self.rtl:
            before_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
            before_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            before_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )

        strip_clip = QRectF(
            front - pad,
            -1_000_000.0,
            float(pad * 2),
            2_000_000.0,
        )
        colors = _effective_ruby_karaoke_colors(self.style)
        _paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            self.ruby_layout.gradient_rect,
            radius,
            stroke_width,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=_ruby_glow_concentration_level(self.style),
            target_clip=strip_clip,
            horizontal_fill_rect=self.ruby_layout.horizontal_gradient_rect,
        )
        for after, clip in (
            (False, before_baked_clip),
            (True, after_baked_clip),
        ):
            painter.save()
            try:
                painter.setClipRect(clip)
                _blit_cached_ruby_glow(
                    painter,
                    self.ruby_layout,
                    self.ruby_font,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    after=after,
                )
            finally:
                painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = max(
            _ruby_visual_padding(self.style, after=after)
            for after in (False, True)
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _RubyGlowLayer:
    """Glow-only layer for one horizontal ruby reading.

    N3 paints ruby/main decorations first, then paints the solid strokes/bodies.
    Splitting ruby glow from ruby body prevents the ruby halo from covering the
    main glyph body while still keeping the ruby stroke/fill above the main glow.
    """

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubyGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        if _ruby_decoration_kind(self.style) != "glow":
            return None
        if _ruby_glow_radius(self.style, after=self.after) == 0:
            return None
        visible, complete, _front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if self.after:
            if not visible:
                return None
            if not _ruby_glow_states_differ(self.style):
                return None
            if not complete:
                return None
        elif _ruby_glow_states_differ(self.style) and visible:
            return None
        return _ruby_glow_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = _build_ruby_glow_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        if not _ruby_glow_states_differ(self.style):
            return
        visible, complete, front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if not visible or complete:
            return
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(self.ruby_layout.ruby.reading)))
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = _ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = _ruby_glow_radius(self.style, after=self.after)
        pad = _glow_extent(
            _ruby_stroke_width(self.style),
            _ruby_stroke2_width(self.style),
            radius,
        )
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if self.rtl == self.after
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        colors = _effective_ruby_karaoke_colors(self.style)
        state = colors.after if self.after else colors.before
        _paint_glow_path(
            painter,
            path,
            state.shadow,
            _fill_brush_rect(
                state.shadow,
                self.ruby_layout.gradient_rect,
                self.ruby_layout.horizontal_gradient_rect,
            ),
            radius,
            _ruby_stroke_width(self.style),
            _ruby_stroke2_width(self.style),
            source_clip=source_clip,
            concentration_level=_ruby_glow_concentration_level(self.style),
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = _ruby_visual_padding(self.style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _RubyTextLayer:
    """Layer wrapper for one horizontal ruby reading."""

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    draw_glow: bool = True

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubyTextLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        if self.after:
            visible, _complete, _front = _ruby_wipe_state(
                self.ruby_layout, self.t_ms
            )
            if not visible:
                return None
        return _ruby_text_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = _build_ruby_text_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        clip_rect = None
        if self.after:
            visible, complete, _front = _ruby_wipe_state(
                self.ruby_layout, self.t_ms
            )
            if not visible:
                return LayerAnimation(opacity=0.0)
            if not complete:
                # 唱完（>= 1.0）不再裁剪：裁剪带右缘恰好压在字框右缘，
                # 会把末字形的描边外扩留在走字前状态。
                clip_rect = _ruby_after_clip_rect_at_time(
                    self.ruby_layout,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    self.t_ms,
                )
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
            clip_rect=clip_rect,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = _ruby_visual_padding(self.style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


def _ruby_text_layer_key(
    layout: _RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        _ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        _karaoke_state_signature(state),
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
        _ruby_shadow_dx(style),
        _ruby_shadow_dy(style),
        _ruby_decoration_kind(style),
        _ruby_glow_radius(style, after=after),
        _ruby_glow_concentration_level(style),
        after,
        draw_glow,
    )


def _ruby_glow_layer_key(
    layout: _RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        "ruby_glow",
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        _ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        _fill_signature(state.shadow),
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
        _ruby_glow_radius(style, after=after),
        _ruby_glow_concentration_level(style),
        after,
    )


def _ruby_horizontal_gradient_rect_signature(
    layout: _RubyLayout,
) -> tuple[float, float, float, float] | None:
    rect = layout.horizontal_gradient_rect
    if rect is None:
        return None
    return (
        round(rect.left() - layout.x, 3),
        round(rect.top() - layout.baseline_y, 3),
        round(rect.width(), 3),
        round(rect.height(), 3),
    )


def _get_or_build_ruby_glow(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> BakedLayer:
    key = (
        "ruby_full_glow",
        _ruby_glow_layer_key(layout, ruby_font, style, rtl, after=after),
    )

    def _build() -> BakedLayer:
        image, dx, dy = _build_ruby_glow_layer(
            layout,
            ruby_font,
            ruby_metrics,
            style,
            rtl,
            after=after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    return _TEXT_RUN_LAYER_CACHE.get_or_build(key, _build)


def _blit_cached_ruby_glow(
    painter: QPainter,
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> None:
    if _ruby_glow_radius(style, after=after) <= 0:
        return
    baked = _get_or_build_ruby_glow(
        layout,
        ruby_font,
        ruby_metrics,
        style,
        rtl,
        after=after,
    )
    if baked.image.isNull():
        return
    anchor = QPointF(
        float(layout.x) + baked.offset.x(),
        float(layout.baseline_y) + baked.offset.y(),
    )
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _build_ruby_text_layer(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple[QImage, int, int]:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    glow_radius = _ruby_glow_radius(style, after=after)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    glow_extra = (
        _glow_extent(stroke_width, stroke2_width, glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0
    )
    extent = max(
        stroke_extent,
        glow_extra,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    ) + 4
    layout_overhang_left = int(math.ceil(_ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = max(0, -shadow_dx) + extent + layout_overhang_left
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_text_layer_stack(
            p,
            path,
            rect,
            state,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=glow_radius,
            draw_glow=draw_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


def _build_ruby_glow_layer(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple[QImage, int, int]:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    glow_radius = _ruby_glow_radius(style, after=after)
    extent = _glow_extent(stroke_width, stroke2_width, glow_radius) + 4
    layout_overhang_left = int(math.ceil(_ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = extent + layout_overhang_left
    pad_right = extent
    pad_top = extent
    pad_bottom = extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_glow_path(
            p,
            path,
            state.shadow,
            _fill_brush_rect(state.shadow, fill_rect, horizontal_fill_rect),
            glow_radius,
            stroke_width,
            stroke2_width,
            concentration_level=_ruby_glow_concentration_level(style),
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


def _ruby_text_rect(layout: _RubyLayout, ruby_metrics: QFontMetrics) -> QRectF:
    left_offset = _ruby_layout_left_offset(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        layout.style,
        layout.ruby.kanji,
    )
    return QRectF(
        float(layout.x + left_offset),
        float(layout.baseline_y - ruby_metrics.ascent()),
        float(layout.reading_width),
        float(ruby_metrics.height()),
    )


def _ruby_after_clip_rect(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    ratio: float,
) -> QRectF:
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    pad = max(
        stroke_extent,
        _glow_extent(stroke_width, stroke2_width, after_glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )
    ratio_c = min(ratio, 1.0)
    clip_left = rect.left() + (rect.width() * (1.0 - ratio_c) if rtl else 0.0) - pad
    return QRectF(
        clip_left,
        rect.top() - pad,
        rect.width() * ratio_c + pad,
        rect.height() + pad * 2,
    )


def _ruby_wipe_state(
    layout: _RubyLayout,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Return ``(visible, complete, front)`` for glyph-geometry ruby wipe."""
    segments = layout.wipe_segments
    if not segments:
        ratio = _ruby_progress_ratio(layout.ruby, t_ms)
        front = layout.wipe_left + (layout.wipe_right - layout.wipe_left) * ratio
        return ratio > 0.0, ratio >= 1.0, front
    return _ruby_segment_wipe_state(segments, layout.ruby.pos_end_ms, t_ms)


def _ruby_segment_wipe_state(
    segments: tuple[_RubyWipeSegment, ...],
    pos_end_ms: int,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Evaluate timed glyph-axis segments, including empty-part pauses."""
    first = segments[0]
    if t_ms <= first.start_ms:
        return False, False, first.axis_start
    previous_front = first.axis_start
    for segment in segments:
        if t_ms < segment.start_ms:
            return True, False, previous_front
        if t_ms < segment.end_ms:
            duration = segment.end_ms - segment.start_ms
            local = (t_ms - segment.start_ms) / duration if duration > 0 else 1.0
            front = segment.axis_start + (segment.axis_end - segment.axis_start) * local
            return True, False, front
        previous_front = segment.axis_end
    complete = t_ms >= max(int(pos_end_ms), segments[-1].end_ms)
    return True, complete, previous_front


def _ruby_after_clip_rect_at_time(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Clip the after layer at the current ruby glyph front, not its box ratio."""
    _visible, _complete, front = _ruby_wipe_state(layout, t_ms)
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    pad = max(
        stroke_extent,
        _glow_extent(stroke_width, stroke2_width, after_glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    if rtl:
        left = min(max(front, wipe_left), wipe_right)
        return QRectF(
            left - pad,
            rect.top() - pad,
            max(wipe_right - left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    right = min(max(front, wipe_left), wipe_right)
    return QRectF(
        wipe_left - pad,
        rect.top() - pad,
        max(right - wipe_left, 0.0) + pad,
        rect.height() + pad * 2,
    )


def _ruby_before_clip_rect_at_time(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Keep the before ruby glow only on the unsung side of the wipe front."""
    _visible, _complete, front = _ruby_wipe_state(layout, t_ms)
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    pad = max(
        _visual_stroke_extent(stroke_width, stroke2_width),
        _glow_extent(
            stroke_width,
            stroke2_width,
            _ruby_glow_radius(style, after=False),
        ),
        2,
    )
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    front = min(max(front, wipe_left), wipe_right)
    if rtl:
        return QRectF(
            wipe_left - pad,
            rect.top() - pad,
            max(front - wipe_left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    return QRectF(
        front,
        rect.top() - pad,
        max(wipe_right - front, 0.0) + pad,
        rect.height() + pad * 2,
    )


def _ruby_target_x_range(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    time_indices = _ruby_time_indices(ruby, intervals)
    if ruby.kanji:
        text_span = _find_ruby_text_span(ruby.kanji, line, preferred_indices=time_indices)
        if text_span is None:
            return None
        return _ruby_text_span_x_range(text_span, line, char_x_ranges)

    indices = time_indices
    if not indices:
        return None
    left = min(char_x_ranges[index][0] for index in indices)
    right = max(char_x_ranges[index][1] for index in indices)
    return left, right


def _ruby_text_span_x_range(
    text_span: tuple[int, int],
    line: TimingLine,
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    span_start, span_end = text_span
    cursor = 0
    left: int | None = None
    right: int | None = None
    for index, ch in enumerate(line.chars):
        if index >= len(char_x_ranges):
            break
        text = ch.text
        text_len = len(text)
        unit_start = cursor
        unit_end = cursor + text_len
        cursor = unit_end
        if text_len <= 0 or unit_end <= span_start or unit_start >= span_end:
            continue
        overlap_start = max(span_start, unit_start) - unit_start
        overlap_end = min(span_end, unit_end) - unit_start
        char_left, char_right = char_x_ranges[index]
        width = char_right - char_left
        segment_left = char_left + round(width * overlap_start / text_len)
        segment_right = char_left + round(width * overlap_end / text_len)
        left = segment_left if left is None else min(left, segment_left)
        right = segment_right if right is None else max(right, segment_right)
    if left is None or right is None or right <= left:
        return None
    return left, right


def _find_ruby_text_span(
    kanji: str,
    line: TimingLine,
    *,
    preferred_indices: list[int] | None = None,
) -> tuple[int, int] | None:
    if not kanji:
        return None
    text = "".join(ch.text for ch in line.chars)
    occurrences: list[tuple[int, int]] = []
    pos = text.find(kanji)
    while pos >= 0:
        occurrences.append((pos, pos + len(kanji)))
        pos = text.find(kanji, pos + 1)
    if not occurrences:
        return None
    if not preferred_indices:
        return occurrences[0]

    preferred = set(preferred_indices)

    def score(span: tuple[int, int]) -> tuple[int, int]:
        indices = _text_span_indices(span, line)
        overlap = len(preferred.intersection(indices))
        distance = min((abs(index - candidate) for index in indices for candidate in preferred), default=0)
        return overlap, -distance

    return max(occurrences, key=score)


def _find_ruby_text_indices(
    kanji: str,
    line: TimingLine,
    *,
    preferred_indices: list[int] | None = None,
) -> list[int]:
    if not kanji:
        return []
    span = _find_ruby_text_span(kanji, line, preferred_indices=preferred_indices)
    if span is None:
        return []
    return _text_span_indices(span, line)


def _text_span_indices(text_span: tuple[int, int], line: TimingLine) -> list[int]:
    span_start, span_end = text_span
    indices: list[int] = []
    cursor = 0
    for index, ch in enumerate(line.chars):
        unit_start = cursor
        unit_end = cursor + len(ch.text)
        cursor = unit_end
        if unit_start < span_end and unit_end > span_start:
            indices.append(index)
    return indices


def _paint_ruby_text_units_with_transition(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
    transition: _LineCharTransition,
    char_index: int,
    char_count: int,
    following_done_ms: int | None,
    rtl: bool = False,
    target_width: int | float | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
) -> None:
    visual_units = _ruby_visual_units_and_intervals(ruby)
    # RTL：按音节反转排布顺序，使首音节落在最右；各音节计时不变。
    if rtl:
        visual_units = list(reversed(visual_units))
    units = [unit for unit, _interval in visual_units]
    intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(intervals):
        _paint_ruby_text(
            painter,
            ruby,
            ruby_font,
            ruby_metrics,
            x,
            baseline_y,
            t_ms,
            style,
            rtl,
            target_width=target_width,
            gradient_rect=gradient_rect,
            horizontal_gradient_rect=horizontal_gradient_rect,
        )
        return

    layout_units = _ruby_layout_units(
        units, ruby_metrics, x, target_width, style=style, base_text=ruby.kanji
    )
    for (unit, unit_x, unit_width), (start_ms, end_ms) in zip(layout_units, intervals):
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            char_index,
            char_count,
            char_start_ms=start_ms,
            char_end_ms=end_ms,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity > 0.0:
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                transform = _character_transform(
                    center_x=unit_x + unit_width / 2,
                    center_y=baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                    scale_origin_x=unit_x,
                    scale_origin_y=baseline_y,
                )
                _paint_ruby_text_fragment(
                    painter,
                    unit,
                    ruby_font,
                    ruby_metrics,
                    unit_x,
                    baseline_y,
                    char_fill_ratio(start_ms, end_ms, t_ms),
                    style,
                    rtl,
                    transform=transform,
                    gradient_rect=gradient_rect,
                    horizontal_gradient_rect=horizontal_gradient_rect,
                )
            finally:
                painter.restore()


def _paint_ruby_text(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
    rtl: bool = False,
    target_width: int | float | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
    wipe_layout: _RubyLayout | None = None,
) -> None:
    # RTL：按可见字形反转读音——小书き假名(ゃゅょ等)是独立字形，也要反过来；
    # 只有零宽浊点/半浊点(゙゚)留在基字后。直接 reading[::-1] 会让浊点
    # 漂移，所以用 _ruby_utopia_visual_units 切分后反转。
    reading = (
        "".join(reversed(_ruby_utopia_visual_units(ruby.reading))) if rtl else ruby.reading
    )
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        x,
        baseline_y,
        target_width,
        style,
        base_text=ruby.kanji,
    )
    _paint_ruby_karaoke_path(
        painter,
        path,
        rect,
        ruby,
        t_ms,
        style,
        rtl,
        ruby_metrics,
        gradient_rect=gradient_rect,
        horizontal_gradient_rect=horizontal_gradient_rect,
        wipe_layout=wipe_layout,
    )


def _ruby_text_path_and_rect(
    reading: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int | float,
    baseline_y: int | float,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> tuple[QPainterPath, QRectF]:
    path = QPainterPath()
    if target_width is None:
        path.addText(float(x), float(baseline_y), ruby_font, reading)
        width = ruby_metrics.horizontalAdvance(reading)
        return path, QRectF(
            float(x),
            float(baseline_y - ruby_metrics.ascent()),
            float(width),
            float(ruby_metrics.height()),
        )

    units = _ruby_utopia_visual_units(reading)
    layout_units = _ruby_layout_units(
        units, ruby_metrics, x, target_width, style=style, base_text=base_text
    )
    for unit, unit_x, _unit_width in layout_units:
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
    layout_width = _ruby_layout_width(
        reading, ruby_metrics, target_width, style=style, base_text=base_text
    )
    layout_left = float(x) + _ruby_layout_left_offset(
        reading,
        ruby_metrics,
        target_width,
        style,
        base_text,
    )
    return path, QRectF(
        layout_left,
        float(baseline_y - ruby_metrics.ascent()),
        float(layout_width),
        float(ruby_metrics.height()),
    )


def _ruby_layout_width(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    units = _ruby_utopia_visual_units(reading)
    unit_layouts = _ruby_unit_layouts(units, ruby_metrics, style)
    natural = sum(width for _unit, width, _offset in unit_layouts)
    interval = float(_ruby_interval_px(style))
    if target_width is None:
        return natural + interval * max(len(units) - 1, 0)
    target = float(max(target_width, 0))
    if len(units) <= 1:
        return max(target, natural)
    gap = _ruby_layout_gap(natural, len(units), target, style, base_text, reading)
    return max(target, natural + gap * (len(units) - 1))


def _ruby_layout_left_offset(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    if target_width is None:
        return 0.0
    units = _ruby_utopia_visual_units(reading)
    if not units:
        return 0.0
    unit_layouts = _ruby_unit_layouts(units, ruby_metrics, style)
    natural = sum(width for _unit, width, _offset in unit_layouts)
    target = float(target_width)
    if len(units) <= 1:
        content_width = natural
    else:
        gap = _ruby_layout_gap(natural, len(units), target, style, base_text, reading)
        content_width = natural + gap * (len(units) - 1)
    return min((target - content_width) / 2.0, 0.0)


def _ruby_layout_left_overhang(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    return max(
        0.0,
        -_ruby_layout_left_offset(reading, ruby_metrics, target_width, style, base_text),
    )


def _ruby_layout_units(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> list[tuple[str, float, float]]:
    origins = _ruby_layout_origins(
        units,
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=base_text,
    )
    return [
        (unit, origin + offset, width)
        for unit, origin, width, offset in origins
    ]


def _ruby_layout_origins(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> list[tuple[str, float, float, float]]:
    """Return N3 ``CharPoint.X`` positions before path-bearing correction."""
    unit_layouts = _ruby_unit_layouts(units, ruby_metrics, style)
    if not units:
        return []
    widths = [width for _unit, width, _offset in unit_layouts]
    natural = sum(widths)
    interval = float(_ruby_interval_px(style))
    if target_width is None:
        cursor = float(x)
        result: list[tuple[str, float, float, float]] = []
        for unit, width, offset in unit_layouts:
            result.append((unit, cursor, width, offset))
            cursor += width + interval
        return result

    target = float(target_width)
    if len(units) <= 1:
        unit, width, offset = unit_layouts[0]
        # N3's center calculation uses integer division.
        unit_left = float(x) + float(_truncate_div(int(target - width), 2))
        return [(unit, unit_left, width, offset)]

    reading = "".join(units)
    alignment = _resolve_ruby_alignment(style, base_text, reading)
    gap = _ruby_layout_gap(natural, len(units), target, style, base_text, reading)
    content_width = natural + gap * (len(units) - 1)
    if alignment == "center":
        cursor = float(x) + float(_truncate_div(int(target - content_width), 2))
    else:
        cursor = float(x) + (target - content_width) / 2.0
    result: list[tuple[str, float, float, float]] = []
    for unit, width, offset in unit_layouts:
        # N3 casts each accumulated EqualSpace cursor to int independently.
        origin = float(int(cursor)) if alignment == "equal_space" else cursor
        result.append((unit, origin, width, offset))
        cursor += width + gap
    return result


def _ruby_layout_draw_bounds(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> tuple[float, float]:
    """Return N3 ruby ``DrawLineLeft/DrawLineRight`` bounds."""
    origins = _ruby_layout_origins(
        units,
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=base_text,
    )
    if not origins:
        return float(x), float(x)
    return (
        min(origin for _unit, origin, _width, _offset in origins),
        max(origin + width for _unit, origin, width, _offset in origins),
    )


def _ruby_layout_gap(
    natural_width: float,
    unit_count: int,
    target_width: float,
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> float:
    """相邻注音字符的间距：Center 固定为 ``RubyInterval``，EqualSpace 按 N3 公式摊分。"""
    if unit_count <= 1:
        return 0.0
    interval = float(_ruby_interval_px(style))
    if _resolve_ruby_alignment(style, base_text, reading) == "center":
        return interval
    if target_width <= natural_width:
        gap = (target_width - natural_width) / (unit_count - 1)
    else:
        gap = (target_width - natural_width) / (unit_count + 1)
    return max(gap, interval)


def _resolve_ruby_alignment(
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> str:
    mode = str(getattr(style, "ruby_alignment", "auto") or "auto")
    if mode in {"center", "equal_space"}:
        return mode
    # auto：正文范围或注音全为英数字时居中，否则均等割り付け（N3 RubyAlignment.Auto）。
    if (base_text and _is_ascii_alnum(base_text)) or _is_ascii_alnum(reading):
        return "center"
    return "equal_space"


def _is_ascii_alnum(text: str) -> bool:
    stripped = [ch for ch in text if not ch.isspace()]
    return bool(stripped) and all(ord(ch) < 128 and ch.isalnum() for ch in stripped)


def _ruby_interval_px(style: Style | None) -> int:
    return int(getattr(style, "ruby_interval_px", 0) or 0)


# ruby 测量资源（QFont + measure_style）与单元布局按值键缓存：_ruby_unit_layouts 逐帧
# 高频调用（同一注音在 gap/宽度/偏移计算里一帧内被反复度量），每次 QFont 构造 +
# dataclasses.replace + 逐单元度量的开销可观。key 覆盖下游度量读取的全部字段。
_RUBY_MEASURE_CACHE: dict[tuple, tuple[QFont, Style]] = {}
_RUBY_MEASURE_CACHE_MAX = 64
_RUBY_UNIT_LAYOUT_CACHE: dict[tuple, list[tuple[str, float, float]]] = {}
_RUBY_UNIT_LAYOUT_CACHE_MAX = 4096


def _ruby_measure_key(style: Style) -> tuple:
    return (
        style.font_family,
        _ruby_font_size(style),
        style.italic,
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
        int(style.space_width_percent),
        bool(style.allow_biting),
    )


def _ruby_measure_resources(style: Style, key: tuple) -> tuple[QFont, Style]:
    cached = _RUBY_MEASURE_CACHE.get(key)
    if cached is not None:
        return cached
    ruby_font = _build_ruby_font(style)
    measure_style = replace(
        style,
        font_size_px=_ruby_font_size(style),
        stroke_width_px=_ruby_stroke_width(style),
        stroke2_width_px=_ruby_stroke2_width(style),
    )
    if len(_RUBY_MEASURE_CACHE) >= _RUBY_MEASURE_CACHE_MAX:
        _RUBY_MEASURE_CACHE.clear()
    _RUBY_MEASURE_CACHE[key] = (ruby_font, measure_style)
    return ruby_font, measure_style


def _ruby_unit_layouts(
    units: list[str],
    ruby_metrics: QFontMetrics,
    style: Style | None,
) -> list[tuple[str, float, float]]:
    if style is None:
        return [(unit, float(ruby_metrics.horizontalAdvance(unit)), 0.0) for unit in units]
    measure_key = _ruby_measure_key(style)
    # metrics 指纹进 key：调用方的 ruby_metrics 可能按基础 style 构建、而 style 是
    # 角色解析后的（见 _layout_rubies），两者字体不必一致；advance 取自 metrics。
    metrics_sig = (
        ruby_metrics.height(),
        ruby_metrics.ascent(),
        ruby_metrics.averageCharWidth(),
        ruby_metrics.maxWidth(),
    )
    layout_key = (tuple(units), metrics_sig, measure_key)
    cached = _RUBY_UNIT_LAYOUT_CACHE.get(layout_key)
    if cached is not None:
        return cached
    ruby_font, measure_style = _ruby_measure_resources(style, measure_key)
    result = [
        (
            unit,
            float(_char_layout_width(unit, ruby_font, ruby_metrics, ruby_metrics, None, measure_style)),
            _char_path_left_offset(unit, ruby_font, ruby_metrics, ruby_metrics, None, measure_style),
        )
        for unit in units
    ]
    if len(_RUBY_UNIT_LAYOUT_CACHE) >= _RUBY_UNIT_LAYOUT_CACHE_MAX:
        _RUBY_UNIT_LAYOUT_CACHE.clear()
    _RUBY_UNIT_LAYOUT_CACHE[layout_key] = result
    return result


def _paint_ruby_text_fragment(
    painter: QPainter,
    text: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int | float,
    baseline_y: int | float,
    ratio: float,
    style: Style,
    rtl: bool = False,
    transform: QTransform | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
) -> None:
    path = QPainterPath()
    path.addText(float(x), float(baseline_y), ruby_font, text)
    rect = QRectF(
        float(x),
        float(baseline_y - ruby_metrics.ascent()),
        float(ruby_metrics.horizontalAdvance(text)),
        float(ruby_metrics.height()),
    )
    if transform is not None and not transform.isIdentity():
        path = transform.map(path)
        rect = path.boundingRect()
    _paint_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
        rtl,
        fill_rect=gradient_rect,
        horizontal_fill_rect=horizontal_gradient_rect,
    )


def _paint_ruby_karaoke_path(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ruby: RubyAnnotation,
    t_ms: int,
    style: Style,
    rtl: bool = False,
    ruby_metrics: QFontMetrics | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
    wipe_layout: _RubyLayout | None = None,
) -> None:
    after_clip_rect = None
    before_glow_clip_rect = None
    if wipe_layout is not None and ruby_metrics is not None:
        visible, complete, _front = _ruby_wipe_state(wipe_layout, t_ms)
        if not visible:
            ratio = 0.0
        elif complete:
            ratio = 1.0
        else:
            # 走字进行中：before / after 两层都要画（此前强制 ratio=1.0 会把
            # before 层整个跳过，未唱读音在过渡窗口内消失）。实际几何完全由
            # 段式 front 的两个 clip rect 决定，中间 ratio 只用于让 fragment
            # 同时走两层的分支。
            ratio = 0.5
            after_clip_rect = _ruby_after_clip_rect_at_time(
                wipe_layout, ruby_metrics, style, rtl, t_ms
            )
            if _ruby_glow_states_differ(style):
                before_glow_clip_rect = _ruby_before_clip_rect_at_time(
                    wipe_layout, ruby_metrics, style, rtl, t_ms
                )
    else:
        ratio = _ruby_progress_ratio(ruby, t_ms, ruby_metrics)
    _paint_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
        rtl,
        fill_rect=gradient_rect,
        horizontal_fill_rect=horizontal_gradient_rect,
        after_clip_rect=after_clip_rect,
        before_glow_clip_rect=before_glow_clip_rect,
    )


def _paint_ruby_karaoke_fragment(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ratio: float,
    style: Style,
    rtl: bool = False,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    after_clip_rect: QRectF | None = None,
    before_glow_clip_rect: QRectF | None = None,
) -> None:
    colors = _effective_ruby_karaoke_colors(style)
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    before_glow_radius = _ruby_glow_radius(style, after=False)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    glow_states_differ = _ruby_glow_states_differ(style)

    # N3 clips before/after outline sources at WipeLeft and blurs afterwards.
    # The sharp colour boundary therefore stays on the ruby ink/edge while the
    # two soft halos may blend across it.
    clip_before_glow = ratio > 0.0 and glow_states_differ and (
        before_glow_radius > 0 or after_glow_radius > 0
    )
    if clip_before_glow and ratio < 1.0:
        if before_glow_clip_rect is not None:
            front = (
                before_glow_clip_rect.right()
                if rtl
                else before_glow_clip_rect.left()
            )
        else:
            front = rect.left() + rect.width() * (1.0 - ratio if rtl else ratio)
        before_pad = _glow_extent(
            stroke_width, stroke2_width, before_glow_radius
        )
        before_source_clip = (
            QRectF(
                -1_000_000.0,
                rect.top() - before_pad,
                front + 1_000_000.0,
                rect.height() + before_pad * 2,
            )
            if rtl
            else QRectF(
                front,
                rect.top() - before_pad,
                1_000_000.0,
                rect.height() + before_pad * 2,
            )
        )
        _paint_glow_path(
            painter,
            path,
            colors.before.shadow,
            _fill_brush_rect(
                colors.before.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            before_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=before_source_clip,
            concentration_level=_glow_concentration_level(paint_style),
        )
        after_pad = _glow_extent(
            stroke_width, stroke2_width, after_glow_radius
        )
        after_source_clip = (
            QRectF(
                front,
                rect.top() - after_pad,
                1_000_000.0,
                rect.height() + after_pad * 2,
            )
            if rtl
            else QRectF(
                -1_000_000.0,
                rect.top() - after_pad,
                front + 1_000_000.0,
                rect.height() + after_pad * 2,
            )
        )
        _paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            _fill_brush_rect(
                colors.after.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            after_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=after_source_clip,
            concentration_level=_glow_concentration_level(paint_style),
        )

    if ratio < 1.0:
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=before_glow_radius,
            draw_glow=not clip_before_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )

    if ratio <= 0.0:
        return

    painter.save()
    try:
        if ratio < 1.0 or after_clip_rect is not None:
            stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
            pad = max(
                stroke_extent,
                _glow_extent(stroke_width, stroke2_width, after_glow_radius)
                if _ruby_decoration_kind(style) == "glow"
                else 0,
                stroke_extent + abs(shadow_dx),
                stroke_extent + abs(shadow_dy),
                2,
            )
            # RTL：已唱区贴读音右缘，左缘（扫光线）随进度左移。前缘必须停在
            # 扫光线本身，pad 只外扩尾缘/上下缘（LTR 尾缘在左，RTL 在右）。
            if after_clip_rect is None:
                if rtl:
                    front = rect.left() + rect.width() * (1.0 - ratio)
                    after_clip_rect = QRectF(
                        front,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
                else:
                    after_clip_rect = QRectF(
                        rect.left() - pad,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
            painter.setClipRect(after_clip_rect)
        # ratio >= 1.0：唱完不再裁剪——裁剪带右缘恰好压在字框右缘，
        # 会把末字形的描边外扩留在走字前状态。
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.after,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=after_glow_radius,
            # 前后发光相同时未唱发光已铺满整读音，再叠只会加亮已唱带；
            # ratio>=1 时未唱层未画，已唱发光必须自己画。
            draw_glow=ratio >= 1.0,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        painter.restore()


def _paint_text_layer_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    colors: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
    draw_glow: bool = True,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    draw_shadow: bool = True,
) -> None:
    brush_rect = fill_rect if fill_rect is not None else rect
    if style.decoration_kind == "glow":
        # ``draw_glow=False`` 让调用方把发光单独按「发光级」宽松裁切处理（卡拉ok 走字
        # 时发光软晕不能跟描边/填充一样按字框硬裁，否则会被裁成方框）。
        if draw_glow:
            _paint_glow_path(
                painter,
                path,
                colors.shadow,
                _fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
                glow_radius,
                stroke_width,
                stroke2_width,
                concentration_level=_glow_concentration_level(style),
            )
    elif (
        style.decoration_kind == "shadow"
        and draw_shadow
        and (shadow_dx or shadow_dy)
    ):
        _paint_shadow_silhouette(
            painter,
            path,
            colors.shadow,
            _fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
            shadow_dx,
            shadow_dy,
            stroke_width,
            stroke2_width,
        )

    if stroke2_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke2,
            _fill_brush_rect(colors.stroke2, brush_rect, horizontal_fill_rect),
            _stroke2_pen_width(stroke_width, stroke2_width),
        )
    if stroke_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke,
            _fill_brush_rect(colors.stroke, brush_rect, horizontal_fill_rect),
            _stroke_pen_width(stroke_width),
            protect_body=_fill_is_alpha(colors.text),
        )
    _paint_fill_path(
        painter,
        path,
        colors.text,
        _fill_brush_rect(colors.text, brush_rect, horizontal_fill_rect),
    )


def _effective_ruby_karaoke_colors(style: Style) -> KaraokeColors:
    if style.ruby_karaoke_colors is not None:
        return style.ruby_karaoke_colors
    if style.karaoke_colors is not None:
        return style.karaoke_colors
    before = KaraokeColorState(
        text=_solid_fill(style.base_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=_solid_fill(style.ruby_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


def _ruby_scale(style: Style) -> float:
    return _ruby_font_size(style) / max(style.font_size_px, 1)


def _scaled_px(value: int, scale: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def _scaled_signed_px(value: int, scale: float) -> int:
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def _ruby_progress_ratio(
    ruby: RubyAnnotation,
    t_ms: int,
    ruby_metrics: QFontMetrics | None = None,
) -> float:
    if not ruby.reading:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)
    if not ruby.reading_part_ms:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)

    parts, intervals = _ruby_progress_parts_and_intervals(ruby)
    if ruby_metrics is not None and len(parts) == len(intervals):
        weights = [max(float(ruby_metrics.horizontalAdvance(part)), 0.0) for part in parts]
    else:
        weights = [1.0] * len(intervals)
    total_weight = sum(weights)
    if total_weight <= 0.0:
        weights = [1.0] * len(intervals)
        total_weight = float(len(intervals))

    completed_weight = 0.0
    for weight, (start, end) in zip(weights, intervals):
        if t_ms < start:
            return completed_weight / total_weight
        if t_ms < end:
            local = char_fill_ratio(start, end, t_ms)
            return (completed_weight + weight * local) / total_weight
        completed_weight += weight
    return 1.0


def _ruby_progress_parts_and_intervals(
    ruby: RubyAnnotation,
) -> tuple[list[str], list[tuple[int, int]]]:
    """Return the exported ruby parts and their checkpoint intervals.

    Nicokara embeds one relative timestamp before every part after the first.
    Preserving those exact slices matters for SUG parity: a multi-character
    part owns its combined rendered width, while a part between consecutive
    timestamps is empty and consumes time without advancing the wipe.
    """
    parts = list(ruby.reading_parts)
    if (
        parts
        and len(parts) == len(ruby.reading_part_ms) + 1
        and "".join(parts) == ruby.reading
    ):
        start = int(ruby.pos_start_ms)
        end = max(start, int(ruby.pos_end_ms))
        anchors = [start]
        for relative_ms in ruby.reading_part_ms:
            timestamp = start + int(relative_ms)
            anchors.append(max(anchors[-1], min(end, timestamp)))
        anchors.append(max(anchors[-1], end))
        return parts, list(zip(anchors, anchors[1:]))

    units = _ruby_reading_units(ruby.reading)
    return units, _ruby_reading_intervals(ruby)


def _ruby_visual_units_and_intervals(
    ruby: RubyAnnotation,
) -> list[tuple[str, tuple[int, int]]]:
    """Expand exported ruby parts into N3 visual characters and exact times.

    An empty exported part is a real pause and therefore yields no geometry.
    Multi-character parts divide their own interval with integer boundaries;
    combining dakuten/handakuten stay attached to the preceding character.
    """
    parts, intervals = _ruby_progress_parts_and_intervals(ruby)
    result: list[tuple[str, tuple[int, int]]] = []
    for part, (start, end) in zip(parts, intervals):
        units = _ruby_utopia_visual_units(part)
        if not units:
            continue
        duration = max(int(end) - int(start), 0)
        count = len(units)
        for index, unit in enumerate(units):
            unit_start = int(start) + duration * index // count
            unit_end = int(start) + duration * (index + 1) // count
            result.append((unit, (unit_start, max(unit_start, unit_end))))
    return result


def _ruby_reading_unit_progress_points(
    ruby: RubyAnnotation,
) -> list[tuple[int, float]]:
    """Return time/progress points for visible ruby characters.

    Every visible text element owns one equal spatial share.  Both ends of
    each element are retained so an empty exported part becomes a real
    progress plateau instead of being smoothed across as if it were text.
    """
    timed_units = _ruby_visual_units_and_intervals(ruby)
    if not timed_units:
        return []
    count = len(timed_units)
    raw_points: list[tuple[int, float]] = []
    for index, (_unit, (start, end)) in enumerate(timed_units):
        start = int(start)
        end = max(start, int(end))
        raw_points.append((start, index / count))
        raw_points.append((end, (index + 1) / count))

    # Zero-duration units can place multiple progress values at one time.
    # Keep the furthest value so the unit completes instantaneously there.
    points: list[tuple[int, float]] = []
    for timestamp, progress in raw_points:
        if points and timestamp == points[-1][0]:
            points[-1] = (timestamp, max(points[-1][1], progress))
        else:
            points.append((timestamp, progress))
    return points


def _reading_unit_progress_ratio(ruby: RubyAnnotation, t_ms: int) -> float | None:
    points = _ruby_reading_unit_progress_points(ruby)
    if not points:
        return None
    if t_ms < points[0][0]:
        return 0.0
    if t_ms >= points[-1][0]:
        return 1.0
    for (start_ms, start_progress), (end_ms, end_progress) in zip(
        points, points[1:]
    ):
        if t_ms < end_ms:
            if end_ms <= start_ms or end_progress <= start_progress:
                return start_progress
            local = (t_ms - start_ms) / (end_ms - start_ms)
            return start_progress + (end_progress - start_progress) * local
    return 1.0


def _ruby_progress_time_at_ratio(
    ruby: RubyAnnotation,
    target: float,
    *,
    plateau_side: str,
) -> int:
    """Invert reading-unit progress, choosing either side of a pause plateau."""
    points = _ruby_reading_unit_progress_points(ruby)
    if not points:
        start = int(ruby.pos_start_ms)
        end = max(start, int(ruby.pos_end_ms))
        return int(round(start + (end - start) * max(0.0, min(1.0, target))))
    target = max(0.0, min(1.0, target))
    if target <= 0.0:
        return points[0][0]
    if target >= 1.0:
        return points[-1][0]

    exact_times = [timestamp for timestamp, progress in points if progress == target]
    if exact_times:
        return min(exact_times) if plateau_side == "left" else max(exact_times)
    for (start_ms, start_progress), (end_ms, end_progress) in zip(
        points, points[1:]
    ):
        if start_progress < target < end_progress:
            local = (target - start_progress) / (end_progress - start_progress)
            return int(round(start_ms + (end_ms - start_ms) * local))
    return points[-1][0]


def _ruby_main_text_slot_times(
    ruby: RubyAnnotation,
    base_index: int,
    base_count: int,
) -> tuple[int, int]:
    count = max(int(base_count), 1)
    start = _ruby_progress_time_at_ratio(
        ruby, base_index / count, plateau_side="right"
    )
    end = _ruby_progress_time_at_ratio(
        ruby, (base_index + 1) / count, plateau_side="left"
    )
    return start, max(start, end)


def _main_text_ruby_progress_ratio(
    ruby: RubyAnnotation,
    t_ms: int,
    *,
    mode: str = "checkpoint_segments",
) -> float:
    """Return the selected progress clock for the ruby's base text.

    The historical ``checkpoint_segments`` mode divides base-text progress
    equally by checkpoint segment.  ``reading_units`` instead gives every
    visible ruby text element one equal share and preserves empty-part pauses;
    callers then map that normalized reading position across the covered base
    characters.  Both clocks stay separate from :func:`_ruby_progress_ratio`,
    whose spatial weights follow rendered ruby widths.
    """
    if mode == "reading_units":
        reading_progress = _reading_unit_progress_ratio(ruby, t_ms)
        if reading_progress is not None:
            return reading_progress

    if not ruby.reading_part_ms:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)

    start = int(ruby.pos_start_ms)
    end = max(start, int(ruby.pos_end_ms))
    anchors = [start]
    for relative_ms in ruby.reading_part_ms:
        timestamp = start + int(relative_ms)
        anchors.append(max(anchors[-1], min(end, timestamp)))
    anchors.append(max(anchors[-1], end))

    segment_count = len(anchors) - 1
    if segment_count <= 0:
        return 1.0
    if t_ms < anchors[0]:
        return 0.0
    if t_ms >= anchors[-1]:
        return 1.0
    for index in range(segment_count):
        segment_start = anchors[index]
        segment_end = anchors[index + 1]
        if t_ms < segment_end:
            duration = segment_end - segment_start
            local = (t_ms - segment_start) / duration if duration > 0 else 1.0
            local = max(0.0, min(1.0, local))
            return (index + local) / segment_count
    return 1.0


def _main_text_ruby_progress_time_at_ratio(
    ruby: RubyAnnotation,
    target: float,
    *,
    mode: str = "checkpoint_segments",
    plateau_side: str,
) -> int:
    if mode == "reading_units" and _ruby_visual_units_and_intervals(ruby):
        return _ruby_progress_time_at_ratio(
            ruby,
            target,
            plateau_side=plateau_side,
        )

    start = int(ruby.pos_start_ms)
    end = max(start, int(ruby.pos_end_ms))
    target = max(0.0, min(1.0, float(target)))
    if not ruby.reading_part_ms:
        return int(round(start + (end - start) * target))

    anchors = [start]
    for relative_ms in ruby.reading_part_ms:
        timestamp = start + int(relative_ms)
        anchors.append(max(anchors[-1], min(end, timestamp)))
    anchors.append(max(anchors[-1], end))

    segment_count = len(anchors) - 1
    if segment_count <= 0:
        return end
    if target <= 0.0:
        return anchors[0]
    if target >= 1.0:
        return anchors[-1]

    points = [
        (timestamp, index / segment_count)
        for index, timestamp in enumerate(anchors)
    ]
    exact_times = [timestamp for timestamp, progress in points if progress == target]
    if exact_times:
        return min(exact_times) if plateau_side == "left" else max(exact_times)
    for (start_ms, start_progress), (end_ms, end_progress) in zip(points, points[1:]):
        if start_progress <= target <= end_progress:
            if end_progress <= start_progress or end_ms <= start_ms:
                return start_ms if plateau_side == "left" else end_ms
            local = (target - start_progress) / (end_progress - start_progress)
            return int(round(start_ms + (end_ms - start_ms) * local))
    return anchors[-1]


def _ruby_reading_intervals(ruby: RubyAnnotation) -> list[tuple[int, int]]:
    units = _ruby_reading_units(ruby.reading)
    if len(ruby.reading_part_ms) >= 2 * max(len(units) - 1, 0):
        return _ruby_reading_intervals_with_pauses(ruby, len(units))
    result: list[tuple[int, int]] = []
    boundaries = _ruby_reading_boundaries(ruby, len(units))
    for index, _unit in enumerate(units):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end < start:
            end = start
        result.append((start, end))
    return result


def _ruby_reading_intervals_with_pauses(
    ruby: RubyAnnotation,
    unit_count: int,
) -> list[tuple[int, int]]:
    if unit_count <= 0:
        return []
    intervals: list[tuple[int, int]] = []
    current_start = ruby.pos_start_ms
    for index in range(unit_count - 1):
        release = ruby.pos_start_ms + ruby.reading_part_ms[index * 2]
        next_start = ruby.pos_start_ms + ruby.reading_part_ms[index * 2 + 1]
        release = max(current_start, min(release, ruby.pos_end_ms))
        next_start = max(release, min(next_start, ruby.pos_end_ms))
        intervals.append((current_start, release))
        current_start = next_start
    intervals.append((current_start, max(current_start, ruby.pos_end_ms)))
    return intervals


def _ruby_utopia_reading_units_and_intervals(ruby: RubyAnnotation) -> list[tuple[str, tuple[int, int]]]:
    mora_units = _ruby_reading_units(ruby.reading)
    mora_intervals = _ruby_reading_intervals(ruby)
    result: list[tuple[str, tuple[int, int]]] = []
    for mora, (start, end) in zip(mora_units, mora_intervals):
        visual_units = _ruby_utopia_visual_units(mora)
        if len(visual_units) <= 1:
            result.append((mora, (start, end)))
            continue
        duration = max(end - start, 0)
        for index, visual in enumerate(visual_units):
            unit_start = start + round(duration * index / len(visual_units))
            unit_end = start + round(duration * (index + 1) / len(visual_units))
            result.append((visual, (unit_start, max(unit_start, unit_end))))
    return result


def _ruby_utopia_visual_units(text: str) -> list[str]:
    units: list[str] = []
    for ch in text:
        if units and ch in {"\u3099", "\u309A"}:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_units(reading: str) -> list[str]:
    units: list[str] = []
    for ch in reading:
        if units and ch in _RUBY_COMBINING_CHARS:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_boundaries(ruby: RubyAnnotation, unit_count: int) -> list[int]:
    if unit_count <= 0:
        return [ruby.pos_start_ms, ruby.pos_end_ms]
    boundaries = [ruby.pos_start_ms]
    for rel_ms in ruby.reading_part_ms[: max(unit_count - 1, 0)]:
        ts = ruby.pos_start_ms + rel_ms
        ts = max(boundaries[-1], min(ruby.pos_end_ms, ts))
        boundaries.append(ts)
    if len(boundaries) < unit_count:
        start = boundaries[-1]
        remaining = unit_count - len(boundaries) + 1
        for step in range(1, remaining):
            boundaries.append(start + round((ruby.pos_end_ms - start) * step / remaining))
    boundaries.append(max(boundaries[-1], ruby.pos_end_ms))
    return boundaries
