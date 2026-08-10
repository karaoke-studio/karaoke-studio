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
from uuid import uuid4

LineBreakKind = Literal["none", "page", "paragraph"]
EntryAnimation = Literal[
    "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
]
ExitAnimation = Literal[
    "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
]
KaraokeAnimation = Literal["inherit", "none", "utopia"]
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


# ---------------------------------------------------------------------------
# 字幕源（SUG / Nicokara LRC）中间表示
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuideSymbol:
    """嵌入工程的行前导唱符轮廓及其逐行显示设置。

    ``path_commands`` 使用 QPainterPath 等价的 M/L/C/Z 命令，坐标位于
    ``units_per_em`` 字形坐标系内，基线为 y=0。它是渲染层的行内虚拟字符，
    不进入歌词正文，也不占用 ``TimingLine.chars`` 的用户可见索引。
    """

    name: str = "导唱符"
    path_commands: tuple[tuple[object, ...], ...] = ()
    units_per_em: int = 1000
    advance_width: float = 1000.0
    duration_ms: int = 1000
    count: int = 1
    role_label: Optional[str] = None
    role_labels: tuple[Optional[str], ...] = ()
    replacement_prefix: tuple[str, ...] = ()
    """被导唱符替代的原始行首打轴单元；空元组表示在正文前额外插入。"""
    kind: Literal["vector", "bitmap"] = "vector"
    bitmap_before_path: Optional[str] = None
    bitmap_after_path: Optional[str] = None
    bitmap_zoom_percent: int = 100
    bitmap_fix_size: bool = False
    bitmap_no_decor: bool = False
    bitmap_force_wipe_decor: bool = False
    bitmap_margin_left_px: int = 0
    bitmap_margin_right_px: int = 0
    bitmap_margin_bottom_px: int = 0
    prefix_timing: Literal["pre_roll", "anchored"] = "pre_roll"


def guide_symbol_role_labels(symbol: GuideSymbol) -> tuple[Optional[str], ...]:
    """Return one role label per inline guide glyph, preserving V1 fallback."""
    count = max(int(symbol.count), 1)
    labels = [label or None for label in symbol.role_labels[:count]]
    labels.extend([symbol.role_label or None] * (count - len(labels)))
    return tuple(labels)


def guide_symbol_with_role_labels(
    symbol: GuideSymbol, labels: list[Optional[str]] | tuple[Optional[str], ...]
) -> GuideSymbol:
    count = max(int(symbol.count), 1)
    normalized = tuple(
        (labels[index] or None) if index < len(labels) else None
        for index in range(count)
    )
    shared = normalized[0] if normalized and len(set(normalized)) == 1 else None
    return replace(symbol, role_label=shared, role_labels=normalized)


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

    explicit_start: bool = False
    """源字幕是否在本字符前显式写了时间戳。

    N3 仅在 ruby 覆盖的正文内部没有显式边界时，才用注音字符重新切分正文；因此不能
    只保留归一化后的 ``start_ms``，还必须保留这个来源信息。手工构造的兼容数据默认
    为 ``False``，继续走旧的 ruby 推导路径。
    """

    explicit_end: bool = False
    """源字幕是否在本字符后显式写了结束/释放时间戳。"""

    vector_glyph: Optional[GuideSymbol] = None
    """仅供渲染层生成的行内虚拟字符使用；字幕源解析出的真实字符恒为 None。"""


@dataclass(frozen=True)
class LineAnimationOverride:
    """一行歌词对全局入场/退场/唱字动画的完整覆盖。"""

    entry_anim: EntryAnimation = "none"
    entry_duration_ms: int = 300
    exit_anim: ExitAnimation = "none"
    exit_duration_ms: int = 300
    karaoke_anim: KaraokeAnimation = "inherit"
    """唱字动画。``inherit`` 沿用全局推导（与 Style 同义），让次字幕这类行能单独
    关掉或打开唱字特效，而不必跟着主字幕走。"""


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
    track_line_index: Optional[int] = None
    """本行在 ``TimingTrack.lines`` 中的下标，由加载器写入。

    只用于把 ``RubyAnnotation.target_line_index`` 对回来：多轨时间重叠时，仅靠
    注音的 ``pos_*`` 无法判定它属于哪一行。手工构造的 track 留 ``None``，此时
    不做行归属否决，行为与历史一致。
    """
    layout_index: int = 0
    """该行使用的布局（N3 ``LayoutIndex``）：0 = 默认布局（``Style`` 自身字段），
    k >= 1 = ``Style.layouts[k-1]``。由 UI 按页联动写入，随项目文件持久化
    （LRC 本身不含布局信息）。"""
    break_before: LineBreakKind = "none"
    """本行前的 N3 分隔类型。

    ``page`` / ``paragraph`` 都会开启新页；区别用于 N3 的跨页衔接与段落语义。
    裸 LRC 不包含该信息；导入 N3 项目时由其 ``LineInfos`` 精确恢复，
    schema v2 项目则把它作为 ``page_plan`` 的兼容投影。
    """
    display_start_override_ms: Optional[int] = None
    """本行「上屏时刻」手动覆盖（毫秒）。None = 按全局提前入场自动计算。
    由字幕轨道拖动写入，随项目文件持久化；覆盖值优先于自动布局。"""
    display_end_override_ms: Optional[int] = None
    """本行「消失时刻」手动覆盖（毫秒）。None = 按全局延迟退场自动计算。"""
    animation_override: Optional[LineAnimationOverride] = None
    """逐行动画覆盖；None = 继承全局 ``Style`` 的入场/退场设置。"""
    guide_symbol: Optional[GuideSymbol] = None
    """可选导唱符；可插在正文前或替代行首标记，但不改变源 ``chars`` 索引。"""
    inline_guide_symbols: dict[int, GuideSymbol] = field(default_factory=dict)
    """按源 ``chars`` 索引保存的行内 SVG 字形替换；原字符与打轴时间保持不变。"""


@dataclass(frozen=True)
class SubtitleLoadingSettings:
    """Rules used to turn one subtitle source into explicit sections and pages."""

    time_gap_section_enabled: bool = True
    section_gap_ms: int = 3100
    blank_line_section_enabled: bool = True
    rows_per_page: int = 2


@dataclass
class TrackPage:
    """One persisted subtitle page.

    ``line_count`` counts renderable lyric lines, not source blank lines and not
    the capacity of the selected layout.
    """

    line_count: int
    layout_id: str = "default"


@dataclass
class TrackSection:
    pages: list[TrackPage] = field(default_factory=list)


@dataclass
class TrackPagePlan:
    sections: list[TrackSection] = field(default_factory=list)


def guide_symbol_replacement_count(
    line: TimingLine, symbol: Optional[GuideSymbol] = None
) -> int:
    """返回可安全替换的行首打轴单元数；源歌词变化时返回 0。"""
    guide = symbol if symbol is not None else line.guide_symbol
    if guide is None or not guide.replacement_prefix:
        return 0
    prefix = tuple(guide.replacement_prefix)
    if max(int(guide.count), 1) != len(prefix):
        return 0
    if len(line.chars) <= len(prefix):
        return 0
    if tuple(char.text for char in line.chars[: len(prefix)]) != prefix:
        return 0
    return len(prefix)


def line_visible_chars(line: TimingLine) -> list[TimingChar]:
    """歌词预览/角色编辑中可见的真实字符（排除已被导唱符替代的前缀）。"""
    return line.chars[guide_symbol_replacement_count(line) :]


@dataclass
class RubyAnnotation:
    """单个 ``@RubyN`` 注音条目。

    对应导出器的格式 ``@RubyN=漢字,読み[t1][t2]...,pos1,pos2``：

    - ``kanji``：基底字（汉字 / 假名）
    - ``reading``：读音去掉 mora 时间戳后的纯文本
    - ``reading_part_ms``：mora 时间戳序列（毫秒，与原始 ``[t]`` 数量相同）
    - ``reading_parts``：被内嵌时间戳分开的原始读音 part；连续时间戳会保留空 part
    - ``pos_start_ms`` / ``pos_end_ms``：本条注音在歌曲时间轴上的生效区间
    - ``target_line_index``：目标所在行在 ``TimingTrack.lines`` 中的下标
    - ``target_char_start`` / ``target_char_end``：目标正文字符的**行内**半开区间

    ``target_*`` 是加载时定死的权威目标。``.sug`` 的逐字注音本来就带精确索引，
    RL / nicokara 的 ``@RubyN`` 则在解析时按 N3 的位置驱动算法解析一次。两者都填
    这两个字段后，渲染侧不再需要按 ``kanji`` 回头做文本搜索——那套搜索只能返回
    一个出现，同一基字在一行内重复时（``ケロケロケロ…``）会把全部注音叠到第一个
    出现上，长短基字重叠时（``呼`` 与 ``呼吸``）又会两条都画出来。

    ``target_line_index`` 解决另一半问题：多轨（主唱 / 和声）时间重叠时，只靠
    ``pos_*`` 时间窗无法判定注音属于哪一行；若两行恰好在同一下标是同一个字，
    另一行的注音就会被一起画上去。有了行下标就是事实而非推断。

    三者均为 ``None`` 表示旧数据，此时回落到历史的文本 + 时间匹配。
    """

    kanji: str
    reading: str
    reading_part_ms: list[int] = field(default_factory=list)
    pos_start_ms: int = 0
    pos_end_ms: int = 0
    reading_parts: list[str] = field(default_factory=list)
    target_line_index: Optional[int] = None
    target_char_start: Optional[int] = None
    target_char_end: Optional[int] = None


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
    page_plan: Optional[TrackPagePlan] = None
    """Authoritative section/page structure used by preview and export.

    ``None`` keeps the legacy path available for bare parser callers and old
    unit tests.  A render project normalizes this field before presentation.
    """
    loading_settings_mode: Literal["global", "custom"] = "global"
    loading_settings: Optional[SubtitleLoadingSettings] = None
    loading_settings_snapshot: SubtitleLoadingSettings = field(
        default_factory=SubtitleLoadingSettings
    )
    """Settings that most recently produced the persisted page plan."""

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
            if line.guide_symbol is not None:
                for label in guide_symbol_role_labels(line.guide_symbol):
                    if label and label not in seen:
                        seen.add(label)
                        options.append(label)
            for ch in line_visible_chars(line):
                label = ch.role_label
                if label and label not in seen:
                    seen.add(label)
                    options.append(label)
        return options


def timing_line_start_ms(line: TimingLine) -> int:
    """行内首个可唱元素的时刻；导唱符存在时它就是虚拟首字符。"""
    if not line.chars:
        return 0
    start = int(line.chars[0].start_ms)
    if line.guide_symbol is not None:
        if (
            line.guide_symbol.replacement_prefix
            or line.guide_symbol.prefix_timing == "anchored"
        ):
            return start
        start -= (
            max(int(line.guide_symbol.duration_ms), 0)
            * max(int(line.guide_symbol.count), 1)
        )
    return start


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
# tail=仅片尾一段；head_tail=开始和片尾各显示一段。
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

    静态文字（曲名 / 艺术家），不走字；默认参数逆向自 NicoKaraMaker3
    「情報小」：教科书体 40px、灰白字黑边、白色小发光、左上、整段显示。
    文字模板 ``{title}`` / ``{artist}`` 由字幕源
    ``@Title`` / ``@Artist`` 元数据替换；也可直接填任意自定义文字（含换行）。
    """

    enabled: bool = False
    text_template: str = "{title} / {artist}"
    """``{title}`` / ``{artist}`` 占位符按元数据替换；``\\n`` 分行。"""

    char_role_labels: list[list[Optional[str]]] = field(default_factory=list)
    """逐行逐字符角色标签；``None`` 表示继承内置「标题」方案。"""

    # 字体（逆向目标项目的 N3「情報小」）
    font_family: str = "UD デジタル 教科書体 N-B"
    font_family_latin: Optional[str] = "Comic Sans MS"
    font_size_px: int = 40
    font_weight: int = 700
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
    shadow_offset_x: int = 10
    shadow_offset_y: int = 10

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
    # None keeps old projects byte-semantically compatible: the ending
    # segment inherits the corresponding opening value until edited.
    tail_duration_ms: Optional[int] = None
    tail_fade_in_ms: Optional[int] = None
    tail_fade_out_ms: Optional[int] = None


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
    letter_spacing_px: Optional[int] = None
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
)

LYRICS_LAYOUT_CHAR_FIELDS: tuple[str, ...] = (
    "letter_spacing_px",
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
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None
    allow_biting: Optional[bool] = None
    font_weight: Optional[int] = None
    italic: Optional[bool] = None
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
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "ruby_font_family",
    "ruby_font_weight",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_font_family_latin",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
)


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
    timing_offset_ms: int
    ruby_main_progress_mode: RubyMainProgressMode
    line_lane_gap_ms: int
    section_gap_ms: int
    sync_entry: bool
    sync_ending: bool
    section_ending_mode: SectionEndingMode
    entry_anim: EntryAnimation
    entry_lead_ms: int
    exit_anim: ExitAnimation
    exit_fade_ms: int
    karaoke_anim: KaraokeAnimation


_STYLE_TIMING_FIELDS = tuple(field.name for field in fields(StyleTimingConfig))


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
    latin_stroke_width_px: Optional[int] = None
    latin_stroke2_enabled: Optional[bool] = None
    latin_stroke2_width_px: Optional[int] = None

    allow_biting: bool = False
    """允许负 side bearing 令相邻字形咬合。"""

    allow_inter_page_line_overlap: bool = False
    """允许不同页面的字幕行保持旧式重叠行为。

    关闭时，渲染器按最终像素范围移动后进入的整页字幕；该字段是项目级设置，
    不属于 ``LyricsLayout``，也不随分页布局预设切换。
    """

    font_weight: int = 400  # Qt 习惯 100-900
    italic: bool = False
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
    glow_radius_px: int = 10
    glow_before_radius_px: int = 10
    glow_after_radius_px: int = 10
    glow_concentration_level: int = 0
    """-1 disables glow; NicoKaraMaker3 ``BlurLevel`` 0/1/2 = low/medium/high."""
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
    ruby_font_follow_main: bool = True
    """注音字体族/字重跟随主文字；注音字号始终由独立字段控制。"""
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

    sync_entry: bool = False
    """同步入场：只把同页较晚上屏的自动 T 尽量向前延长；碰撞时允许部分同步，
    已经更早上屏的 T 绝不向后压缩。"""

    sync_ending: bool = False
    """同步退场：只把同页较早消失的自动 T 尽量向后延长；碰撞时允许部分同步，
    已经更晚消失的 T 绝不向前压缩。"""

    section_ending_mode: SectionEndingMode = "hold"
    """段落结束行为：``hold`` 维持现状（按 N3 TopLong 挂到段末）；``clear`` 段末即
    清屏，字幕不拖进间奏。"""

    entry_anim: EntryAnimation = "none"
    """入场动画：none / fade / slide_in / rise / char_fade / char_drip / spin_flip / utopia。"""

    entry_lead_ms: int = 300
    """入场动画时长；不改变歌词填色时间，只影响显示窗口起点后的过渡。"""

    exit_anim: ExitAnimation = "none"
    """退场动画：none / fade / slide_out / rise / char_fade / char_drip / spin_flip / utopia。"""

    exit_fade_ms: int = 300
    """退场动画时长；在显示窗口结束前开始。"""

    karaoke_anim: KaraokeAnimation = "inherit"
    """唱字动画：inherit（兼容旧 Utopia）/ none / utopia。"""

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
    if override is None:
        return style
    changes: dict[str, object] = {
        "entry_anim": override.entry_anim,
        "entry_lead_ms": max(int(override.entry_duration_ms), 0),
        "exit_anim": override.exit_anim,
        "exit_fade_ms": max(int(override.exit_duration_ms), 0),
    }
    if override.karaoke_anim != "inherit":
        # inherit 表示「保持全局那一档」，必须原样留下 style.karaoke_anim。
        # 若把 "inherit" 写进行样式，effective_karaoke_animation 会转而去看这一行
        # 被覆盖后的入退场——全局显式设的 utopia 就这么丢了。
        changes["karaoke_anim"] = override.karaoke_anim
    return style.with_timing(**changes)


def effective_karaoke_animation(style: Style) -> Literal["none", "utopia"]:
    """Resolve the singing animation while preserving legacy Utopia projects."""
    timing = style.timing
    if timing.karaoke_anim == "utopia":
        return "utopia"
    if timing.karaoke_anim == "none":
        return "none"
    return (
        "utopia"
        if "utopia" in {timing.entry_anim, timing.exit_anim}
        else "none"
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
        letter_spacing_px=style.letter_spacing_px,
        allow_biting=style.allow_biting,
        ruby_interval_px=style.ruby_interval_px,
        ruby_alignment=style.ruby_alignment,
        ruby_gap_px=style.ruby_gap_px,
    )


def ensure_page_layout_defaults(style: Style) -> Style:
    """Return a style with stable unique IDs and complete 1..8 defaults.

    Missing built-ins are appended, never inserted, so numeric layout indices
    from schema-v1 projects and N3 imports retain their meaning.
    """

    layouts = deepcopy(style.layouts)
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

    required_rows = [1, 3, 4, 5, 6, 7, 8]
    if max(1, min(len(style.line_alignments), 8)) != 2:
        required_rows.append(2)
    for rows in required_rows:
        layout_id = f"builtin-{rows}"
        if layout_id in used:
            continue
        layouts.append(_builtin_page_layout(style, rows))
        used.add(layout_id)

    raw_mapping = style.default_layout_by_row_count
    mapping: dict[int, str] = {}
    capacity_by_id = {
        layout.layout_id: max(1, min(len(layout.line_alignments), 8))
        for layout in layouts
    }
    for rows in range(1, 9):
        layout_id = str(raw_mapping.get(rows, "") or "")
        if rows == 2:
            default_capacity = max(1, min(len(style.line_alignments), 8))
            fallback = "default" if default_capacity == 2 else "builtin-2"
            mapping[rows] = (
                layout_id
                if (
                    (layout_id == "default" and default_capacity == rows)
                    or capacity_by_id.get(layout_id) == rows
                )
                else fallback
            )
            continue
        fallback = f"builtin-{rows}"
        mapping[rows] = (
            layout_id if capacity_by_id.get(layout_id) == rows else fallback
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


def subtitle_loading_settings_to_dict(
    settings: SubtitleLoadingSettings,
) -> dict[str, object]:
    return {
        "time_gap_section_enabled": bool(settings.time_gap_section_enabled),
        "section_gap_ms": max(int(settings.section_gap_ms), 0),
        "blank_line_section_enabled": bool(settings.blank_line_section_enabled),
        "rows_per_page": max(1, min(int(settings.rows_per_page), 4)),
    }


def subtitle_loading_settings_from_dict(value: object) -> SubtitleLoadingSettings:
    defaults = SubtitleLoadingSettings()
    if not isinstance(value, dict):
        return defaults
    try:
        gap = max(int(value.get("section_gap_ms", defaults.section_gap_ms)), 0)
    except (TypeError, ValueError):
        gap = defaults.section_gap_ms
    try:
        rows = max(1, min(int(value.get("rows_per_page", defaults.rows_per_page)), 4))
    except (TypeError, ValueError):
        rows = defaults.rows_per_page
    return SubtitleLoadingSettings(
        time_gap_section_enabled=bool(
            value.get("time_gap_section_enabled", defaults.time_gap_section_enabled)
        ),
        section_gap_ms=gap,
        blank_line_section_enabled=bool(
            value.get("blank_line_section_enabled", defaults.blank_line_section_enabled)
        ),
        rows_per_page=rows,
    )


def track_page_plan_to_dict(plan: Optional[TrackPagePlan]) -> Optional[dict[str, object]]:
    if plan is None:
        return None
    return {
        "sections": [
            {
                "pages": [
                    {
                        "line_count": max(1, min(int(page.line_count), 8)),
                        "layout_id": str(page.layout_id or "default"),
                    }
                    for page in section.pages
                    if int(page.line_count) > 0
                ]
            }
            for section in plan.sections
            if any(int(page.line_count) > 0 for page in section.pages)
        ]
    }


def track_page_plan_from_dict(value: object) -> Optional[TrackPagePlan]:
    if not isinstance(value, dict) or not isinstance(value.get("sections"), list):
        return None
    sections: list[TrackSection] = []
    for raw_section in value["sections"]:
        if not isinstance(raw_section, dict) or not isinstance(
            raw_section.get("pages"), list
        ):
            continue
        pages: list[TrackPage] = []
        for raw_page in raw_section["pages"]:
            if not isinstance(raw_page, dict):
                continue
            try:
                count = int(raw_page.get("line_count", 0))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            pages.append(
                TrackPage(
                    line_count=max(1, min(count, 8)),
                    layout_id=str(raw_page.get("layout_id") or "default"),
                )
            )
        if pages:
            sections.append(TrackSection(pages=pages))
    return TrackPagePlan(sections=sections)


def normalize_glow_concentration_level(value: object, fallback: int = 0) -> int:
    """Normalize -1 (disabled) plus the three NicoKaraMaker3 blur levels."""
    try:
        return max(-1, min(2, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(-1, min(2, int(fallback)))


def line_animation_override_to_dict(
    override: Optional[LineAnimationOverride],
) -> Optional[dict[str, object]]:
    if override is None:
        return None
    data: dict[str, object] = {
        "entry_anim": override.entry_anim,
        "entry_duration_ms": max(int(override.entry_duration_ms), 0),
        "exit_anim": override.exit_anim,
        "exit_duration_ms": max(int(override.exit_duration_ms), 0),
    }
    # inherit 不落盘：它不带信息，而且没用这个功能的项目重新保存后文件不该平白多
    # 出一个键（读回时缺键本来就按 inherit 处理）。
    if override.karaoke_anim != "inherit":
        data["karaoke_anim"] = override.karaoke_anim
    return data


def line_animation_override_from_dict(value: object) -> Optional[LineAnimationOverride]:
    if not isinstance(value, dict):
        return None
    entry = value.get("entry_anim")
    exit_ = value.get("exit_anim")
    valid_entry = {
        "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
    }
    valid_exit = {
        "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
    }
    if entry not in valid_entry or exit_ not in valid_exit:
        return None

    def duration(key: str, fallback: int) -> int:
        try:
            return max(int(value.get(key, fallback)), 0)
        except (TypeError, ValueError):
            return fallback

    karaoke = value.get("karaoke_anim")
    if karaoke not in {"inherit", "none", "utopia"}:
        # 旧项目没有这一项，按继承处理——渲染结果与加这个字段之前一致。
        karaoke = "inherit"
    return LineAnimationOverride(
        entry_anim=entry,
        entry_duration_ms=duration("entry_duration_ms", 300),
        exit_anim=exit_,
        exit_duration_ms=duration("exit_duration_ms", 300),
        karaoke_anim=karaoke,
    )


def guide_symbol_to_dict(symbol: Optional[GuideSymbol]) -> Optional[dict[str, object]]:
    if symbol is None:
        return None
    data: dict[str, object] = {
        "name": symbol.name,
        "path_commands": [list(command) for command in symbol.path_commands],
        "units_per_em": max(int(symbol.units_per_em), 1),
        "advance_width": max(float(symbol.advance_width), 0.0),
        "duration_ms": max(int(symbol.duration_ms), 0),
        "count": max(int(symbol.count), 1),
        "role_label": symbol.role_label or None,
        "role_labels": [label or None for label in symbol.role_labels],
        "replacement_prefix": list(symbol.replacement_prefix),
    }
    if symbol.kind != "vector" or symbol.bitmap_before_path:
        data.update(
            {
                "kind": symbol.kind,
                "bitmap_before_path": symbol.bitmap_before_path,
                "bitmap_after_path": symbol.bitmap_after_path,
                "bitmap_zoom_percent": max(int(symbol.bitmap_zoom_percent), 1),
                "bitmap_fix_size": bool(symbol.bitmap_fix_size),
                "bitmap_no_decor": bool(symbol.bitmap_no_decor),
                "bitmap_force_wipe_decor": bool(symbol.bitmap_force_wipe_decor),
                "bitmap_margin_left_px": int(symbol.bitmap_margin_left_px),
                "bitmap_margin_right_px": int(symbol.bitmap_margin_right_px),
                "bitmap_margin_bottom_px": int(symbol.bitmap_margin_bottom_px),
                "prefix_timing": symbol.prefix_timing,
            }
        )
    elif symbol.prefix_timing != "pre_roll":
        data["prefix_timing"] = symbol.prefix_timing
    return data


def guide_symbol_from_dict(value: object) -> Optional[GuideSymbol]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "vector")
    if kind not in {"vector", "bitmap"}:
        kind = "vector"
    raw_commands = value.get("path_commands")
    if not isinstance(raw_commands, list):
        if kind != "bitmap":
            return None
        raw_commands = []
    before_path = str(value.get("bitmap_before_path") or "").strip() or None
    after_path = str(value.get("bitmap_after_path") or "").strip() or None
    if kind == "bitmap" and before_path is None:
        return None
    commands: list[tuple[object, ...]] = []
    expected_lengths = {"M": 3, "L": 3, "C": 7, "Q": 5, "Z": 1}
    try:
        for raw in raw_commands:
            if not isinstance(raw, (list, tuple)) or not raw:
                return None
            command_kind = str(raw[0]).upper()
            if len(raw) != expected_lengths.get(command_kind, -1):
                return None
            commands.append((command_kind, *(float(item) for item in raw[1:])))
        units_per_em = max(int(value.get("units_per_em", 1000)), 1)
        advance_width = max(float(value.get("advance_width", units_per_em)), 0.0)
        duration_ms = max(int(value.get("duration_ms", 1000)), 0)
        count = max(int(value.get("count", 1)), 1)
        zoom_percent = max(int(value.get("bitmap_zoom_percent", 100)), 1)
        margin_left = int(value.get("bitmap_margin_left_px", 0))
        margin_right = int(value.get("bitmap_margin_right_px", 0))
        margin_bottom = int(value.get("bitmap_margin_bottom_px", 0))
    except (TypeError, ValueError):
        return None
    if kind == "vector" and not commands:
        return None
    prefix_timing = str(value.get("prefix_timing") or "pre_roll")
    if prefix_timing not in {"pre_roll", "anchored"}:
        prefix_timing = "pre_roll"
    role = value.get("role_label")
    raw_role_labels = value.get("role_labels")
    role_labels = (
        tuple(str(label).strip() or None if label else None for label in raw_role_labels[:count])
        if isinstance(raw_role_labels, list)
        else ()
    )
    raw_replacement_prefix = value.get("replacement_prefix")
    replacement_prefix = (
        tuple(str(text) for text in raw_replacement_prefix if str(text))
        if isinstance(raw_replacement_prefix, list)
        else ()
    )
    return GuideSymbol(
        name=str(value.get("name") or "导唱符"),
        path_commands=tuple(commands),
        units_per_em=units_per_em,
        advance_width=advance_width,
        duration_ms=duration_ms,
        count=count,
        role_label=str(role).strip() or None if role else None,
        role_labels=role_labels,
        replacement_prefix=replacement_prefix,
        kind=kind,  # type: ignore[arg-type]
        bitmap_before_path=before_path,
        bitmap_after_path=after_path,
        bitmap_zoom_percent=zoom_percent,
        bitmap_fix_size=bool(value.get("bitmap_fix_size", False)),
        bitmap_no_decor=bool(value.get("bitmap_no_decor", False)),
        bitmap_force_wipe_decor=bool(value.get("bitmap_force_wipe_decor", False)),
        bitmap_margin_left_px=margin_left,
        bitmap_margin_right_px=margin_right,
        bitmap_margin_bottom_px=margin_bottom,
        prefix_timing=prefix_timing,  # type: ignore[arg-type]
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
        elif item.name == "default_layout_by_row_count":
            data[item.name] = {str(key): str(item) for key, item in value.items()}
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
            "timing_offset_ms",
            "line_lane_gap_ms",
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
            "affects_ruby_anchor",
            "allow_biting",
            "allow_inter_page_line_overlap",
            "stroke2_enabled",
            "ruby_font_follow_main",
            "ruby_colors_follow_main",
            "ruby_horizontal_gradient_with_main",
            "dual_line_layout",
            "right_to_left",
            "vertical",
            "sync_entry",
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
        elif key == "ruby_decoration_kind":
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
        elif key == "line_alignments":
            changes[key] = _line_alignments_from_payload(value)
        elif key == "entry_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
                }
                else defaults.entry_anim
            )
        elif key == "exit_anim":
            changes[key] = (
                value
                if value in {
                    "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
                }
                else defaults.exit_anim
            )
        elif key == "karaoke_anim":
            changes[key] = (
                value if value in {"inherit", "none", "utopia"} else defaults.karaoke_anim
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
    if "ruby_colors_follow_main" not in payload:
        # 旧工程没有显式开关：已有独立注音矩阵继续独立，否则采用新默认跟随。
        changes["ruby_colors_follow_main"] = (
            changes.get("ruby_karaoke_colors") is None
        )
    _migrate_title_references(changes)
    return ensure_page_layout_defaults(Style(**changes))


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
)

_TITLE_FONT_VISUAL_SIZE_FIELDS: tuple[str, ...] = (
    "font_size_px",
    "stroke_width_px",
    "stroke2_width_px",
    "glow_radius_px",
    "shadow_offset_x",
    "shadow_offset_y",
)


def rescale_font_sizes(style: Style, new_height: int) -> Style:
    """Scale font visual pixel fields when the output height changes.

    This mirrors N3's ``SizeAndRatio`` arithmetic: values are multiplied by
    ``new_height / font_reference_height`` and truncated toward zero. Optional
    overrides remain ``None`` so their inheritance semantics are preserved.
    Character/layout spacing is handled separately by ``rescale_layout_sizes``.
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
        name: scale_dataclass(scheme, _FONT_VISUAL_SIZE_FIELDS)
        for name, scheme in style.custom_style_schemes.items()
    }
    singer_overrides = {
        singer: scale_dataclass(scheme, _FONT_VISUAL_SIZE_FIELDS)
        for singer, scheme in style.singer_style_overrides.items()
    }
    title_overlay = style.title_overlay
    if title_overlay is not None:
        title_overlay = scale_dataclass(
            title_overlay,
            _TITLE_FONT_VISUAL_SIZE_FIELDS,
        )
    changes = {
        name: scaled(getattr(style, name)) for name in _FONT_VISUAL_SIZE_FIELDS
    }
    return replace(
        style,
        **changes,
        custom_style_schemes=custom_schemes,
        singer_style_overrides=singer_overrides,
        title_overlay=title_overlay,
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
        "letter_spacing_px": layout.letter_spacing_px,
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
        letter_spacing_px=(
            _int_value(payload.get("letter_spacing_px"), 0)
            if payload.get("letter_spacing_px") is not None
            else None
        ),
        allow_biting=(
            bool(payload.get("allow_biting"))
            if payload.get("allow_biting") is not None
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
            "ruby_latin_stroke_width_px",
            "ruby_latin_stroke2_width_px",
        }:
            parsed = _int_value(value, 0)
            changes[key] = parsed if parsed > 0 else None
        elif key in {
            "ruby_colors_follow_main",
            "ruby_horizontal_gradient_with_main",
        }:
            changes[key] = bool(value) if value is not None else None
        else:
            changes[key] = value
    if (
        "ruby_colors_follow_main" not in payload
        and changes.get("ruby_karaoke_colors") is not None
    ):
        # 旧角色方案保存过独立注音矩阵时，保留原行为。
        changes["ruby_colors_follow_main"] = False
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
        "tail_duration_ms": title.tail_duration_ms,
        "tail_fade_in_ms": title.tail_fade_in_ms,
        "tail_fade_out_ms": title.tail_fade_out_ms,
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
    if decoration not in {"none", "shadow", "glow"}:
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
    )


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
    """把持久化标题标签规范成与当前逐行文字严格等长的矩阵。

    整行同一个角色的行按「整行角色」处理：这种行与字符数无关，因此
    ``{title}`` / ``{artist}`` 展开成元数据、或标题文字改长改短之后，角色
    依然覆盖整行；只有逐字符混排的行才需要标签与文字严格对位。
    """
    raw_rows = payload if isinstance(payload, list) else []
    normalized: list[list[Optional[str]]] = []
    for row_index, line in enumerate(str(text).split("\n")):
        raw = raw_rows[row_index] if row_index < len(raw_rows) else []
        values = raw if isinstance(raw, (list, tuple)) else []
        row_role = title_row_role(values)
        if row_role is not None:
            normalized.append([row_role] * len(line))
            continue
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
    # 整行角色跟着整行走：改字后新增的字符也留在同一个角色里，而不是逐字符
    # 对位后把没匹配上的部分退回标题默认。
    new_lines = new_text.split("\n")
    if len(new_lines) == len(normalized):
        for row_index, values in enumerate(normalized):
            row_role = title_row_role(values)
            if row_role is not None:
                rows[row_index] = [row_role] * len(new_lines[row_index])
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
        image_scale_pct=max(
            1, min(_int_value(payload.get("image_scale_pct"), 100), 1000)
        ),
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
