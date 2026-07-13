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

from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from difflib import SequenceMatcher
import math
from pathlib import Path
import re
from typing import Literal, Optional

LineBreakKind = Literal["none", "page", "paragraph"]
EntryAnimation = Literal["none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"]
ExitAnimation = Literal["none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"]

SCHEMA_VERSION = 1
PROJECT_FILE_SUFFIX = ".yurika"
STYLE_PRESET_FILE_SUFFIX = ".krstyle.json"
SUBTITLE_SOURCE_SUFFIX = ".sug"


# ---------------------------------------------------------------------------
# 字幕源（SUG / Nicokara LRC）中间表示
# ---------------------------------------------------------------------------


@dataclass
class TimingChar:
    """单个字符 + 它在歌曲时间轴上的起点 / 行内停顿释放点。"""

    text: str
    """渲染字符。通常单个 codepoint；偶尔可能是被合在同一 [ts] 下的多个字符。"""

    start_ms: int
    """该字符的演唱起点（毫秒），来自前导 ``[MM:SS:CC]``。"""

    pause_release_ms: Optional[int] = None
    """行内"呼吸/演唱停顿"释放点，仅当该字符后立即有一个 ``[MM:SS:CC]`` 且后面还有
    另一个起始 ``[MM:SS:CC]`` 时存在。语义对应导出器里的 ``ch.is_sentence_end``。"""

    role_label: Optional[str] = None
    """该字符所属「角色 / 配色」标签（来自行内 ``【N配色】`` 等标签）。同一行内可多次
    切换，标签会跨行延续到下次切换为止。``None`` = 未指定（用全局/默认样式）。"""

    source_span_start_ms: Optional[int] = None
    """原始 LRC ``[start]多字[next]`` 共享时间块的起点。仅多字块设置。"""

    source_span_end_ms: Optional[int] = None
    """共享时间块的终点。Painter 用当前字体的字符宽度在该区间内重新分时。"""

    source_span_index: int = 0
    """当前字符在共享时间块内的零基索引。"""

    source_span_count: int = 1
    """共享时间块内的字符总数；1 表示普通独立字符。"""


@dataclass(frozen=True)
class LineAnimationOverride:
    """一行歌词对全局入场/退场动画的完整覆盖。"""

    entry_anim: EntryAnimation = "none"
    entry_duration_ms: int = 300
    exit_anim: ExitAnimation = "none"
    exit_duration_ms: int = 300


@dataclass
class TimingLine:
    """一行歌词（可能为空行——保留用户排版意图）。"""

    chars: list[TimingChar] = field(default_factory=list)
    end_ms: Optional[int] = None
    """行末 ``[MM:SS:CC]``（最后一个字符的演唱终点）。空行 / 仅有标签的行可能为 None。"""
    singer_label: Optional[str] = None
    """行首 ``【演唱者名】`` 标签。NicokaraExporter 在演唱者切换处插入。"""
    singer_id: Optional[int] = None
    """解析阶段分配的稳定歌手序号。仅用于配色覆盖，不参与布局。"""
    is_blank: bool = False
    """是否是用户主动留的空行（无任何字符 / 时间戳 / 标签）。"""
    layout_index: int = 0
    """该行使用的布局（N3 ``LayoutIndex``）：0 = 默认布局（``Style`` 自身字段），
    k >= 1 = ``Style.layouts[k-1]``。由 UI 按页联动写入，随项目文件持久化
    （LRC 本身不含布局信息）。"""
    break_before: LineBreakKind = "none"
    """本行前的 N3 分隔类型。

    ``page`` / ``paragraph`` 都会开启新页；区别用于 N3 的跨页衔接与段落语义。
    直接载入 LRC 时由 SeqLinesBreaker 等价算法生成，导入 N3 项目时由其
    ``LineInfos`` 精确恢复。
    """
    display_start_override_ms: Optional[int] = None
    """本行「上屏时刻」手动覆盖（毫秒）。None = 按全局提前入场自动计算。
    由字幕轨道拖动写入，随项目文件持久化；覆盖值优先于自动布局。"""
    display_end_override_ms: Optional[int] = None
    """本行「消失时刻」手动覆盖（毫秒）。None = 按全局延迟退场自动计算。"""
    animation_override: Optional[LineAnimationOverride] = None
    """逐行动画覆盖；None = 继承全局 ``Style`` 的入场/退场设置。"""


@dataclass
class RubyAnnotation:
    """单个 ``@RubyN`` 注音条目。

    对应导出器的格式 ``@RubyN=漢字,読み[t1][t2]...,pos1,pos2``：

    - ``kanji``：基底字（汉字 / 假名）
    - ``reading``：读音去掉 mora 时间戳后的纯文本
    - ``reading_part_ms``：mora 时间戳序列（毫秒，与原始 ``[t]`` 数量相同）
    - ``reading_parts``：被内嵌时间戳分开的原始读音 part；连续时间戳会保留空 part
    - ``pos_start_ms`` / ``pos_end_ms``：本条注音在歌曲时间轴上的生效区间
    """

    kanji: str
    reading: str
    reading_part_ms: list[int] = field(default_factory=list)
    pos_start_ms: int = 0
    pos_end_ms: int = 0
    reading_parts: list[str] = field(default_factory=list)


@dataclass
class TimingTrackMeta:
    """歌曲元数据（来自文件尾部 ``@Title`` / ``@Artist`` 等标签）。"""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    tagging_by: Optional[str] = None
    silence_ms: int = 0
    """``@SilencemSec``：曲首静音长度（毫秒）。"""
    offset_ms: int = 0
    """``@Offset``：全局时间偏移（毫秒，有符号）。"""
    custom: list[str] = field(default_factory=list)
    """无法识别的自定义尾部行（原样保留，便于 round-trip）。"""


@dataclass
class TimingTrack:
    """解析 SUG 项目或 Nicokara LRC 后的完整中间表示。"""

    meta: TimingTrackMeta = field(default_factory=TimingTrackMeta)
    lines: list[TimingLine] = field(default_factory=list)
    rubies: list[RubyAnnotation] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(line.chars) for line in self.lines)

    @property
    def non_blank_line_count(self) -> int:
        return sum(1 for line in self.lines if not line.is_blank)

    @property
    def singer_options(self) -> list[tuple[int, str]]:
        seen: set[int] = set()
        options: list[tuple[int, str]] = []
        for line in self.lines:
            if line.singer_id is None or line.singer_label is None:
                continue
            if line.singer_id in seen:
                continue
            seen.add(line.singer_id)
            options.append((line.singer_id, line.singer_label))
        return options

    @property
    def role_options(self) -> list[str]:
        """字幕里出现过的「角色 / 配色」标签，按首次出现顺序去重。"""
        seen: set[str] = set()
        options: list[str] = []
        for line in self.lines:
            for ch in line.chars:
                label = ch.role_label
                if label and label not in seen:
                    seen.add(label)
                    options.append(label)
        return options


# ---------------------------------------------------------------------------
# 渲染项目持久化模型（``.yurika``）
# ---------------------------------------------------------------------------


@dataclass
class SubtitleSource:
    """字幕源引用（优先 SUG 项目，也兼容 Nicokara 逐字 LRC）。"""

    path: str = ""
    singer_filter: Optional[list[int]] = None


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
ColorFillMode = Literal[
    "solid",
    "gradient_horizontal",
    "gradient_vertical",
    "split_vertical",
    "image",
]
ColorStateKey = Literal["before", "after"]
ColorLayerKey = Literal["text", "stroke", "stroke2", "shadow"]
DecorationKind = Literal["shadow", "glow"]
RubyAlignment = Literal["auto", "center", "equal_space"]
RUBY_ALIGNMENTS: tuple[RubyAlignment, ...] = ("auto", "center", "equal_space")
SmartHorizontal = Literal["none", "center_position", "equal_margins"]
SMART_HORIZONTALS: tuple[SmartHorizontal, ...] = (
    "none",
    "center_position",
    "equal_margins",
)
SectionEndingMode = Literal["hold", "clear"]
LitStyle = Literal["volume", "circle", "square", "rounded"]
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
# tail=仅片尾一段；head_tail=开头 + 片尾各一段。
TitleShowMode = Literal["whole", "head", "tail", "head_tail"]
TITLE_SHOW_MODES: tuple[TitleShowMode, ...] = ("whole", "head", "tail", "head_tail")

TITLE_SCHEME_NAME = "标题"
"""标题外观所引用的配色方案名：标题的字体与颜色统一由
``custom_style_schemes[TITLE_SCHEME_NAME]`` 描述（在字体页与其他角色方案一起编辑），
``TitleOverlay`` 只保留文字内容、布局引用与显示时段。"""

TITLE_LAYOUT_NAME = "タイトル左上"
"""默认标题布局名（对齐 N3 出厂布局预设 index 4）。"""


@dataclass
class PaintFill:
    """One fill definition shared by text, stroke, second stroke and shadow."""

    mode: ColorFillMode = "solid"
    color: str = "#FFFFFF"
    start_color: str = "#FFFFFF"
    end_color: str = "#FFFFFF"
    gradient_stops: list[tuple[float, str]] = field(default_factory=list)
    split_top_color: str = "#FFFFFF"
    split_bottom_color: str = "#FFFFFF"
    split_position_pct: float = 50
    # Hard-edged vertical color bands: each item marks where that color starts.
    # The final 100% endpoint repeats the last band color for editor/persistence.
    split_stops: list[tuple[float, str]] = field(default_factory=list)
    image_path: str = ""
    image_scale_pct: int = 100


def _paint_fill(color: str, *, mode: ColorFillMode = "solid", end: Optional[str] = None) -> PaintFill:
    end_color = end or color
    return PaintFill(
        mode=mode,
        color=color,
        start_color=color,
        end_color=end_color,
        gradient_stops=[(0, color), (100, end_color)],
        split_top_color=color,
        split_bottom_color=end_color,
        split_stops=[(0, color), (50, end_color), (100, end_color)],
    )


@dataclass
class KaraokeColorState:
    """Colors for one karaoke state: before singing or after singing."""

    text: PaintFill = field(default_factory=lambda: _paint_fill("#FFFFFF"))
    stroke: PaintFill = field(default_factory=lambda: _paint_fill("#222222"))
    stroke2: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))
    shadow: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))


@dataclass
class KaraokeColors:
    """NicoKara-style color matrix: before/after x visual layers."""

    before: KaraokeColorState = field(
        default_factory=lambda: KaraokeColorState(
            text=_paint_fill("#FFFFFF"),
            stroke=_paint_fill("#222222"),
            stroke2=_paint_fill("#000000"),
            shadow=_paint_fill("#000000"),
        )
    )
    after: KaraokeColorState = field(
        default_factory=lambda: KaraokeColorState(
            text=_paint_fill("#FF5A6F"),
            stroke=_paint_fill("#222222"),
            stroke2=_paint_fill("#000000"),
            shadow=_paint_fill("#000000"),
        )
    )


@dataclass
class TitleOverlay:
    """标题字幕叠加层（B7）。

    静态文字（曲名 / 艺术家），不走字；默认参数逆向自 NicoKaraMaker3：
    明朝体、白字黑边、左上、整段显示。文字模板 ``{title}`` / ``{artist}`` 由字幕源
    ``@Title`` / ``@Artist`` 元数据替换；也可直接填任意自定义文字（含换行）。
    """

    enabled: bool = False
    text_template: str = "{title} / {artist}"
    """``{title}`` / ``{artist}`` 占位符按元数据替换；``\\n`` 分行。"""

    char_role_labels: list[list[Optional[str]]] = field(default_factory=list)
    """逐行逐字符角色标签；``None`` 表示继承内置「标题」方案。"""

    # 字体（逆向 ニコカラ「標準配色」字体：明朝体、字号 100、描边 15/5）
    font_family: str = "游明朝"
    font_family_latin: Optional[str] = None
    font_size_px: int = 100
    font_weight: int = 400
    italic: bool = False
    letter_spacing_px: int = 0
    line_gap_px: int = 15

    # 颜色（单态：不走字）。逆向 ニコカラ「標準配色」的「走字前」外观（标题永不走字）：
    # 填充 #FFEBEB、内描边黑、外二重边白、模糊发光 #E19696。
    fill: PaintFill = field(default_factory=lambda: _paint_fill("#FFEBEB"))
    stroke: PaintFill = field(default_factory=lambda: _paint_fill("#000000"))
    stroke_width_px: int = 15
    stroke2: PaintFill = field(default_factory=lambda: _paint_fill("#FFFFFF"))
    stroke2_width_px: int = 5
    decoration_kind: DecorationKind = "glow"
    glow_radius_px: int = 10
    glow_concentration_level: int = 0
    """NicoKaraMaker3 ``BlurLevel``: 0/1/2 = 低/中/高发光浓度。"""
    shadow: PaintFill = field(default_factory=lambda: _paint_fill("#E19696"))
    shadow_offset_x: int = 0
    shadow_offset_y: int = 2

    # 位置（锚点 9 宫格 + 内边距 / 偏移；逆向 ニコカラ「タイトル左上」）。
    # 这些字段与上方字体/颜色字段一样，现在是「解析结果」：渲染时由
    # ``layout_index`` 引用的布局方案与 ``TITLE_SCHEME_NAME`` 配色方案推导，
    # 仅当布局/方案缺失（旧工程迁移前）时按字段原值绘制。
    anchor: TitleAnchor = "top_left"
    align: HorizontalAlign = "left"
    offset_x: int = 50
    offset_y: int = 50

    layout_index: Optional[int] = 1
    """标题引用的布局方案（同 ``TimingLine.layout_index``：0 = 默认布局，
    n = ``Style.layouts[n-1]``）。默认 1 指向内置「タイトル左上」；``None``
    表示旧工程的显式 anchor/offset 字段仍然生效（加载时会自动迁移）。"""

    # 显示时段（逆向 ニコカラ TitleShowTime，默认 Head=整段显示）
    show_mode: TitleShowMode = "whole"
    head_offset_ms: int = 0
    duration_ms: int = 10000
    tail_offset_ms: int = 0
    fade_in_ms: int = 300
    fade_out_ms: int = 300


@dataclass
class LyricsLayout:
    """一套可命名的布局定义（N3 ``LyricsLayoutModel`` 的布局几何子集）。

    只包含「位置」域字段；字间距 / 咬合 / 注音间距等在本项目属于字体与配色
    方案域（可按角色覆盖），不进布局对象。生效优先级：全局 ``Style`` <
    布局 < 歌手/角色方案。
    """

    name: str = "布局"
    line_y_position: LineYPosition = "bottom"
    line_y_margin_px: int = 80
    line_gap_px: int = 90
    smart_horizontal: SmartHorizontal = "equal_margins"
    horizontal_margin_px: int = 50
    line_alignments: list[HorizontalAlign] = field(
        default_factory=lambda: ["left", "right"]
    )


LYRICS_LAYOUT_FIELDS: tuple[str, ...] = (
    "line_y_position",
    "line_y_margin_px",
    "line_gap_px",
    "smart_horizontal",
    "horizontal_margin_px",
    "line_alignments",
)
"""布局对象作用到 ``Style`` 上的字段（不含 ``name``）。"""


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
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None
    allow_biting: Optional[bool] = None
    font_weight: Optional[int] = None
    italic: Optional[bool] = None
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
    glow_radius_px: Optional[int] = None
    glow_before_radius_px: Optional[int] = None
    glow_after_radius_px: Optional[int] = None
    glow_concentration_level: Optional[int] = None
    shadow_color: Optional[str] = None
    shadow_offset_x: Optional[int] = None
    shadow_offset_y: Optional[int] = None
    ruby_font_size_px: Optional[int] = None
    ruby_font_family: Optional[str] = None
    ruby_font_family_latin: Optional[str] = None
    ruby_font_weight: Optional[int] = None
    ruby_latin_font_size_px: Optional[int] = None
    ruby_latin_font_weight: Optional[int] = None
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
    ruby_glow_radius_px: Optional[int] = None
    ruby_glow_before_radius_px: Optional[int] = None
    ruby_glow_after_radius_px: Optional[int] = None
    ruby_glow_concentration_level: Optional[int] = None
    ruby_shadow_offset_x: Optional[int] = None
    ruby_shadow_offset_y: Optional[int] = None
    karaoke_colors: Optional[KaraokeColors] = None
    ruby_karaoke_colors: Optional[KaraokeColors] = None


@dataclass
class StylePreset:
    """应用级可复用的单目标字幕样式预设。

    ``group`` 只负责整理与筛选，不参与角色解析；预设名称全局唯一。工程角色
    使用时会深拷贝 ``scheme``，因此预设的重命名、分组或删除不会反向影响工程。
    """

    name: str
    group: str = ""
    scheme: SubtitleStyleScheme = field(default_factory=SubtitleStyleScheme)
    # N3 templates retain their original payload so sizes can be resolved
    # again for the target project's output height when the preset is used.
    source_type: str = ""
    source_data: dict[str, Any] = field(default_factory=dict)


def default_title_layout() -> LyricsLayout:
    """内置标题布局（N3 出厂预设「タイトル左上」：Top、行間 15、余白 50/50、Left）。"""
    return LyricsLayout(
        name=TITLE_LAYOUT_NAME,
        line_y_position="top",
        line_y_margin_px=50,
        line_gap_px=15,
        smart_horizontal="equal_margins",
        horizontal_margin_px=50,
        line_alignments=["left"],
    )


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
        font_family_latin=title.font_family_latin,
        font_size_px=title.font_size_px,
        font_weight=title.font_weight,
        italic=title.italic,
        letter_spacing_px=title.letter_spacing_px,
        stroke_width_px=title.stroke_width_px,
        stroke2_enabled=title.stroke2_width_px > 0,
        stroke2_width_px=title.stroke2_width_px,
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
    """默认「标题」方案 = ``TitleOverlay`` 字段默认值（ニコカラ標準配色标题外观）。"""
    return title_scheme_from_overlay(TitleOverlay())


@dataclass
class Style:
    """字幕样式（A4 / A5 / A6 实装的纯色 + 横书き子集）。

    字段默认值面向 NicoKaraMaker 风格：教科书体 + 小字号 + 双行底部布局。
    后续 A5 / A6 / B3 等任务在此基础上扩字段（渐变 / 发光 / 注音 / 动画）。
    """

    # 字体
    font_family: str = "UD Digi Kyokasho N-B"
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
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None

    allow_biting: bool = False
    """允许负 side bearing 令相邻字形咬合。"""

    font_weight: int = 400  # Qt 习惯 100-900
    italic: bool = False

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
    glow_radius_px: int = 10
    glow_before_radius_px: int = 10
    glow_after_radius_px: int = 10
    glow_concentration_level: int = 0
    """NicoKaraMaker3 ``BlurLevel``: 0/1/2 = 低/中/高发光浓度。"""
    shadow_color: str = "#000000"
    shadow_offset_x: int = 10
    """阴影 X 偏移。N3 阴影偏移固定为 DecorSize（双轴同值），新建默认 10
    （``CreateLyricsFont``），这里默认值对齐。"""
    shadow_offset_y: int = 10
    karaoke_colors: Optional[KaraokeColors] = None

    singer_style_overrides: dict[int, SubtitleStyleScheme] = field(default_factory=dict)
    """B2：按歌手自动套用的字幕 tab 方案。不覆盖位置、时间或布局。"""

    custom_style_schemes: dict[str, SubtitleStyleScheme] = field(
        default_factory=lambda: {TITLE_SCHEME_NAME: default_title_scheme()}
    )
    """用户自行添加的配色方案。当前用于编辑/复用，后续可接入方案分配。
    内置「标题」方案（``TITLE_SCHEME_NAME``）描述标题外观，随字体页统一编辑。"""

    # ふりがな / ruby（B1）
    ruby_font_size_px: int = 45
    ruby_font_family: Optional[str] = None
    ruby_font_family_latin: Optional[str] = None
    ruby_font_weight: Optional[int] = None
    ruby_latin_font_size_px: Optional[int] = None
    ruby_latin_font_weight: Optional[int] = None
    ruby_font_follow_main: bool = True
    """新建样式的注音字体跟随主文字；任一注音字体字段被编辑后关闭跟随。"""
    ruby_color: str = "#FF5A6F"
    ruby_gap_px: int = 0
    """NicokaraMaker3 ``LyricsAndRubyInterval`` default: 0 px."""
    ruby_interval_px: int = 0
    """NicokaraMaker3 ``RubyInterval``：注音字符间最小间距，可为负。"""
    ruby_alignment: RubyAlignment = "auto"
    """注音相对正文范围的排布（N3 ``RubyAlignment``）：``auto`` = 正文或注音全为
    英数时居中、否则均等分布；``center`` = 整组居中；``equal_space`` = 均等分布。"""
    ruby_stroke_width_px: Optional[int] = 10
    ruby_stroke2_enabled: Optional[bool] = True
    ruby_stroke2_width_px: Optional[int] = 3
    ruby_latin_stroke_width_px: Optional[int] = None
    ruby_latin_stroke2_enabled: Optional[bool] = None
    ruby_latin_stroke2_width_px: Optional[int] = None
    ruby_decoration_kind: Optional[DecorationKind] = None
    ruby_glow_radius_px: Optional[int] = None
    ruby_glow_before_radius_px: Optional[int] = None
    ruby_glow_after_radius_px: Optional[int] = None
    ruby_glow_concentration_level: Optional[int] = None
    """Optional ruby override; ``None`` inherits ``glow_concentration_level``."""
    ruby_shadow_offset_x: Optional[int] = None
    ruby_shadow_offset_y: Optional[int] = None
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

    line_y_margin_px: int = 80
    """``line_y_position`` 为 ``"top"`` / ``"bottom"`` 时距离顶/底边的内边距。"""

    dual_line_layout: bool = True
    """默认上下双行显示：当前行在上，下一行在下。"""

    line_horizontal_layout: LineHorizontalLayout = "asymmetric"
    """双行水平布局：``asymmetric`` 为上左下右，``center`` 为两行居中，
    ``per_row`` 为逐行独立对齐 + X/Y（对标 Sayatoo「布局」第一行 / 第二行）。"""

    line_gap_px: int = 90
    """双行布局中两行主文字外框之间的间距，不包含 ruby 高度。"""

    line_alignments: list[HorizontalAlign] = field(
        default_factory=lambda: ["left", "right"]
    )
    """每行（lane）的水平对齐列表（N3 ``HorizontalAlignments``），仅 ``asymmetric``
    模式使用。列表长度即多行显示的行数，索引 0 = 最上行。显示行数恒等于列表
    长度，因此 Bottom 锚定的「从下往上取列表末尾」与正序索引等价。"""

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

    layouts: list["LyricsLayout"] = field(
        default_factory=lambda: [default_title_layout()]
    )
    """额外的命名布局定义（N3 ``LyricsLayouts``）。``Style`` 自身的布局字段是
    「默认布局」（index 0），本列表从 index 1 起被 ``TimingLine.layout_index``
    引用。布局定义是可复用预设，随全局设置与项目文件一起持久化。
    默认内置「タイトル左上」（对齐 N3 出厂预设），供标题引用。"""

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

    timing_offset_ms: int = 0
    """字幕整体时间偏移。正值延后显示，负值提前显示。"""

    line_lane_gap_ms: int = 300
    """同一显示 lane 上相邻两句之间保留的时间间隔。"""

    line_continuity_snap_ms: int = 800
    """同 lane 间隔较短时，下一句可提前到上一句结束后立即显示的阈值。"""

    line_pair_second_delay_ms: int = 3000
    """双行组中下行相对上行表示开始的默认延迟。"""

    line_max_hold_ms: int = 12_000
    """单句显示窗口最长保留时间，避免长间奏时字幕过久挂屏。"""

    # 段落 / 同步退场（对标 Sayatoo sync_ending / section_ending_mode）。
    # Sayatoo 用手动信号划段落；LRC 无信号，这里改为按间奏间隔自动分段。
    section_gap_ms: int = 4000
    """自动分段阈值：相邻两句演唱空隙（间奏）超过此值即开新段落。"""

    sync_ending: bool = False
    """同步退场：开启后同段落内一组两行在段末同时退场，而非逐行先后消失。"""

    section_ending_mode: SectionEndingMode = "hold"
    """段落结束行为：``hold`` 维持现状（可挂屏到 max_hold）；``clear`` 段末即清屏，
    字幕不拖进间奏。"""

    entry_anim: EntryAnimation = "none"
    """入场动画：none / fade / slide_in / rise / char_fade / spin_flip / utopia。"""

    entry_lead_ms: int = 300
    """入场动画时长；不改变歌词填色时间，只影响显示窗口起点后的过渡。"""

    exit_anim: ExitAnimation = "none"
    """退场动画：none / fade / slide_out / rise / char_fade / spin_flip / utopia。"""

    exit_fade_ms: int = 300
    """退场动画时长；在显示窗口结束前开始。"""

    # 指示灯（Sayatoo SignalsLits.sx：lit.* / signals.duration）
    lit_enabled: bool = False
    lit_style: LitStyle = "volume"
    lit_number: int = 4
    lit_size: int = 32
    lit_offset_x: int = 0
    lit_offset_y: int = -24
    lit_tracking: int = 0
    lit_fill_color: str = "#0000FF"
    lit1_fill_color: str = "#FF0000"
    lit2_fill_color: str = "#FFFF00"
    lit3_fill_color: str = "#00FF00"
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

    # 标题字幕 overlay（B7）。None = 用默认（关闭）。
    title_overlay: Optional[TitleOverlay] = None


def style_with_line_animation(style: Style, line: TimingLine) -> Style:
    """把逐行动画覆盖套到样式上；其他视觉与布局字段保持不变。"""
    override = line.animation_override
    if override is None:
        return style
    return replace(
        style,
        entry_anim=override.entry_anim,
        entry_lead_ms=max(int(override.entry_duration_ms), 0),
        exit_anim=override.exit_anim,
        exit_fade_ms=max(int(override.exit_duration_ms), 0),
    )


@dataclass
class BackgroundSource:
    """字幕画面的正式背景源。

    ``image_sequence`` 的 ``path`` 可以是首帧/编号模式；``source_fps`` 决定取帧
    速度。视频偏移保留为毫秒，供预览和导出统一应用。
    """

    kind: Literal["video", "image", "image_sequence", "solid"] = "solid"
    path: Optional[str] = None
    color: str = "#000000"
    source_fps: Optional[int] = None
    sequence_start_number: int = 0
    video_offset_ms: int = 0


def background_sequence_frame_path(source: BackgroundSource, t_ms: int) -> Optional[Path]:
    """按 ffmpeg ``%0Nd``/``%d`` 编号模式解析图片序列当前帧。"""
    if source.kind != "image_sequence" or not source.path:
        return None
    index = (
        max(int(t_ms), 0) * max(int(source.source_fps or 60), 1) // 1000
        + max(int(source.sequence_start_number), 0)
    )
    raw = str(source.path)
    match = re.search(r"%0?(\d*)d", raw)
    if match:
        width = int(match.group(1) or 0)
        number = f"{index:0{width}d}" if width else str(index)
        return Path(raw[: match.start()] + number + raw[match.end() :])
    return Path(raw)


def infer_image_sequence_pattern(path: Path) -> tuple[Path, int]:
    """把 ``frame_0001.png`` 转成 ffmpeg 模式并保留起始编号。"""
    match = re.search(r"(\d+)$", path.stem)
    if not match:
        return path, 0
    start = int(match.group(1))
    pattern = path.with_name(
        path.stem[: match.start()] + f"%0{len(match.group(1))}d" + path.suffix
    )
    return pattern, start


# 旧代码曾公开 ``Background`` 名称；保留别名以兼容项目外调用。
Background = BackgroundSource


@dataclass
class OutputConfig:
    """输出参数占位。"""

    width: int = 1920
    height: int = 1080
    fps: int = 60
    encoder_mode: str = "cpu"
    crf: int = 18
    preset: str = "veryfast"
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


def normalize_glow_concentration_level(value: object, fallback: int = 0) -> int:
    """Clamp NicoKaraMaker3 ``BlurLevel`` to its three supported levels."""
    try:
        return max(0, min(2, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(0, min(2, int(fallback)))


def line_animation_override_to_dict(
    override: Optional[LineAnimationOverride],
) -> Optional[dict[str, object]]:
    if override is None:
        return None
    return {
        "entry_anim": override.entry_anim,
        "entry_duration_ms": max(int(override.entry_duration_ms), 0),
        "exit_anim": override.exit_anim,
        "exit_duration_ms": max(int(override.exit_duration_ms), 0),
    }


def line_animation_override_from_dict(value: object) -> Optional[LineAnimationOverride]:
    if not isinstance(value, dict):
        return None
    entry = value.get("entry_anim")
    exit_ = value.get("exit_anim")
    valid_entry = {"none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"}
    valid_exit = {"none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"}
    if entry not in valid_entry or exit_ not in valid_exit:
        return None

    def duration(key: str, fallback: int) -> int:
        try:
            return max(int(value.get(key, fallback)), 0)
        except (TypeError, ValueError):
            return fallback

    return LineAnimationOverride(
        entry_anim=entry,
        entry_duration_ms=duration("entry_duration_ms", 300),
        exit_anim=exit_,
        exit_duration_ms=duration("exit_duration_ms", 300),
    )


def style_to_dict(style: Style) -> dict:
    """Serialize ``Style`` into JSON-friendly primitives."""
    data: dict = {}
    for item in fields(Style):
        value = getattr(style, item.name)
        if item.name in {"karaoke_colors", "ruby_karaoke_colors"}:
            data[item.name] = karaoke_colors_to_dict(value) if value is not None else None
        elif item.name == "layouts":
            data[item.name] = [lyrics_layout_to_dict(layout) for layout in value]
        elif item.name == "title_overlay":
            data[item.name] = title_overlay_to_dict(value) if value is not None else None
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
    return data


def style_from_dict(payload: object) -> Style:
    """Build ``Style`` from a dict, ignoring unknown or invalid fields."""
    if not isinstance(payload, dict):
        return Style()
    defaults = Style()
    changes: dict = {}
    style_fields = {item.name for item in fields(Style)}
    for key, value in payload.items():
        if key not in style_fields:
            continue
        if key in {"karaoke_colors", "ruby_karaoke_colors"}:
            changes[key] = karaoke_colors_from_dict(value)
        elif key == "layouts":
            changes[key] = _layouts_from_payload(value)
        elif key == "title_overlay":
            changes[key] = title_overlay_from_dict(value)
        elif key == "singer_style_overrides":
            changes[key] = _singer_overrides_from_dict(value)
        elif key == "custom_style_schemes":
            changes[key] = _custom_schemes_from_dict(value)
        elif key == "glow_concentration_level":
            changes[key] = normalize_glow_concentration_level(value)
        elif key == "ruby_glow_concentration_level":
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
            "ruby_font_size_px",
            "ruby_gap_px",
            "ruby_interval_px",
            "ruby_glow_radius_px",
            "ruby_glow_before_radius_px",
            "ruby_glow_after_radius_px",
            "ruby_shadow_offset_x",
            "ruby_shadow_offset_y",
            "viewport_offset_x",
            "viewport_offset_y",
            "viewport_scale_pct",
            "viewport_rotation_deg",
            "line_y_margin_px",
            "line_gap_px",
            "horizontal_margin_px",
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
            "timing_offset_ms",
            "line_lane_gap_ms",
            "line_continuity_snap_ms",
            "line_pair_second_delay_ms",
            "line_max_hold_ms",
            "section_gap_ms",
            "entry_lead_ms",
            "exit_fade_ms",
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
            "volume_size",
            "volume_offset_x",
            "volume_offset_y",
            "volume_column_width",
            "volume_column_count",
            "volume_column_spacing",
            "volume_align",
            "volume_flash_times",
            "volume_transition_ratio_pct",
        }:
            changes[key] = _int_value(value, getattr(defaults, key))
        elif key in {
            "volume_ratio",
            "volume_flash_duration_ratio",
        }:
            changes[key] = _float_value(value, getattr(defaults, key))
        elif key in {
            "italic",
            "allow_biting",
            "stroke2_enabled",
            "ruby_font_follow_main",
            "dual_line_layout",
            "right_to_left",
            "vertical",
            "sync_ending",
            "lit_enabled",
            "lit_shadow",
        }:
            changes[key] = bool(value)
        elif key == "lit_style":
            changes[key] = value if value in {"volume", "circle", "square", "rounded"} else defaults.lit_style
        elif key == "lit_transition_mode":
            changes[key] = value if value in {"none", "fade", "slide"} else defaults.lit_transition_mode
        elif key == "section_ending_mode":
            changes[key] = value if value in {"hold", "clear"} else defaults.section_ending_mode
        elif key == "line_y_position":
            changes[key] = value if value in {"top", "center", "bottom"} else defaults.line_y_position
        elif key == "line_horizontal_layout":
            changes[key] = value if value in {"asymmetric", "center", "per_row"} else defaults.line_horizontal_layout
        elif key in {"row1_align", "row2_align"}:
            changes[key] = value if value in HORIZONTAL_ALIGNS else getattr(defaults, key)
        elif key == "viewport_align":
            changes[key] = value if value in VIEWPORT_ALIGNS else defaults.viewport_align
        elif key == "decoration_kind":
            changes[key] = value if value in {"shadow", "glow"} else defaults.decoration_kind
        elif key == "ruby_decoration_kind":
            changes[key] = value if value in {"shadow", "glow"} else None
        elif key == "ruby_alignment":
            changes[key] = value if value in RUBY_ALIGNMENTS else defaults.ruby_alignment
        elif key == "smart_horizontal":
            changes[key] = value if value in SMART_HORIZONTALS else defaults.smart_horizontal
        elif key == "line_alignments":
            changes[key] = _line_alignments_from_payload(value)
        elif key == "entry_anim":
            changes[key] = (
                value
                if value in {"none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"}
                else defaults.entry_anim
            )
        elif key == "exit_anim":
            changes[key] = (
                value
                if value in {"none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"}
                else defaults.exit_anim
            )
        elif key in {
            "font_family_latin",
            "ruby_font_family",
            "ruby_font_family_latin",
        }:
            changes[key] = str(value) if value else None
        elif key in {
            "latin_font_size_px",
            "latin_font_weight",
            "latin_stroke_width_px",
            "latin_stroke2_width_px",
            "ruby_font_weight",
            "ruby_latin_font_size_px",
            "ruby_latin_font_weight",
            "ruby_stroke_width_px",
            "ruby_stroke2_width_px",
            "ruby_latin_stroke_width_px",
            "ruby_latin_stroke2_width_px",
        }:
            # 英数轨覆盖：None = 跟随日文轨，序列化保留 null
            changes[key] = None if value is None else _int_value(value, 0)
        elif key in {
            "latin_stroke2_enabled",
            "ruby_stroke2_enabled",
            "ruby_latin_stroke2_enabled",
        }:
            changes[key] = None if value is None else bool(value)
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
    _migrate_title_references(changes)
    return Style(**changes)


def _migrate_title_references(changes: dict) -> None:
    """旧工程标题迁移：显式外观/位置字段 → 「标题」方案 + 布局引用。

    新版工程恒满足两个不变量：``custom_style_schemes`` 含 ``TITLE_SCHEME_NAME``、
    启用标题时 ``title_overlay.layout_index`` 非 None。旧工程加载时按原
    ``TitleOverlay`` 字段折算补齐，保证外观不变。
    """
    title = changes.get("title_overlay")
    schemes = changes.get("custom_style_schemes")
    if schemes is None:
        # 快照没有方案字典：仅当标题需要迁移时显式给出（否则交给默认值）。
        if title is not None:
            changes["custom_style_schemes"] = {
                TITLE_SCHEME_NAME: title_scheme_from_overlay(title)
            }
    elif TITLE_SCHEME_NAME not in schemes:
        schemes = dict(schemes)
        schemes[TITLE_SCHEME_NAME] = (
            title_scheme_from_overlay(title)
            if title is not None
            else default_title_scheme()
        )
        changes["custom_style_schemes"] = schemes
    if title is None or title.layout_index is not None:
        return
    layouts = list(changes.get("layouts") or [])
    layouts.append(
        _layout_from_title_position(title, {layout.name for layout in layouts})
    )
    changes["layouts"] = layouts
    changes["title_overlay"] = replace(title, layout_index=len(layouts))


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
        )
        for layout in style.layouts
    ]
    margin = scaled(style.horizontal_margin_px)
    return replace(
        style,
        line_y_margin_px=scaled(style.line_y_margin_px),
        line_gap_px=scaled(style.line_gap_px),
        horizontal_margin_px=margin,
        upper_line_left_margin_px=margin,
        lower_line_right_margin_px=margin,
        layouts=layouts,
        layout_reference_height=new_height,
    )


def lyrics_layout_to_dict(layout: LyricsLayout) -> dict:
    return {
        "name": layout.name,
        "line_y_position": layout.line_y_position,
        "line_y_margin_px": layout.line_y_margin_px,
        "line_gap_px": layout.line_gap_px,
        "smart_horizontal": layout.smart_horizontal,
        "horizontal_margin_px": layout.horizontal_margin_px,
        "line_alignments": list(layout.line_alignments),
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
        line_y_position=position,  # type: ignore[arg-type]
        line_y_margin_px=_int_value(payload.get("line_y_margin_px"), defaults.line_y_margin_px),
        line_gap_px=_int_value(payload.get("line_gap_px"), defaults.line_gap_px),
        smart_horizontal=smart,  # type: ignore[arg-type]
        horizontal_margin_px=_int_value(
            payload.get("horizontal_margin_px"), defaults.horizontal_margin_px
        ),
        line_alignments=_line_alignments_from_payload(payload.get("line_alignments")),
    )


def _layouts_from_payload(payload: object) -> list[LyricsLayout]:
    if not isinstance(payload, list):
        return []
    return [lyrics_layout_from_dict(item) for item in payload[:32]]


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
        else:
            changes[key] = value
    return SubtitleStyleScheme(**changes)


def title_overlay_to_dict(title: TitleOverlay) -> dict:
    return {
        "enabled": title.enabled,
        "text_template": title.text_template,
        "char_role_labels": [list(row) for row in title.char_role_labels],
        "font_family": title.font_family,
        "font_family_latin": title.font_family_latin,
        "font_size_px": title.font_size_px,
        "font_weight": title.font_weight,
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
    }


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
    show_mode = payload.get("show_mode", defaults.show_mode)
    if show_mode not in TITLE_SHOW_MODES:
        show_mode = defaults.show_mode
    decoration = payload.get("decoration_kind", defaults.decoration_kind)
    if decoration not in {"shadow", "glow"}:
        decoration = defaults.decoration_kind
    text_template = str(payload.get("text_template", defaults.text_template))
    return TitleOverlay(
        enabled=bool(payload.get("enabled", defaults.enabled)),
        text_template=text_template,
        char_role_labels=normalize_title_char_role_labels(
            text_template, payload.get("char_role_labels")
        ),
        font_family=str(payload.get("font_family", defaults.font_family)),
        font_family_latin=(
            str(payload["font_family_latin"])
            if payload.get("font_family_latin")
            else None
        ),
        font_size_px=_int_value(payload.get("font_size_px"), defaults.font_size_px),
        font_weight=_int_value(payload.get("font_weight"), defaults.font_weight),
        italic=bool(payload.get("italic", defaults.italic)),
        letter_spacing_px=_int_value(payload.get("letter_spacing_px"), defaults.letter_spacing_px),
        line_gap_px=_int_value(payload.get("line_gap_px"), defaults.line_gap_px),
        fill=paint_fill_from_dict(payload.get("fill"), fallback="#FFEBEB"),
        stroke=paint_fill_from_dict(payload.get("stroke"), fallback="#000000"),
        stroke_width_px=_int_value(payload.get("stroke_width_px"), defaults.stroke_width_px),
        stroke2=paint_fill_from_dict(payload.get("stroke2"), fallback="#FFFFFF"),
        stroke2_width_px=_int_value(payload.get("stroke2_width_px"), defaults.stroke2_width_px),
        decoration_kind=decoration,  # type: ignore[arg-type]
        glow_radius_px=_int_value(payload.get("glow_radius_px"), defaults.glow_radius_px),
        glow_concentration_level=normalize_glow_concentration_level(
            payload.get("glow_concentration_level"), defaults.glow_concentration_level
        ),
        shadow=paint_fill_from_dict(payload.get("shadow"), fallback="#E19696"),
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
    )


def normalize_title_char_role_labels(
    text: str, payload: object
) -> list[list[Optional[str]]]:
    """把持久化标题标签规范成与当前逐行文字严格等长的矩阵。"""
    raw_rows = payload if isinstance(payload, list) else []
    normalized: list[list[Optional[str]]] = []
    for row_index, line in enumerate(str(text).split("\n")):
        raw = raw_rows[row_index] if row_index < len(raw_rows) else []
        values = raw if isinstance(raw, (list, tuple)) else []
        normalized.append(
            [
                (str(values[index]).strip() or None)
                if index < len(values) and values[index]
                else None
                for index in range(len(line))
            ]
        )
    return normalized


def migrate_title_char_role_labels(
    old_text: str,
    old_labels: object,
    new_text: str,
) -> list[list[Optional[str]]]:
    """按字符差异把标题角色迁移到新文字；新增/替换字符回到标题默认。"""
    old_text = str(old_text)
    new_text = str(new_text)
    normalized = normalize_title_char_role_labels(old_text, old_labels)
    flat_old_labels: list[Optional[str]] = []
    for row_index, line in enumerate(old_text.split("\n")):
        flat_old_labels.extend(normalized[row_index])
        if row_index + 1 < len(old_text.split("\n")):
            flat_old_labels.append(None)

    migrated_flat: list[Optional[str]] = [None] * len(new_text)
    matcher = SequenceMatcher(a=old_text, b=new_text, autojunk=False)
    for old_start, new_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            if old_start + offset < len(flat_old_labels):
                migrated_flat[new_start + offset] = flat_old_labels[old_start + offset]

    rows: list[list[Optional[str]]] = [[]]
    for index, char in enumerate(new_text):
        if char == "\n":
            rows.append([])
        else:
            rows[-1].append(migrated_flat[index])
    return rows


def karaoke_colors_to_dict(colors: KaraokeColors) -> dict:
    return {
        "before": karaoke_color_state_to_dict(colors.before),
        "after": karaoke_color_state_to_dict(colors.after),
    }


def karaoke_colors_from_dict(payload: object) -> Optional[KaraokeColors]:
    if not isinstance(payload, dict):
        return None
    return KaraokeColors(
        before=karaoke_color_state_from_dict(payload.get("before")),
        after=karaoke_color_state_from_dict(payload.get("after")),
    )


def karaoke_color_state_to_dict(state: KaraokeColorState) -> dict:
    return {
        "text": paint_fill_to_dict(state.text),
        "stroke": paint_fill_to_dict(state.stroke),
        "stroke2": paint_fill_to_dict(state.stroke2),
        "shadow": paint_fill_to_dict(state.shadow),
    }


def karaoke_color_state_from_dict(payload: object) -> KaraokeColorState:
    if not isinstance(payload, dict):
        return KaraokeColorState()
    return KaraokeColorState(
        text=paint_fill_from_dict(payload.get("text")),
        stroke=paint_fill_from_dict(payload.get("stroke"), fallback="#222222"),
        stroke2=paint_fill_from_dict(payload.get("stroke2"), fallback="#000000"),
        shadow=paint_fill_from_dict(payload.get("shadow"), fallback="#000000"),
    )


def paint_fill_to_dict(fill: PaintFill) -> dict:
    return {
        "mode": fill.mode,
        "color": fill.color,
        "start_color": fill.start_color,
        "end_color": fill.end_color,
        "gradient_stops": list(fill.gradient_stops),
        "split_top_color": fill.split_top_color,
        "split_bottom_color": fill.split_bottom_color,
        "split_position_pct": fill.split_position_pct,
        "split_stops": list(fill.split_stops),
        "image_path": fill.image_path,
        "image_scale_pct": fill.image_scale_pct,
    }


def paint_fill_from_dict(payload: object, *, fallback: str = "#FFFFFF") -> PaintFill:
    if not isinstance(payload, dict):
        return _paint_fill(fallback)
    default = _paint_fill(fallback)
    mode = str(payload.get("mode", default.mode))
    if mode not in {"solid", "gradient_horizontal", "gradient_vertical", "split_vertical", "image"}:
        mode = default.mode
    color = str(payload.get("color", default.color))
    start_color = str(payload.get("start_color", color))
    end_color = str(payload.get("end_color", color))
    stops = payload.get("gradient_stops", [(0, start_color), (100, end_color)])
    split_top_color = str(payload.get("split_top_color", start_color))
    split_bottom_color = str(payload.get("split_bottom_color", end_color))
    split_position_pct = _gradient_stop_position(
        payload.get("split_position_pct"), 50
    )
    split_stops_payload = payload.get("split_stops")
    if not split_stops_payload:
        split_stops_payload = [
            (0, split_top_color),
            (split_position_pct, split_bottom_color),
            (100, split_bottom_color),
        ]
    return PaintFill(
        mode=mode,  # type: ignore[arg-type]
        color=color,
        start_color=start_color,
        end_color=end_color,
        gradient_stops=_gradient_stops_from_payload(stops, start_color, end_color),
        split_top_color=split_top_color,
        split_bottom_color=split_bottom_color,
        split_position_pct=split_position_pct,
        split_stops=_gradient_stops_from_payload(
            split_stops_payload, split_top_color, split_bottom_color
        ),
        image_path=str(payload.get("image_path", "")),
        image_scale_pct=max(1, _int_value(payload.get("image_scale_pct"), 100)),
    )


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


def _gradient_stops_from_payload(
    payload: object,
    start_color: str,
    end_color: str,
) -> list[tuple[float, str]]:
    if not isinstance(payload, list):
        return [(0, start_color), (100, end_color)]
    result: list[tuple[float, str]] = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        result.append((_gradient_stop_position(item[0], 0), str(item[1])))
    if not result:
        return [(0, start_color), (100, end_color)]
    positions = {position for position, _color in result}
    if 0 not in positions:
        result.append((0, start_color))
    if 100 not in positions:
        result.append((100, end_color))
    # Python's sort is stable: equal-position stops retain their source order.
    return sorted(result, key=lambda item: item[0])


def _gradient_stop_position(value: object, fallback: float) -> float:
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        position = fallback
    if not math.isfinite(position):
        position = fallback
    position = max(0.0, min(100.0, position))
    # Keep old integer projects byte-stable while preserving imported fractions.
    return int(position) if position.is_integer() else position


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
