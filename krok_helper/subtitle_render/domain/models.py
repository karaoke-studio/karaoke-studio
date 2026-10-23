"""渲染项目数据模型。

包含两层模型：

1. **TimingTrack** 及其下属 — 字幕源（SUG 项目 / Nicokara 逐字 LRC）解析后的中间表示。
   每行可寻址到具体字符、字符的起始毫秒、行末与行内停顿释放时间戳、ふりがな 注音。
   解析器在 :mod:`subtitle_sources` 实现。

2. **RenderProject** 及其下属 — 渲染项目的持久化模型（``.yurika``），含
   字幕源引用、背景、样式、输出参数。MVP 阶段 :class:`Style` / 序列化等仍为占位，
   后续 P0 任务（A4/A6/A8 等）落地。

**字幕源格式**：支持 SUG 项目（``.sug``）与 Nicokara 逐字 LRC（``.lrc``）。
``.sug`` 直接读取 SUG domain 中的逐字时间戳、注音、演唱者/分色信息；``.lrc``
保留对既有 Nicokara 文件的导入能力。不支持 ``.ass`` / ``.nkm``。
"""

from __future__ import annotations

import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from difflib import SequenceMatcher
from typing import Iterable, Literal, Optional
from uuid import uuid4

from krok_helper.subtitle_render.domain.background import (
    Background,
    BackgroundSource,
    background_sequence_frame_path,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.domain.paint import (
    ColorFillMode,
    ColorLayerKey,
    ColorStateKey,
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    _paint_fill,
)
from krok_helper.subtitle_render.serialization.paint import (
    karaoke_colors_from_dict,
    karaoke_colors_to_dict,
    karaoke_color_state_from_dict,
    karaoke_color_state_to_dict,
    paint_fill_from_dict,
    paint_fill_to_dict,
)

from krok_helper.subtitle_render.domain.timing import (
    ENTRY_FX_KINDS,
    EXIT_FX_KINDS,
    EntryAnimation,
    EntryFx,
    ExitAnimation,
    ExitFx,
    GuideSymbol,
    KaraokeAnimation,
    LineAnimationOverride,
    LineBreakKind,
    RubyAnnotation,
    SING_FX_KINDS,
    SingFx,
    SubtitleLoadingSettings,
    SubtitleSource,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
    TrackPage,
    TrackPagePlan,
    TrackSection,
    guide_symbol_has_visual,
    guide_symbol_replacement_count,
    guide_symbol_replaces_prefix,
    guide_symbol_role_labels,
    guide_symbol_with_role_labels,
    line_visible_chars,
    timing_line_start_ms,
)


from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    guide_symbol_to_dict,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    subtitle_loading_settings_from_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_from_dict,
    track_page_plan_to_dict,
)

RubyMainProgressMode = Literal["checkpoint_segments", "reading_units"]
LayoutSemantics = Literal["legacy", "n3_1074"]

SCHEMA_VERSION = 2
PROJECT_FILE_SUFFIX = ".yurika"
STYLE_PRESET_FILE_SUFFIX = ".krstyle.json"
SUBTITLE_SOURCE_SUFFIX = ".sug"
# 导出文件名默认后缀（{视频文件名}_yurika出力.mp4）；N3 导入时会把
# N3 自动命名的「_ニコカラメーカー3出力」映射成它。
DEFAULT_OUTPUT_NAME_SUFFIX = "_yurika出力"

#: 导出文件名模板可用的占位符 → 面向用户的说明。
EXPORT_NAME_TEMPLATE_FIELDS = {
    "source_name": "素材名（视频 > 背景素材 > 字幕文件，与导出目录同源）",
    "video_name": "视频文件名",
    "subtitle_name": "字幕文件名",
}
#: 默认模板；渲染结果与改造前完全一致，不动老用户的输出习惯。
DEFAULT_EXPORT_NAME_TEMPLATE = f"{{source_name}}{DEFAULT_OUTPUT_NAME_SUFFIX}"


LineYPosition = Literal["top", "center", "bottom"]
LineHorizontalLayout = Literal["asymmetric", "center", "per_row"]
HorizontalAlign = Literal["left", "center", "right"]
HORIZONTAL_ALIGNS: tuple[HorizontalAlign, ...] = ("left", "center", "right")
ViewportAlign = Literal[
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
]
VIEWPORT_ALIGNS: tuple[ViewportAlign, ...] = (
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
)
DecorationKind = Literal["none", "shadow", "glow"]
RubyAlignment = Literal["auto", "center", "equal_space"]
RUBY_ALIGNMENTS: tuple[RubyAlignment, ...] = ("auto", "center", "equal_space")
SmartHorizontal = Literal["none", "center_position", "equal_margins"]
SMART_HORIZONTALS: tuple[SmartHorizontal, ...] = (
    "none",
    "center_position",
    "equal_margins",
)
SectionEndingMode = Literal["hold", "clear"]
OverlapFallbackMode = Literal["lift", "displace"]
"""跨页冲突在时间压缩到底仍无法消除时的处理方式。

``lift`` = 抬升避让（移动后进入的整页字幕，旧行为）；``displace`` = 由将要
演唱的下一句直接顶掉还在走字的上一句（截短其显示窗，允许吃掉走字时长，
默认值）。
"""
LitStyle = Literal[
    "volume", "circle", "square", "rounded", "image", "star",
    "note8", "note16", "notepair",
]
VolumeAppearanceMode = Literal["custom", "auto", "role"]
"""音量柱外观模式：``custom`` 手动逐项设置；``auto`` 大小与颜色自动跟随
主文字的字号与配色（见 :func:`resolve_volume_appearance`）；``role`` 大小
同 auto 按字号推导，装饰管线改用 ``volume_role_name`` 指定来源的配色
（与扫字线「复用配色方案」同口径）。"""
ScanlineEffect = Literal[
    "none", "sakura", "feather", "snow", "snow_solid", "star", "note",
]
"""扫字线粒子特效档位：沿走字锋面发射装饰粒子。

``none`` 无粒子；``sakura`` 樱花、``feather`` 羽毛、``snow`` 雪花（镂空）/
``snow_solid`` 雪花（实心）、``star`` 星光、``note`` 音符——自锋面后方朝
左上飘出。仅扫字线档位生效。
"""
SCANLINE_EFFECT_KINDS: frozenset[str] = frozenset(
    {"none", "sakura", "feather", "snow", "snow_solid", "star", "note"}
)
ScanlineShape = Literal["band", "flame", "bubble"]
"""扫字线形态：``band`` 普通高亮带；``flame`` 烈焰棒；``bubble`` 水泡。

烈焰棒/水泡是**独立形态**，不是普通高亮带的颜色变体：烈焰棒把扫字线本体
变成一根发光火棒并向两侧喷火舌；水泡从锋面底部发射镂空水泡、围绕扫字线
上浮。两者都会替换普通高亮带；烈焰棒颜色取扫字线本身的颜色方案，水泡
固定水蓝/白水感。
"""
SCANLINE_SHAPE_KINDS: frozenset[str] = frozenset({"band", "flame", "bubble"})
LitAppearanceMode = Literal["custom", "auto", "role"]
"""指示灯外观模式：``custom`` 手动逐项设置；``auto`` 大小自动跟随主文字
字号，矢量灯走主文字装饰管线（见 :func:`resolve_lit_appearance`）；
``role`` 大小同 auto 按字号推导，装饰管线改用 ``lit_role_name`` 指定
来源的配色（与扫字线「复用配色方案」同口径）。"""
# 标题字幕（B7）：静态叠加文字的锚点 / 对齐 / 显示时段模式。
TitleAnchor = Literal[
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
]
TITLE_ANCHORS: tuple[TitleAnchor, ...] = (
    "top_left",
    "top_center",
    "top_right",
    "center_left",
    "center",
    "center_right",
    "bottom_left",
    "bottom_center",
    "bottom_right",
)
# whole=整段显示（ニコカラ Head 默认，0→曲尾）；head=仅开头一段；
# tail=仅片尾一段；head_tail=开始和片尾各显示一段；custom=用户自定义
# 任意时间段（``custom_windows``，可多段、每段独立淡入淡出）。
TitleShowMode = Literal["whole", "head", "tail", "head_tail", "custom"]
TITLE_SHOW_MODES: tuple[TitleShowMode, ...] = (
    "whole",
    "head",
    "tail",
    "head_tail",
    "custom",
)

TITLE_SCHEME_NAME = "标题"
"""标题外观所引用的配色方案名：标题的字体与颜色统一由
``custom_style_schemes[TITLE_SCHEME_NAME]`` 描述（在字体页与其他角色方案一起编辑），
``TitleOverlay`` 只保留文字内容、布局引用与显示时段。"""

TITLE_LAYOUT_NAME = "タイトル左上"
"""默认标题布局名（对齐 N3 出厂布局预设 index 4）。"""

SCANLINE_GLOBAL_ROLE_KEY = "__global__"
"""``scanline_role_name`` 的保留键：全局默认来源（主样式自身配色）。

与角色/「标题」方案名（``custom_style_schemes`` 的键）区分开，用户自建
同名角色不会与它冲突；渲染端据此走主样式自身的走字后文字填充。
"""


@dataclass
class TitleTimeWindow:
    """「自定义」显示模式的单个时间段（绝对时间，项目时间轴坐标）。

    ``fade_in_ms`` / ``fade_out_ms`` 是**淡入/淡出动画的持续时长**（毫秒），
    不是起止时间。
    """

    begin_ms: int = 0
    end_ms: int = 10_000
    fade_in_ms: int = 500
    fade_out_ms: int = 500

    def normalized(self) -> "TitleTimeWindow":
        """Clamp values and drop degenerate windows via ``end <= begin``."""
        begin = max(int(self.begin_ms), 0)
        end = max(int(self.end_ms), 0)
        return TitleTimeWindow(
            begin_ms=begin,
            end_ms=end,
            fade_in_ms=max(int(self.fade_in_ms), 0),
            fade_out_ms=max(int(self.fade_out_ms), 0),
        )


def title_time_window_to_dict(window: TitleTimeWindow) -> dict:
    return {
        "begin_ms": int(window.begin_ms),
        "end_ms": int(window.end_ms),
        "fade_in_ms": int(window.fade_in_ms),
        "fade_out_ms": int(window.fade_out_ms),
    }


def title_time_window_from_dict(payload: object) -> Optional[TitleTimeWindow]:
    if not isinstance(payload, dict):
        return None
    return TitleTimeWindow(
        begin_ms=_int_value(payload.get("begin_ms"), 0),
        end_ms=_int_value(payload.get("end_ms"), 0),
        fade_in_ms=_int_value(payload.get("fade_in_ms"), 0),
        fade_out_ms=_int_value(payload.get("fade_out_ms"), 0),
    )


@dataclass
class TitleOverlay:
    """标题字幕叠加层（B7）。

    静态文字（曲名 / 艺术家），不走字；默认参数逆向自 NicoKaraMaker3
    「情報小」：教科书体 40px、灰白字黑边、白色小发光、左上、整段显示。
    文字模板 ``{title}`` / ``{artist}`` 由字幕源
    ``@Title`` / ``@Artist`` 元数据替换；也可直接填任意自定义文字（含换行）。
    """

    name: str = "标题 1"
    """条目显示名（属性页卡片标题与字幕源下拉共用；新增时按「标题 N」避让）。"""

    enabled: bool = False
    text_template: str = "{title} / {artist}"
    """``{title}`` / ``{artist}`` 占位符按元数据替换；``\\n`` 分行。"""

    char_role_labels: list[list[Optional[str]]] = field(default_factory=list)
    """逐行逐字符角色标签；``None`` 表示继承内置「标题」方案。"""

    guide_symbols: dict[int, GuideSymbol] = field(default_factory=dict)
    """逐行行前导唱符（行号 → 符号），插在该行文字之前显示。

    标题永不走字：位图导唱符恒取「走字前」一侧图片，``duration_ms`` 的
    走字节奏不生效，仅 ``count``（插入几个字形）参与布局。"""

    inline_guide_symbols: dict[tuple[int, int], GuideSymbol] = field(
        default_factory=dict
    )
    """逐字符导唱符替换（``(行号, 字符下标)`` → 符号）。

    与 :attr:`char_role_labels` 同一套对位契约：键挂在 ``text_template``
    的字符矩阵上，模板仍含 ``{title}`` / ``{artist}`` 占位时无法对位，
    首次逐字编辑 / 导入导唱符前宿主会先冻结模板。"""

    # 字体（逆向目标项目的 N3「情報小」）
    font_family: str = "UD デジタル 教科書体 N-B"
    font_family_latin: Optional[str] = "Comic Sans MS"
    font_size_px: int = 40
    font_weight: int = 700
    latin_font_stretch_pct: int = 100
    italic: bool = False
    letter_spacing_px: int = 0
    line_gap_px: int = 15

    # 颜色（单态：不走字）。N3「情報小」走字前：#EBEBEB、黑色 5px
    # 描边、关闭二重描边、白色 2px 发光。
    fill: PaintFill = field(default_factory=lambda: _paint_fill("#EBEBEB"))
    stroke: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))
    stroke_width_px: int = 5
    stroke2: PaintFill = field(default_factory=lambda: _paint_fill("#FFFFFF"))
    stroke2_width_px: int = 0
    decoration_kind: DecorationKind = "glow"
    glow_radius_px: int = 2
    glow_concentration_level: int = 0
    """-1 disables glow; NicoKaraMaker3 ``BlurLevel`` 0/1/2 = low/medium/high."""
    shadow: PaintFill = field(default_factory=lambda: _paint_fill("#FFFFFF"))
    shadow_offset_x: int = 5
    shadow_offset_y: int = 5

    # 位置（锚点 9 宫格 + 内边距 / 偏移；逆向 ニコカラ「タイトル左上」）。
    # 这些字段与上方字体/颜色字段一样，现在是「解析结果」：渲染时由
    # ``layout_index`` 引用的布局方案与 ``TITLE_SCHEME_NAME`` 配色方案推导，
    # 仅当布局/方案缺失（旧工程迁移前）时按字段原值绘制。
    anchor: TitleAnchor = "top_left"
    align: HorizontalAlign = "left"
    offset_x: int = 50
    offset_y: int = 50

    scheme_name: Optional[str] = None
    """标题基础外观引用的配色方案名；``None`` 表示内置「标题」方案。
    引用的方案被删除时回落内置方案（逐字角色标签不受影响）。"""

    layout_index: Optional[int] = 1
    """标题引用的布局方案（同 ``TimingLine.layout_index``：0 = 默认布局，
    n = ``Style.layouts[n-1]``）。默认 1 指向内置「タイトル左上」；``None``
    表示旧工程的显式 anchor/offset 字段仍然生效（加载时会自动迁移）。"""

    # 显示时段（逆向 ニコカラ TitleShowTime）。新工程/新增条目默认「自定义」
    # 且窗口为空 = 全程显示（不再用静态 0-10s 窗口，避免标题中途消失）；
    # 旧工程加载在 ``title_overlay_from_dict`` 中对缺失字段固定回落 whole，
    # 字节语义不变。
    show_mode: TitleShowMode = "custom"
    head_offset_ms: int = 0
    duration_ms: int = 10000
    tail_offset_ms: int = 0
    fade_in_ms: int = 300
    fade_out_ms: int = 300
    # None keeps old projects byte-semantically compatible: the ending
    # segment inherits the corresponding opening value until edited.
    tail_duration_ms: Optional[int] = None
    tail_fade_in_ms: Optional[int] = None
    tail_fade_out_ms: Optional[int] = None
    # 「自定义」模式的显示时间段；空列表 = 全程显示（见 ``title_show_window``）。
    # ``show_mode != "custom"`` 时忽略不删，切回自定义模式后原样恢复。
    custom_windows: list[TitleTimeWindow] = field(default_factory=list)


@dataclass
class LyricsLayout:
    """一套可命名的 N3 ``LyricsLayoutModel`` 布局定义。

    字符排版字段为 ``None`` 时继承 ``Style``，用于兼容旧版 ``.yurika``；N3
    导入和新版编辑器会保存显式值，包括合法的 0 / ``False``。
    """

    name: str = "布局"
    layout_id: str = ""
    """Stable project-local identifier.  Legacy numeric indices remain a projection."""
    line_y_position: LineYPosition = "bottom"
    line_y_margin_px: int = 80
    line_gap_px: int = 90
    smart_horizontal: SmartHorizontal = "equal_margins"
    horizontal_margin_px: int = 50
    line_alignments: list[HorizontalAlign] = field(
        default_factory=lambda: ["left", "right"]
    )
    line_offsets_x: list[int] = field(default_factory=list)
    """各布局行位的额外水平偏移（px）；缺省为 0，正值向右。"""
    letter_spacing_px: Optional[int] = None
    space_width_percent: Optional[int] = None
    """空格宽度（占字号百分比）的布局级覆盖；``None`` 继承全局 ``Style``。"""
    force_top_bottom_n3: Optional[bool] = None
    """单行底部页是否启用 N3「强制顶底」行位机制；``None`` 继承全局 ``Style``。"""
    allow_biting: Optional[bool] = None
    ruby_interval_px: Optional[int] = None
    ruby_alignment: Optional[RubyAlignment] = None
    ruby_gap_px: Optional[int] = None


LYRICS_LAYOUT_GEOMETRY_FIELDS: tuple[str, ...] = (
    "line_y_position",
    "line_y_margin_px",
    "line_gap_px",
    "smart_horizontal",
    "horizontal_margin_px",
    "line_alignments",
    "line_offsets_x",
    "force_top_bottom_n3",
)

LYRICS_LAYOUT_CHAR_FIELDS: tuple[str, ...] = (
    "letter_spacing_px",
    "space_width_percent",
    "allow_biting",
    "ruby_interval_px",
    "ruby_alignment",
    "ruby_gap_px",
)

LYRICS_LAYOUT_FIELDS: tuple[str, ...] = (
    *LYRICS_LAYOUT_GEOMETRY_FIELDS,
    *LYRICS_LAYOUT_CHAR_FIELDS,
)
"""布局对象可作用到 ``Style`` 上的全部字段（不含 ``name``）。"""


@dataclass
class SubtitleStyleScheme:
    """字幕 tab 的完整视觉方案；不包含位置、布局和显示时间。"""

    font_family: Optional[str] = None
    font_family_latin: Optional[str] = None
    font_size_px: Optional[int] = None
    letter_spacing_px: Optional[int] = None
    space_width_percent: Optional[int] = None
    latin_font_size_px: Optional[int] = None
    latin_font_weight: Optional[int] = None
    latin_font_stretch_pct: Optional[int] = None
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None
    allow_biting: Optional[bool] = None
    font_weight: Optional[int] = None
    italic: Optional[bool] = None
    latin_italic: Optional[bool] = None
    ruby_italic: Optional[bool] = None
    ruby_latin_italic: Optional[bool] = None
    affects_ruby_anchor: Optional[bool] = None
    """Whether glyphs using this scheme contribute to the shared ruby baseline."""
    base_color: Optional[str] = None
    fill_color: Optional[str] = None
    fill_gradient_enabled: Optional[bool] = None
    fill_gradient_start_color: Optional[str] = None
    fill_gradient_end_color: Optional[str] = None
    fill_gradient_angle_deg: Optional[int] = None
    stroke_color: Optional[str] = None
    stroke_width_px: Optional[int] = None
    stroke2_enabled: Optional[bool] = None
    stroke2_width_px: Optional[int] = None
    decoration_kind: Optional[DecorationKind] = None
    decoration_kind_before: Optional[DecorationKind] = None
    decoration_before_sync: Optional[bool] = None
    """走字前装饰同步开关；``None`` 继承全局 ``Style`` 设置。"""
    glow_radius_px: Optional[int] = None
    glow_before_radius_px: Optional[int] = None
    glow_after_radius_px: Optional[int] = None
    glow_concentration_level: Optional[int] = None
    glow_before_concentration_level: Optional[int] = None
    """走字前发光浓度；``None`` = 跟随 ``glow_concentration_level``。"""
    shadow_color: Optional[str] = None
    shadow_offset_x: Optional[int] = None
    shadow_offset_y: Optional[int] = None
    shadow_before_offset_x: Optional[int] = None
    shadow_before_offset_y: Optional[int] = None
    """走字前阴影偏移；``None`` = 跟随 ``shadow_offset_x/y``。"""
    ruby_font_size_px: Optional[int] = None
    ruby_font_family: Optional[str] = None
    ruby_font_family_latin: Optional[str] = None
    ruby_font_weight: Optional[int] = None
    ruby_latin_font_size_px: Optional[int] = None
    ruby_latin_font_weight: Optional[int] = None
    ruby_latin_font_stretch_pct: Optional[int] = None
    ruby_font_follow_main: Optional[bool] = None
    ruby_color: Optional[str] = None
    ruby_gap_px: Optional[int] = None
    ruby_stroke_width_px: Optional[int] = None
    ruby_stroke2_enabled: Optional[bool] = None
    ruby_stroke2_width_px: Optional[int] = None
    ruby_latin_stroke_width_px: Optional[int] = None
    ruby_latin_stroke2_enabled: Optional[bool] = None
    ruby_latin_stroke2_width_px: Optional[int] = None
    ruby_decoration_kind: Optional[DecorationKind] = None
    ruby_decoration_kind_before: Optional[DecorationKind] = None
    """走字前注音装饰类型；``None`` = 跟随主文字/注音走字后槽。"""
    ruby_glow_radius_px: Optional[int] = None
    ruby_glow_before_radius_px: Optional[int] = None
    ruby_glow_after_radius_px: Optional[int] = None
    ruby_glow_concentration_level: Optional[int] = None
    ruby_glow_before_concentration_level: Optional[int] = None
    ruby_shadow_offset_x: Optional[int] = None
    ruby_shadow_offset_y: Optional[int] = None
    ruby_shadow_before_offset_x: Optional[int] = None
    ruby_shadow_before_offset_y: Optional[int] = None
    karaoke_colors: Optional[KaraokeColors] = None
    ruby_colors_follow_main: Optional[bool] = None
    """注音配色是否实时跟随本角色方案的主文字配色。"""
    ruby_horizontal_gradient_with_main: Optional[bool] = None
    """注音横向渐变是否与主文字共享整行渐变范围。"""
    ruby_karaoke_colors: Optional[KaraokeColors] = None
    n3_font_inheritance: bool = False
    """N3 子字体槽的 ``None`` 属于方案内 fallback，不继承外部全局方案。"""


N3_FONT_INHERITANCE_FIELDS: tuple[str, ...] = (
    "font_family_latin",
    "latin_font_size_px",
    "latin_font_weight",
    "latin_italic",
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "ruby_font_family",
    "ruby_font_weight",
    "ruby_italic",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_font_family_latin",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_latin_italic",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
)


PRESET_REFERENCE_HEIGHT = 1080
"""样式预设库存放像素字段的统一基准高度。

预设库是跨项目共享的应用级数据，保存时统一把字号等像素字段换算到该高度；
应用时再按目标项目的输出高度换算回去。每条预设用 ``reference_height``
记录自身像素实际对应的高度，因此该基准将来变化也不会让旧预设失配。
"""


@dataclass
class StylePreset:
    """应用级可复用的单目标字幕样式预设。

    ``preset_id`` 是预设库中的稳定标识；``(group, name)`` 在库内唯一，因此
    不同分组可以保存同名预设。工程角色使用时会深拷贝 ``scheme``，因此预设
    的重命名、分组或删除不会反向影响工程。
    """

    name: str
    group: str = ""
    scheme: SubtitleStyleScheme = field(default_factory=SubtitleStyleScheme)
    preset_id: str = ""
    # N3 templates retain their original payload so sizes can be resolved
    # again for the target project's output height when the preset is used.
    source_type: str = ""
    source_data: dict[str, Any] = field(default_factory=dict)
    reference_height: int = PRESET_REFERENCE_HEIGHT
    """``scheme`` 像素字段对应的输出高度。

    用户保存的预设统一归一化到 :data:`PRESET_REFERENCE_HEIGHT`；N3 模板预设
    的 ``scheme`` 只是缓存，该字段记录解析时的目标高度，权威值仍在 payload。
    缺省 ``1080`` 同时充当旧库数据（从未记录高度）的兜底。
    """


def default_title_layout() -> LyricsLayout:
    """内置标题布局（N3 出厂预设「タイトル左上」：Top、行間 15、余白 50/50、Left）。"""
    return LyricsLayout(
        name=TITLE_LAYOUT_NAME,
        layout_id="title-default",
        line_y_position="top",
        line_y_margin_px=50,
        line_gap_px=15,
        smart_horizontal="equal_margins",
        horizontal_margin_px=50,
        line_alignments=["left"],
    )


def title_fallback_layout(style: "Style") -> LyricsLayout:
    """悬空标题布局引用的统一回退。

    优先取布局库里的出厂「タイトル左上」；库缺失（手改 JSON / 外部清库）
    时直接取代码内置定义。渲染与标题卡片下拉都走这里，保证显示与实际
    渲染同口径。
    """
    for layout in style.layouts:
        if layout.name == TITLE_LAYOUT_NAME:
            return layout
    return default_title_layout()


DEFAULT_LAYOUT_BY_ROW_COUNT: dict[int, str] = {
    1: "builtin-1",
    2: "default",
    3: "builtin-3",
    4: "builtin-4",
    5: "builtin-5",
    6: "builtin-6",
    7: "builtin-7",
    8: "builtin-8",
}

_DEFAULT_PAGE_LAYOUT_SPECS: dict[int, tuple[list[HorizontalAlign], int]] = {
    1: (["center"], 90),
    2: (["left", "right"], 90),
    3: (["left", "center", "right"], 60),
    4: (["left", "right", "left", "right"], 40),
    5: (["left", "right", "left", "right", "left"], 25),
    6: (["left", "right", "left", "right", "left", "right"], 15),
    7: (["left", "right", "left", "right", "left", "right", "left"], 8),
    8: (["left", "right", "left", "right", "left", "right", "left", "right"], 0),
}


def default_page_layouts() -> list[LyricsLayout]:
    """Return title plus the built-in 1/3..8-line project layouts.

    The two-line project default is represented by ``Style`` itself (stable ID
    ``default``), so no extra two-line object is inserted and legacy indices do
    not gain a duplicate default.
    """

    layouts = [default_title_layout()]
    for rows, (alignments, gap) in _DEFAULT_PAGE_LAYOUT_SPECS.items():
        if rows == 2:
            continue
        layouts.append(
            LyricsLayout(
                name=f"{rows} 行布局",
                layout_id=f"builtin-{rows}",
                line_gap_px=gap,
                line_alignments=list(alignments),
            )
        )
    return layouts


def _title_karaoke_colors(
    fill: PaintFill, stroke: PaintFill, stroke2: PaintFill, shadow: PaintFill
) -> KaraokeColors:
    """标题永不走字，走字前/走字后同色，编辑任一态都能看到效果。"""
    state = KaraokeColorState(text=fill, stroke=stroke, stroke2=stroke2, shadow=shadow)
    return KaraokeColors(before=state, after=deepcopy(state))


def title_scheme_from_overlay(title: "TitleOverlay") -> SubtitleStyleScheme:
    """把（旧工程的）``TitleOverlay`` 显式外观字段折算成「标题」配色方案。"""
    return SubtitleStyleScheme(
        font_family=title.font_family,
        # 标题方案必须自包含。N3 标题没有另设英数页时，英数跟随标题自身的
        # 日文字体，而不是继承项目全局歌词的英数字体。
        font_family_latin=title.font_family_latin or title.font_family,
        font_size_px=title.font_size_px,
        latin_font_size_px=title.font_size_px,
        font_weight=title.font_weight,
        latin_font_weight=title.font_weight,
        latin_font_stretch_pct=title.latin_font_stretch_pct,
        italic=title.italic,
        letter_spacing_px=title.letter_spacing_px,
        stroke_width_px=title.stroke_width_px,
        latin_stroke_width_px=title.stroke_width_px,
        stroke2_enabled=title.stroke2_width_px > 0,
        latin_stroke2_enabled=title.stroke2_width_px > 0,
        stroke2_width_px=title.stroke2_width_px,
        latin_stroke2_width_px=title.stroke2_width_px,
        decoration_kind=title.decoration_kind,
        glow_radius_px=title.glow_radius_px,
        glow_before_radius_px=title.glow_radius_px,
        glow_after_radius_px=title.glow_radius_px,
        glow_concentration_level=title.glow_concentration_level,
        shadow_offset_x=title.shadow_offset_x,
        shadow_offset_y=title.shadow_offset_y,
        karaoke_colors=_title_karaoke_colors(
            title.fill, title.stroke, title.stroke2, title.shadow
        ),
    )


def default_title_scheme() -> SubtitleStyleScheme:
    """默认「标题」方案：目标 N3 项目 ``Dark spiral journey`` 的「情報小」。"""
    before = KaraokeColorState(
        text=_paint_fill("#EBEBEB"),
        stroke=_paint_fill("#000000"),
        stroke2=_paint_fill("#FFFFFF"),
        shadow=_paint_fill("#FFFFFF"),
    )
    after = KaraokeColorState(
        text=_paint_fill("#EBEBEB"),
        stroke=_paint_fill("#000000"),
        stroke2=_paint_fill("#000000"),
        shadow=_paint_fill("#FFFFFF"),
    )
    colors = KaraokeColors(before=before, after=after)
    return SubtitleStyleScheme(
        font_family="UD デジタル 教科書体 N-B",
        font_family_latin="Comic Sans MS",
        font_size_px=40,
        letter_spacing_px=0,
        latin_font_size_px=40,
        latin_font_weight=700,
        latin_font_stretch_pct=100,
        latin_stroke_width_px=5,
        latin_stroke2_enabled=False,
        latin_stroke2_width_px=5,
        font_weight=700,
        italic=False,
        base_color="#EBEBEB",
        fill_color="#EBEBEB",
        stroke_color="#000000",
        stroke_width_px=5,
        stroke2_enabled=False,
        stroke2_width_px=5,
        decoration_kind="glow",
        glow_radius_px=2,
        glow_before_radius_px=2,
        glow_after_radius_px=2,
        glow_concentration_level=0,
        shadow_color="#FFFFFF",
        ruby_font_size_px=45,
        ruby_font_family="UD デジタル 教科書体 N-B",
        ruby_font_family_latin="UD デジタル 教科書体 N-B",
        ruby_font_weight=700,
        ruby_latin_font_size_px=45,
        ruby_latin_font_weight=700,
        ruby_font_follow_main=False,
        ruby_color="#EBEBEB",
        ruby_stroke_width_px=10,
        ruby_stroke2_enabled=False,
        ruby_stroke2_width_px=3,
        ruby_latin_stroke_width_px=10,
        ruby_latin_stroke2_enabled=False,
        ruby_latin_stroke2_width_px=3,
        ruby_colors_follow_main=False,
        karaoke_colors=deepcopy(colors),
        ruby_karaoke_colors=deepcopy(colors),
    )


def migrate_legacy_app_title_default(style: "Style") -> "Style":
    """只迁移应用级旧内置标题；项目/N3 显式标题由调用方保留原样。"""
    scheme = style.custom_style_schemes.get(TITLE_SCHEME_NAME)
    colors = scheme.karaoke_colors if scheme is not None else None
    if not (
        scheme is not None
        and scheme.font_family == "游明朝"
        and scheme.font_size_px == 100
        and scheme.font_weight == 400
        and scheme.stroke_width_px == 15
        and scheme.stroke2_enabled is True
        and scheme.stroke2_width_px == 5
        and scheme.decoration_kind == "glow"
        and scheme.glow_before_radius_px == 10
        and colors is not None
        and colors.before.text.color == "#FFEBEB"
        and colors.before.stroke.color == "#000000"
        and colors.before.stroke2.color == "#FFFFFF"
        and colors.before.shadow.color == "#E19696"
    ):
        return style
    schemes = dict(style.custom_style_schemes)
    schemes[TITLE_SCHEME_NAME] = default_title_scheme()
    return replace(style, custom_style_schemes=schemes)


@dataclass(frozen=True)
class StyleTimingConfig:
    """Nested timing/animation view over the legacy flat :class:`Style`."""

    line_lead_in_ms: int
    line_tail_ms: int
    line_protect_ms: int
    entry_anim_protect_ms: int
    exit_anim_protect_ms: int
    timing_offset_ms: int
    ruby_main_progress_mode: RubyMainProgressMode
    line_lane_gap_ms: int
    section_gap_ms: int
    sync_entry: bool
    sync_ending: bool
    allow_entry_exit_animation_overlap: bool
    allow_inter_page_line_overlap: bool
    overlap_fallback_mode: OverlapFallbackMode
    sync_each_page: bool
    auto_fill_section_time: bool
    section_ending_mode: SectionEndingMode
    entry_anim: EntryAnimation
    entry_lead_ms: int
    exit_anim: ExitAnimation
    exit_fade_ms: int
    karaoke_anim: KaraokeAnimation
    reverse_karaoke_anim: KaraokeAnimation
    section_edge_anim_enabled: bool
    section_edge_both_animations: bool
    section_head_anim: EntryAnimation
    section_tail_anim: ExitAnimation


_STYLE_TIMING_FIELDS = tuple(field.name for field in fields(StyleTimingConfig))


TRACK_TIMING_FIELDS: tuple[str, ...] = (
    "line_lead_in_ms",
    "line_tail_ms",
    "timing_offset_ms",
    "section_ending_mode",
    "line_lane_gap_ms",
    "line_protect_ms",
    "entry_anim_protect_ms",
    "exit_anim_protect_ms",
    "sync_entry",
    "sync_ending",
    "sync_each_page",
    "allow_entry_exit_animation_overlap",
    "allow_inter_page_line_overlap",
    "overlap_fallback_mode",
    "auto_fill_section_time",
    "ruby_main_progress_mode",
)
"""时间卡片可按字幕轴覆盖的字段（``_STYLE_TIMING_FIELDS`` 子集）。

UI 路由、主轴值快照与 ``display_timing.overrides`` 的持久化校验共用这一
份清单；分段阈值（``section_gap_ms``）归每源加载设置，不在此列。"""


@dataclass
class Style:
    """字幕样式（A4 / A5 / A6 实装的纯色 + 横书き子集）。

    字段默认值面向 NicoKaraMaker 风格：教科书体 + 小字号 + 双行底部布局。
    后续 A5 / A6 / B3 等任务在此基础上扩字段（渐变 / 发光 / 注音 / 动画）。
    """

    # Internal layout contract.  Existing projects deliberately keep the
    # product's established geometry; direct N3 imports opt into the isolated
    # 10.74-compatible path without exposing an engine label in the UI.
    layout_semantics: LayoutSemantics = "legacy"

    # 字体
    font_family: str = "UD デジタル 教科書体 N-B"
    font_family_latin: Optional[str] = None
    """英数（ASCII）字体；为空时英数与日文共用 ``font_family``。"""
    font_size_px: int = 100
    letter_spacing_px: int = 0
    """NicokaraMaker3 ``LyricsInterval`` default: 0 px."""
    space_width_percent: int = 20
    """空格宽度占字号的百分比；20% 对齐 NicokaraMaker3 默认值。"""

    # 英数（ASCII）轨的可选覆盖：``None`` 实时跟随日文轨对应字段。
    # 字体族沿用历史字段 ``font_family_latin``（同一语义）。
    latin_font_size_px: Optional[int] = None
    latin_font_weight: Optional[int] = None
    latin_font_stretch_pct: int = 100
    """Latin glyph width percentage; 100 preserves the font's natural width."""
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None

    allow_biting: bool = False
    """允许负 side bearing 令相邻字形咬合。"""

    allow_inter_page_line_overlap: bool = False
    """允许不同页面的字幕行保持旧式重叠行为。

    关闭时，渲染器按最终主文字像素范围压缩冲突时间，仍无法消除时移动后进入的
    整页字幕；开启时跳过这两类跨页避让。主轨取本字段全局值，副字幕源可在
    时间卡片按轴覆盖（``TRACK_TIMING_FIELDS``）。不属于 ``LyricsLayout``，
    也不随分页布局预设切换。
    """

    overlap_fallback_mode: OverlapFallbackMode = "displace"
    """「重叠设置」：时间压缩消耗完两侧底线仍无法消除跨页冲突时的策略。

    ``displace``（默认）由将要演唱的下一句直接顶掉还在走字的上一句（允许
    吃掉走字时长）。被顶掉的句子不是瞬间消失：按其「出场动画保护时间」播放
    退场动画，动画恰好在下一句上屏时刻结束（走字显示到退场开始为止，退场
    动画充当交接过渡）。手工拖过消失时间的句子不参与自动压缩，时间数据保持
    原值，顶掉只发生在渲染层。该模式不做页面平移避让（抬升画面）；ForceBottom
    行位上移照常保留。``lift`` 为旧行为：抬升后进入的整页字幕（空间避让）。
    仅在 ``allow_inter_page_line_overlap`` 关闭时参与解算。主轨取本字段
    全局值，副字幕源可在时间卡片按轴覆盖（``TRACK_TIMING_FIELDS``）。
    """

    font_weight: int = 400  # Qt 习惯 100-900
    italic: bool = False
    """主文字·日文槽的斜体（四个字体槽的主组）。

    其余三个槽的斜体沿 ``latin_italic`` / ``ruby_italic`` /
    ``ruby_latin_italic`` 独立控制；``None`` = 跟随上一级（与描边 2 开关
    同一条跟随链：英数→主文字日文、注音日文→主文字、注音英数→注音日文）。
    旧工程只存 ``italic``：三个新槽缺省 ``None`` = 三跟随，原值自然落在
    日文-主文字组，画面逐字节不变。
    """
    latin_italic: Optional[bool] = None
    ruby_italic: Optional[bool] = None
    ruby_latin_italic: Optional[bool] = None
    affects_ruby_anchor: bool = True
    """该样式字符是否参与整行统一 ruby 基线的高度计算。"""

    # 颜色（六位十六进制 #RRGGBB，含前缀 #）
    base_color: str = "#FFFFFF"
    """未唱状态填充色（底色）。"""

    fill_color: str = "#FF5A6F"
    fill_gradient_enabled: bool = False
    fill_gradient_start_color: str = "#FF5A6F"
    fill_gradient_end_color: str = "#0055FF"
    fill_gradient_angle_deg: int = 0
    """已唱状态填充色。默认取工作台主色。"""

    stroke_color: str = "#222222"
    stroke_width_px: int = 15
    stroke2_enabled: bool = True
    stroke2_width_px: int = 5

    decoration_kind: DecorationKind = "shadow"
    decoration_kind_before: Optional[DecorationKind] = None
    """走字前装饰类型；``None`` = 跟随 ``decoration_kind``（旧工程/未设置）。"""
    decoration_before_sync: bool = True
    """颜色卡「装饰 · 同步」开关：开启时前后共用装饰类型/浓度/阴影 X/Y。

    打开时以走字前当前值为源复制到走字后，之后任一侧编辑同步两侧；
    发光宽度不参与同步。旧工程加载时置 False。
    """
    glow_radius_px: int = 10
    glow_before_radius_px: int = 10
    glow_after_radius_px: int = 10
    glow_concentration_level: int = 0
    """-1 disables glow; NicoKaraMaker3 ``BlurLevel`` 0/1/2 = low/medium/high."""
    glow_before_concentration_level: Optional[int] = None
    """走字前发光浓度；``None`` = 跟随 ``glow_concentration_level``。"""
    shadow_color: str = "#000000"
    shadow_offset_x: int = 5
    """阴影 X 偏移。N3 阴影偏移固定为 DecorSize（双轴同值），1080 下默认 5。"""
    shadow_offset_y: int = 5
    shadow_before_offset_x: Optional[int] = None
    shadow_before_offset_y: Optional[int] = None
    """走字前阴影偏移；``None`` = 跟随 ``shadow_offset_x/y``。"""
    karaoke_colors: Optional[KaraokeColors] = None

    singer_style_overrides: dict[int, SubtitleStyleScheme] = field(default_factory=dict)
    """B2：按歌手自动套用的字幕 tab 方案。不覆盖位置、时间或布局。"""

    custom_style_schemes: dict[str, SubtitleStyleScheme] = field(
        default_factory=lambda: {TITLE_SCHEME_NAME: default_title_scheme()}
    )
    """用户自行添加的配色方案。当前用于编辑/复用，后续可接入方案分配。
    内置「标题」方案（``TITLE_SCHEME_NAME``）描述标题外观，随字体页统一编辑。"""

    font_reference_height: int = 1080
    """字体视觉像素字段当前对应的输出高度。

    与 N3 ``SizeAndRatio.Reference`` 一致：输出高度变化时，字号、描边、发光和
    阴影偏移按比例重算；字体族、字重、颜色等非像素参数保持不变。
    """

    # ふりがな / ruby（B1）
    ruby_font_size_px: int = 45
    ruby_font_family: Optional[str] = None
    ruby_font_family_latin: Optional[str] = None
    ruby_font_weight: Optional[int] = None
    ruby_latin_font_size_px: Optional[int] = None
    ruby_latin_font_weight: Optional[int] = None
    ruby_latin_font_stretch_pct: Optional[int] = None
    ruby_font_follow_main: bool = True
    """注音字体族/字重跟随主文字；注音字号始终由独立字段控制。"""
    ruby_color: str = "#FF5A6F"
    ruby_gap_px: int = 0
    """NicokaraMaker3 ``LyricsAndRubyInterval`` default: 0 px."""
    ruby_interval_px: int = 0
    """NicokaraMaker3 ``RubyInterval``：注音字符间最小间距，可为负。

    非负时布局逐对保证相邻注音的描边（含二重描边）不互相叠压；
    为负时视为用户主动压紧，不再做描边避让（允许叠压）。
    """
    ruby_alignment: RubyAlignment = "auto"
    """注音相对正文范围的排布（N3 ``RubyAlignment``）：``auto`` = 正文或注音的
    拉丁字母数字多于其他可见字符、或全部可见字符均为符号时居中，否则均等
    分布（含符号的英数读音如 ``e-bay`` 也居中，不再被均分撑开）；
    ``center`` = 整组居中；``equal_space`` = 均等分布。"""
    ruby_stroke_width_px: Optional[int] = 10
    ruby_stroke2_enabled: Optional[bool] = True
    """注音描边 2 开关；``None`` 为未设定（面板半选，跟随主文字开关）。"""
    ruby_stroke2_width_px: Optional[int] = 3
    ruby_latin_stroke_width_px: Optional[int] = None
    ruby_latin_stroke2_enabled: Optional[bool] = None
    ruby_latin_stroke2_width_px: Optional[int] = None
    ruby_decoration_kind: Optional[DecorationKind] = None
    ruby_decoration_kind_before: Optional[DecorationKind] = None
    """走字前注音装饰类型；``None`` = 跟随主文字走字前/注音走字后槽。"""
    ruby_glow_radius_px: Optional[int] = None
    ruby_glow_before_radius_px: Optional[int] = None
    ruby_glow_after_radius_px: Optional[int] = None
    ruby_glow_concentration_level: Optional[int] = None
    """Optional ruby override; ``None`` inherits ``glow_concentration_level``."""
    ruby_glow_before_concentration_level: Optional[int] = None
    ruby_shadow_offset_x: Optional[int] = None
    ruby_shadow_offset_y: Optional[int] = None
    ruby_shadow_before_offset_x: Optional[int] = None
    ruby_shadow_before_offset_y: Optional[int] = None
    ruby_colors_follow_main: bool = True
    """注音整套配色默认实时跟随主文字；角色方案可独立覆盖。"""
    ruby_horizontal_gradient_with_main: bool = True
    """注音横向渐变默认与主文字共享整行渐变范围。"""
    ruby_karaoke_colors: Optional[KaraokeColors] = None
    """注音独立配色矩阵；为空时退回 ``ruby_color`` / 主文字配色。可由「应用主文字
    配色」一键从主文字矩阵复制（颜色照搬，描边宽度/阴影偏移在渲染时按注音字号比例缩放）。"""

    # 视图（整体字幕层 2D 变换，对标 Sayatoo「视图」组）
    viewport_align: ViewportAlign = "center"
    """缩放与旋转的锚点（九宫格）。仅在缩放≠100% 或旋转≠0 时影响画面。"""

    viewport_offset_x: int = 0
    """整体字幕层水平位移，正值向右。"""

    viewport_offset_y: int = 0
    """整体字幕层垂直位移，正值向下。"""

    viewport_scale_pct: int = 100
    """整体字幕层缩放百分比，围绕 ``viewport_align`` 锚点。"""

    viewport_rotation_deg: int = 0
    """整体字幕层 Z 轴旋转角度，围绕 ``viewport_align`` 锚点，顺时针为正。"""

    # 行位置（字幕区上下定位）
    line_y_position: LineYPosition = "bottom"
    """``"top"`` / ``"center"`` / ``"bottom"`` —— 简单 vertical-anchor。"""

    line_y_margin_px: int = 60
    """``line_y_position`` 为 ``"top"`` / ``"bottom"`` 时距离顶/底边的内边距。"""

    dual_line_layout: bool = True
    """默认上下双行显示：当前行在上，下一行在下。"""

    line_horizontal_layout: LineHorizontalLayout = "asymmetric"
    """双行水平布局：``asymmetric`` 为上左下右，``center`` 为两行居中，
    ``per_row`` 为逐行独立对齐 + X/Y（对标 Sayatoo「布局」第一行 / 第二行）。"""

    line_gap_px: int = 45
    """双行布局中两行主文字外框之间的间距，不包含 ruby 高度。"""

    line_alignments: list[HorizontalAlign] = field(
        default_factory=lambda: ["left", "right"]
    )
    """每行（lane）的水平对齐列表（N3 ``HorizontalAlignments``），仅 ``asymmetric``
    模式使用。列表长度即多行显示的行数，索引 0 = 最上行。显示行数恒等于列表
    长度，因此 Bottom 锚定的「从下往上取列表末尾」与正序索引等价。"""
    line_offsets_x: list[int] = field(default_factory=list)
    """各布局行位的额外水平偏移（px）；缺省为 0，正值向右。"""

    horizontal_margin_px: int = 50
    """左右余白（N3 ``HorizontalMargin``）：Left 行左缘贴此值，Right 行右缘贴
    ``width - 此值``。"""

    upper_line_left_margin_px: int = 50
    """【旧字段】双行布局上排左边距。已由 ``horizontal_margin_px`` 取代，保留用于
    旧工程迁移与 native 后端（C++ 仍读取该键）序列化兼容。"""

    lower_line_right_margin_px: int = 50
    """【旧字段】双行布局下排右边距。同上，保留序列化兼容。"""

    smart_horizontal: SmartHorizontal = "equal_margins"
    """智能水平配置（N3 ``SmartHorizon``，仅 ``asymmetric`` 双行布局）：短行向中央
    收拢。``none`` = 不调整（同时关闭单行页居中）；``center_position`` =
    中心位置对齐（逐行判断，N3 Single）；``equal_margins`` = 左右余白对齐
    （整页判断，N3 Multi，N3 默认）。"""

    force_top_bottom_n3: bool = False
    """单行底部对齐页的 N3「强制顶底」行位机制（默认关）：开启时先把孤行强制到
    最下行，与上一页同位行冲突时再上移一行；关闭时孤行直接显示在天然
    行位（T1），不做任何强制/上移。N3 导入（``n3_1074``）固定开启以对齐
    N3 的固有行为（见逆向文档「单行页在 Bottom 模式」一节）。"""

    layouts: list["LyricsLayout"] = field(default_factory=default_page_layouts)
    """额外的命名布局定义（N3 ``LyricsLayouts``）。``Style`` 自身的布局字段是
    「默认布局」（index 0），本列表从 index 1 起被 ``TimingLine.layout_index``
    引用。布局定义是可复用预设，随全局设置与项目文件一起持久化。
    默认内置「タイトル左上」（对齐 N3 出厂预设），供标题引用。"""

    default_layout_by_row_count: dict[int, str] = field(
        default_factory=lambda: dict(DEFAULT_LAYOUT_BY_ROW_COUNT)
    )
    """Page row count to stable layout ID mapping.  Keys are always 1..8."""

    layout_reference_height: int = 1080
    """布局像素字段（上下余白 / 行间距 / 左右余白）当前对应的输出高度
    （N3 ``SizeAndRatio.Reference``）。输出高度变化时按比例重算并更新此值。"""

    # 逐行独立布局（per_row 模式，对标 Sayatoo「布局」第一行 / 第二行）
    # 对齐决定该行的水平锚点（left=贴左 / center=居中 / right=贴右），
    # offset_x/y 为锚点之上的像素位移，正值向右 / 向下。
    row1_align: HorizontalAlign = "left"
    row1_offset_x: int = 50
    row1_offset_y: int = 0
    row2_align: HorizontalAlign = "right"
    row2_offset_x: int = -50
    row2_offset_y: int = 0

    right_to_left: bool = False
    """从右到左排版（对标 Sayatoo layout.right_to_left）：字符自右向左排布，
    卡拉ok 扫光从右向左推进；注音定位、读音字形顺序（含小书き假名）与扫光方向均随之反转。"""

    vertical: bool = False
    """竖排（縦書き，对标 Sayatoo layout.vertical）：字符上→下堆叠成列、卡拉ok 扫光
    上→下、注音排在右侧、双行变右→左双列；旋转类字形(ー/括号/箭头)按 Unicode UTR#50
    旋转 90°，标点(、。)移到右上、小书き假名右上偏移。竖排时 ``right_to_left`` 被忽略。"""

    line_lead_in_ms: int = 1800
    """理想表示开始 = 歌唱开始前的毫秒数；填充仍从真实字符时间开始。"""

    line_tail_ms: int = 1000
    """表示结束至少延续到同组两行歌唱结束后的毫秒数。"""

    line_protect_ms: int = 0
    """同 lane 冲突挤压时保留的显示时间；0 表示按 lead/tail 与退场动画自动计算。"""

    entry_anim_protect_ms: int = 250
    """「入场动画保护时间」：自动压缩时入场动画最多被压到这么短（渲染端
    按窗口加速播放整段动画）；0 = 不设下限，入场动画可以被完全压掉。
    实际压缩底线为 max(本值, ``line_protect_ms``)。"""

    exit_anim_protect_ms: int = 100
    """「出场动画保护时间」：自动压缩时退场动画至少保留的可见时长；
    0 = 不设下限。实际压缩底线为 max(本值, ``line_protect_ms``)。"""

    timing_offset_ms: int = 0
    """字幕整体时间偏移。正值延后显示，负值提前显示。"""

    ruby_main_progress_mode: RubyMainProgressMode = "checkpoint_segments"
    """带注音正文的走字切分方式。

    正文组内有显式时间边界时，两种模式都保留正文逐字时钟；仅在内部边界缺失时，
    ``checkpoint_segments`` 按注音内部时间点形成的时间段数均分正文，
    ``reading_units`` 按注音可视字符数映射正文字符。
    """

    line_lane_gap_ms: int = 300
    """同一显示 lane 上相邻两句之间保留的时间间隔。"""

    # 分段与页级同步入退场。
    # Sayatoo 用手动信号划段落；LRC 无信号，这里改为按间奏间隔自动分段。
    section_gap_ms: int = 4000
    """自动分段阈值：相邻两句演唱空隙（间奏）超过此值即开新段落。"""

    sync_entry: bool = True
    """同步入场：先取同步页最长延长候选，再逐个压缩实际碰撞的行。"""

    sync_ending: bool = False
    """同步退场：先取同步页最长延长候选，再逐个压缩实际碰撞的行。"""

    allow_entry_exit_animation_overlap: bool = False
    """允许相邻页面的入场和退场动画在时间上重叠。"""

    sync_each_page: bool = False
    """每句同步：开启时每页同步；关闭时仅同步段首入场和段尾退场。"""

    auto_fill_section_time: bool = True
    """自动填充段内时间：按相邻页对应高度延长退场，段尾填充到本页结束。"""

    section_ending_mode: SectionEndingMode = "hold"
    """段落结束行为：``hold`` 维持现状（按 N3 TopLong 挂到段末）；``clear`` 段末即
    清屏，字幕不拖进间奏。"""

    entry_anim: EntryAnimation = "fade"
    """入场动画：none / fade / slide_in / rise / char_fade / char_drip / spin_flip / utopia
    / tracking_in（字距收拢）/ wave_in（波浪上浮）。"""

    entry_lead_ms: int = 300
    """入场动画时长；不改变歌词填色时间，只影响显示窗口起点后的过渡。"""

    exit_anim: ExitAnimation = "fade"
    """退场动画：none / fade / slide_out / rise / char_fade / char_drip / spin_flip / utopia
    / scatter_out（碎散爆开）/ converge_out（收拢消散）。"""

    exit_fade_ms: int = 300
    """退场动画时长；在显示窗口结束前开始。"""

    karaoke_anim: KaraokeAnimation = "utopia"
    """唱字动画：inherit（兼容旧 Utopia）/ none / utopia / zoom_pulse。

    扫字线是独立叠加特效，由 :attr:`scanline_enabled` 总开关控制，
    不再占用唱字动画档位。``zoom_pulse`` 整字放大：唱字期间持续缓出放大、
    唱字结束后缓入缩回。"""

    reverse_karaoke_anim: KaraokeAnimation = "inherit"
    """反向走字行的唱字动画；inherit 表示沿用普通唱字特效。

    是否叠加扫字线由 :attr:`scanline_reverse_enabled` 单独控制。"""

    # 扫字线（Sayatoo 式走字锋面高亮）：由独立总开关控制，是叠加在基础
    # 唱字动画上的绘制参数，不再占用 karaoke_anim 档位。
    scanline_enabled: bool = False
    """扫字线总开关（音量柱/指示灯同语义）：开启后普通唱字行叠加锋面高亮。"""

    scanline_reverse_enabled: bool = False
    """扫字线是否应用于反向唱字行；仅在 ``scanline_enabled`` 开启时生效。

    与 :attr:`scanline_forward_enabled` 组合成三态：仅正向 / 仅反向 / 全部。
    """

    scanline_forward_enabled: bool = True
    """扫字线是否应用于普通（正向）唱字行。"""

    scanline_legacy_global: bool = field(
        default=False, repr=False, compare=False
    )
    """内部迁移标记：本样式读自旧扫字线档位，供逐行关闭迁移判定。"""

    scanline_width_px: int = 16
    """扫字线粗细（像素，``SCANLINE_BASE_HEIGHT``=1080 基准）：以走字锋面为
    中心的高亮带宽度。存储恒为 1080 画布下的值；编辑 spin 显示与渲染消费
    都按当前输出高度从基准值映射（见 ``scanline_px_for_output``）。"""

    scanline_mode: str = "color"
    """扫字线模式：``default`` 稳定回到出厂默认（火焰=标准火色阶、水泡=
    水蓝高光，不受``scanline_color``残留影响）；``color`` 单独颜色
    （``scanline_color`` 填充）；
    ``brighten`` 底色发光——分别保留锋面两侧原有前后色的 HSV 色相与饱和度，
    只提高明度；提升幅度由 ``scanline_brightness_pct`` 控制；
    ``follow_before`` / ``follow_after`` 跟随字体——与底色发光同一条通路
    （当前行实际配色、全层重绘、共用亮度提升），但整条高亮带固定用
    走字前 / 走字后那一态的颜色，而不是锋面两侧各用各的；
    ``role`` 复用配色方案——整条高亮带用 ``scanline_role_name`` 指定来源
    （``__global__`` 全局默认 / 角色或「标题」方案）的「走字后-主文字」
    填充（支持渐变/拼色/图片），仅重绘字形填充层。"""

    scanline_color: str = "#FFFFFF"
    """扫字线高亮颜色（#RRGGBB）；仅 ``scanline_mode == "color"`` 时生效。"""

    scanline_role_name: Optional[str] = None
    """复用配色方案模式下引用的来源名：保留键 ``__global__`` 表示全局默认
    （主样式自身的走字后文字填充），其余为 ``custom_style_schemes`` 的键
    （角色名或「标题」）；仅 ``scanline_mode == "role"`` 时生效。来源改名/
    删除时由 ``roleReferencesRemapped`` 维护链同步改写；渲染时名字查不到
    则回退 ``color`` 模式的 ``scanline_color``。"""

    scanline_brightness_pct: int = 60
    """底色发光的亮度提升（百分比）；仅 ``scanline_mode == "brighten"`` 时生效。"""

    scanline_glow_px: int = 8
    """扫字线字形内柔化范围（像素，``SCANLINE_BASE_HEIGHT``=1080 基准，语义
    同 ``scanline_width_px``）；只改变带内透明度，不向字形外扩散。"""

    scanline_effect: ScanlineEffect = "none"
    """扫字线粒子特效：沿走字锋面发射装饰粒子（见 :data:`SCANLINE_EFFECT_KINDS`）。

    樱花 / 羽毛 / 星光 / 音符自锋面后方朝左上飘出（与走字同向的速度感）。
    颜色与尺寸复用「唱字装饰粒子」的旋钮/配色方案；仅在扫字线总开关开启
    且该行应用扫字线时渲染。"""

    scanline_effect_size_pct: int = 30
    """扫字线特效粒子尺寸（相对主文字字号的百分比，30 = 30% 字号）。"""

    scanline_effect_count: int = 4
    """扫字线特效粒子密度：每个字符的基准颗数（长音符按持续时间
    折算，见 ``particles.scanline_particles_per_char``）。"""

    scanline_effect_color_mode: str = "theme"
    """扫字线粒子特效颜色模式：``theme`` 按特效档位主题色（樱花粉/羽毛白/
    雪花蓝白/星光金/音符白粉）；其余与唱字装饰粒子同语义（color /
    sakura / follow_mix / follow_before / follow_after / role）。"""

    scanline_effect_color: str = "#FFB7C5"
    """扫字线粒子颜色一（``scanline_effect_color_mode == "color"``）。"""

    scanline_effect_color2: str = "#FFD7E0"
    """扫字线粒子颜色二（单独颜色档双色随机混发）。"""

    scanline_effect_role_name: Optional[str] = None
    """扫字线粒子 ``role`` 模式引用的配色来源名。"""

    scanline_effect_color_layers: str = "solid"
    """扫字线粒子的取色层级（仅来源配色四档生效）。"""

    scanline_shape: ScanlineShape = "band"
    """扫字线形态：普通高亮带 / 烈焰棒 / 水泡（见 :data:`SCANLINE_SHAPE_KINDS`）。

    烈焰棒/水泡是独立形态并**替换**普通高亮带；烈焰棒颜色沿用扫字线颜色
    方案，水泡从锋面底部发射、固定水蓝白水感。纯绘制参数，不参与布局推导。"""

    zoom_pulse_curve_level: int = 1
    """整字放大缓动档位（0~5）：0=线性；1~5 为缓出/缓入多项式阶数，越大峰值
    停留越久（1 为默认）。纯绘制参数，不参与布局推导。"""

    karaoke_stroke_flash: bool = False
    """唱字描边闪光：每个字唱到的瞬间，描边以白色高亮短暂脉冲后回落。
    可与任意唱字动画叠加；纯绘制参数，不参与布局推导。"""

    entry_fx: EntryFx = "none"
    """入场装饰粒子：none / sparkle（星光闪烁）/ ripple（涟漪光环）。
    锚定行入场窗口，绘制参数见 fx_* 字段。"""

    exit_fx: ExitFx = "none"
    """退场装饰粒子：none / sparkle（星光闪烁）/ ripple（涟漪光环，向内收束）。"""

    sing_fx: SingFx = "none"
    """唱字装饰粒子：none / twinkle（星光闪烁）/ note（音符飘出）/
    petal（花瓣飘动）。每个字的唱字窗口内发射。"""

    fx_particle_size_em: float = 0.40
    """粒子尺寸（相对主字号比例，0.40 = 40% 字号）：星光/音符 sprite 的目标
    边长；涟漪按其 2.6 倍扩散。相对字号缩放，避免不同分辨率/字号下过小或过大。"""

    fx_particle_count: int = 14
    """粒子数量：星光闪烁一次发射的 sprite 数；闪烁/音符按比例折算为每字数量。"""

    fx_particle_color: str = "#FFFFFF"
    """粒子颜色一（#RRGGBB）；``fx_particle_color_mode == "color"`` 时与
    ``fx_particle_color2`` 双色随机混发。"""

    fx_particle_color2: str = "#FFFFFF"
    """粒子颜色二（#RRGGBB，默认白色——白色即颜色本身，无「未设置」
    态）：单独颜色档恒为双色随机混发（2026-10 用户口径：必须设置双色）。"""

    fx_particle_color_mode: str = "color"
    """粒子颜色模式（仿扫字线）：``color`` 单独颜色——颜色一/颜色二双色
    随机混发（两色相同折叠为单色）；
    ``sakura`` 默认颜色——两种樱花粉随机混发（规划器拆双 burst，每颗粒子
    各取一色）；``follow_mix`` 跟随字体·前后各一——所在行实际配色的
    走字前/后「主文字」**实色**双色随机混发（渐变/拼色取停止色平均）；
    ``follow_before`` / ``follow_after`` 跟随字体——
    用**所在行**实际配色的走字前 / 走字后「主文字」完整装饰规格（渐变/
    描边原样下发，与音符「跟随字体」同源；行级口径：行内逐字混合配色
    不逐字取色，与唱字粒子的行级规划一致）；
    ``role`` 复用配色方案——用 ``fx_particle_role_name`` 指定来源
    （``__global__`` 全局默认 / 角色或「标题」方案）的「走字后-主文字」
    填充折算成实色（花瓣粒子改取该来源的走字前/后实色双色随机混发）。
    图片填充取不到代表色，回退单独颜色。在
    ``plan_line_bursts`` 内解析成每 burst 实色下发，两条后端同色。"""

    fx_particle_role_name: Optional[str] = None
    """粒子 ``role`` 颜色模式引用的来源名：语义同 ``scanline_role_name``
    （改名连写、删除连模式一起回退 ``color``，见
    ``remap_particle_role_reference``）；仅 ``fx_particle_color_mode ==
    "role"`` 时生效。渲染时名字查不到则回退 ``color`` 模式的
    ``fx_particle_color``。"""

    fx_particle_color_layers: str = "solid"
    """取色层级（仅对从配色方案取色的四档生效：跟随字体·走字前 / 走字后 /
    前后各一 / 复用配色方案）：``solid`` 仅实色——只取该态「主文字」色，
    粒子呈纯色剪影（默认）；``stroke`` +描边——按角色方案的描边栈（方案
    启用二重描边时一起加）、不加装饰；``decor`` +装饰——不加描边、仅加
    装饰层；``all`` 全有——描边栈 + 装饰层，完全按角色方案。装饰层随角色
    方案的 ``decoration_kind`` 二选一：``shadow`` 为偏移剪影、``glow`` 为
    多级描边弥散晕（半径取走字前/后、按粒子尺寸同比缩放，与文字装饰
    同口径）。偏好记忆：改一次一直沿用（应用级习惯，不随打开工程回退，
    见 ``PARTICLE_MODULE_STYLE_FIELDS``）。"""

    anim_fx_size_em: float = 0.40
    """入退场动画粒子尺寸（相对主字号比例，0.40 = 40% 字号）。

    2026-10 用户口径：入退场粒子颜色/尺寸与唱字装饰粒子、扫字线特效彻底
    解耦，各有独立设置；药丸切换当前编辑对象。"""

    anim_fx_color: str = "#FFB7C5"
    """入退场粒子颜色一（仅在 ``anim_fx_color_mode == "color"`` 时生效）。"""

    anim_fx_color2: str = "#FFD7E0"
    """入退场粒子颜色二（单独颜色档双色随机混发）。"""

    anim_fx_color_mode: str = "sakura"
    """入退场粒子颜色模式：与唱字装饰粒子的模式同语义（sakura 默认颜色 /
    color 单独颜色 / follow_mix / follow_before / follow_after / role）。"""

    anim_fx_role_name: Optional[str] = None
    """入退场粒子 ``role`` 模式引用的配色来源名。"""

    anim_fx_color_layers: str = "solid"
    """入退场粒子的取色层级（仅来源配色四档生效）。"""

    fx_apply_to_entry_exit: bool = False
    """已废弃：旧版「仅唱字 / 入退场同用」开关。

    2026-10 起入退场粒子改用 ``anim_fx_*`` 独立设置；本字段只用于读取旧
    工程时把旧设置迁移到 ``anim_fx_*``，渲染端不再消费。"""

    section_edge_anim_enabled: bool = False
    """段首尾独立动画：开启后段首页/段尾页各行按下面两个动画替换入退场。"""

    section_edge_both_animations: bool = False
    """「同时设置出入场」：开启时段边缘页入退场都替换；默认各页只替换自己
    一侧（段首页只换入场、段尾页只换退场），单页段既是首又是尾、两侧都换。"""

    section_head_anim: EntryAnimation = "fade"
    """段边缘页替换用的入场动画。"""

    section_tail_anim: ExitAnimation = "fade"
    """段边缘页替换用的退场动画。"""

    # 行背景带（三段式背景条）：每条歌词行背后铺一条随行宽的图片带。
    # 只参与绘制，不参与排版 —— 不加进行宽、行网格与防重叠包络。
    line_band_enabled: bool = False
    """行背景带总开关。"""

    line_band_image: str = ""
    """图源：``preset:<id>`` 或绝对路径（用户上传）。空串表示未选图。"""

    line_band_fill_mode: str = "three_slice"
    """填充模式：``stretch`` 拉伸 / ``repeat`` 重复 / ``cover`` 裁切 /
    ``three_slice`` 三段式（左右端点不缩放、中段循环）。"""

    line_band_cap_left_px: int = 0
    """三段式左端点宽（**源图像素**，不是屏幕像素）；左右独立。"""

    line_band_cap_right_px: int = 0
    """三段式右端点宽（源图像素）。"""

    line_band_mid_mode: str = "repeat"
    """三段式下中段的行为：``repeat`` 平铺 / ``stretch`` 拉伸。"""

    line_band_pad_top_px: int = 20
    """上边距：文字顶（开启注音基准时取注音顶）到带子上缘。"""

    line_band_pad_bottom_px: int = 20
    """下边距：主文字底到带子下缘。"""

    line_band_pad_left_px: int = 40
    """左边距：文字左缘到带子左缘。"""

    line_band_pad_right_px: int = 40
    """右边距：文字右缘到带子右缘。"""

    line_band_include_ruby: bool = False
    """``line_band_pad_top_px`` 的量取基准：True=注音顶，False=主文字顶。"""

    line_band_offset_y_px: int = 0
    """整条带子上下微调。"""

    line_band_opacity_pct: int = 100
    """带子整体透明度百分比。"""

    line_band_tint: str = ""
    """用户颜色（仅可调色预设生效）；空串表示用预设默认色。"""

    line_band_tint_strength_pct: int = 100
    """上色强度百分比：0=保持素材原本的中性灰。"""

    # 指示灯（Sayatoo SignalsLits.sx：lit.* / signals.duration）
    # 形状指示灯与音量柱是两个独立模块。lit_enabled 只控制悬浮形状灯；
    # volume_enabled 控制会插入字幕行首、参与行宽布局的音量柱。
    lit_enabled: bool = False
    volume_enabled: bool = False
    lit_appearance_mode: LitAppearanceMode = "auto"
    """指示灯外观联动：``auto`` 时灯大小按主文字字号推导（比例见
    ``lit_auto_size_ratio_pct``），矢量灯改走主文字的完整装饰管线——
    填充/渐变/描边/二重描边/发光/阴影取**段首行第一个角色**的有效配色
    （无角色时为该行样式）的走字后状态，整字放大唱字动画与文字同款并按
    灯尺寸/字号 同比缩放（见 ``signal._draw_lit_decorated_group``）；auto
    档不跟随行入退场动画（独立悬浮模块，靠自身倒计时转场淡出）。
    ``role`` 大小推导同 auto，装饰管线改用 ``lit_role_name`` 指定来源
    （扫字线「复用配色方案」同口径，见 :func:`appearance_role_source`）。
    ``custom`` 时全部使用下面的独立字段。推导在渲染期实时进行，改字号/
    配色/输出高度后指示灯自动跟随，工程里不落具体值。"""
    lit_role_name: Optional[str] = None
    """指示灯 ``role`` 外观档引用的来源名：保留键 ``__global__`` 表示全局
    默认（主样式自身配色），其余为 ``custom_style_schemes`` 的键（角色名
    或「标题」）；仅 ``lit_appearance_mode == "role"`` 时生效。来源改名时
    由 ``roleReferencesRemapped`` 维护链同步改写；**删除被引用角色时保留
    悬空引用**（UI 下拉以幽灵条目展示原名，渲染按 auto 档口径回退——段
    首行第一个角色）。"""
    lit_auto_size_ratio_pct: int = 50
    """auto/role 档灯边长相对主文字字号的百分比（默认 50%）；仅非 custom
    模式生效。"""
    volume_appearance_mode: VolumeAppearanceMode = "auto"
    """音量柱外观联动：``auto`` 时整体高度/柱宽/描边宽按主文字字号推导，
    且柱体改用主文字的完整装饰管线——填充/渐变/描边/发光/阴影取**段首行
    第一个角色**的有效配色（无角色时为该行样式），整字放大唱字动画与文字
    同款并按 柱高/字号 同比缩放（见 ``signal._draw_volume_decorated_group``）；
    ``role`` 大小推导同 auto，装饰管线改用 ``volume_role_name`` 指定来源
    （扫字线「复用配色方案」同口径，见 :func:`appearance_role_source`）。
    ``custom`` 时全部使用下面的独立字段。推导在渲染期实时进行，改字号/
    配色/输出高度后音量柱自动跟随，工程里不落具体值。"""
    volume_role_name: Optional[str] = None
    """音量柱 ``role`` 外观档引用的来源名：语义同 ``lit_role_name``（改名
    连写、删除保留悬空引用）；仅 ``volume_appearance_mode == "role"`` 时
    生效。"""
    volume_auto_size_ratio_pct: int = 50
    """auto/role 档整体高度相对主文字字号的百分比（默认 50%）；仅非
    custom 模式生效。"""
    volume_auto_column_ratio_pct: int = 25
    """auto/role 档柱宽相对整体高度的百分比（默认 25%，与 N3 默认 48:12
    一致）；描边上限等比例链随柱宽推导，仅非 custom 模式生效。"""
    # Keep the serialized/default discriminator for source compatibility with
    # direct Style(lit_enabled=True) callers; the new UI always writes a shape.
    lit_style: LitStyle = "circle"
    lit_image_path: str = ""
    """形状灯「图片」模式的素材路径（#RRGGBB 之外唯一非数值 lit 参数）。

    仅 ``lit_style == "image"`` 时生效：图片等比 contain 进「大小」的
    方形槽位（两端同口径）；描边/柔化/阴影/边缘亮度只作用于矢量形状，
    图片模式下忽略。图片缺失或无法解码时回退为圆形（与 native 一致）。
    """
    lit_number: int = 4
    lit_size: int = 45
    lit_offset_x: int = 0
    lit_offset_y: int = -24
    lit_tracking: int = 0
    lit_fill_color: str = "#0000FF"
    lit_stroke_color: str = "#FFFFFF"
    lit_stroke_width: int = 2
    lit_stroke_soften: int = 0
    lit_opacity_pct: int = 100
    lit_edge_brightness_pct: int = 60
    lit_shadow: bool = True
    lit_time_offset_ms: int = 0
    lit_waiting_time_ms: int = 0
    lit_transition_mode: str = "fade"
    lit_transition_ratio_pct: int = 67
    lit_transition_angle_deg: int = 0
    lit_transition_distance: int = 0
    signals_duration_ms: int = 4000
    volume_duration_ms: int = 4000
    volume_waiting_time_ms: int = 0
    volume_time_offset_ms: int = 0
    volume_stroke_width: int = 2
    volume_opacity_pct: int = 100
    volume_size: int = 48
    volume_offset_x: int = 0
    volume_offset_y: int = 0
    volume_column_width: int = 12
    volume_column_count: int = 4
    volume_column_spacing: int = 0
    volume_align: int = 1
    volume_ratio: float = 3.0
    volume_fill_color: str = "#FFFFFF"
    volume_stroke_color: str = "#0000FF"
    volume_overlay_fill_color: str = "#0000FF"
    volume_overlay_stroke_color: str = "#FFFFFF"
    volume_flash_times: int = 3
    volume_flash_duration_ratio: float = 1.0
    volume_transition_ratio_pct: int = 67

    # 标题字幕 overlay 列表（B7 → 多标题）。默认一条（关闭），保持「至少
    # 一条」的现状不变量；用户在属性页删除全部条目后允许为空列表。
    title_overlays: list[TitleOverlay] = field(default_factory=lambda: [TitleOverlay()])
    # 用户显式删除的软件预设布局 id（``title-default`` / ``builtin-N``）：
    # ``ensure_page_layout_defaults`` 不再自动补回这些预设，删除才能持久。
    hidden_builtin_layout_ids: list[str] = field(default_factory=list)

    @property
    def timing(self) -> StyleTimingConfig:
        """Return a typed timing view without changing legacy field storage."""
        return StyleTimingConfig(
            **{name: getattr(self, name) for name in _STYLE_TIMING_FIELDS}
        )

    def with_timing(
        self,
        timing: Optional[StyleTimingConfig] = None,
        **changes: object,
    ) -> "Style":
        """Return a style with timing changes while preserving flat-field APIs."""
        unknown = set(changes) - set(_STYLE_TIMING_FIELDS)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"unsupported timing field(s): {names}")
        values = {
            name: getattr(timing if timing is not None else self, name)
            for name in _STYLE_TIMING_FIELDS
        }
        values.update(changes)
        return replace(self, **values)

    @property
    def default_layout(self) -> LyricsLayout:
        """Return the flat default-layout fields through the nested layout model."""
        return LyricsLayout(
            name="默认布局",
            layout_id="default",
            **{
                name: deepcopy(getattr(self, name))
                for name in LYRICS_LAYOUT_FIELDS
            },
        )

    def with_default_layout(
        self,
        layout: Optional[LyricsLayout] = None,
        **changes: object,
    ) -> "Style":
        """Update default-layout fields while keeping legacy flat storage."""
        unknown = set(changes) - set(LYRICS_LAYOUT_FIELDS)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"unsupported layout field(s): {names}")
        values = {}
        for name in LYRICS_LAYOUT_FIELDS:
            value = getattr(layout, name) if layout is not None else getattr(self, name)
            values[name] = deepcopy(getattr(self, name) if value is None else value)
        values.update(changes)
        return replace(self, **values)

    @property
    def appearance(self) -> SubtitleStyleScheme:
        """Return default typography/paint fields through the scheme model."""
        return SubtitleStyleScheme(
            **{
                name: deepcopy(getattr(self, name))
                for name in STYLE_APPEARANCE_FIELDS
            }
        )

    def with_appearance(
        self,
        appearance: Optional[SubtitleStyleScheme] = None,
        **changes: object,
    ) -> "Style":
        """Update default appearance while keeping legacy flat storage."""
        unknown = set(changes) - set(STYLE_APPEARANCE_FIELDS)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise TypeError(f"unsupported appearance field(s): {names}")
        values = {}
        for name in STYLE_APPEARANCE_FIELDS:
            value = (
                getattr(appearance, name)
                if appearance is not None
                else getattr(self, name)
            )
            values[name] = deepcopy(getattr(self, name) if value is None else value)
        values.update(changes)
        return replace(self, **values)


_STYLE_FIELD_NAMES = frozenset(field.name for field in fields(Style))
STYLE_APPEARANCE_FIELDS = tuple(
    field.name
    for field in fields(SubtitleStyleScheme)
    if field.name in _STYLE_FIELD_NAMES and field.name not in LYRICS_LAYOUT_FIELDS
)


def style_with_line_animation(style: Style, line: TimingLine) -> Style:
    """把逐行动画覆盖套到样式上；其他视觉与布局字段保持不变。"""
    override = line.animation_override
    changes: dict[str, object] = {}
    if override is not None:
        changes.update({
            "entry_anim": override.entry_anim,
            "entry_lead_ms": max(int(override.entry_duration_ms), 0),
            "exit_anim": override.exit_anim,
            "exit_fade_ms": max(int(override.exit_duration_ms), 0),
        })
    scanline_line_override: Optional[bool] = (
        override.scanline_enabled if override is not None else None
    )
    if override is not None and override.karaoke_anim != "inherit":
        # inherit 表示「保持全局那一档」，必须原样留下 style.karaoke_anim。
        # 若把 "inherit" 写进行样式，effective_karaoke_animation 会转而去看这一行
        # 被覆盖后的入退场——全局显式设的 utopia 就这么丢了。
        override_anim = override.karaoke_anim
        if override_anim in {
            "scanline", "utopia_scanline", "zoom_pulse_scanline",
        }:
            changes["karaoke_anim"] = {
                "scanline": "none",
                "utopia_scanline": "utopia",
                "zoom_pulse_scanline": "zoom_pulse",
            }[override_anim]
            scanline_line_override = True
        else:
            changes["karaoke_anim"] = override_anim
    if line.wipe_reverse and style.reverse_karaoke_anim != "inherit":
        reverse_anim = style.reverse_karaoke_anim
        if reverse_anim in {
            "scanline", "utopia_scanline", "zoom_pulse_scanline",
        }:
            changes["karaoke_anim"] = {
                "scanline": "none",
                "utopia_scanline": "utopia",
                "zoom_pulse_scanline": "zoom_pulse",
            }[reverse_anim]
            scanline_line_override = True
        else:
            changes["karaoke_anim"] = reverse_anim
    merged = style.with_timing(**changes) if changes else style
    if (
        scanline_line_override is True
        and override is not None
        and override.scanline_legacy
        and line.wipe_reverse
        and style.reverse_karaoke_anim != "inherit"
        and not style.scanline_reverse_enabled
    ):
        # 旧逐行 scanline 先于旧反向明确档位求出；反向明确选了非扫字线
        # 时，不能把旧逐行标记当成新的强制开启。
        scanline_line_override = None
    if scanline_line_override is True:
        scanline_active = True
    elif scanline_line_override is False:
        scanline_active = False
    else:
        direction_enabled = (
            merged.scanline_reverse_enabled
            if line.wipe_reverse
            else merged.scanline_forward_enabled
        )
        scanline_active = bool(merged.scanline_enabled and direction_enabled)
    merged = replace(merged, scanline_enabled=scanline_active)
    if override is not None and override.sing_fx != "inherit":
        # 唱字装饰粒子与唱字动画同语义：inherit 留全局档，其余档写进行样式。
        # sing_fx 不属于 StyleTimingConfig（非时间字段），flat 字段直接替换。
        merged = replace(merged, sing_fx=override.sing_fx)
    return merged


def effective_karaoke_animation(style: Style) -> Literal["none", "no_wipe", "utopia"]:
    """Resolve the singing animation while preserving legacy Utopia projects.

    扫字线是叠加特效，不改变基础动画：``scanline`` 按 ``none``（纯 Wipe）、
    ``utopia_scanline`` / ``zoom_pulse_scanline`` 按 ``utopia`` 渲染本体，高亮层
    另见 :func:`effective_karaoke_scanline`。整字放大（``zoom_pulse``）同样按
    ``utopia`` 本体渲染（复用逐字变换管线），缩放曲线与原点另见
    :func:`effective_karaoke_zoom_pulse`。
    """
    timing = style.timing
    if (
        timing.karaoke_anim == "utopia"
        or timing.karaoke_anim == "utopia_scanline"
        or timing.karaoke_anim == "zoom_pulse"
        or timing.karaoke_anim == "zoom_pulse_scanline"
    ):
        return "utopia"
    if timing.karaoke_anim == "none":
        return "none"
    if timing.karaoke_anim == "no_wipe":
        return "no_wipe"
    if timing.karaoke_anim == "scanline":
        return "none"
    return (
        "utopia"
        if "utopia" in {timing.entry_anim, timing.exit_anim}
        else "none"
    )


def effective_karaoke_zoom_pulse(style: Style) -> bool:
    """Return whether the whole-char zoom pulse wipe curve is active.

    只认显式选择的 ``zoom_pulse`` / ``zoom_pulse_scanline`` 档位；``inherit``
    的旧项目推导与 utopia/扫字线档位保持 False。反向行经
    :func:`style_with_line_animation` 合成后 ``karaoke_anim`` 已是 reverse
    档位，无需单独判断。
    """
    return style.timing.karaoke_anim in {"zoom_pulse", "zoom_pulse_scanline"}


def effective_karaoke_scanline(style: Style) -> bool:
    """Return whether the karaoke scan-line highlight overlay is active.

    以 ``scanline_enabled`` 总开关为准；旧工程未迁移时兼容旧档位。反向行在
    :func:`style_with_line_animation` 里已按 ``scanline_reverse_enabled`` 关掉。
    """

    return bool(
        style.scanline_enabled
        or style.timing.karaoke_anim
        in {"scanline", "utopia_scanline", "zoom_pulse_scanline"}
    )


PARTICLE_COLOR_TARGETS = ("anim", "sing", "scanline")
"""粒子颜色设置的三个独立编辑对象（药丸切换）：

``anim`` 入退场动画粒子（``anim_fx_*``）；``sing`` 唱字装饰粒子
（``fx_particle_*``）；``scanline`` 扫字线粒子特效（``scanline_effect_*``）。
"""


def particle_color_style(style: Style, target: str) -> Style:
    """把某个目标的颜色字段投影成 ``fx_particle_*`` 视图。

    渲染端的颜色解析（``particles.py`` 的 burst 规划）只认
    ``fx_particle_color_mode/color/...`` 一组字段；三个目标解耦后，规划
    入退场/扫字线粒子时用本函数构造一个只替换颜色字段的 Style 视图，轨迹/
    尺寸/角色解析全部沿用原样式，因此无需复制整套颜色解析逻辑。
    """

    if target == "anim":
        return replace(
            style,
            fx_particle_color_mode=style.anim_fx_color_mode,
            fx_particle_color=style.anim_fx_color,
            fx_particle_color2=style.anim_fx_color2,
            fx_particle_color_layers=style.anim_fx_color_layers,
            fx_particle_role_name=style.anim_fx_role_name,
        )
    if target == "scanline":
        mode = style.scanline_effect_color_mode
        # theme 由规划器按档位主题色处理；这里给一个合法的兜底模式。
        if mode == "theme":
            mode = "sakura"
        return replace(
            style,
            fx_particle_color_mode=mode,
            fx_particle_color=style.scanline_effect_color,
            fx_particle_color2=style.scanline_effect_color2,
            fx_particle_color_layers=style.scanline_effect_color_layers,
            fx_particle_role_name=style.scanline_effect_role_name,
        )
    return style



@dataclass
class OutputConfig:
    """输出参数占位。"""

    width: int = 1920
    height: int = 1080
    fps: int = 60
    encoder_mode: str = "cpu"
    crf: int = 18
    preset: str = "medium"
    codec: str = "h264"
    output_path: str = ""


@dataclass
class RenderProject:
    """渲染项目根对象占位。"""

    subtitle_source: SubtitleSource = field(default_factory=SubtitleSource)
    global_style: Style = field(default_factory=Style)
    background: BackgroundSource = field(default_factory=BackgroundSource)
    output: OutputConfig = field(default_factory=OutputConfig)
    audio_path: Optional[str] = None
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 持久化辅助（settings.json / .krstyle.json / .yurika 共用）
# ---------------------------------------------------------------------------


def _builtin_page_layout(style: Style, rows: int) -> LyricsLayout:
    alignments, gap = _DEFAULT_PAGE_LAYOUT_SPECS[rows]
    return LyricsLayout(
        name=f"{rows} 行布局",
        layout_id=f"builtin-{rows}",
        line_y_position=style.line_y_position,
        line_y_margin_px=style.line_y_margin_px,
        line_gap_px=gap,
        smart_horizontal=style.smart_horizontal,
        horizontal_margin_px=style.horizontal_margin_px,
        line_alignments=list(alignments),
        force_top_bottom_n3=style.force_top_bottom_n3,
        letter_spacing_px=style.letter_spacing_px,
        allow_biting=style.allow_biting,
        ruby_interval_px=style.ruby_interval_px,
        ruby_alignment=style.ruby_alignment,
        ruby_gap_px=style.ruby_gap_px,
    )


def builtin_preset_layout_ids() -> frozenset[str]:
    """软件出厂预设布局 id（タイトル左上 + 1~8 行布局）。"""

    return frozenset({"title-default", *(f"builtin-{rows}" for rows in range(1, 9))})


_builtin_preset_ids = builtin_preset_layout_ids


def ensure_page_layout_defaults(style: Style) -> Style:
    """Return a style with stable unique IDs and complete 1..8 defaults.

    Missing built-ins are appended, never inserted, so numeric layout indices
    from schema-v1 projects and N3 imports retain their meaning.  Presets the
    user explicitly deleted (``hidden_builtin_layout_ids``) stay deleted —
    ``title-default`` included; when it is not hidden it is re-seeded whenever
    missing, because the default title (``layout_index=1``) must always
    resolve to the shipped top-left preset.
    """

    layouts = deepcopy(style.layouts)
    hidden = {
        str(value)
        for value in style.hidden_builtin_layout_ids
        if str(value) in _builtin_preset_ids()
    }
    used: set[str] = {"default"}
    for index, layout in enumerate(layouts):
        candidate = str(layout.layout_id or "").strip()
        if not candidate or candidate in used:
            if index == 0 and layout.name == TITLE_LAYOUT_NAME and "title-default" not in used:
                candidate = "title-default"
            else:
                candidate = f"legacy-{index + 1}"
                while candidate in used:
                    candidate = f"layout-{uuid4().hex}"
        layout.layout_id = candidate
        if candidate.startswith("builtin-"):
            try:
                builtin_rows = int(candidate.removeprefix("builtin-"))
            except ValueError:
                builtin_rows = 0
            if layout.name == f"默认 {builtin_rows} 行":
                layout.name = f"{builtin_rows} 行布局"
        used.add(candidate)

    if "title-default" not in used and "title-default" not in hidden:
        layouts.append(default_title_layout())
        used.add("title-default")

    required_rows = [1, 3, 4, 5, 6, 7, 8]
    if max(1, min(len(style.line_alignments), 8)) != 2:
        required_rows.append(2)
    for rows in required_rows:
        layout_id = f"builtin-{rows}"
        if layout_id in used or layout_id in hidden:
            continue
        layouts.append(_builtin_page_layout(style, rows))
        used.add(layout_id)

    raw_mapping = style.default_layout_by_row_count
    mapping: dict[int, str] = {}
    capacity_by_id = {
        layout.layout_id: max(1, min(len(layout.line_alignments), 8))
        for layout in layouts
    }

    def _fallback_for(rows: int, default_capacity: int) -> str:
        # 预设被显式删除时按容量找不到目标，退回全局默认布局。
        preferred = (
            "default" if default_capacity == rows else f"builtin-{rows}"
        )
        return preferred if preferred in capacity_by_id else "default"

    default_capacity = max(1, min(len(style.line_alignments), 8))
    for rows in range(1, 9):
        layout_id = str(raw_mapping.get(rows, "") or "")
        fallback = _fallback_for(rows, default_capacity)
        mapping[rows] = (
            layout_id
            if (
                (layout_id == "default" and default_capacity == rows)
                or capacity_by_id.get(layout_id) == rows
            )
            else fallback
        )
    if layouts == style.layouts and mapping == style.default_layout_by_row_count:
        return style
    return replace(style, layouts=layouts, default_layout_by_row_count=mapping)


def layout_index_for_id(style: Style, layout_id: str) -> int:
    if layout_id == "default":
        return 0
    for index, layout in enumerate(style.layouts, start=1):
        if layout.layout_id == layout_id:
            return index
    return 0


def layout_id_for_index(style: Style, layout_index: int) -> str:
    index = int(layout_index)
    if index <= 0 or index > len(style.layouts):
        return "default"
    return style.layouts[index - 1].layout_id or f"legacy-{index}"


def layout_capacity(style: Style, layout_id: str) -> int:
    if layout_id == "default":
        return max(1, min(len(style.line_alignments), 8))
    index = layout_index_for_id(style, layout_id)
    if index <= 0:
        return max(1, min(len(style.line_alignments), 8))
    return max(1, min(len(style.layouts[index - 1].line_alignments), 8))


def layout_display_name(style: Style, layout_id: str) -> str:
    """Return the user-facing name for a stable page-layout ID."""

    if layout_id == "default":
        return f"{layout_capacity(style, layout_id)} 行布局（默认）"
    index = layout_index_for_id(style, layout_id)
    if index <= 0:
        return f"{layout_capacity(style, 'default')} 行布局（默认）"
    return style.layouts[index - 1].name


def normalize_glow_concentration_level(value: object, fallback: int = 0) -> int:
    """Normalize -1 (disabled) plus the three NicoKaraMaker3 blur levels."""
    try:
        return max(-1, min(2, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(-1, min(2, int(fallback)))


def style_for_track(style: Style, track: object) -> Style:
    """Resolve the per-track display timing into an effective style.

    防泄露契约（每轴在整条显示时间管线里只用它自己的样式）：

    - 主轨 / 跟随主字幕的副轨 / ``overrides`` 为空 → 原样返回全局
      ``style`` 对象（副轴默认与旧工程行为逐字节一致）；
    - 非跟随副轨 → ``style.with_timing(**overrides)``，overrides 是
      **绝对值**，因此本函数幂等——对已解析的样式再解析一次结果不变，
      入口兜底调用不会重复叠加。

    ``track.display_timing`` 按鸭子类型读取，缺省视为跟随。
    """

    display_timing = getattr(track, "display_timing", None)
    if display_timing is None:
        return style
    overrides = getattr(display_timing, "overrides", None) or {}
    if getattr(display_timing, "follow_main", True) or not overrides:
        return style
    return style.with_timing(**overrides)


def volume_auto_values(
    style: "Style", *, basis: "Style | None" = None
) -> dict[str, object]:
    """Derive auto-mode volume metrics/colors from the main lyric font.

    比例链默认与 N3 默认值（整体 48 : 柱宽 12，字号 100）一致：整体高度 =
    字号 × ``volume_auto_size_ratio_pct``（默认 50%），柱宽 = 整体高度 ×
    ``volume_auto_column_ratio_pct``（默认 25%）。auto 档的绘制走文字装饰
    管线（描边宽 =
    文字描边宽 × 高度比、上限半个柱宽，颜色取自文字配色矩阵），见
    ``signal._draw_volume_lit_group``。取整统一半向上（与面板浮点回显
    口径相同），避免 banker's rounding 抖动。

    ``basis``：推导源样式——「跟随字体」要配合**信号宿主行实际唱到的
    字号**，角色工程里它是主轨段首行最高频首角色方案叠加后的样式（见
    ``style_semantics.auto_appearance_basis``）；缺省用全局主样式（旧口径）。
    """
    source = basis if basis is not None else style
    font_size = max(int(source.font_size_px), 1)
    size_ratio = max(
        int(getattr(source, "volume_auto_size_ratio_pct", 50) or 0), 1
    )
    column_ratio = max(
        int(getattr(source, "volume_auto_column_ratio_pct", 25) or 0), 1
    )
    size = max(4, int(font_size * size_ratio / 100.0 + 0.5))
    column_width = max(1, int(size * column_ratio / 100.0 + 0.5))
    stroke_width = min(
        max(int(int(source.stroke_width_px or 0) * size / font_size + 0.5), 0),
        column_width // 2,
    )
    return {
        "volume_size": size,
        "volume_column_width": column_width,
        "volume_stroke_width": stroke_width,
        "volume_fill_color": source.base_color,
        "volume_stroke_color": source.stroke_color,
        "volume_overlay_fill_color": source.fill_color,
        "volume_overlay_stroke_color": source.stroke_color,
    }


def resolve_volume_appearance(
    style: "Style", *, auto_basis: "Style | None" = None
) -> "Style":
    """Return the style with auto-mode volume values materialized.

    仅在独立音量柱模块开启且模式为 ``auto`` / ``role`` 时替换字段（role 档
    大小推导同 auto，装饰配色由绘制端按 ``volume_role_name`` 另行解析）；
    其余情况原样返回。Python 绘制/布局（``signal.volume_style``）与 native
    IR 序列化（``render_ir``）都必须经过本函数，两条后端才会拿到同一组数值。
    ``auto_basis`` 是推导源样式（见 ``volume_auto_values``）：painter 与
    render_ir 两侧必须传同一份基准，两条后端才会推出同一组数值。
    """
    if not style.volume_enabled or style.volume_appearance_mode == "custom":
        return style
    return replace(style, **volume_auto_values(style, basis=auto_basis))


def lit_auto_values(
    style: "Style", *, basis: "Style | None" = None
) -> dict[str, object]:
    """Derive auto-mode lamp metrics/colors from the main lyric font.

    灯边长 = 字号 × ``lit_auto_size_ratio_pct``（默认 50%），描边宽 =
    文字描边宽 × 灯尺寸/字号（上限半个灯宽）。auto 档的矢量灯绘制走文字
    装饰管线（全程取走字后配色），这里的物化值只服务布局度量、native IR
    与面板回显。取整统一半向上（与 ``volume_auto_values`` 口径相同）。
    ``basis`` 语义同 ``volume_auto_values``。
    """
    source = basis if basis is not None else style
    font_size = max(int(source.font_size_px), 1)
    size_ratio = max(
        int(getattr(source, "lit_auto_size_ratio_pct", 50) or 0), 1
    )
    size = max(4, int(font_size * size_ratio / 100.0 + 0.5))
    stroke_width = min(
        max(int(int(source.stroke_width_px or 0) * size / font_size + 0.5), 0),
        size // 2,
    )
    return {
        "lit_size": size,
        "lit_stroke_width": stroke_width,
        "lit_fill_color": source.fill_color,
        "lit_stroke_color": source.stroke_color,
    }


def resolve_lit_appearance(
    style: "Style", *, auto_basis: "Style | None" = None
) -> "Style":
    """Return the style with auto-mode lamp values materialized.

    仅在形状指示灯模块开启、模式为 ``auto`` / ``role`` 且不是 legacy volume
    口径时替换字段（role 档大小推导同 auto，装饰配色由绘制端按
    ``lit_role_name`` 另行解析）；其余情况原样返回。Python 布局
    （``painter`` 的信号度量）、绘制（``signal``）与 native IR 序列化
    （``render_ir``）都必须经过本函数，两条后端才会拿到同一组数值。
    ``auto_basis`` 语义同 ``resolve_volume_appearance``。
    """
    if (
        not style.lit_enabled
        or style.lit_style == "volume"
        or style.lit_appearance_mode == "custom"
    ):
        return style
    return replace(style, **lit_auto_values(style, basis=auto_basis))


def style_to_dict(style: Style) -> dict:
    """Serialize ``Style`` into JSON-friendly primitives."""
    data: dict = {}
    for item in fields(Style):
        value = getattr(style, item.name)
        if item.name in {"karaoke_colors", "ruby_karaoke_colors"}:
            data[item.name] = karaoke_colors_to_dict(value) if value is not None else None
        elif item.name == "scanline_legacy_global":
            continue
        elif item.name == "layouts":
            data[item.name] = [lyrics_layout_to_dict(layout) for layout in value]
        elif item.name == "default_layout_by_row_count":
            data[item.name] = {str(key): str(item) for key, item in value.items()}
        elif item.name == "title_overlays":
            overlays = [title_overlay_to_dict(overlay) for overlay in value]
            data["title_overlays"] = overlays
            # 兼容镜像：旧版本程序读新文件时至少保住第一个标题（权威 key
            # 是 ``title_overlays``，读取端优先采用）。
            data["title_overlay"] = overlays[0] if overlays else None
        elif item.name == "singer_style_overrides":
            data[item.name] = {
                str(key): subtitle_style_scheme_to_dict(scheme)
                for key, scheme in value.items()
            }
        elif item.name == "custom_style_schemes":
            data[item.name] = {
                str(key): subtitle_style_scheme_to_dict(scheme)
                for key, scheme in value.items()
            }
        else:
            data[item.name] = value
    # 兼容对象（直接 Style(karaoke_anim="..._scanline")）序列化时也写成
    # 新语义：基础动画 + 总开关 + 方向范围，避免保存重载后丢扫字线。
    legacy_forward = data.get("karaoke_anim") in {
        "scanline", "utopia_scanline", "zoom_pulse_scanline",
    }
    legacy_reverse = data.get("reverse_karaoke_anim") in {
        "scanline", "utopia_scanline", "zoom_pulse_scanline",
    }
    if legacy_forward or legacy_reverse:
        data["scanline_enabled"] = True
        if legacy_forward:
            data["karaoke_anim"] = {
                "scanline": "none",
                "utopia_scanline": "utopia",
                "zoom_pulse_scanline": "zoom_pulse",
            }[data["karaoke_anim"]]
            data["scanline_forward_enabled"] = True
        if legacy_reverse:
            data["reverse_karaoke_anim"] = {
                "scanline": "none",
                "utopia_scanline": "utopia",
                "zoom_pulse_scanline": "zoom_pulse",
            }[data["reverse_karaoke_anim"]]
            data["scanline_reverse_enabled"] = True
        if legacy_forward and not legacy_reverse:
            if data.get("reverse_karaoke_anim") == "inherit":
                data["scanline_reverse_enabled"] = True
            else:
                data["scanline_reverse_enabled"] = False
        if legacy_reverse and not legacy_forward:
            data["scanline_forward_enabled"] = False
    # 扫字线像素字段的存储基准标记:值恒为 SCANLINE_BASE_HEIGHT(1080)下的
    # 基准值。读取端据此区分新旧格式(见 ``style_from_dict`` 的一次性迁移)。
    data["scanline_px_base"] = SCANLINE_BASE_HEIGHT
    # 信号偏移基准标记：旧 payload 无此标记，读取时按工程基准高度一次性
    # 折算到 1080 基准（见 _migrate_signal_offset_px_base）。
    data["signal_offset_px_base"] = SIGNAL_OFFSET_BASE_HEIGHT
    return data


RUBY_DECORATION_OVERRIDE_FIELDS: tuple[str, ...] = (
    "ruby_decoration_kind",
    "ruby_decoration_kind_before",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_glow_before_concentration_level",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_shadow_before_offset_x",
    "ruby_shadow_before_offset_y",
)
"""注音装饰的独立覆盖槽。``None`` = 未设定，渲染时回退主文字对应值。"""


def _ruby_materialize_px(value: int, scale: float) -> int:
    """与 :func:`krok_helper.subtitle_render.engine.ruby.scaled_px` 同式。"""
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def _ruby_materialize_signed_px(value: int, scale: float) -> int:
    """与 :func:`krok_helper.subtitle_render.engine.ruby.scaled_signed_px` 同式。"""
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def materialize_ruby_decoration_changes(style: Style) -> dict[str, object]:
    """把渲染器为 None 注音装饰槽推导的回退值物化成存储值。

    渲染侧（Python ``engine/render/effects/metrics.py`` 与 native
    ``gpu_scene_projection.cpp``）对未设定的注音装饰槽按主文字值 × 注音/
    主文字字号比例回退。本函数把同一回退值填进 None 槽，只填空槽、已设
    值原样保留，因此物化前后渲染结果不变。「关闭默认跟随主文字」的整卡
    快照与旧工程迁移都走这里。发光半径物化时不吃浓度门控（保留主文字的
    潜在半径，供之后切换装饰类型继续编辑）；``ruby_glow_radius_px`` 是
    旧字段，渲染链为 before/after → 旧字段 → 主文字缩放，物化同样按该
    链取值。
    """
    scale = max(int(style.ruby_font_size_px), 1) / max(int(style.font_size_px), 1)
    main_before_kind = style.decoration_kind_before or style.decoration_kind
    main_before_concentration = (
        style.glow_before_concentration_level
        if style.glow_before_concentration_level is not None
        else style.glow_concentration_level
    )
    main_before_shadow_x = (
        style.shadow_before_offset_x
        if style.shadow_before_offset_x is not None
        else style.shadow_offset_x
    )
    main_before_shadow_y = (
        style.shadow_before_offset_y
        if style.shadow_before_offset_y is not None
        else style.shadow_offset_y
    )
    changes: dict[str, object] = {}
    if style.ruby_decoration_kind is None:
        changes["ruby_decoration_kind"] = style.decoration_kind
    if style.ruby_decoration_kind_before is None:
        changes["ruby_decoration_kind_before"] = (
            style.ruby_decoration_kind
            if style.ruby_decoration_kind is not None
            else main_before_kind
        )
    if style.ruby_shadow_offset_x is None:
        changes["ruby_shadow_offset_x"] = _ruby_materialize_signed_px(
            style.shadow_offset_x, scale
        )
    if style.ruby_shadow_offset_y is None:
        changes["ruby_shadow_offset_y"] = _ruby_materialize_signed_px(
            style.shadow_offset_y, scale
        )
    if style.ruby_shadow_before_offset_x is None:
        changes["ruby_shadow_before_offset_x"] = (
            int(style.ruby_shadow_offset_x)
            if style.ruby_shadow_offset_x is not None
            else _ruby_materialize_signed_px(main_before_shadow_x, scale)
        )
    if style.ruby_shadow_before_offset_y is None:
        changes["ruby_shadow_before_offset_y"] = (
            int(style.ruby_shadow_offset_y)
            if style.ruby_shadow_offset_y is not None
            else _ruby_materialize_signed_px(main_before_shadow_y, scale)
        )
    legacy_glow = (
        max(int(style.ruby_glow_radius_px), 0)
        if style.ruby_glow_radius_px is not None
        else None
    )
    if style.ruby_glow_before_radius_px is None:
        changes["ruby_glow_before_radius_px"] = (
            legacy_glow
            if legacy_glow is not None
            else (
                max(int(style.ruby_glow_after_radius_px), 0)
                if style.ruby_glow_after_radius_px is not None
                else _ruby_materialize_px(
                    max(int(style.glow_before_radius_px), 0), scale
                )
            )
        )
    if style.ruby_glow_after_radius_px is None:
        changes["ruby_glow_after_radius_px"] = (
            legacy_glow
            if legacy_glow is not None
            else _ruby_materialize_px(max(int(style.glow_after_radius_px), 0), scale)
        )
    if style.ruby_glow_concentration_level is None:
        changes["ruby_glow_concentration_level"] = normalize_glow_concentration_level(
            style.glow_concentration_level
        )
    if style.ruby_glow_before_concentration_level is None:
        changes["ruby_glow_before_concentration_level"] = (
            normalize_glow_concentration_level(
                style.ruby_glow_concentration_level
            )
            if style.ruby_glow_concentration_level is not None
            else normalize_glow_concentration_level(main_before_concentration)
        )
    return changes


def _migrate_ruby_follow_independence(style: Style) -> Style:
    """旧工程迁移到「跟随开关管全部注音装饰」的新语义（外观不变）。

    - 开关为开却存有注音装饰独立覆盖 → 翻转为关：旧渲染本就按字段优先
      绘制，翻转只是让开关诚实。配色矩阵不参与翻转：跟随中的矩阵副本
      （如 N3 导入）走「编辑主文字颜色即清空」的既有同步路径。
    - 开关为关且装饰槽仍有 None → 按主文字当前值物化：旧版这些槽实时
      跟随主文字（阴影 X/Y 等 BUG），物化后冻结为独立值，画面与旧版一致。
    """
    has_decoration_override = any(
        getattr(style, field_name) is not None
        for field_name in RUBY_DECORATION_OVERRIDE_FIELDS
    )
    if style.ruby_colors_follow_main:
        if not has_decoration_override:
            return style
        style = replace(style, ruby_colors_follow_main=False)
    changes = materialize_ruby_decoration_changes(style)
    if changes:
        style = replace(style, **changes)
    return style


def style_from_dict(payload: object, *, legacy_project: bool = False) -> Style:
    """Build ``Style`` from a dict, ignoring unknown or invalid fields.

    ``legacy_project`` 标记「加载旧版 ``.yurika``」：旧工程没有装饰同步
    开关，按用户要求默认不同步（外观不变，走字前槽位保持 ``None`` 实时
    跟随走字后）。应用默认样式/撤销快照等其它入口缺字段时仍按新工程默认
    开启同步。
    """
    if not isinstance(payload, dict):
        return Style()
    defaults = Style()
    changes: dict = {}
    style_fields = {item.name for item in fields(Style)}
    for key, value in payload.items():
        if key == "title_overlay":
            # 旧版单标题 key 已不是 Style 字段，必须在字段过滤之前处理：
            # 折算成单条目列表；不覆盖权威 key ``title_overlays``（两个 key
            # 同时出现时以 ``title_overlays`` 为准，与写出端的镜像语义一致）。
            legacy = title_overlay_from_dict(value)
            if legacy is not None:
                changes.setdefault("title_overlays", [legacy])
            continue
        if key not in style_fields:
            continue
        if key in {"karaoke_colors", "ruby_karaoke_colors"}:
            changes[key] = karaoke_colors_from_dict(value)
        elif key == "layouts":
            changes[key] = _layouts_from_payload(value)
        elif key == "default_layout_by_row_count":
            if isinstance(value, dict):
                parsed_mapping: dict[int, str] = {}
                for raw_rows, raw_id in value.items():
                    try:
                        rows = int(raw_rows)
                    except (TypeError, ValueError):
                        continue
                    if 1 <= rows <= 8 and str(raw_id).strip():
                        parsed_mapping[rows] = str(raw_id).strip()
                changes[key] = parsed_mapping
        elif key == "title_overlays":
            # 权威 key：条目列表。非法条目跳过；空列表 = 用户删除了全部标题。
            if isinstance(value, list):
                changes[key] = [
                    overlay
                    for overlay in (
                        title_overlay_from_dict(entry) for entry in value
                    )
                    if overlay is not None
                ]
        elif key == "singer_style_overrides":
            changes[key] = _singer_overrides_from_dict(value)
        elif key == "custom_style_schemes":
            changes[key] = _custom_schemes_from_dict(value)
        elif key == "hidden_builtin_layout_ids":
            # 只保留合法预设 id，未知值丢弃。
            valid = _builtin_preset_ids()
            changes[key] = [
                str(item) for item in value if str(item) in valid
            ] if isinstance(value, list) else []
        elif key == "glow_concentration_level":
            changes[key] = normalize_glow_concentration_level(value)
        elif key in {
            "glow_before_concentration_level",
            "ruby_glow_concentration_level",
            "ruby_glow_before_concentration_level",
        }:
            changes[key] = (
                normalize_glow_concentration_level(value) if value is not None else None
            )
        elif key in {
            "font_size_px",
            "letter_spacing_px",
            "space_width_percent",
            "font_weight",
            "stroke_width_px",
            "stroke2_width_px",
            "glow_radius_px",
            "glow_before_radius_px",
            "glow_after_radius_px",
            "shadow_offset_x",
            "shadow_offset_y",
            "shadow_before_offset_x",
            "shadow_before_offset_y",
            "ruby_font_size_px",
            "ruby_gap_px",
            "ruby_interval_px",
            "ruby_glow_radius_px",
            "ruby_glow_before_radius_px",
            "ruby_glow_after_radius_px",
            "ruby_shadow_offset_x",
            "ruby_shadow_offset_y",
            "ruby_shadow_before_offset_x",
            "ruby_shadow_before_offset_y",
            "viewport_offset_x",
            "viewport_offset_y",
            "viewport_scale_pct",
            "viewport_rotation_deg",
            "line_y_margin_px",
            "line_gap_px",
            "horizontal_margin_px",
            "font_reference_height",
            "layout_reference_height",
            "upper_line_left_margin_px",
            "lower_line_right_margin_px",
            "row1_offset_x",
            "row1_offset_y",
            "row2_offset_x",
            "row2_offset_y",
            "line_lead_in_ms",
            "line_tail_ms",
            "line_protect_ms",
            "entry_anim_protect_ms",
            "exit_anim_protect_ms",
            "timing_offset_ms",
            "line_lane_gap_ms",
            "section_gap_ms",
            "entry_lead_ms",
            "exit_fade_ms",
            "scanline_width_px",
            "scanline_glow_px",
            "scanline_brightness_pct",
            "scanline_effect_size_pct",
            "scanline_effect_count",
            "zoom_pulse_curve_level",
            "lit_number",
            "lit_size",
            "lit_offset_x",
            "lit_offset_y",
            "lit_tracking",
            "lit_stroke_width",
            "lit_stroke_soften",
            "lit_opacity_pct",
            "lit_edge_brightness_pct",
            "lit_time_offset_ms",
            "lit_waiting_time_ms",
            "lit_transition_ratio_pct",
            "lit_transition_angle_deg",
            "lit_transition_distance",
            "signals_duration_ms",
            "lit_auto_size_ratio_pct",
            "volume_duration_ms",
            "volume_waiting_time_ms",
            "volume_time_offset_ms",
            "volume_stroke_width",
            "volume_opacity_pct",
            "volume_size",
            "volume_offset_x",
            "volume_offset_y",
            "volume_column_width",
            "volume_column_count",
            "volume_column_spacing",
            "volume_align",
            "volume_auto_size_ratio_pct",
            "volume_auto_column_ratio_pct",
            "volume_flash_times",
            "volume_transition_ratio_pct",
            "line_band_cap_left_px",
            "line_band_cap_right_px",
            "line_band_pad_top_px",
            "line_band_pad_bottom_px",
            "line_band_pad_left_px",
            "line_band_pad_right_px",
            "line_band_offset_y_px",
            "line_band_opacity_pct",
            "line_band_tint_strength_pct",
        }:
            changes[key] = _int_value(value, getattr(defaults, key))
        elif key in {
            "volume_ratio",
            "volume_flash_duration_ratio",
        }:
            changes[key] = _float_value(value, getattr(defaults, key))
        elif key in {
            "italic",
            "affects_ruby_anchor",
            "allow_biting",
            "allow_inter_page_line_overlap",
            "force_top_bottom_n3",
            "stroke2_enabled",
            "ruby_font_follow_main",
            "ruby_colors_follow_main",
            "ruby_horizontal_gradient_with_main",
            "dual_line_layout",
            "right_to_left",
            "vertical",
            "decoration_before_sync",
            "sync_entry",
            "sync_ending",
            "allow_entry_exit_animation_overlap",
            "sync_each_page",
            "auto_fill_section_time",
            "section_edge_anim_enabled",
            "section_edge_both_animations",
            "lit_enabled",
            "volume_enabled",
            "lit_shadow",
            "line_band_enabled",
            "line_band_include_ruby",
        }:
            changes[key] = _bool_value(value)
        elif key == "line_band_fill_mode":
            changes[key] = (
                value
                if value in {"stretch", "repeat", "cover", "three_slice"}
                else defaults.line_band_fill_mode
            )
        elif key == "line_band_mid_mode":
            changes[key] = (
                value
                if value in {"repeat", "stretch"}
                else defaults.line_band_mid_mode
            )
        elif key in {"line_band_image", "line_band_tint"}:
            changes[key] = value if isinstance(value, str) else ""
        elif key == "lit_style":
            changes[key] = (
                value
                if value in {
                    "volume", "circle", "square", "rounded", "image",
                    "star", "note8", "note16", "notepair",
                }
                else defaults.lit_style
            )
        elif key == "volume_appearance_mode":
            changes[key] = (
                value
                if value in {"custom", "auto", "role"}
                else defaults.volume_appearance_mode
            )
        elif key == "lit_appearance_mode":
            changes[key] = (
                value
                if value in {"custom", "auto", "role"}
                else defaults.lit_appearance_mode
            )
        elif key == "lit_transition_mode":
            changes[key] = value if value in {"none", "fade", "slide"} else defaults.lit_transition_mode
        elif key == "section_ending_mode":
            changes[key] = value if value in {"hold", "clear"} else defaults.section_ending_mode
        elif key == "overlap_fallback_mode":
            changes[key] = (
                value if value in {"lift", "displace"} else defaults.overlap_fallback_mode
            )
        elif key == "layout_semantics":
            changes[key] = value if value in {"legacy", "n3_1074"} else defaults.layout_semantics
        elif key == "line_y_position":
            changes[key] = value if value in {"top", "center", "bottom"} else defaults.line_y_position
        elif key == "line_horizontal_layout":
            changes[key] = value if value in {"asymmetric", "center", "per_row"} else defaults.line_horizontal_layout
        elif key in {"row1_align", "row2_align"}:
            changes[key] = value if value in HORIZONTAL_ALIGNS else getattr(defaults, key)
        elif key == "viewport_align":
            changes[key] = value if value in VIEWPORT_ALIGNS else defaults.viewport_align
        elif key == "decoration_kind":
            changes[key] = value if value in {"none", "shadow", "glow"} else defaults.decoration_kind
        elif key in {"decoration_kind_before", "ruby_decoration_kind", "ruby_decoration_kind_before"}:
            changes[key] = value if value in {"none", "shadow", "glow"} else None
        elif key == "ruby_alignment":
            changes[key] = value if value in RUBY_ALIGNMENTS else defaults.ruby_alignment
        elif key == "ruby_main_progress_mode":
            changes[key] = (
                value
                if isinstance(value, str)
                and value in {"checkpoint_segments", "reading_units"}
                else defaults.ruby_main_progress_mode
            )
        elif key == "smart_horizontal":
            changes[key] = value if value in SMART_HORIZONTALS else defaults.smart_horizontal
        elif key == "line_offsets_x":
            changes[key] = _line_offsets_from_payload(value)
        elif key == "line_alignments":
            changes[key] = _line_alignments_from_payload(value)
        elif key == "entry_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
                    "stretch_in", "glow_in", "assemble_in", "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid", "flame", "feather", "lightning",
                }
                else defaults.entry_anim
            )
        elif key == "exit_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
                    "stretch_out", "glow_out", "dissolve_out", "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid", "flame", "feather", "lightning",
                }
                else defaults.exit_anim
            )
        elif key in {"entry_fx", "exit_fx"}:
            allowed = ENTRY_FX_KINDS if key == "entry_fx" else EXIT_FX_KINDS
            changes[key] = (
                value if isinstance(value, str) and value in allowed
                else getattr(defaults, key)
            )
        elif key == "sing_fx":
            changes[key] = (
                value if isinstance(value, str) and value in SING_FX_KINDS
                else defaults.sing_fx
            )
        elif key == "fx_particle_color":
            changes[key] = (
                value
                if isinstance(value, str)
                and value.startswith("#")
                and len(value) in {7, 9}
                and all(
                    character in "0123456789abcdefABCDEF"
                    for character in value[1:]
                )
                else defaults.fx_particle_color
            )
        elif key == "fx_particle_color2":
            changes[key] = (
                value
                if isinstance(value, str)
                and value.startswith("#")
                and len(value) in {7, 9}
                and all(
                    character in "0123456789abcdefABCDEF"
                    for character in value[1:]
                )
                else defaults.fx_particle_color2
            )
        elif key == "fx_particle_size_em":
            try:
                em_value = float(value)
            except (TypeError, ValueError):
                em_value = 0.40
            changes[key] = max(0.05, min(2.0, em_value))
        elif key == "fx_particle_count":
            changes[key] = max(2, min(64, _int_value(value, 14)))
        elif key == "fx_particle_color_mode":
            changes[key] = (
                value
                if value in {
                    "color", "sakura", "follow_mix", "follow_before", "follow_after",
                    "role",
                }
                else defaults.fx_particle_color_mode
            )
        elif key == "fx_particle_role_name":
            parsed_role_name = str(value).strip() if value else ""
            changes[key] = parsed_role_name or None
        elif key == "fx_particle_color_layers":
            changes[key] = (
                value
                if value in {"solid", "stroke", "decor", "all"}
                else defaults.fx_particle_color_layers
            )
        elif key in {"anim_fx_color", "anim_fx_color2",
                     "scanline_effect_color", "scanline_effect_color2"}:
            changes[key] = (
                value
                if isinstance(value, str)
                and value.startswith("#")
                and len(value) in {7, 9}
                and all(
                    character in "0123456789abcdefABCDEF"
                    for character in value[1:]
                )
                else getattr(defaults, key)
            )
        elif key == "anim_fx_size_em":
            try:
                anim_em = float(value)
            except (TypeError, ValueError):
                anim_em = defaults.anim_fx_size_em
            changes[key] = max(0.05, min(2.0, anim_em))
        elif key == "anim_fx_color_mode":
            changes[key] = (
                value
                if value in {
                    "color", "sakura", "follow_mix", "follow_before",
                    "follow_after", "role",
                }
                else defaults.anim_fx_color_mode
            )
        elif key in {"anim_fx_role_name", "scanline_effect_role_name"}:
            parsed_role_name = str(value).strip() if value else ""
            changes[key] = parsed_role_name or None
        elif key == "anim_fx_color_layers":
            changes[key] = (
                value
                if value in {"solid", "stroke", "decor", "all"}
                else defaults.anim_fx_color_layers
            )
        elif key == "scanline_effect_color_mode":
            changes[key] = (
                value
                if value in {
                    "theme", "color", "sakura", "follow_mix",
                    "follow_before", "follow_after", "role",
                }
                else defaults.scanline_effect_color_mode
            )
        elif key == "scanline_effect_color_layers":
            changes[key] = (
                value
                if value in {"solid", "stroke", "decor", "all"}
                else defaults.scanline_effect_color_layers
            )
        elif key == "fx_apply_to_entry_exit":
            changes[key] = bool(value)
        elif key == "karaoke_stroke_flash":
            changes[key] = bool(value)
        elif key in {"karaoke_anim", "reverse_karaoke_anim"}:
            changes[key] = (
                value
                if value in {
                    "inherit", "none", "no_wipe", "utopia", "scanline", "utopia_scanline",
                    "zoom_pulse", "zoom_pulse_scanline"
                }
                else getattr(defaults, key)
            )
        elif key == "scanline_mode":
            changes[key] = (
                value
                if value in {
                    "default",
                    "color",
                    "brighten",
                    "follow_before",
                    "follow_after",
                    "role",
                }
                else defaults.scanline_mode
            )
        elif key == "scanline_effect":
            if value in {"flame", "bubble"}:
                # 旧试行档位（粒子形态的火焰/气泡）升级为「形态」：读到旧值
                # 时清空粒子特效并映射成对应的柱状形态，不丢用户设定。
                changes[key] = "none"
                changes.setdefault("scanline_shape", value)
            else:
                changes[key] = (
                    value
                    if isinstance(value, str) and value in SCANLINE_EFFECT_KINDS
                    else defaults.scanline_effect
                )
        elif key == "scanline_shape":
            changes[key] = (
                value
                if isinstance(value, str) and value in SCANLINE_SHAPE_KINDS
                else defaults.scanline_shape
            )
        elif key == "scanline_role_name":
            parsed_role_name = str(value).strip() if value else ""
            changes[key] = parsed_role_name or None
        elif key in {"lit_role_name", "volume_role_name"}:
            parsed_role_name = str(value).strip() if value else ""
            changes[key] = parsed_role_name or None
        elif key == "section_head_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
                    "stretch_in", "glow_in", "assemble_in", "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid", "flame", "feather", "lightning",
                }
                else defaults.section_head_anim
            )
        elif key == "section_tail_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
                    "stretch_out", "glow_out", "dissolve_out", "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid", "flame", "feather", "lightning",
                }
                else defaults.section_tail_anim
            )
        elif key in {
            "font_family_latin",
            "ruby_font_family",
            "ruby_font_family_latin",
        }:
            changes[key] = str(value) if value else None
        elif key == "latin_font_stretch_pct":
            changes[key] = max(50, min(200, _int_value(value, 100)))
        elif key == "ruby_latin_font_stretch_pct":
            changes[key] = (
                None if value is None
                else max(50, min(200, _int_value(value, 100)))
            )
        elif key in {
            "latin_font_size_px",
            "latin_font_weight",
            "latin_stroke_width_px",
            "latin_stroke2_width_px",
            "ruby_font_weight",
            "ruby_latin_font_size_px",
            "ruby_latin_font_weight",
            "ruby_latin_stroke_width_px",
            "ruby_latin_stroke2_width_px",
        }:
            # N3 的子字体槽用 0 表示沿 fallback 链继承，而不是显式零尺寸。
            parsed = _int_value(value, 0)
            changes[key] = parsed if parsed > 0 else None
        elif key in {
            "ruby_stroke_width_px",
            "ruby_stroke2_width_px",
        }:
            # 注音日文描边仍允许显式 0；只有英数槽的 0 表示继承。
            changes[key] = None if value is None else _int_value(value, 0)
        elif key in {
            "latin_stroke2_enabled",
            "ruby_stroke2_enabled",
            "ruby_latin_stroke2_enabled",
            "latin_italic",
            "ruby_italic",
            "ruby_latin_italic",
            "scanline_enabled",
            "scanline_reverse_enabled",
            "scanline_forward_enabled",
            "fill_gradient_enabled",
        }:
            changes[key] = None if value is None else _bool_value(value)
        elif value is not None:
            changes[key] = str(value)
    if "glow_radius_px" in changes:
        if "glow_before_radius_px" not in changes:
            changes["glow_before_radius_px"] = changes["glow_radius_px"]
        if "glow_after_radius_px" not in changes:
            changes["glow_after_radius_px"] = changes["glow_radius_px"]
    # 旧工程迁移：没有 horizontal_margin_px 时沿用旧的上排左边距（默认双双为 50）。
    if "horizontal_margin_px" not in changes and "upper_line_left_margin_px" in changes:
        changes["horizontal_margin_px"] = changes["upper_line_left_margin_px"]
    if "ruby_colors_follow_main" not in payload:
        # 旧工程没有显式开关：已有独立注音矩阵继续独立，否则采用新默认跟随。
        changes["ruby_colors_follow_main"] = (
            changes.get("ruby_karaoke_colors") is None
        )
    if "decoration_before_sync" not in payload:
        # 旧工程没有装饰同步开关：新工程默认开启；旧版 .yurika 默认关闭，
        # 但走字前槽位仍为 None（实时跟随走字后），画面不变。
        changes["decoration_before_sync"] = not legacy_project
    # 旧版「入退场联动」：开启时入退场粒子曾直接复用唱字粒子的颜色/尺寸。
    # 分离设置后一次性搬进 anim_fx_*，旧工程画面不变；未开启则保持
    # anim_fx_* 默认档（旧固定默认 = 樱花粉/40%，与旧画面一致）。
    if (
        not any(str(key).startswith("anim_fx_") for key in payload)
        and bool(payload.get("fx_apply_to_entry_exit"))
    ):
        changes.setdefault(
            "anim_fx_color_mode",
            changes.get("fx_particle_color_mode", defaults.fx_particle_color_mode),
        )
        changes.setdefault(
            "anim_fx_color",
            changes.get("fx_particle_color", defaults.fx_particle_color),
        )
        changes.setdefault(
            "anim_fx_color2",
            changes.get("fx_particle_color2", defaults.fx_particle_color2),
        )
        changes.setdefault(
            "anim_fx_color_layers",
            changes.get(
                "fx_particle_color_layers", defaults.fx_particle_color_layers
            ),
        )
        changes.setdefault(
            "anim_fx_role_name",
            changes.get("fx_particle_role_name", defaults.fx_particle_role_name),
        )
        changes.setdefault(
            "anim_fx_size_em",
            changes.get("fx_particle_size_em", defaults.fx_particle_size_em),
        )
    # 旧版扫字线粒子与唱字装饰粒子共用 fx_particle_*：只有用户确实改过
    # 颜色模式/双色槽时才把旧设置搬进 scanline_effect_color_*；全默认白
    # 对应旧「主题色」档，保持新默认 theme 即可，不覆盖。
    if (
        "scanline_effect_color_mode" not in payload
        and "scanline_effect_color" not in payload
    ):
        effect = str(changes.get("scanline_effect", defaults.scanline_effect))
        old_mode = str(
            changes.get("fx_particle_color_mode", defaults.fx_particle_color_mode)
        )
        old_color = str(
            changes.get("fx_particle_color", defaults.fx_particle_color)
        )
        old_color2 = str(
            changes.get("fx_particle_color2", defaults.fx_particle_color2)
        )
        customized = not (
            old_mode == "color"
            and old_color.upper() == "#FFFFFF"
            and old_color2.upper() == "#FFFFFF"
        )
        if effect not in {"none", "flame", "bubble"} and customized:
            changes.setdefault("scanline_effect_color_mode", old_mode)
            changes.setdefault("scanline_effect_color", old_color)
            changes.setdefault("scanline_effect_color2", old_color2)
            changes.setdefault(
                "scanline_effect_color_layers",
                changes.get(
                    "fx_particle_color_layers",
                    defaults.scanline_effect_color_layers,
                ),
            )
            changes.setdefault(
                "scanline_effect_role_name",
                changes.get(
                    "fx_particle_role_name", defaults.scanline_effect_role_name
                ),
            )
    # 旧工程用 lit_enabled + lit_style=volume 表示音量柱。新版把两个模块
    # 分开；只有缺少新字段时才迁移，避免覆盖新版工程的显式组合。
    if "volume_enabled" not in payload and changes.get("lit_style") == "volume":
        changes["volume_enabled"] = bool(changes.get("lit_enabled", False))
        changes["lit_enabled"] = False
        changes["lit_style"] = "circle"
        changes["volume_duration_ms"] = changes.get("signals_duration_ms", 4000)
        changes["volume_waiting_time_ms"] = changes.get("lit_waiting_time_ms", 0)
        changes["volume_time_offset_ms"] = changes.get("lit_time_offset_ms", 0)
        changes["volume_stroke_width"] = changes.get("lit_stroke_width", 2)
        changes["volume_opacity_pct"] = changes.get("lit_opacity_pct", 100)
    _migrate_title_references(changes)
    _migrate_scanline_toggle(changes, payload)
    _migrate_scanline_px_base(changes, payload)
    _migrate_signal_offset_px_base(changes, payload)
    return _migrate_ruby_follow_independence(ensure_page_layout_defaults(Style(**changes)))


def _migrate_scanline_toggle(changes: dict, payload: dict) -> None:
    """旧工程把扫字线档位迁到独立总开关 + 反向应用开关。"""

    base_by_mode = {
        "scanline": "none",
        "utopia_scanline": "utopia",
        "zoom_pulse_scanline": "zoom_pulse",
    }
    raw_forward = str(changes.get("karaoke_anim", "inherit") or "inherit")
    raw_reverse = str(
        changes.get("reverse_karaoke_anim", "inherit") or "inherit"
    )
    forward_old = raw_forward in base_by_mode
    reverse_old = raw_reverse in base_by_mode
    if forward_old:
        changes["karaoke_anim"] = base_by_mode[raw_forward]
    if reverse_old:
        changes["reverse_karaoke_anim"] = base_by_mode[raw_reverse]
    if "scanline_enabled" not in payload:
        changes["scanline_enabled"] = forward_old or reverse_old
    changes["scanline_legacy_global"] = bool(forward_old or reverse_old)
    if "scanline_forward_enabled" not in payload:
        if forward_old:
            changes["scanline_forward_enabled"] = True
        elif reverse_old:
            changes["scanline_forward_enabled"] = False
    if "scanline_reverse_enabled" not in payload:
        if reverse_old:
            changes["scanline_reverse_enabled"] = True
        elif raw_reverse == "inherit" and forward_old:
            changes["scanline_reverse_enabled"] = True


def _migrate_scanline_px_base(changes: dict, payload: dict) -> None:
    """旧数据迁移：扫字线像素值 → 固定 1080 基准存储。

    v4.3.2 起扫字线粗细/柔化恒存 ``SCANLINE_BASE_HEIGHT``(1080)基准值,
    ``style_to_dict`` 会写 ``scanline_px_base`` 标记。旧 payload(无标记)
    里的值是其工程 ``font_reference_height`` 画布下的实画值,读取时一次性
    折算到 1080 基准——例如 4K(2160)工程里 100px 实画值迁移为基准 50。
    """
    if payload.get("scanline_px_base") == SCANLINE_BASE_HEIGHT:
        return
    if not any(name in changes for name in _SCANLINE_SIZE_FIELDS):
        return
    reference = _int_value(payload.get("font_reference_height"), SCANLINE_BASE_HEIGHT)
    reference = max(int(reference), 1)
    if reference == SCANLINE_BASE_HEIGHT:
        return
    for name in _SCANLINE_SIZE_FIELDS:
        if name in changes:
            changes[name] = scanline_base_px_from_output(changes[name], reference)


def _migrate_signal_offset_px_base(changes: dict, payload: dict) -> None:
    """旧数据迁移：信号偏移改为固定 1080 基准存储（同扫字线口径）。

    新版 ``style_to_dict`` 恒写 ``signal_offset_px_base`` 标记；旧 payload
    （无标记）里的偏移是其工程 ``font_reference_height`` 画布下的绝对值，
    读取时一次性折算到 1080 基准，画面保持不变。
    """
    if payload.get("signal_offset_px_base") == SIGNAL_OFFSET_BASE_HEIGHT:
        return
    if not any(name in changes for name in _SIGNAL_OFFSET_FIELDS):
        return
    reference = _int_value(
        payload.get("font_reference_height"), SIGNAL_OFFSET_BASE_HEIGHT
    )
    reference = max(int(reference), 1)
    if reference == SIGNAL_OFFSET_BASE_HEIGHT:
        return
    for name in _SIGNAL_OFFSET_FIELDS:
        if name in changes:
            changes[name] = signal_offset_base_px_from_output(
                changes[name], reference
            )


def _migrate_title_references(changes: dict) -> None:
    """旧工程标题迁移：显式外观/位置字段 → 「标题」方案 + 布局引用。

    新版工程恒满足两个不变量：``custom_style_schemes`` 含 ``TITLE_SCHEME_NAME``、
    启用标题时 ``layout_index`` 非 None。旧工程加载时按原 ``TitleOverlay``
    字段折算补齐，保证外观不变。多标题条目只可能来自新版工程（旧版单标题
    key 只折算出一条），因此方案补齐只看第一条，布局引用逐条目处理。
    """
    titles = list(changes.get("title_overlays") or [])
    first_title = titles[0] if titles else None
    schemes = changes.get("custom_style_schemes")
    if schemes is None:
        # 快照没有方案字典：仅当标题需要迁移时显式给出（否则交给默认值）。
        if first_title is not None:
            changes["custom_style_schemes"] = {
                TITLE_SCHEME_NAME: title_scheme_from_overlay(first_title)
            }
    elif TITLE_SCHEME_NAME not in schemes:
        schemes = dict(schemes)
        schemes[TITLE_SCHEME_NAME] = (
            title_scheme_from_overlay(first_title)
            if first_title is not None
            else default_title_scheme()
        )
        changes["custom_style_schemes"] = schemes
    else:
        # 8c3b9b5 之前的标题方案没有英数独立字段。英数轨加入后，这些 None
        # 会被解释成继承全局歌词方案，导致英文曲名/歌手名突然换字体和描边。
        # 只补缺失字段，保留用户或 N3 项目显式保存的英数标题设置。
        title_scheme = schemes[TITLE_SCHEME_NAME]
        title_family = title_scheme.font_family or TitleOverlay().font_family
        title_size = title_scheme.font_size_px or TitleOverlay().font_size_px
        title_weight = title_scheme.font_weight or TitleOverlay().font_weight
        title_stroke = (
            title_scheme.stroke_width_px
            if title_scheme.stroke_width_px is not None
            else TitleOverlay().stroke_width_px
        )
        title_stroke2_enabled = (
            title_scheme.stroke2_enabled
            if title_scheme.stroke2_enabled is not None
            else TitleOverlay().stroke2_width_px > 0
        )
        title_stroke2 = (
            title_scheme.stroke2_width_px
            if title_scheme.stroke2_width_px is not None
            else TitleOverlay().stroke2_width_px
        )
        completed = replace(
            title_scheme,
            font_family_latin=title_scheme.font_family_latin or title_family,
            latin_font_size_px=(
                title_scheme.latin_font_size_px
                if title_scheme.latin_font_size_px is not None
                else title_size
            ),
            latin_font_weight=(
                title_scheme.latin_font_weight
                if title_scheme.latin_font_weight is not None
                else title_weight
            ),
            latin_font_stretch_pct=(
                title_scheme.latin_font_stretch_pct
                if title_scheme.latin_font_stretch_pct is not None
                else 100
            ),
            latin_stroke_width_px=(
                title_scheme.latin_stroke_width_px
                if title_scheme.latin_stroke_width_px is not None
                else title_stroke
            ),
            latin_stroke2_enabled=(
                title_scheme.latin_stroke2_enabled
                if title_scheme.latin_stroke2_enabled is not None
                else title_stroke2_enabled
            ),
            latin_stroke2_width_px=(
                title_scheme.latin_stroke2_width_px
                if title_scheme.latin_stroke2_width_px is not None
                else title_stroke2
            ),
        )
        if completed != title_scheme:
            schemes = dict(schemes)
            schemes[TITLE_SCHEME_NAME] = completed
            changes["custom_style_schemes"] = schemes
    migrated_titles: list[TitleOverlay] = []
    titles_changed = False
    layouts = list(changes.get("layouts") or [])
    for title in titles:
        if title.layout_index is not None:
            migrated_titles.append(title)
            continue
        layouts.append(
            _layout_from_title_position(title, {layout.name for layout in layouts})
        )
        migrated_titles.append(replace(title, layout_index=len(layouts)))
        titles_changed = True
    if titles_changed:
        changes["layouts"] = layouts
        changes["title_overlays"] = migrated_titles
    if "title_overlays" not in changes:
        # 旧工程没有标题配置（key 缺失或 null）：补默认**禁用**条目并固定
        # whole 语义——不跟随 TitleOverlay 的新默认（custom），保证旧工程
        # 启用标题后的行为与历史版本一致。
        changes["title_overlays"] = [
            replace(TitleOverlay(), show_mode="whole", custom_windows=[])
        ]


def _layout_from_title_position(
    title: TitleOverlay, existing_names: set[str]
) -> LyricsLayout:
    """旧工程标题的 anchor/offset → 等效布局。居中锚点的正负偏移语义无法用
    余白表达，按 0 余白近似（默认标题为 top_left，几乎不受影响）。"""
    anchor = str(title.anchor)
    if anchor.endswith("left"):
        horizontal = "left"
    elif anchor.endswith("right"):
        horizontal = "right"
    else:
        horizontal = "center"
    vertical = (
        "top" if anchor.startswith("top")
        else "bottom" if anchor.startswith("bottom")
        else "center"
    )
    name = TITLE_LAYOUT_NAME
    suffix = 2
    while name in existing_names:
        name = f"{TITLE_LAYOUT_NAME} {suffix}"
        suffix += 1
    return LyricsLayout(
        name=name,
        line_y_position=vertical,  # type: ignore[arg-type]
        line_y_margin_px=max(int(title.offset_y), 0),
        line_gap_px=max(int(title.line_gap_px), 0),
        smart_horizontal="equal_margins",
        horizontal_margin_px=max(int(title.offset_x), 0),
        line_alignments=[horizontal],  # type: ignore[list-item]
    )


def rescale_layout_sizes(style: Style, new_height: int) -> Style:
    """输出高度变化时按 N3 ``SizeAndRatio`` 语义重算布局像素字段。

    ``new = int(new_height * old / reference)``（向 0 截断，0 保持 0），作用于
    默认布局与所有额外布局的 上下余白 / 行间距 / 左右余白；旧的上/下行边距
    镜像跟随左右余白。高度不变或非法时原样返回。
    """
    reference = max(int(style.layout_reference_height), 1)
    new_height = int(new_height)
    if new_height <= 0 or new_height == reference:
        return style

    def scaled(value: int) -> int:
        return int(new_height * (int(value) / reference))

    layouts = [
        replace(
            layout,
            line_y_margin_px=scaled(layout.line_y_margin_px),
            line_gap_px=scaled(layout.line_gap_px),
            horizontal_margin_px=scaled(layout.horizontal_margin_px),
            line_offsets_x=[scaled(value) for value in layout.line_offsets_x],
            letter_spacing_px=(
                None
                if layout.letter_spacing_px is None
                else scaled(layout.letter_spacing_px)
            ),
            ruby_interval_px=(
                None
                if layout.ruby_interval_px is None
                else scaled(layout.ruby_interval_px)
            ),
            ruby_gap_px=(
                None if layout.ruby_gap_px is None else scaled(layout.ruby_gap_px)
            ),
        )
        for layout in style.layouts
    ]
    margin = scaled(style.horizontal_margin_px)
    return replace(
        style,
        line_y_margin_px=scaled(style.line_y_margin_px),
        line_gap_px=scaled(style.line_gap_px),
        horizontal_margin_px=margin,
        line_offsets_x=[scaled(value) for value in style.line_offsets_x],
        letter_spacing_px=scaled(style.letter_spacing_px),
        ruby_interval_px=scaled(style.ruby_interval_px),
        ruby_gap_px=scaled(style.ruby_gap_px),
        upper_line_left_margin_px=margin,
        lower_line_right_margin_px=margin,
        layouts=layouts,
        layout_reference_height=new_height,
    )


_FONT_VISUAL_SIZE_FIELDS: tuple[str, ...] = (
    "font_size_px",
    "latin_font_size_px",
    "stroke_width_px",
    "latin_stroke_width_px",
    "stroke2_width_px",
    "latin_stroke2_width_px",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "shadow_offset_x",
    "shadow_offset_y",
    "shadow_before_offset_x",
    "shadow_before_offset_y",
    "ruby_font_size_px",
    "ruby_latin_font_size_px",
    "ruby_stroke_width_px",
    "ruby_stroke2_width_px",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_width_px",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_shadow_before_offset_x",
    "ruby_shadow_before_offset_y",
)

_TITLE_FONT_VISUAL_SIZE_FIELDS: tuple[str, ...] = (
    "font_size_px",
    "stroke_width_px",
    "stroke2_width_px",
    "glow_radius_px",
    "shadow_offset_x",
    "shadow_offset_y",
)

# 扫字线像素字段（粗细 / 柔化半径）采用**固定 1080 基准**存储：内部与存盘
# 恒为 1080 画布下的值，不随输出高度 rescale——前台（编辑 spin、渲染）按
# 当前画布高度从基准值映射，任何画布切换都从同一基准重新推导，与切换
# 历史无关。它们是 Style 上的全局绘制参数，``SubtitleStyleScheme``（配色
# 方案 / 样式预设）不含这些字段，因此不能并入 ``_FONT_VISUAL_SIZE_FIELDS``
# ——``rescale_scheme_font_sizes`` 会按该表对 scheme 逐字段 ``getattr``，
# 混入会导致 AttributeError。
_SCANLINE_SIZE_FIELDS: tuple[str, ...] = ("scanline_width_px", "scanline_glow_px")

SCANLINE_BASE_HEIGHT = 1080
"""扫字线像素字段（:data:`_SCANLINE_SIZE_FIELDS`）的固定存储基准高度。"""


def _base_scaled_px(value: int, source_height: int, target_height: int) -> int:
    """基准↔输出高度的通用像素换算（扫字线像素 / 信号偏移共用）。"""
    source = max(int(source_height), 1)
    target = max(int(target_height), 1)
    value = int(value)
    if source == target or value == 0:
        return value
    return int(round(value * target / source))


def scanline_px_for_output(value: int, output_height: int) -> int:
    """1080 基准的扫字线像素值 → 目标输出高度下的实画值。"""
    return _base_scaled_px(value, SCANLINE_BASE_HEIGHT, output_height)


def scanline_base_px_from_output(value: int, output_height: int) -> int:
    """输出高度下的实画值 → 1080 基准存储值（编辑写回用，反方向换算）。"""
    return _base_scaled_px(value, output_height, SCANLINE_BASE_HEIGHT)


def style_with_output_scanline(style: Style, output_height: int) -> Style:
    """渲染入口用：把基准语义的扫字线像素字段换算为输出高度下的实画值。

    1080 输出（或非法高度）原样返回同一对象，避免等值替换造成对象 churn。
    """
    height = int(output_height)
    if height <= 0 or height == SCANLINE_BASE_HEIGHT:
        return style
    return replace(
        style,
        scanline_width_px=scanline_px_for_output(style.scanline_width_px, height),
        scanline_glow_px=scanline_px_for_output(style.scanline_glow_px, height),
    )


# 指示灯/音量柱偏移字段（lit_offset_x/y、volume_offset_x/y）与扫字线像素
# 字段同口径：**固定 1080 基准**存储——内部与存盘恒为 1080 画布下的值，
# 不随输出高度 rescale；前台（编辑 spin、渲染）按当前画布高度从基准值
# 映射，任何画布切换都从同一基准重新推导，与切换历史无关。
_SIGNAL_OFFSET_FIELDS: tuple[str, ...] = (
    "lit_offset_x",
    "lit_offset_y",
    "volume_offset_x",
    "volume_offset_y",
)

SIGNAL_OFFSET_BASE_HEIGHT = 1080
"""指示灯/音量柱偏移字段的固定存储基准高度。"""


def signal_offset_px_for_output(value: int, output_height: int) -> int:
    """1080 基准的信号偏移值 → 目标输出高度下的实画值。"""
    return _base_scaled_px(value, SIGNAL_OFFSET_BASE_HEIGHT, output_height)


def signal_offset_base_px_from_output(value: int, output_height: int) -> int:
    """输出高度下的实画值 → 1080 基准存储值（编辑写回用，反方向换算）。"""
    return _base_scaled_px(value, output_height, SIGNAL_OFFSET_BASE_HEIGHT)


def style_with_output_signal_offsets(style: Style, output_height: int) -> Style:
    """渲染入口用：把基准语义的信号偏移换算为输出高度下的实画值。

    1080 输出（或非法高度）原样返回同一对象，避免等值替换造成对象 churn。
    """
    height = int(output_height)
    if height <= 0 or height == SIGNAL_OFFSET_BASE_HEIGHT:
        return style
    return replace(
        style,
        **{
            name: signal_offset_px_for_output(getattr(style, name), height)
            for name in _SIGNAL_OFFSET_FIELDS
        },
    )


def rescale_scheme_font_sizes(
    scheme: SubtitleStyleScheme,
    reference_height: int,
    target_height: int,
) -> SubtitleStyleScheme:
    """按 N3 ``SizeAndRatio`` 语义在两个输出高度之间换算单个配色方案。

    与 :func:`rescale_font_sizes` 共用字段表和向 0 截断规则；``None`` 字段
    保持 ``None`` 以保留继承语义，高度一致或非法时原样返回。样式预设库
    保存/应用时用它把像素字段在 ``PRESET_REFERENCE_HEIGHT`` 与项目输出
    高度之间互转。
    """
    reference = max(int(reference_height), 1)
    target = int(target_height)
    if target <= 0 or target == reference:
        return scheme

    def scaled(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return int(target * (int(value) / reference))

    return replace(
        scheme,
        **{name: scaled(getattr(scheme, name)) for name in _FONT_VISUAL_SIZE_FIELDS},
    )


def rescale_font_sizes(style: Style, new_height: int) -> Style:
    """Scale font visual pixel fields when the output height changes.

    This mirrors N3's ``SizeAndRatio`` arithmetic: values are multiplied by
    ``new_height / font_reference_height`` and truncated toward zero. Optional
    overrides remain ``None`` so their inheritance semantics are preserved.
    Character/layout spacing is handled separately by ``rescale_layout_sizes``.
    Scanline pixel fields use a fixed ``SCANLINE_BASE_HEIGHT`` storage base and
    are rescaled at render time instead (see ``style_with_output_scanline``),
    so they deliberately stay untouched here.
    """
    reference = max(int(style.font_reference_height), 1)
    new_height = int(new_height)
    if new_height <= 0 or new_height == reference:
        return style

    def scaled(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return int(new_height * (int(value) / reference))

    def scale_dataclass(value: object, names: tuple[str, ...]):
        return replace(
            value,
            **{name: scaled(getattr(value, name)) for name in names},
        )

    custom_schemes = {
        name: rescale_scheme_font_sizes(scheme, reference, new_height)
        for name, scheme in style.custom_style_schemes.items()
    }
    singer_overrides = {
        singer: rescale_scheme_font_sizes(scheme, reference, new_height)
        for singer, scheme in style.singer_style_overrides.items()
    }
    title_overlays = [
        scale_dataclass(overlay, _TITLE_FONT_VISUAL_SIZE_FIELDS)
        for overlay in style.title_overlays
    ]
    changes = {
        name: scaled(getattr(style, name)) for name in _FONT_VISUAL_SIZE_FIELDS
    }
    return replace(
        style,
        **changes,
        custom_style_schemes=custom_schemes,
        singer_style_overrides=singer_overrides,
        title_overlays=title_overlays,
        font_reference_height=new_height,
    )


def lyrics_layout_to_dict(layout: LyricsLayout) -> dict:
    return {
        "name": layout.name,
        "layout_id": layout.layout_id,
        "line_y_position": layout.line_y_position,
        "line_y_margin_px": layout.line_y_margin_px,
        "line_gap_px": layout.line_gap_px,
        "smart_horizontal": layout.smart_horizontal,
        "horizontal_margin_px": layout.horizontal_margin_px,
        "line_alignments": list(layout.line_alignments),
        "line_offsets_x": list(layout.line_offsets_x),
        "force_top_bottom_n3": layout.force_top_bottom_n3,
        "letter_spacing_px": layout.letter_spacing_px,
        "space_width_percent": layout.space_width_percent,
        "allow_biting": layout.allow_biting,
        "ruby_interval_px": layout.ruby_interval_px,
        "ruby_alignment": layout.ruby_alignment,
        "ruby_gap_px": layout.ruby_gap_px,
    }


def lyrics_layout_from_dict(payload: object) -> LyricsLayout:
    if not isinstance(payload, dict):
        return LyricsLayout()
    defaults = LyricsLayout()
    position = payload.get("line_y_position", defaults.line_y_position)
    if position not in {"top", "center", "bottom"}:
        position = defaults.line_y_position
    smart = payload.get("smart_horizontal", defaults.smart_horizontal)
    if smart not in SMART_HORIZONTALS:
        smart = defaults.smart_horizontal
    return LyricsLayout(
        name=str(payload.get("name") or defaults.name),
        layout_id=str(payload.get("layout_id") or ""),
        line_y_position=position,  # type: ignore[arg-type]
        line_y_margin_px=_int_value(payload.get("line_y_margin_px"), defaults.line_y_margin_px),
        line_gap_px=_int_value(payload.get("line_gap_px"), defaults.line_gap_px),
        smart_horizontal=smart,  # type: ignore[arg-type]
        horizontal_margin_px=_int_value(
            payload.get("horizontal_margin_px"), defaults.horizontal_margin_px
        ),
        line_alignments=_line_alignments_from_payload(payload.get("line_alignments")),
        line_offsets_x=_line_offsets_from_payload(payload.get("line_offsets_x")),
        letter_spacing_px=(
            _int_value(payload.get("letter_spacing_px"), 0)
            if payload.get("letter_spacing_px") is not None
            else None
        ),
        space_width_percent=(
            _int_value(payload.get("space_width_percent"), 20)
            if payload.get("space_width_percent") is not None
            else None
        ),
        allow_biting=(
            bool(payload.get("allow_biting"))
            if payload.get("allow_biting") is not None
            else None
        ),
        force_top_bottom_n3=(
            bool(payload.get("force_top_bottom_n3"))
            if payload.get("force_top_bottom_n3") is not None
            else None
        ),
        ruby_interval_px=(
            _int_value(payload.get("ruby_interval_px"), 0)
            if payload.get("ruby_interval_px") is not None
            else None
        ),
        ruby_alignment=(
            payload.get("ruby_alignment")
            if payload.get("ruby_alignment") in RUBY_ALIGNMENTS
            else None
        ),
        ruby_gap_px=(
            _int_value(payload.get("ruby_gap_px"), 0)
            if payload.get("ruby_gap_px") is not None
            else None
        ),
    )


def _layouts_from_payload(payload: object) -> list[LyricsLayout]:
    if not isinstance(payload, list):
        return []
    return [lyrics_layout_from_dict(item) for item in payload[:32]]


def migrate_spacing_bindings_to_used_layouts(
    style: Style,
    lines: Iterable[object],
) -> Style:
    """旧工程迁移：空格宽度 / 字间距从方案域收敛到布局域。

    - 空格宽度：按旧语义逐行统计实际生效值（歌手覆盖优先，否则全局），
      取覆盖行数最多的值（并列时全局值优先）绑定到所有被引用的布局
      （含默认布局=全局字段与标题引用的布局）；方案 / 歌手覆盖里的空格
      宽度槽位全部丢弃（置 ``None``）。
    - 字间距：被引用布局槽位为 ``None`` 时物化为全局值，已有显式值保留。

    对迁移后的样式重复执行是幂等的。
    """

    counts: dict[int, int] = {}
    for line in lines:
        singer_id = getattr(line, "singer_id", None)
        override = (
            style.singer_style_overrides.get(singer_id)
            if singer_id is not None
            else None
        )
        value = (
            override.space_width_percent
            if override is not None and override.space_width_percent is not None
            else style.space_width_percent
        )
        value = int(value)
        counts[value] = counts.get(value, 0) + 1
    global_space = int(style.space_width_percent)
    if counts:
        space = max(counts, key=lambda v: (counts[v], v == global_space, v))
    else:
        space = global_space

    used = {0}
    for line in lines:
        index = int(getattr(line, "layout_index", 0) or 0)
        if 0 <= index <= len(style.layouts):
            used.add(index)
    for title in style.title_overlays:
        if title.layout_index is not None:
            title_index = int(title.layout_index)
            if 0 <= title_index <= len(style.layouts):
                used.add(title_index)

    letter_global = int(style.letter_spacing_px)
    layouts = [
        (
            replace(
                layout,
                space_width_percent=space,
                letter_spacing_px=(
                    layout.letter_spacing_px
                    if layout.letter_spacing_px is not None
                    else letter_global
                ),
            )
            if index in used
            else layout
        )
        for index, layout in enumerate(style.layouts, start=1)
    ]
    return replace(
        style,
        space_width_percent=space,
        layouts=layouts,
        custom_style_schemes={
            name: replace(scheme, space_width_percent=None)
            for name, scheme in style.custom_style_schemes.items()
        },
        singer_style_overrides={
            singer_id: replace(scheme, space_width_percent=None)
            for singer_id, scheme in style.singer_style_overrides.items()
        },
    )


def _line_offsets_from_payload(payload: object) -> list[int]:
    if not isinstance(payload, list):
        return []
    return [_int_value(value, 0) for value in payload[:8]]


def _line_alignments_from_payload(payload: object) -> list[HorizontalAlign]:
    """校验每行对齐列表；非法项回退 left，空列表回退默认双行。"""
    if not isinstance(payload, list):
        return ["left", "right"]
    result: list[HorizontalAlign] = [
        value if value in HORIZONTAL_ALIGNS else "left" for value in payload
    ]
    result = result[:8]  # 行数上限，防御异常数据
    return result or ["left", "right"]


def subtitle_style_scheme_to_dict(scheme: SubtitleStyleScheme) -> dict:
    data: dict = {}
    for item in fields(SubtitleStyleScheme):
        value = getattr(scheme, item.name)
        if item.name in {"karaoke_colors", "ruby_karaoke_colors"}:
            data[item.name] = karaoke_colors_to_dict(value) if value is not None else None
        else:
            data[item.name] = value
    return data


def subtitle_style_scheme_from_dict(payload: object) -> SubtitleStyleScheme:
    if not isinstance(payload, dict):
        return SubtitleStyleScheme()
    changes: dict = {}
    scheme_fields = {item.name for item in fields(SubtitleStyleScheme)}
    for key, value in payload.items():
        if key not in scheme_fields:
            continue
        if key in {"karaoke_colors", "ruby_karaoke_colors"}:
            changes[key] = karaoke_colors_from_dict(value)
        elif key in {"glow_concentration_level", "ruby_glow_concentration_level"}:
            changes[key] = (
                normalize_glow_concentration_level(value) if value is not None else None
            )
        elif key in {
            "glow_before_concentration_level",
            "ruby_glow_before_concentration_level",
        }:
            changes[key] = (
                normalize_glow_concentration_level(value) if value is not None else None
            )
        elif key in {
            "decoration_kind_before",
            "ruby_decoration_kind_before",
        }:
            changes[key] = (
                value if value in {"none", "shadow", "glow"} else None
            )
        elif key in {
            "font_family_latin",
            "ruby_font_family",
            "ruby_font_family_latin",
        }:
            changes[key] = str(value) if value else None
        elif key in {"latin_font_stretch_pct", "ruby_latin_font_stretch_pct"}:
            changes[key] = (
                None if value is None
                else max(50, min(200, _int_value(value, 100)))
            )
        elif key in {
            "latin_font_size_px",
            "latin_font_weight",
            "latin_stroke_width_px",
            "latin_stroke2_width_px",
            "ruby_font_weight",
            "ruby_latin_font_size_px",
            "ruby_latin_font_weight",
            "ruby_latin_stroke_width_px",
            "ruby_latin_stroke2_width_px",
        }:
            parsed = _int_value(value, 0)
            changes[key] = parsed if parsed > 0 else None
        elif key in {
            "ruby_colors_follow_main",
            "ruby_horizontal_gradient_with_main",
            "decoration_before_sync",
            "latin_italic",
            "ruby_italic",
            "ruby_latin_italic",
        }:
            changes[key] = bool(value) if value is not None else None
        elif key in {
            "shadow_before_offset_x",
            "shadow_before_offset_y",
            "ruby_shadow_before_offset_x",
            "ruby_shadow_before_offset_y",
        }:
            changes[key] = None if value is None else _int_value(value, 0)
        else:
            changes[key] = value
    has_ruby_override = (
        changes.get("ruby_karaoke_colors") is not None
        or any(
            changes.get(field_name) is not None
            for field_name in RUBY_DECORATION_OVERRIDE_FIELDS
        )
    )
    if "ruby_colors_follow_main" not in payload and has_ruby_override:
        # 旧角色方案保存过独立注音矩阵/装饰时，保留原行为。
        changes["ruby_colors_follow_main"] = False
    if changes.get("ruby_colors_follow_main") and any(
        changes.get(field_name) is not None
        for field_name in RUBY_DECORATION_OVERRIDE_FIELDS
    ):
        # 与全局迁移同款翻转：开关为开却带装饰独立覆盖 → 关（渲染本就按
        # 字段优先）；矩阵副本不参与翻转，跟随 N3 导入映射的既有语义。
        changes["ruby_colors_follow_main"] = False
    return SubtitleStyleScheme(**changes)


def title_overlay_to_dict(title: TitleOverlay) -> dict:
    return {
        "name": title.name,
        "enabled": title.enabled,
        "text_template": title.text_template,
        "char_role_labels": [list(row) for row in title.char_role_labels],
        "guide_symbols": [
            [int(row), guide_symbol_to_dict(symbol)]
            for row, symbol in sorted(title.guide_symbols.items())
            if symbol is not None
        ],
        "inline_guide_symbols": [
            [int(row), int(index), guide_symbol_to_dict(symbol)]
            for (row, index), symbol in sorted(title.inline_guide_symbols.items())
            if symbol is not None
        ],
        "scheme_name": title.scheme_name,
        "font_family": title.font_family,
        "font_family_latin": title.font_family_latin,
        "font_size_px": title.font_size_px,
        "font_weight": title.font_weight,
        "latin_font_stretch_pct": title.latin_font_stretch_pct,
        "italic": title.italic,
        "letter_spacing_px": title.letter_spacing_px,
        "line_gap_px": title.line_gap_px,
        "fill": paint_fill_to_dict(title.fill),
        "stroke": paint_fill_to_dict(title.stroke),
        "stroke_width_px": title.stroke_width_px,
        "stroke2": paint_fill_to_dict(title.stroke2),
        "stroke2_width_px": title.stroke2_width_px,
        "decoration_kind": title.decoration_kind,
        "glow_radius_px": title.glow_radius_px,
        "glow_concentration_level": title.glow_concentration_level,
        "shadow": paint_fill_to_dict(title.shadow),
        "shadow_offset_x": title.shadow_offset_x,
        "shadow_offset_y": title.shadow_offset_y,
        "anchor": title.anchor,
        "align": title.align,
        "offset_x": title.offset_x,
        "offset_y": title.offset_y,
        "layout_index": title.layout_index,
        "show_mode": title.show_mode,
        "head_offset_ms": title.head_offset_ms,
        "duration_ms": title.duration_ms,
        "tail_offset_ms": title.tail_offset_ms,
        "fade_in_ms": title.fade_in_ms,
        "fade_out_ms": title.fade_out_ms,
        "tail_duration_ms": title.tail_duration_ms,
        "tail_fade_in_ms": title.tail_fade_in_ms,
        "tail_fade_out_ms": title.tail_fade_out_ms,
        "custom_windows": [
            title_time_window_to_dict(window) for window in title.custom_windows
        ],
    }


def _title_guide_symbols_from_dict(
    text_template: str, payload: dict
) -> tuple[dict[int, GuideSymbol], dict[tuple[int, int], GuideSymbol]]:
    """解析标题导唱符的两个持久化列表并按当前模板文字裁剪。"""
    row_symbols: dict[int, GuideSymbol] = {}
    raw_rows = payload.get("guide_symbols")
    if isinstance(raw_rows, list):
        for entry in raw_rows:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            try:
                row = int(entry[0])
            except (TypeError, ValueError):
                continue
            symbol = guide_symbol_from_dict(entry[1])
            if symbol is not None:
                row_symbols[row] = symbol
    inline_symbols: dict[tuple[int, int], GuideSymbol] = {}
    raw_inline = payload.get("inline_guide_symbols")
    if isinstance(raw_inline, list):
        for entry in raw_inline:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                continue
            try:
                row, index = int(entry[0]), int(entry[1])
            except (TypeError, ValueError):
                continue
            symbol = guide_symbol_from_dict(entry[2])
            if symbol is not None:
                inline_symbols[(row, index)] = symbol
    return normalize_title_guide_symbols(
        text_template, row_symbols, inline_symbols
    )


def title_overlay_from_dict(payload: object) -> Optional[TitleOverlay]:
    if not isinstance(payload, dict):
        return None
    defaults = TitleOverlay()
    anchor = payload.get("anchor", defaults.anchor)
    if anchor not in TITLE_ANCHORS:
        anchor = defaults.anchor
    align = payload.get("align", defaults.align)
    if align not in HORIZONTAL_ALIGNS:
        align = defaults.align
    # 缺失 show_mode（更老的工程）固定回落 whole：不能跟随 TitleOverlay 的
    # 新默认 custom，否则旧工程重存后显示行为会变。
    show_mode = payload.get("show_mode", "whole")
    if show_mode not in TITLE_SHOW_MODES:
        show_mode = "whole"
    decoration = payload.get("decoration_kind", defaults.decoration_kind)
    if decoration not in {"none", "shadow", "glow"}:
        decoration = defaults.decoration_kind
    text_template = str(payload.get("text_template", defaults.text_template))
    scheme_name = payload.get("scheme_name", defaults.scheme_name)
    if scheme_name is not None:
        scheme_name = str(scheme_name).strip() or None
    guide_symbols, inline_guide_symbols = _title_guide_symbols_from_dict(
        text_template, payload
    )
    return TitleOverlay(
        name=str(payload.get("name", defaults.name)) or defaults.name,
        enabled=bool(payload.get("enabled", defaults.enabled)),
        text_template=text_template,
        scheme_name=scheme_name,
        char_role_labels=normalize_title_char_role_labels(
            text_template, payload.get("char_role_labels")
        ),
        guide_symbols=guide_symbols,
        inline_guide_symbols=inline_guide_symbols,
        font_family=str(payload.get("font_family", defaults.font_family)),
        font_family_latin=(
            str(payload["font_family_latin"])
            if payload.get("font_family_latin")
            else None
        ),
        font_size_px=_int_value(payload.get("font_size_px"), defaults.font_size_px),
        font_weight=_int_value(payload.get("font_weight"), defaults.font_weight),
        latin_font_stretch_pct=max(
            50, min(200, _int_value(payload.get("latin_font_stretch_pct"), 100))
        ),
        italic=bool(payload.get("italic", defaults.italic)),
        letter_spacing_px=_int_value(payload.get("letter_spacing_px"), defaults.letter_spacing_px),
        line_gap_px=_int_value(payload.get("line_gap_px"), defaults.line_gap_px),
        fill=paint_fill_from_dict(payload.get("fill"), fallback=defaults.fill.color),
        stroke=paint_fill_from_dict(payload.get("stroke"), fallback=defaults.stroke.color),
        stroke_width_px=_int_value(payload.get("stroke_width_px"), defaults.stroke_width_px),
        stroke2=paint_fill_from_dict(
            payload.get("stroke2"), fallback=defaults.stroke2.color
        ),
        stroke2_width_px=_int_value(payload.get("stroke2_width_px"), defaults.stroke2_width_px),
        decoration_kind=decoration,  # type: ignore[arg-type]
        glow_radius_px=_int_value(payload.get("glow_radius_px"), defaults.glow_radius_px),
        glow_concentration_level=normalize_glow_concentration_level(
            payload.get("glow_concentration_level"), defaults.glow_concentration_level
        ),
        shadow=paint_fill_from_dict(payload.get("shadow"), fallback=defaults.shadow.color),
        shadow_offset_x=_int_value(payload.get("shadow_offset_x"), defaults.shadow_offset_x),
        shadow_offset_y=_int_value(payload.get("shadow_offset_y"), defaults.shadow_offset_y),
        anchor=anchor,  # type: ignore[arg-type]
        align=align,  # type: ignore[arg-type]
        offset_x=_int_value(payload.get("offset_x"), defaults.offset_x),
        offset_y=_int_value(payload.get("offset_y"), defaults.offset_y),
        # 缺失（旧工程）时保持 None，由 style_from_dict 的迁移逻辑补布局引用。
        layout_index=(
            _int_value(payload.get("layout_index"), 0)
            if payload.get("layout_index") is not None
            else None
        ),
        show_mode=show_mode,  # type: ignore[arg-type]
        head_offset_ms=_int_value(payload.get("head_offset_ms"), defaults.head_offset_ms),
        duration_ms=_int_value(payload.get("duration_ms"), defaults.duration_ms),
        tail_offset_ms=_int_value(payload.get("tail_offset_ms"), defaults.tail_offset_ms),
        fade_in_ms=_int_value(payload.get("fade_in_ms"), defaults.fade_in_ms),
        fade_out_ms=_int_value(payload.get("fade_out_ms"), defaults.fade_out_ms),
        tail_duration_ms=(
            _int_value(payload.get("tail_duration_ms"), defaults.duration_ms)
            if payload.get("tail_duration_ms") is not None
            else None
        ),
        tail_fade_in_ms=(
            _int_value(payload.get("tail_fade_in_ms"), defaults.fade_in_ms)
            if payload.get("tail_fade_in_ms") is not None
            else None
        ),
        tail_fade_out_ms=(
            _int_value(payload.get("tail_fade_out_ms"), defaults.fade_out_ms)
            if payload.get("tail_fade_out_ms") is not None
            else None
        ),
        custom_windows=[
            window
            for window in (
                title_time_window_from_dict(item)
                for item in (
                    payload.get("custom_windows")
                    if isinstance(payload.get("custom_windows"), list)
                    else []
                )
            )
            if window is not None
        ],
    )


def title_line_units(line: str) -> list[str]:
    """把标题一行切成渲染单元格：组合记号并入基字并做 NFC 组合。

    与轨道加载入口的 ``normalize_combining_chars`` 同一语义（issue #14）：
    分解形浊点（テ+゙）单独成格会在逐字排版里撑出比全角假名还宽的空隙。
    标题没有逐字计时，单元格只在渲染 / 逐字编辑时枚举，因此不改动存储的
    ``text_template``，各消费点按本函数切分。可组合序列组合成合成形
    （デ），无合成形的组合（な+゙）保持分解但同格渲染。
    """
    return _title_unit_index_map(line)[0]


def _title_unit_index_map(line: str) -> tuple[list[str], list[int]]:
    """返回 ``(单元格序列, 码点→单元格下标映射)``；见 :func:`title_line_units`。"""
    units: list[str] = []
    index_map: list[int] = []
    for char in str(line):
        if units and unicodedata.category(char) in {"Mn", "Mc", "Me"}:
            units[-1] += char
        else:
            units.append(char)
        index_map.append(len(units) - 1)
    return [unicodedata.normalize("NFC", unit) for unit in units], index_map


def title_row_role(values: object) -> Optional[str]:
    """整行同一个角色时返回该角色名，否则 ``None``（默认或逐字符混排）。"""
    if not isinstance(values, (list, tuple)) or not values:
        return None
    labels = {
        (str(value).strip() or None) if value else None for value in values
    }
    if len(labels) != 1:
        return None
    return next(iter(labels))


def normalize_title_char_role_labels(
    text: str, payload: object
) -> list[list[Optional[str]]]:
    """把持久化标题标签规范成与当前逐行文字严格等长的单元格矩阵。

    整行同一个角色的行按「整行角色」处理：这种行与字符数无关，因此
    ``{title}`` / ``{artist}`` 展开成元数据、或标题文字改长改短之后，角色
    依然覆盖整行；只有逐字符混排的行才需要标签与文字严格对位。

    对位基是 :func:`title_line_units` 的渲染单元格。存量数据按码点保存
    （组合记号规范化之前）：标签数等于码点数时每格取首码点位置的标签，
    与新基无缝衔接。
    """
    raw_rows = payload if isinstance(payload, list) else []
    normalized: list[list[Optional[str]]] = []
    for row_index, line in enumerate(str(text).split("\n")):
        units, index_map = _title_unit_index_map(line)
        raw = raw_rows[row_index] if row_index < len(raw_rows) else []
        values = raw if isinstance(raw, (list, tuple)) else []
        row_role = title_row_role(values)
        if row_role is not None:
            normalized.append([row_role] * len(units))
            continue
        labels: list[Optional[str]] = []
        if len(values) == len(index_map) and len(values) != len(units):
            # 旧码点基：组合记号并入基字后行变短，每格取首码点位置的标签。
            first_positions: dict[int, int] = {}
            for position, unit_index in enumerate(index_map):
                first_positions.setdefault(unit_index, position)
            for unit_index in range(len(units)):
                position = first_positions.get(unit_index)
                value = (
                    values[position]
                    if position is not None and position < len(values)
                    else None
                )
                labels.append(str(value).strip() or None if value else None)
        else:
            # 单元格基直接对位；长度不齐时按位截断 / 补 None（历史行为）。
            for unit_index in range(len(units)):
                value = values[unit_index] if unit_index < len(values) else None
                labels.append(str(value).strip() or None if value else None)
        normalized.append(
            [
                labels[index] if index < len(labels) else None
                for index in range(len(units))
            ]
        )
    return normalized


def remap_scanline_role_reference(
    style: "Style", mapping: dict[str, Optional[str]]
) -> Optional["Style"]:
    """Rename or clear the scan-line role reference (``scanline_role_name``).

    Deleting the referenced role also resets ``scanline_mode`` to ``color``：
    不留一个引用悬空的 role 模式（渲染端虽会回退白色，但 UI 下拉会假装
    选中第一项）。Returns ``None`` when the reference is absent or untouched,
    so callers can skip the style write.
    """

    changes: dict[str, object] = {}
    for name_field, mode_field, fallback_mode in (
        ("scanline_role_name", "scanline_mode", "color"),
        ("scanline_effect_role_name", "scanline_effect_color_mode", "theme"),
    ):
        name = getattr(style, name_field, None)
        if name is None or name not in mapping:
            continue
        target = mapping.get(name)
        if target is None:
            changes[name_field] = None
            changes[mode_field] = fallback_mode
        else:
            changes[name_field] = target
    if not changes:
        return None
    return replace(style, **changes)


def remap_appearance_role_references(
    style: "Style", mapping: dict[str, Optional[str]]
) -> Optional["Style"]:
    """Rename the lit/volume appearance role references.

    指示灯/音量柱 ``role`` 外观档的来源名（``lit_role_name`` /
    ``volume_role_name``）与扫字线同一条改名维护链：改名连带改写引用。
    **删除被引用角色时不动**——保留悬空引用（UI 下拉以幽灵条目展示原名，
    渲染端按 auto 口径回退：段首行第一个角色），角色重建同名方案后自动
    接回，不静默改写用户的外观档选择。Returns ``None`` when no reference
    is touched, so callers can skip the style write.
    """

    changes: dict[str, object] = {}
    for name_field in ("lit_role_name", "volume_role_name"):
        name = getattr(style, name_field, None)
        if name is None or name not in mapping:
            continue
        target = mapping.get(name)
        if target is not None:
            changes[name_field] = target
    if not changes:
        return None
    return replace(style, **changes)


def remap_particle_role_reference(
    style: "Style", mapping: dict[str, Optional[str]]
) -> Optional["Style"]:
    """Rename or clear the particle color-mode role reference.

    粒子颜色 ``role`` 模式的来源名（``fx_particle_role_name``）与扫字线
    ``remap_scanline_role_reference`` 同一条改名/删除维护链：改名连带改写
    引用；删除被引用角色时连模式一起回退 ``color``（不留引用悬空的 role
    模式，UI 下拉不会假装选中第一项）。Returns ``None`` when the reference
    is absent or untouched, so callers can skip the style write.
    """

    changes: dict[str, object] = {}
    for name_field, mode_field, fallback_mode in (
        ("fx_particle_role_name", "fx_particle_color_mode", "color"),
        ("anim_fx_role_name", "anim_fx_color_mode", "sakura"),
    ):
        name = getattr(style, name_field, None)
        if name is None or name not in mapping:
            continue
        target = mapping.get(name)
        if target is None:
            changes[name_field] = None
            changes[mode_field] = fallback_mode
        else:
            changes[name_field] = target
    if not changes:
        return None
    return replace(style, **changes)


def remap_title_char_role_labels(
    title: TitleOverlay, mapping: dict[str, Optional[str]]
) -> Optional[TitleOverlay]:
    """Rename or clear every role reference owned by one title overlay.

    Covers the per-character matrix plus both guide-symbol slots (row-level
    and inline), so deleting a role cannot leave a dangling name behind in
    parts of the title that have no editor row of their own.

    The matrix is normalized on the way through: persisted raw rows can carry
    more cells than the current text (``{title}`` expansion, shortened lines),
    and those unreachable cells would otherwise keep feeding the role dropdown
    with a name that no longer has a scheme.

    Returns ``None`` when nothing referenced any remapped role and the stored
    matrix was already clean, so callers can skip both the style write and its
    undo entry.
    """

    labels = normalize_title_char_role_labels(
        title.text_template, title.char_role_labels
    )
    raw_rows = [
        [str(value) if value else None for value in row]
        if isinstance(row, (list, tuple))
        else []
        for row in title.char_role_labels
    ]
    # 原始矩阵里有角色名、但规范化后对不上：多出来的格子是渲染/编辑器都看
    # 不到的残留，必须趁这次改名/删除一起清掉（否则 _content_role_options
    # 会继续把它反推成幽灵角色）。
    needs_matrix_cleanup = any(
        label for row in raw_rows for label in row
    ) and raw_rows != labels
    changed = needs_matrix_cleanup
    remapped: list[list[Optional[str]]] = []
    for row in labels:
        new_row: list[Optional[str]] = []
        for label in row:
            if label in mapping:
                new_row.append(mapping[label])
                changed = True
            else:
                new_row.append(label)
        remapped.append(new_row)

    def _remap_symbol(symbol: GuideSymbol) -> tuple[GuideSymbol, bool]:
        symbol_labels = list(guide_symbol_role_labels(symbol))
        touched = [label for label in symbol_labels if label in mapping]
        if not touched:
            return symbol, False
        return (
            guide_symbol_with_role_labels(
                symbol,
                [mapping[label] if label in mapping else label for label in symbol_labels],
            ),
            True,
        )

    guide_symbols: dict[int, GuideSymbol] = {}
    for row, symbol in title.guide_symbols.items():
        guide_symbols[row], symbol_changed = _remap_symbol(symbol)
        changed |= symbol_changed
    inline_guide_symbols: dict[tuple[int, int], GuideSymbol] = {}
    for key, symbol in title.inline_guide_symbols.items():
        inline_guide_symbols[key], symbol_changed = _remap_symbol(symbol)
        changed |= symbol_changed
    if not changed:
        return None
    return replace(
        title,
        char_role_labels=remapped,
        guide_symbols=guide_symbols,
        inline_guide_symbols=inline_guide_symbols,
    )


@dataclass(frozen=True)
class TitleRoleRowsAssignment:
    """Immutable result of assigning one role to title text rows."""

    title: TitleOverlay
    role_label: Optional[str]
    rows: tuple[int, ...]


def assign_role_to_title_rows(
    title: TitleOverlay,
    rows: list[int],
    role_name: str,
) -> Optional[TitleRoleRowsAssignment]:
    """Return an updated title when at least one valid row changes role."""

    lines = title.text_template.split("\n")
    valid_rows = tuple(
        sorted(
            {
                int(row)
                for row in rows
                if 0 <= int(row) < len(lines) and lines[int(row)]
            }
        )
    )
    if not valid_rows:
        return None
    label = role_name.strip() if role_name else None
    labels = normalize_title_char_role_labels(
        title.text_template, title.char_role_labels
    )
    changed = False
    for row in valid_rows:
        new_values = [label] * len(title_line_units(lines[row]))
        if labels[row] != new_values:
            labels[row] = new_values
            changed = True
    if not changed:
        return None
    return TitleRoleRowsAssignment(
        title=replace(title, char_role_labels=labels),
        role_label=label,
        rows=valid_rows,
    )


def migrate_title_char_role_labels(
    old_text: str,
    old_labels: object,
    new_text: str,
) -> list[list[Optional[str]]]:
    """按单元格差异把标题角色迁移到新文字；新增/替换字符回到标题默认。

    对位基是 :func:`title_line_units` 的渲染单元格（组合记号并入基字），
    与渲染 / 编辑器的枚举同基；换行作为分隔符参与扁平对位。
    """
    old_lines = str(old_text).split("\n")
    new_lines = str(new_text).split("\n")
    normalized = normalize_title_char_role_labels(old_text, old_labels)

    def _flatten(lines: list[str]) -> tuple[list[str], list[Optional[str]]]:
        units_flat: list[str] = []
        labels_flat: list[Optional[str]] = []
        for row_index, line in enumerate(lines):
            unit_count = len(title_line_units(line))
            units_flat.extend(title_line_units(line))
            labels_flat.extend(normalized[row_index][:unit_count] if row_index < len(normalized) else [])
            if row_index + 1 < len(lines):
                units_flat.append("\n")
                labels_flat.append(None)
        return units_flat, labels_flat

    old_flat, flat_old_labels = _flatten(old_lines)
    new_flat, _ = _flatten(new_lines)

    migrated_flat: list[Optional[str]] = [None] * len(new_flat)
    matcher = SequenceMatcher(a=old_flat, b=new_flat, autojunk=False)
    for old_start, new_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            if old_start + offset < len(flat_old_labels):
                migrated_flat[new_start + offset] = flat_old_labels[old_start + offset]

    rows: list[list[Optional[str]]] = [[]]
    for position, unit in enumerate(new_flat):
        if unit == "\n":
            rows.append([])
        else:
            rows[-1].append(migrated_flat[position])
    # 整行角色跟着整行走：改字后新增的字符也留在同一个角色里，而不是逐字符
    # 对位后把没匹配上的部分退回标题默认。
    if len(new_lines) == len(normalized):
        for row_index, values in enumerate(normalized):
            row_role = title_row_role(values)
            if row_role is not None:
                rows[row_index] = [row_role] * len(title_line_units(new_lines[row_index]))
    return rows


def normalize_title_guide_symbols(
    text: str,
    row_symbols: object,
    inline_symbols: object,
) -> tuple[dict[int, GuideSymbol], dict[tuple[int, int], GuideSymbol]]:
    """把持久化标题导唱符裁剪到当前文字范围内（越界 / 无视觉条目丢弃）。

    行内键按 :func:`title_line_units` 的单元格基校验。组合记号规范化之前
    按码点保存的键在含分解浊点的行上可能越界——这些条目丢弃（可在逐字
    编辑器重新挂载）；无组合记号的行码点基与单元格基等长，不受影响。
    """
    lines = str(text).split("\n")
    rows: dict[int, GuideSymbol] = {}
    if isinstance(row_symbols, dict):
        for raw_row, symbol in row_symbols.items():
            try:
                row = int(raw_row)
            except (TypeError, ValueError):
                continue
            if 0 <= row < len(lines) and guide_symbol_has_visual(symbol):
                rows[row] = symbol
    unit_counts = [len(title_line_units(line)) for line in lines]
    inline: dict[tuple[int, int], GuideSymbol] = {}
    if isinstance(inline_symbols, dict):
        for raw_key, symbol in inline_symbols.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                continue
            try:
                row, index = int(raw_key[0]), int(raw_key[1])
            except (TypeError, ValueError):
                continue
            if (
                0 <= row < len(lines)
                and 0 <= index < unit_counts[row]
                and guide_symbol_has_visual(symbol)
            ):
                inline[(row, index)] = symbol
    return rows, inline


def migrate_title_guide_symbols(
    old_text: str,
    old_row_symbols: object,
    old_inline_symbols: object,
    new_text: str,
) -> tuple[dict[int, GuideSymbol], dict[tuple[int, int], GuideSymbol]]:
    """标题文字编辑后按单元格 / 行差异迁移导唱符。

    行前导唱符跟着整行走（行级 ``SequenceMatcher`` 等价块平移行号）；
    行内替换沿用角色标签同一套扁平单元格对位（:func:`title_line_units`，
    组合记号并入基字）——只有在新旧文字里都存活的字符保得住自己的图片，
    新增 / 改写字符退回普通文字。
    """
    old_text = str(old_text)
    new_text = str(new_text)
    rows, inline = normalize_title_guide_symbols(
        old_text, old_row_symbols, old_inline_symbols
    )
    if old_text == new_text:
        return rows, inline

    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")

    old_flat: list[Optional[str]] = []
    old_row_starts: list[int] = []
    old_units_by_row: list[list[str]] = []
    flat_old_symbols: list[Optional[GuideSymbol]] = []
    for row_index, line in enumerate(old_lines):
        units = title_line_units(line)
        old_units_by_row.append(units)
        old_row_starts.append(len(old_flat))
        for index in range(len(units)):
            old_flat.append(units[index])
            flat_old_symbols.append(inline.get((row_index, index)))
        if row_index + 1 < len(old_lines):
            old_flat.append("\n")
            flat_old_symbols.append(None)
    new_units_by_row: list[list[str]] = []
    new_flat: list[str] = []
    for row_index, line in enumerate(new_lines):
        units = title_line_units(line)
        new_units_by_row.append(units)
        new_flat.extend(units)
        if row_index + 1 < len(new_lines):
            new_flat.append("\n")

    migrated_flat: list[Optional[GuideSymbol]] = [None] * len(new_flat)
    old_to_new: dict[int, int] = {}
    matcher = SequenceMatcher(a=old_flat, b=new_flat, autojunk=False)
    for old_start, new_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            migrated_flat[new_start + offset] = flat_old_symbols[old_start + offset]
            old_to_new[old_start + offset] = new_start + offset

    new_row_starts: list[int] = []
    position = 0
    for units in new_units_by_row:
        new_row_starts.append(position)
        position += len(units) + 1

    def _new_row_for(position: int) -> Optional[int]:
        for row_index in range(len(new_row_starts) - 1, -1, -1):
            if new_row_starts[row_index] <= position:
                return row_index
        return None

    # 行前导唱符按字符锚点迁移：该行只要有字符在新文字里存活，符号就跟着
    # 它的第一个存活字符落到所在行——单行标题改一两个字不会把行前图片弄丢。
    migrated_rows: dict[int, GuideSymbol] = {}
    for row_index, symbol in rows.items():
        if row_index >= len(old_row_starts):
            continue
        start = old_row_starts[row_index]
        for old_position in range(start, start + len(old_units_by_row[row_index])):
            new_position = old_to_new.get(old_position)
            if new_position is None:
                continue
            target_row = _new_row_for(new_position)
            if target_row is not None:
                migrated_rows[target_row] = symbol
            break

    if not inline:
        return migrated_rows, {}

    migrated_inline: dict[tuple[int, int], GuideSymbol] = {}
    row_index = 0
    char_index = 0
    for position, symbol in enumerate(migrated_flat):
        if new_flat[position] == "\n":
            row_index += 1
            char_index = 0
            continue
        if symbol is not None:
            migrated_inline[(row_index, char_index)] = symbol
        char_index += 1
    return migrated_rows, migrated_inline


def _singer_overrides_from_dict(payload: object) -> dict[int, SubtitleStyleScheme]:
    if not isinstance(payload, dict):
        return {}
    result: dict[int, SubtitleStyleScheme] = {}
    for key, value in payload.items():
        try:
            singer_id = int(key)
        except (TypeError, ValueError):
            continue
        result[singer_id] = subtitle_style_scheme_from_dict(value)
    return result


def _custom_schemes_from_dict(payload: object) -> dict[str, SubtitleStyleScheme]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): subtitle_style_scheme_from_dict(value)
        for key, value in payload.items()
        if str(key)
    }


def _bool_value(value: object) -> bool:
    """Parse persisted booleans; historical payloads wrote ``"False"`` strings."""

    if isinstance(value, str):
        return value.strip().lower() not in {
            "", "0", "false", "no", "off", "none", "null",
        }
    return bool(value)


def _int_value(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float_value(value: object, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
