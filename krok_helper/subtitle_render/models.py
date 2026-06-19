"""渲染项目数据模型。

包含两层模型：

1. **TimingTrack** 及其下属 — 字幕源（Nicokara 逐字 LRC）解析后的中间表示。
   每行可寻址到具体字符、字符的起始毫秒、行末与行内停顿释放时间戳、ふりがな 注音。
   解析器在 :mod:`subtitle_sources` 实现。

2. **RenderProject** 及其下属 — 渲染项目的持久化模型（``.krrender.json``），含
   字幕源引用、背景、样式、输出参数。MVP 阶段 :class:`Style` / 序列化等仍为占位，
   后续 P0 任务（A4/A6/A8 等）落地。

**字幕源格式**：唯一支持 Nicokara 逐字 LRC（``.lrc``，SUG ``NicokaraExporter``
产物，含 ``@Ruby`` / ``@Offset`` / ``@Title`` / ``@Artist`` / 演唱者标签）。
不支持 ``.ass`` / ``.sug`` / ``.nkm``——SUG 已能输出 Nicokara LRC，模块只需要解析
Nicokara LRC 即可（SUG submodule 自身只有导出器没有解析器）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Literal, Optional

SCHEMA_VERSION = 1
PROJECT_FILE_SUFFIX = ".krrender.json"
STYLE_PRESET_FILE_SUFFIX = ".krstyle.json"
SUBTITLE_SOURCE_SUFFIX = ".lrc"


# ---------------------------------------------------------------------------
# 字幕源（Nicokara LRC）中间表示
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


@dataclass
class RubyAnnotation:
    """单个 ``@RubyN`` 注音条目。

    对应导出器的格式 ``@RubyN=漢字,読み[t1][t2]...,pos1,pos2``：

    - ``kanji``：基底字（汉字 / 假名）
    - ``reading``：读音去掉 mora 时间戳后的纯文本
    - ``reading_part_ms``：mora 时间戳序列（毫秒，与原始 ``[t]`` 数量相同）
    - ``pos_start_ms`` / ``pos_end_ms``：本条注音在歌曲时间轴上的生效区间
    """

    kanji: str
    reading: str
    reading_part_ms: list[int] = field(default_factory=list)
    pos_start_ms: int = 0
    pos_end_ms: int = 0


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
    """解析 Nicokara LRC 后的完整中间表示。"""

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


# ---------------------------------------------------------------------------
# 渲染项目持久化模型（``.krrender.json``）
# ---------------------------------------------------------------------------


@dataclass
class SubtitleSource:
    """字幕源引用（Nicokara 逐字 LRC，唯一格式）。"""

    path: str = ""
    singer_filter: Optional[list[int]] = None


LineYPosition = Literal["top", "center", "bottom"]
LineHorizontalLayout = Literal["asymmetric", "center"]
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
EntryAnimation = Literal["none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"]
ExitAnimation = Literal["none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"]


@dataclass
class PaintFill:
    """One fill definition shared by text, stroke, second stroke and shadow."""

    mode: ColorFillMode = "solid"
    color: str = "#FFFFFF"
    start_color: str = "#FFFFFF"
    end_color: str = "#FFFFFF"
    gradient_stops: list[tuple[int, str]] = field(default_factory=list)
    split_top_color: str = "#FFFFFF"
    split_bottom_color: str = "#FFFFFF"
    split_position_pct: int = 50
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
class SubtitleStyleScheme:
    """字幕 tab 的完整视觉方案；不包含位置、布局和显示时间。"""

    font_family: Optional[str] = None
    font_size_px: Optional[int] = None
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
    stroke2_width_px: Optional[int] = None
    decoration_kind: Optional[DecorationKind] = None
    glow_radius_px: Optional[int] = None
    shadow_color: Optional[str] = None
    shadow_offset_x: Optional[int] = None
    shadow_offset_y: Optional[int] = None
    ruby_font_size_px: Optional[int] = None
    ruby_color: Optional[str] = None
    ruby_gap_px: Optional[int] = None
    karaoke_colors: Optional[KaraokeColors] = None


@dataclass
class Style:
    """字幕样式（A4 / A5 / A6 实装的纯色 + 横书き子集）。

    字段默认值面向 NicoKaraMaker 风格：教科书体 + 小字号 + 双行底部布局。
    后续 A5 / A6 / B3 等任务在此基础上扩字段（渐变 / 发光 / 注音 / 动画）。
    """

    # 字体
    font_family: str = "UD Digi Kyokasho N-B"
    font_size_px: int = 100
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
    stroke_width_px: int = 9
    stroke2_width_px: int = 0

    decoration_kind: DecorationKind = "shadow"
    glow_radius_px: int = 10
    shadow_color: str = "#000000"
    shadow_offset_x: int = 0
    shadow_offset_y: int = 1
    karaoke_colors: Optional[KaraokeColors] = None

    singer_style_overrides: dict[int, SubtitleStyleScheme] = field(default_factory=dict)
    """B2：按歌手自动套用的字幕 tab 方案。不覆盖位置、时间或布局。"""

    custom_style_schemes: dict[str, SubtitleStyleScheme] = field(default_factory=dict)
    """用户自行添加的配色方案。当前用于编辑/复用，后续可接入方案分配。"""

    # ふりがな / ruby（B1）
    ruby_font_size_px: int = 35
    ruby_color: str = "#FF5A6F"
    ruby_gap_px: int = 4

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
    """双行水平布局：``asymmetric`` 为上左下右，``center`` 为两行居中。"""

    line_gap_px: int = 90
    """双行布局中两行主文字外框之间的间距，不包含 ruby 高度。"""

    upper_line_left_margin_px: int = 50
    """双行布局中上排字幕距离左边的边距。"""

    lower_line_right_margin_px: int = 50
    """双行布局中下排字幕距离右边的边距。"""

    line_lead_in_ms: int = 1800
    """理想表示开始 = 歌唱开始前的毫秒数；填充仍从真实字符时间开始。"""

    line_tail_ms: int = 1000
    """表示结束至少延续到同组两行歌唱结束后的毫秒数。"""

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

    entry_anim: EntryAnimation = "none"
    """入场动画：none / fade / slide_in / rise / char_fade / spin_flip / utopia。"""

    entry_lead_ms: int = 300
    """入场动画时长；不改变歌词填色时间，只影响显示窗口起点后的过渡。"""

    exit_anim: ExitAnimation = "none"
    """退场动画：none / fade / slide_out / rise / char_fade / spin_flip / utopia。"""

    exit_fade_ms: int = 300
    """退场动画时长；在显示窗口结束前开始。"""


@dataclass
class Background:
    """背景层占位。kind 取值：solid / image / video / loop_video。"""

    kind: Literal["solid", "image", "video", "loop_video"] = "solid"
    color: str = "#000000"
    path: Optional[str] = None
    video_offset_ms: int = 0


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
    background: Background = field(default_factory=Background)
    output: OutputConfig = field(default_factory=OutputConfig)
    audio_path: Optional[str] = None
    schema_version: int = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 持久化辅助（settings.json / .krstyle.json / .krrender.json 共用）
# ---------------------------------------------------------------------------


def style_to_dict(style: Style) -> dict:
    """Serialize ``Style`` into JSON-friendly primitives."""
    data: dict = {}
    for item in fields(Style):
        value = getattr(style, item.name)
        if item.name == "karaoke_colors":
            data[item.name] = karaoke_colors_to_dict(value) if value is not None else None
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
        if key == "karaoke_colors":
            changes[key] = karaoke_colors_from_dict(value)
        elif key == "singer_style_overrides":
            changes[key] = _singer_overrides_from_dict(value)
        elif key == "custom_style_schemes":
            changes[key] = _custom_schemes_from_dict(value)
        elif key in {
            "font_size_px",
            "font_weight",
            "stroke_width_px",
            "stroke2_width_px",
            "glow_radius_px",
            "shadow_offset_x",
            "shadow_offset_y",
            "ruby_font_size_px",
            "ruby_gap_px",
            "viewport_offset_x",
            "viewport_offset_y",
            "viewport_scale_pct",
            "viewport_rotation_deg",
            "line_y_margin_px",
            "line_gap_px",
            "upper_line_left_margin_px",
            "lower_line_right_margin_px",
            "line_lead_in_ms",
            "line_tail_ms",
            "timing_offset_ms",
            "line_lane_gap_ms",
            "line_continuity_snap_ms",
            "line_pair_second_delay_ms",
            "line_max_hold_ms",
            "entry_lead_ms",
            "exit_fade_ms",
        }:
            changes[key] = _int_value(value, getattr(defaults, key))
        elif key in {"italic", "dual_line_layout"}:
            changes[key] = bool(value)
        elif key == "line_y_position":
            changes[key] = value if value in {"top", "center", "bottom"} else defaults.line_y_position
        elif key == "line_horizontal_layout":
            changes[key] = value if value in {"asymmetric", "center"} else defaults.line_horizontal_layout
        elif key == "viewport_align":
            changes[key] = value if value in VIEWPORT_ALIGNS else defaults.viewport_align
        elif key == "decoration_kind":
            changes[key] = value if value in {"shadow", "glow"} else defaults.decoration_kind
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
        elif value is not None:
            changes[key] = str(value)
    return Style(**changes)


def subtitle_style_scheme_to_dict(scheme: SubtitleStyleScheme) -> dict:
    data: dict = {}
    for item in fields(SubtitleStyleScheme):
        value = getattr(scheme, item.name)
        if item.name == "karaoke_colors":
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
        if key == "karaoke_colors":
            changes[key] = karaoke_colors_from_dict(value)
        else:
            changes[key] = value
    return SubtitleStyleScheme(**changes)


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
    return PaintFill(
        mode=mode,  # type: ignore[arg-type]
        color=color,
        start_color=start_color,
        end_color=end_color,
        gradient_stops=_gradient_stops_from_payload(stops, start_color, end_color),
        split_top_color=str(payload.get("split_top_color", start_color)),
        split_bottom_color=str(payload.get("split_bottom_color", end_color)),
        split_position_pct=max(0, min(100, _int_value(payload.get("split_position_pct"), 50))),
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
) -> list[tuple[int, str]]:
    if not isinstance(payload, list):
        return [(0, start_color), (100, end_color)]
    result: list[tuple[int, str]] = []
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        result.append((max(0, min(100, _int_value(item[0], 0))), str(item[1])))
    if not result:
        return [(0, start_color), (100, end_color)]
    positions = {position for position, _color in result}
    if 0 not in positions:
        result.append((0, start_color))
    if 100 not in positions:
        result.append((100, end_color))
    return sorted(result)


def _int_value(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
