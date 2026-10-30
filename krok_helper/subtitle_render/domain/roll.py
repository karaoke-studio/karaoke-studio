"""前黑/后黑（片头/片尾插入段）领域模型与展示时间线计算。

前黑在主视频之前插入一段片头，后黑在主视频之后追加一段片尾。两段各自
独立配置：背景素材（视频 / 图片序列 / 一图流（含 GIF、WebP 动图）/ 纯色）、
填充风格、独立替换音频、持续时长、与主视频的过渡和自我过渡。过渡只作用
于背景层，字幕渲染不受过渡影响。

展示时间线（display 坐标，0 = 成片开头）::

    [0, pre_ms)                              前黑段（背景=前黑素材）
    [pre_ms, main_end_ms)                    主段（背景=主背景 + 字幕）
    [main_end_ms, main_end_ms + post_ms)     后黑段（字幕轴可继续延伸）
    整体时长 total_ms = max(pre_ms + 字幕轴长, main_end_ms + post_ms)

字幕补偿口径（参考「读取 SUG 导出补偿」的展示层平移思路）：

- 前黑开启且「自动偏移非标题时间戳」开 → 歌词（主轴/副轴，含手调上/离屏
  覆盖）整体 +pre_ms 展示；关闭自动偏移或关闭前黑则自动恢复原时间。该
  偏移只存在于 yurika 内的展示层，不回写 SUG/LRC 源文件。
- 标题随前黑时长变化整体 ±Δ（数据级平移，tail 模式锚定总时长自动跟随）；
  前黑缩短/关闭时落入负值的窗口钳回 0（调用方负责提示）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from krok_helper.subtitle_render.domain.background import BackgroundSource


# --------------------------------------------------------------------------- 填充风格

ROLL_FILL_STRETCH = "stretch"
"""拉伸：无视宽高比铺满画布。"""

ROLL_FILL_TILE = "tile"
"""平铺：按素材原始尺寸自画布左上角平铺，超出部分裁掉。"""

ROLL_FILL_ORIGINAL = "original"
"""不改变：素材原始尺寸居中，超出画布裁掉、不足处补黑。"""

ROLL_FILL_FIT = "fit"
"""自适应大小：等比完整放入画布 + 纯黑边（contain，与主视频背景口径一致）。"""

ROLL_FILL_MODES = (ROLL_FILL_STRETCH, ROLL_FILL_TILE, ROLL_FILL_ORIGINAL, ROLL_FILL_FIT)

ROLL_TILE_MAX_CELLS = 4096
"""平铺滤镜的最大格子数；超过时两侧统一回退「自适应大小」，避免 split 爆炸。"""

ROLL_FILL_LABELS = {
    ROLL_FILL_STRETCH: "拉伸",
    ROLL_FILL_TILE: "平铺",
    ROLL_FILL_ORIGINAL: "不改变",
    ROLL_FILL_FIT: "自适应大小",
}


# --------------------------------------------------------------------------- 过渡风格

ROLL_MAIN_TRANSITION_BLACK = "black"
ROLL_MAIN_TRANSITION_WHITE = "white"
ROLL_MAIN_TRANSITION_CROSSFADE = "crossfade"

ROLL_MAIN_TRANSITION_STYLES = (
    ROLL_MAIN_TRANSITION_BLACK,
    ROLL_MAIN_TRANSITION_WHITE,
    ROLL_MAIN_TRANSITION_CROSSFADE,
)

ROLL_MAIN_TRANSITION_LABELS = {
    ROLL_MAIN_TRANSITION_BLACK: "黑场过渡",
    ROLL_MAIN_TRANSITION_WHITE: "白场过渡",
    ROLL_MAIN_TRANSITION_CROSSFADE: "交叉溶解",
}

ROLL_SELF_TRANSITION_BLACK = "black"
ROLL_SELF_TRANSITION_WHITE = "white"

ROLL_SELF_TRANSITION_STYLES = (ROLL_SELF_TRANSITION_BLACK, ROLL_SELF_TRANSITION_WHITE)

ROLL_SELF_TRANSITION_LABELS = {
    ROLL_SELF_TRANSITION_BLACK: "黑场过渡",
    ROLL_SELF_TRANSITION_WHITE: "白场过渡",
}


def _clamp_non_negative(value: object, fallback: int = 0) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(parsed, 0)


def _normalize_enum(value: object, allowed: tuple[str, ...], default: str) -> str:
    text = str(value) if value is not None else ""
    return text if text in allowed else default


# --------------------------------------------------------------------------- 段配置

@dataclass(frozen=True)
class RollSegmentConfig:
    """一段前黑/后黑的用户配置（两段字段一致、互不联动）。"""

    enabled: bool = False
    source: BackgroundSource = field(default_factory=BackgroundSource)
    source_has_audio: bool = False
    """素材自带音轨（浏览时探针结果，持久化缓存；纯图/纯色恒 False）。"""
    source_width: int = 0
    source_height: int = 0
    """素材像素尺寸（探针/解码结果缓存；平铺填充的行列数依赖它）。"""
    source_duration_ms: int = 0
    """素材自身时长（仅供属性页提示；0 = 无固有时长/未知）。

    注意：它**不参与**该段实际持续时长 ``duration_ms`` 的自动填充——用户在
    「持续时长」里填多少就是多少，避免加载图片/视频时悄悄改掉已填值。
    """
    audio_path: Optional[str] = None
    """独立音频；填写后取代背景素材自带的声音（可为空）。"""
    duration_ms: int = 3000
    """持续时长（默认 3 秒）；0 = 该段关闭（与总开关关闭等效）。"""
    fill: str = ROLL_FILL_FIT
    auto_offset_subtitles: bool = True
    """自动偏移非标题时间戳（仅前黑消费；后黑忽略该位）。"""
    main_transition_ms: int = 300
    """与主视频的过渡动画时长（默认 300ms；0 = 不过渡）。"""
    main_transition_style: str = ROLL_MAIN_TRANSITION_CROSSFADE
    self_transition_ms: int = 300
    """段自身的淡入/淡出动画时长（默认 300ms；0 = 不过渡）。"""
    self_transition_style: str = ROLL_SELF_TRANSITION_BLACK

    def normalized(self) -> "RollSegmentConfig":
        source = self.source
        kind = source.kind if source.kind in {"video", "image", "image_sequence", "solid"} else "solid"
        if kind != source.kind:
            source = replace(source, kind=kind)
        return RollSegmentConfig(
            enabled=bool(self.enabled),
            source=source,
            source_has_audio=bool(self.source_has_audio),
            source_width=max(int(self.source_width or 0), 0),
            source_height=max(int(self.source_height or 0), 0),
            source_duration_ms=max(int(self.source_duration_ms or 0), 0),
            audio_path=str(self.audio_path).strip() if self.audio_path else None,
            duration_ms=_clamp_non_negative(self.duration_ms),
            fill=_normalize_enum(self.fill, ROLL_FILL_MODES, ROLL_FILL_FIT),
            auto_offset_subtitles=bool(self.auto_offset_subtitles),
            main_transition_ms=_clamp_non_negative(self.main_transition_ms),
            main_transition_style=_normalize_enum(
                self.main_transition_style, ROLL_MAIN_TRANSITION_STYLES,
                ROLL_MAIN_TRANSITION_BLACK,
            ),
            self_transition_ms=_clamp_non_negative(self.self_transition_ms),
            self_transition_style=_normalize_enum(
                self.self_transition_style, ROLL_SELF_TRANSITION_STYLES,
                ROLL_SELF_TRANSITION_BLACK,
            ),
        )

    def source_ready(self) -> bool:
        source = self.source
        if source.kind == "solid":
            return True
        return bool(source.path)


def roll_segment_active(config: RollSegmentConfig) -> bool:
    """总开关开启、时长 > 0 且素材就绪，三者同时成立该段才生效。"""

    normalized = config.normalized()
    return normalized.enabled and normalized.duration_ms > 0 and normalized.source_ready()


def effective_roll_ms(config: RollSegmentConfig) -> int:
    return config.normalized().duration_ms if roll_segment_active(config) else 0


def roll_source_path(config: RollSegmentConfig) -> Optional[Path]:
    path = config.normalized().source.path
    return Path(path) if path else None


ROLL_DEFAULT_FIELDS = (
    "duration_ms",
    "fill",
    "auto_offset_subtitles",
    "main_transition_ms",
    "main_transition_style",
    "self_transition_ms",
    "self_transition_style",
)


def roll_defaults_to_dict(config: RollSegmentConfig) -> dict:
    """把一段配置投影为 app 级「新建段默认值」（不含素材/音频/开关）。"""

    normalized = config.normalized()
    return {key: getattr(normalized, key) for key in ROLL_DEFAULT_FIELDS}


def roll_defaults_from_dict(payload: object) -> dict:
    """读取 app 级默认值；缺键/非法值回落到出厂默认并归一化。"""

    factory = RollSegmentConfig()
    data = payload if isinstance(payload, dict) else {}
    merged = {**roll_defaults_to_dict(factory), **data}
    try:
        config = roll_segment_from_dict(merged)
    except (TypeError, ValueError):
        config = factory
    return roll_defaults_to_dict(config)


def roll_config_with_defaults(defaults: dict) -> RollSegmentConfig:
    """新建段配置：应用 app 级默认值，但保持未启用、纯色黑底。"""

    normalized = roll_defaults_from_dict(defaults)
    return replace(
        RollSegmentConfig(source=BackgroundSource(kind="solid")),
        **normalized,
    )


def roll_audio_source_path(config: RollSegmentConfig) -> Optional[Path]:
    """该段实际使用的音频路径：独立音频优先，否则视频素材内嵌音轨。"""

    normalized = config.normalized()
    if normalized.audio_path:
        return Path(normalized.audio_path)
    if normalized.source.kind == "video" and normalized.source.path:
        return Path(normalized.source.path)
    return None


def roll_segment_has_audio(config: RollSegmentConfig) -> bool:
    """段生效且能提供音频（独立音频或带音轨的视频素材）。"""

    if not roll_segment_active(config):
        return False
    normalized = config.normalized()
    if normalized.audio_path:
        return True
    return bool(
        normalized.source.kind == "video"
        and normalized.source_has_audio
        and normalized.source.path
    )


def format_roll_source_duration(duration_ms: int) -> str:
    """属性页素材时长提示：``【素材时长： hh:mm:ss.ms（xxx.xxx秒）】``。"""

    total_ms = max(int(duration_ms or 0), 0)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return (
        f"【素材时长： {hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        f"（{total_ms / 1000.0:.3f}秒）】"
    )


# --------------------------------------------------------------------------- 序列化

def roll_segment_to_dict(config: RollSegmentConfig) -> dict:
    normalized = config.normalized()
    source = normalized.source
    payload: dict = {
        "enabled": normalized.enabled,
        "source_has_audio": normalized.source_has_audio,
        "source_width": normalized.source_width,
        "source_height": normalized.source_height,
        "source_duration_ms": normalized.source_duration_ms,
        "duration_ms": normalized.duration_ms,
        "fill": normalized.fill,
        "auto_offset_subtitles": normalized.auto_offset_subtitles,
        "main_transition_ms": normalized.main_transition_ms,
        "main_transition_style": normalized.main_transition_style,
        "self_transition_ms": normalized.self_transition_ms,
        "self_transition_style": normalized.self_transition_style,
        "source": {
            "kind": source.kind,
            "path": source.path,
            "color": source.color,
            "source_fps": source.source_fps,
            "sequence_start_number": source.sequence_start_number,
        },
    }
    if normalized.audio_path:
        payload["audio_path"] = normalized.audio_path
    return payload


def roll_segment_from_dict(payload: object) -> RollSegmentConfig:
    if not isinstance(payload, dict):
        return RollSegmentConfig()
    source_payload = payload.get("source")
    source = BackgroundSource()
    if isinstance(source_payload, dict):
        source = BackgroundSource(
            kind=str(source_payload.get("kind") or "solid"),
            path=(
                str(source_payload["path"]) if source_payload.get("path") else None
            ),
            color=str(source_payload.get("color") or "#000000"),
            source_fps=(
                int(source_payload["source_fps"])
                if source_payload.get("source_fps")
                else None
            ),
            sequence_start_number=max(
                int(source_payload.get("sequence_start_number") or 0), 0
            ),
        )
    return RollSegmentConfig(
        enabled=bool(payload.get("enabled")),
        source=source,
        source_has_audio=bool(payload.get("source_has_audio")),
        source_width=_clamp_non_negative(payload.get("source_width")),
        source_height=_clamp_non_negative(payload.get("source_height")),
        source_duration_ms=_clamp_non_negative(payload.get("source_duration_ms")),
        audio_path=str(payload["audio_path"]) if payload.get("audio_path") else None,
        duration_ms=_clamp_non_negative(payload.get("duration_ms")),
        fill=_normalize_enum(payload.get("fill"), ROLL_FILL_MODES, ROLL_FILL_FIT),
        auto_offset_subtitles=bool(payload.get("auto_offset_subtitles", True)),
        main_transition_ms=_clamp_non_negative(payload.get("main_transition_ms")),
        main_transition_style=_normalize_enum(
            payload.get("main_transition_style"),
            ROLL_MAIN_TRANSITION_STYLES,
            ROLL_MAIN_TRANSITION_BLACK,
        ),
        self_transition_ms=_clamp_non_negative(payload.get("self_transition_ms")),
        self_transition_style=_normalize_enum(
            payload.get("self_transition_style"),
            ROLL_SELF_TRANSITION_STYLES,
            ROLL_SELF_TRANSITION_BLACK,
        ),
    )


# --------------------------------------------------------------------------- 时间线

@dataclass(frozen=True)
class RollTimelinePlan:
    """由当前项目状态推导出的展示时间线（display 坐标）。

    ``main_end_ms`` 同时也是后黑段的起点（后黑始终接在主背景素材之后，
    即使字幕轴更长——此时字幕会在后黑背景上继续渲染到字幕轴结束）。
    """

    pre: RollSegmentConfig
    post: RollSegmentConfig
    pre_ms: int = 0
    post_ms: int = 0
    auto_offset: bool = True
    main_start_ms: int = 0
    main_end_ms: int = 0
    """主段结束（= 后黑起点）的 display 时刻。"""
    content_end_ms: int = 0
    """内容结束（不含后黑）的 display 时刻，标题 tail 锚定用。"""
    total_ms: int = 0
    track_end_ms: int = 0
    lyric_shift_ms: int = 0
    """歌词（主轴/副轴）展示层整体后移量；标题不受该值影响。"""

    @property
    def has_pre(self) -> bool:
        return self.pre_ms > 0

    @property
    def has_post(self) -> bool:
        return self.post_ms > 0

    @property
    def active(self) -> bool:
        return self.pre_ms > 0 or self.post_ms > 0

    def main_time_ms(self, display_ms: int) -> int:
        """display 时刻 → 主段素材时刻（供主背景视频 seek）。"""

        return display_ms - self.pre_ms

    def post_time_ms(self, display_ms: int) -> int:
        """display 时刻 → 后黑段素材时刻。"""

        return display_ms - self.main_end_ms


def build_roll_timeline(
    pre: Optional[RollSegmentConfig],
    post: Optional[RollSegmentConfig],
    *,
    track_end_ms: int,
    main_media_end_ms: Optional[int] = None,
    main_audio_end_ms: Optional[int] = None,
    background_kind: str = "solid",
) -> RollTimelinePlan:
    """按需求口径推导整体时间线。

    - 主段长度：视频背景 = 视频时长（后黑接在原背景视频后）；非视频背景
      = max(字幕轴, 独立音频) 的内容长度（图片/纯色素材本身没有终点）。
    - 整体时长 = max(字幕轴候选, 前黑 + 主段 + 后黑)。字幕轴候选随
      「自动偏移」开关变化：开启 → 前黑 + 字幕轴（歌词整体后移）；关闭
      → 字幕轴（歌词保持原时间，不再无条件加前黑）。
    - 标题 tail 锚定的内容结束：标题始终随主视频锚点平移（与前黑联动，
      与歌词偏移开关无关），auto 开 = 前黑 + max(字幕轴, 主素材)；
      auto 关 = max(字幕轴, 前黑 + 主素材)。
    - 两段全关时退化为既有口径（content_end = max(字幕轴, 媒体)，与
      PreviewDurationController 的历史公式一致）。
    """

    pre_cfg = (pre or RollSegmentConfig()).normalized()
    post_cfg = (post or RollSegmentConfig()).normalized()
    pre_ms = effective_roll_ms(pre_cfg)
    post_ms = effective_roll_ms(post_cfg)
    auto_offset = pre_ms > 0 and pre_cfg.auto_offset_subtitles
    track_end = max(int(track_end_ms or 0), 0)
    content_end = track_end
    for media_end in (main_media_end_ms, main_audio_end_ms):
        if media_end is not None and int(media_end) > content_end:
            content_end = int(media_end)
    if background_kind == "video" and main_media_end_ms is not None and int(main_media_end_ms) > 0:
        main_ms = int(main_media_end_ms)
    else:
        main_ms = content_end
    main_start = pre_ms
    main_end = main_start + main_ms
    subtitle_axis_end = pre_ms + track_end if auto_offset else track_end
    content_end_display = (
        pre_ms + content_end if auto_offset else max(track_end, main_end)
    )
    total = max(subtitle_axis_end, main_end + post_ms, content_end_display)
    return RollTimelinePlan(
        pre=pre_cfg,
        post=post_cfg,
        pre_ms=pre_ms,
        post_ms=post_ms,
        auto_offset=pre_cfg.auto_offset_subtitles,
        main_start_ms=main_start,
        main_end_ms=main_end,
        content_end_ms=content_end_display,
        total_ms=total,
        track_end_ms=track_end,
        lyric_shift_ms=pre_ms if auto_offset else 0,
    )


# --------------------------------------------------------------------------- 过渡

@dataclass(frozen=True)
class RollLayerAlphas:
    """某一 display 时刻三层背景 + 白闪层的透明度（预览与导出共用口径）。

    图层自下而上：前黑 → 主段 → 后黑；``flash_alpha`` 是盖在三层之上的
    白色闪场层（黑场/白场过渡与白场自我过渡出现）。黑场/白场过渡按
    Adobe Premiere 的 Dip to Black / Dip to White 口径：出场段在剪切点前
    半个窗口淡到过渡色，入场段在剪切点后半个窗口从过渡色淡入；交叉溶解
    仍是重叠窗口的 source-over。
    """

    pre_alpha: float = 0.0
    main_alpha: float = 0.0
    post_alpha: float = 0.0
    flash_alpha: float = 0.0
    pre_self_white: bool = False
    post_self_white: bool = False


def _ramp(value: float) -> float:
    return max(0.0, min(1.0, value))


def roll_dip_half_ms(
    style: str,
    transition_ms: int,
    outgoing_ms: int,
    incoming_ms: int,
    *,
    main_ms: int | None = None,
) -> float:
    """黑场/白场过渡的半窗时长（Adobe Premiere 式「跨剪切点居中」口径）。

    Premiere 的 Dip to Black / Dip to White 把过渡分成两半：剪切点前半个窗
    口由出场段淡出到黑/白，剪切点后半个窗口由入场段从黑/白淡入；入场段
    从它自己的第一帧开始继续播放，不在过渡期间冻结。总时长因此不变。

    过渡总长先钳到两侧段长（避免淡出/淡入超出素材），黑场/白场再对半分；
    ``main_ms`` 给定时两段过渡的半窗合计不超过主段长度，防止前黑淡入与
    后黑淡出在主段里重叠。交叉溶解不走这个口径，返回 0。
    """

    if style == ROLL_MAIN_TRANSITION_CROSSFADE:
        return 0.0
    total = min(
        max(int(transition_ms), 0),
        max(int(outgoing_ms), 0),
        max(int(incoming_ms), 0),
    )
    if main_ms is not None:
        total = min(total, max(int(main_ms), 0))
    return total / 2.0


def roll_layer_alphas(plan: RollTimelinePlan, display_ms: int) -> RollLayerAlphas:
    """Compose segment fades and symmetric transitions before resolving layer opacity.

    Black/white main transitions use Premiere's centered dip: the outgoing
    segment fades to the transition color over the half-window before the cut,
    the incoming segment starts at the cut and fades in from that color over
    the half-window after it. Both segments keep playing; nothing is frozen.
    Crossfade keeps the historical overlapping window.

    The resulting premultiplied weights match the export filter order. Layers
    are pre, main, post, white.
    """
    if not plan.active:
        return RollLayerAlphas(main_alpha=1.0)
    t = int(display_ms)
    weights = [0.0, 0.0, 0.0]
    white = 0.0
    main_ms = max(plan.main_end_ms - plan.pre_ms, 0)

    def add_segment(index: int, weight: float, local_ms: int) -> None:
        nonlocal white
        fade = 1.0
        config = plan.pre if index == 0 else plan.post
        duration = plan.pre_ms if index == 0 else plan.post_ms
        if index != 1:
            self_ms = min(config.self_transition_ms, duration)
            if self_ms > 0:
                fade = _ramp(local_ms / self_ms) if index == 0 else _ramp((duration-local_ms) / self_ms)
            if config.self_transition_style == ROLL_SELF_TRANSITION_WHITE:
                white += weight * (1.0-fade)
        weights[index] += weight * fade

    def transition(outgoing: int, incoming: int, x: float, style: str, local_ms: int) -> None:
        nonlocal white
        if style == ROLL_MAIN_TRANSITION_CROSSFADE:
            left, right = 1.0-x, x
        else:
            left, right = max(1.0-2*x, 0.0), max(2*x-1.0, 0.0)
            if style == ROLL_MAIN_TRANSITION_WHITE:
                white += 1.0-left-right
        add_segment(outgoing, left, local_ms)
        add_segment(incoming, right, 0)

    pre_half = (
        roll_dip_half_ms(
            plan.pre.main_transition_style,
            plan.pre.main_transition_ms,
            plan.pre_ms,
            main_ms,
            main_ms=main_ms,
        )
        if plan.has_pre
        else 0.0
    )
    post_half = (
        roll_dip_half_ms(
            plan.post.main_transition_style,
            plan.post.main_transition_ms,
            main_ms,
            plan.post_ms,
            main_ms=main_ms,
        )
        if plan.has_post
        else 0.0
    )
    # 两段黑/白过渡的半窗合计不得超过主段，避免主段里同时淡入又淡出。
    if pre_half + post_half > main_ms and pre_half + post_half > 0:
        scale = main_ms / (pre_half + post_half) if main_ms > 0 else 0.0
        pre_half *= scale
        post_half *= scale
    pre_v = min(plan.pre.main_transition_ms, plan.pre_ms, main_ms) if plan.has_pre else 0
    post_v = min(plan.post.main_transition_ms, plan.main_end_ms - plan.pre_ms, plan.post_ms) if plan.has_post else 0
    if (
        plan.has_post
        and plan.post.main_transition_style == ROLL_MAIN_TRANSITION_CROSSFADE
        and pre_half > 0
    ):
        # 前黑 dip 的淡入会占掉主段开头 pre_half；后黑 crossfade 的窗口若
        # 与它重叠，预览会在主段里同时处理两种过渡而导出只做 xfade。
        # 把 crossfade 总长钳到剩余主段，保证两窗口不重叠。
        post_v = min(post_v, max(main_ms - pre_half, 0.0))

    pre_dip = plan.has_pre and pre_half > 0 and plan.pre.main_transition_style != ROLL_MAIN_TRANSITION_CROSSFADE
    post_dip = plan.has_post and post_half > 0 and plan.post.main_transition_style != ROLL_MAIN_TRANSITION_CROSSFADE

    def dip_out(style: str, alpha: float, index: int, local_ms: int) -> None:
        """出场段淡到过渡色：黑场直接降 alpha；白场保持内容、白闪上升。"""
        nonlocal white
        if style == ROLL_MAIN_TRANSITION_WHITE:
            add_segment(index, 1.0, local_ms)
            # 多个白闪重叠时按 source-over 组合：1-(1-w1)(1-w2)。
            white = _ramp(1.0 - (1.0 - white) * _ramp(alpha))
        else:
            add_segment(index, alpha, local_ms)

    def dip_in(style: str, alpha: float, index: int, local_ms: int) -> None:
        """入场段从过渡色淡入：黑场降 alpha；白场保持内容、白闪下降。"""
        nonlocal white
        if style == ROLL_MAIN_TRANSITION_WHITE:
            add_segment(index, 1.0, local_ms)
            white = _ramp(1.0 - (1.0 - white) * _ramp(alpha))
        else:
            add_segment(index, alpha, local_ms)

    if plan.has_pre and t < plan.pre_ms:
        start = plan.pre_ms - pre_half
        if pre_dip and t >= start:
            alpha = max(0.0, min(1.0, (plan.pre_ms - t) / pre_half))
            dip_out(plan.pre.main_transition_style, alpha, 0, t)
        elif (
            plan.pre.main_transition_style == ROLL_MAIN_TRANSITION_CROSSFADE
            and pre_v > 0
            and t >= plan.pre_ms - pre_v
        ):
            transition(0, 1, (t-plan.pre_ms+pre_v)/pre_v, plan.pre.main_transition_style, t)
        else:
            add_segment(0, 1.0, t)
    elif plan.pre_ms <= t < plan.main_end_ms:
        if pre_dip and t < plan.pre_ms + pre_half:
            alpha = max(0.0, min(1.0, (t - plan.pre_ms) / pre_half))
            dip_in(plan.pre.main_transition_style, alpha, 1, t - plan.pre_ms)
        elif post_dip and t >= plan.main_end_ms - post_half:
            alpha = max(0.0, min(1.0, (plan.main_end_ms - t) / post_half))
            dip_out(plan.post.main_transition_style, alpha, 1, t - plan.pre_ms)
        elif (
            plan.post.main_transition_style == ROLL_MAIN_TRANSITION_CROSSFADE
            and plan.has_post
            and post_v > 0
            and t >= plan.main_end_ms - post_v
        ):
            transition(
                1, 2, (t-plan.main_end_ms+post_v)/post_v,
                plan.post.main_transition_style, t-plan.pre_ms,
            )
        else:
            add_segment(1, 1.0, t-plan.pre_ms)
    elif plan.has_post and plan.main_end_ms <= t < plan.main_end_ms + plan.post_ms:
        if post_dip and t < plan.main_end_ms + post_half:
            alpha = max(0.0, min(1.0, (t - plan.main_end_ms) / post_half))
            dip_in(plan.post.main_transition_style, alpha, 2, t - plan.main_end_ms)
        else:
            add_segment(2, 1.0, t-plan.main_end_ms)

    # Convert premultiplied color weights to source-over opacities, top down.
    remaining = max(1.0-white, 0.0)
    alphas = [0.0, 0.0, 0.0]
    for index in (2, 1, 0):
        alphas[index] = _ramp(weights[index]/remaining) if remaining > 1e-9 else 0.0
        remaining = max(remaining-weights[index], 0.0)
    return RollLayerAlphas(pre_alpha=alphas[0], main_alpha=alphas[1],
                           post_alpha=alphas[2], flash_alpha=_ramp(white))


# --------------------------------------------------------------------------- 标题平移

def _shift_clamped(value: int, delta_ms: int) -> tuple[int, bool]:
    shifted = int(value) + int(delta_ms)
    if shifted >= 0:
        return shifted, False
    return 0, True


def shift_style_title_overlays(style, delta_ms: int) -> tuple[Any, int]:
    """前黑有效时长变化 ±Δ 时整体平移标题窗口，返回 (新 style, 钳制数)。

    标题（含新增在前黑区间 [0, pre_ms) 内的"负值"标题）随主视频锚点整体
    ±Δ；缩短/关闭前黑时落入负值的 head 起点与自定义窗口被钳回 0，完全
    落入负区间的窗口移到零点并保留持续时长。tail 模式锚定内容结尾，总时长
    随 Δ 整体平移，无需改 ``tail_offset_ms``。
    """

    delta = int(delta_ms)
    overlays = list(getattr(style, "title_overlays", ()) or ())
    if delta == 0 or not overlays:
        return style, 0
    from krok_helper.subtitle_render.domain.models import TitleTimeWindow

    clamped_count = 0
    new_overlays = []
    for overlay in overlays:
        clamped = False
        head_offset, head_clamped = _shift_clamped(
            getattr(overlay, "head_offset_ms", 0), delta
        )
        clamped = clamped or head_clamped
        windows = []
        for window in getattr(overlay, "custom_windows", ()) or ():
            original_span = max(int(window.end_ms) - int(window.begin_ms), 0)
            begin, begin_clamped = _shift_clamped(window.begin_ms, delta)
            end, end_clamped = _shift_clamped(window.end_ms, delta)
            clamped = clamped or begin_clamped or end_clamped
            if end <= begin:
                # 完全落入负区间的窗口：锚回 0 但保留原持续时长——标题内容
                # 不凭空消失，用户可在正片里重新安排它的位置。
                begin, end = 0, original_span
            windows.append(
                TitleTimeWindow(
                    begin_ms=begin,
                    end_ms=end,
                    fade_in_ms=window.fade_in_ms,
                    fade_out_ms=window.fade_out_ms,
                )
            )
        new_overlays.append(
            replace(overlay, head_offset_ms=head_offset, custom_windows=windows)
        )
        if clamped:
            clamped_count += 1
    return replace(style, title_overlays=new_overlays), clamped_count


# --------------------------------------------------------------------------- 填充几何

def roll_fill_dest_rect(
    fill: str,
    source_w: int,
    source_h: int,
    canvas_w: int,
    canvas_h: int,
) -> tuple[int, int, int, int]:
    """纯图片类素材按填充风格落到画布的目标矩形（x, y, w, h）。

    与导出 ffmpeg 链一一对应：stretch=scale、fit=scale decrease+pad、
    original=crop+pad、tile 见 :func:`roll_tile_geometry`。
    """

    sw = max(int(source_w), 1)
    sh = max(int(source_h), 1)
    cw = max(int(canvas_w), 1)
    ch = max(int(canvas_h), 1)
    if fill == ROLL_FILL_STRETCH:
        return 0, 0, cw, ch
    if fill == ROLL_FILL_ORIGINAL:
        return (cw - sw) // 2, (ch - sh) // 2, sw, sh
    # fit（contain）与 tile 的首个格子都按等比完整放入处理。
    scale = min(cw / sw, ch / sh)
    dw = max(int(round(sw * scale)), 1)
    dh = max(int(round(sh * scale)), 1)
    return (cw - dw) // 2, (ch - dh) // 2, dw, dh


def roll_tile_geometry(
    source_w: int,
    source_h: int,
    canvas_w: int,
    canvas_h: int,
) -> tuple[int, int]:
    """平铺列数/行数（自左上角铺起，超出画布由画布裁掉）。"""

    sw = max(int(source_w), 1)
    sh = max(int(source_h), 1)
    cols = -(-max(int(canvas_w), 1) // sw)
    rows = -(-max(int(canvas_h), 1) // sh)
    return cols, rows
