"""Backend-neutral subtitle timing, ruby, guide, page, and source models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Optional


LineBreakKind = Literal["none", "page", "paragraph"]
EntryAnimation = Literal[
    "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
]
ExitAnimation = Literal[
    "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
]
KaraokeAnimation = Literal["inherit", "none", "utopia"]


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
    allocate_layout_by_actual_rows: bool = False
    apply_sug_export_compensation: bool = True
    """读取 ``.sug`` 时是否应用打轴模块（SUG）「设置 → 导出 → 软件导出补偿」
    （``export.software_compensation_ms``）。补偿叠加在 ``.sug`` 自带的导出
    偏移之上，与 SUG 导出 LRC 的口径一致；不影响 LRC ``@Offset`` 元数据与
    ``style.timing_offset_ms``。仅在重新解析 ``.sug`` 文件时生效。"""


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


def guide_symbol_has_visual(symbol: object) -> bool:
    """是否是能真正画出东西的导唱符：SVG 轮廓，或 ``@Emoji`` 这类位图小头像。

    位图导唱符没有 ``path_commands``，早期只按轮廓判断的校验会把它当成非法值
    整条丢弃（行内小头像因此在保存/撤销/逐字符编辑里消失）。
    """
    if not isinstance(symbol, GuideSymbol):
        return False
    return bool(symbol.path_commands) or (
        symbol.kind == "bitmap" and bool(symbol.bitmap_before_path)
    )


def guide_symbol_replaces_prefix(symbol: Optional[GuideSymbol]) -> bool:
    """该行级导唱符是否占用行首打轴单元（而不是额外插在正文之前）。

    ``@Emoji`` 小头像与「行前导唱符」都属于后者：它们不替代任何真实字符，因此
    行首标记替换必须绕开 ``TimingLine.guide_symbol`` 这个唯一槽位，改用行内替换，
    否则会把小头像顶掉。
    """
    return isinstance(symbol, GuideSymbol) and bool(symbol.replacement_prefix)


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

