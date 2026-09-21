"""Typed project-load planning independent from the subtitle frontend."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.subtitle_render.domain.models import (
    Style,
    TRACK_TIMING_FIELDS,
    style_from_dict,
)
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    SubtitleLoadingSettings,
    TimingTrack,
    guide_symbol_has_visual,
    guide_symbol_replacement_anchored,
)
from krok_helper.subtitle_render.engine.layout.page.plan import (
    build_legacy_page_plan,
    normalize_page_plan,
    project_page_plan_to_legacy_fields,
)
from krok_helper.subtitle_render.engine.timing.timeline import apply_n3_seq_line_breaks
from krok_helper.subtitle_render.project.store import split_project_paths
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    line_animation_override_from_dict,
    subtitle_loading_settings_from_dict,
    track_page_plan_from_dict,
)
from krok_helper.subtitle_render.settings.screen import (
    ScreenSettings,
    screen_settings_from_dict,
)


@dataclass(frozen=True)
class DeferredProjectAsset:
    """One immutable project asset request to be applied after the UI settles."""

    kind: str
    payload: Any


@dataclass(frozen=True)
class AppliedTrackProjectState:
    """Observable results from restoring persisted state onto one timing track."""

    char_role_labels_changed: bool
    guide_symbol_mismatches: tuple[int, ...]


def apply_track_project_data(
    track: TimingTrack,
    style: Style,
    payload: object,
) -> AppliedTrackProjectState:
    """Restore all persisted per-track fields without touching frontend state."""

    data = payload if isinstance(payload, dict) else {}
    _apply_line_breaks(track, data.get("line_breaks_before"))
    _apply_layout_indices(track, style, data.get("line_layout_indices"))
    _restore_page_state(track, style, data)
    roles_changed = _apply_char_role_labels(track, data.get("char_role_labels"))
    resolve_guide_row = _guide_symbol_row_resolver(data)
    guide_mismatches = _apply_guide_symbols(
        track,
        data.get("line_guide_symbols"),
        resolve_guide_row,
    )
    _apply_inline_guide_symbols(
        track,
        data.get("line_inline_guide_symbols"),
        resolve_guide_row,
    )
    _apply_display_overrides(track, data.get("line_display_overrides"))
    _apply_animation_overrides(track, data.get("line_animation_overrides"))
    _apply_wipe_reverse_overrides(track, data.get("line_wipe_reverse_overrides"))
    _apply_display_timing(track, data.get("display_timing"))
    return AppliedTrackProjectState(
        char_role_labels_changed=roles_changed,
        guide_symbol_mismatches=tuple(guide_mismatches),
    )


_TIMING_OVERRIDE_INT_FIELDS = frozenset({
    "line_lead_in_ms",
    "line_tail_ms",
    "timing_offset_ms",
    "line_lane_gap_ms",
    "line_protect_ms",
    "entry_anim_protect_ms",
    "exit_anim_protect_ms",
})
_TIMING_OVERRIDE_BOOL_FIELDS = frozenset({
    "sync_entry",
    "sync_ending",
    "sync_each_page",
    "allow_entry_exit_animation_overlap",
    "allow_inter_page_line_overlap",
    "auto_fill_section_time",
})
_TIMING_OVERRIDE_ENUM_FIELDS = {
    "section_ending_mode": frozenset({"hold", "clear"}),
    "ruby_main_progress_mode": frozenset({"checkpoint_segments", "reading_units"}),
    "overlap_fallback_mode": frozenset({"lift", "displace"}),
}


def _parse_timing_override_value(field: str, value: object) -> object:
    """防御解析单个按轴时间覆盖值；不合法返回 ``None``（丢弃该项）。"""

    if field in _TIMING_OVERRIDE_INT_FIELDS:
        try:
            return max(int(value), 0) if field != "timing_offset_ms" else int(value)
        except (TypeError, ValueError):
            return None
    if field in _TIMING_OVERRIDE_BOOL_FIELDS:
        return value if isinstance(value, bool) else None
    allowed = _TIMING_OVERRIDE_ENUM_FIELDS.get(field)
    if allowed is not None:
        return value if value in allowed else None
    return None


def _apply_display_timing(track: TimingTrack, payload: object) -> None:
    """恢复按轴时间策略；旧工程缺失/为空键一律回落默认（跟随主轴）。"""

    timing = track.display_timing
    if not isinstance(payload, dict):
        timing.follow_main = True
        timing.overrides.clear()
        return
    follow = payload.get("follow_main", True)
    timing.follow_main = (
        bool(follow) if isinstance(follow, (bool, int)) else True
    )
    raw_overrides = payload.get("overrides")
    overrides: dict[str, object] = {}
    if isinstance(raw_overrides, dict):
        for field, value in raw_overrides.items():
            if field not in TRACK_TIMING_FIELDS:
                continue
            parsed = _parse_timing_override_value(str(field), value)
            if parsed is not None:
                overrides[str(field)] = parsed
    timing.overrides.clear()
    timing.overrides.update(overrides)


def _guide_symbol_row_resolver(data: dict) -> Callable[[object], object]:
    """解析 ``guide_symbol_table``：行数据里的字符串 ID 映射回符号字典。"""
    table = data.get("guide_symbol_table")
    if not isinstance(table, dict):
        return lambda value: value

    def resolve(value: object) -> object:
        if isinstance(value, str):
            return table.get(value)
        return value

    return resolve


def _apply_layout_indices(track: TimingTrack, style: Style, payload: object) -> None:
    if not isinstance(payload, list):
        return
    limit = len(style.layouts)
    for line, value in zip(track.lines, payload):
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        line.layout_index = index if 0 <= index <= limit else 0


def _apply_line_breaks(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, value in zip(track.lines, payload):
        kind = str(value)
        line.break_before = kind if kind in {"page", "paragraph"} else "none"


def _restore_page_state(track: TimingTrack, style: Style, data: dict) -> None:
    restored = track_page_plan_from_dict(data.get("page_plan"))
    if restored is None:
        saved_breaks = data.get("line_breaks_before")
        has_complete_legacy_breaks = (
            isinstance(saved_breaks, list) and len(saved_breaks) >= len(track.lines)
        )
        if not has_complete_legacy_breaks:
            # Schema-v1 files omitted explicit boundaries. The historical LRC
            # loader supplied N3's two-line boundaries, so replay that rule
            # before constructing the modern page plan.
            apply_n3_seq_line_breaks(track)
        track.page_plan = build_legacy_page_plan(
            track,
            style,
            section_gap_ms=max(int(style.section_gap_ms), 0),
        )
        track.loading_settings_mode = "custom"
        track.loading_settings = SubtitleLoadingSettings(
            time_gap_section_enabled=True,
            section_gap_ms=max(int(style.section_gap_ms), 0),
            blank_line_section_enabled=False,
            rows_per_page=2,
        )
        track.loading_settings_snapshot = track.loading_settings
    else:
        track.page_plan = normalize_page_plan(track, style, restored)
        mode = str(data.get("loading_settings_mode") or "global")
        track.loading_settings_mode = (
            mode if mode in {"global", "custom"} else "global"
        )
        track.loading_settings = (
            subtitle_loading_settings_from_dict(data.get("loading_settings"))
            if track.loading_settings_mode == "custom"
            else None
        )
        track.loading_settings_snapshot = subtitle_loading_settings_from_dict(
            data.get("loading_settings_snapshot")
        )
    project_page_plan_to_legacy_fields(track, style)


_EMOJI_SYMBOL_NAME_PREFIX = "N3 Emoji "


def _emoji_label_positions(line: TimingLine) -> set[int]:
    """源解析插入的 ``@Emoji`` 标签字符位（合成字符，加载期自动产生）。

    只认「触发词是完整 ``【…】`` 标签且该位字符文本恰为触发词」的头像——
    即标签插入产生的合成字符。普通替换词（``@Emoji=♪``）的头像挂在真实
    字符上，不移动坐标，不算标签位。
    """

    positions: set[int] = set()
    for index, symbol in line.inline_guide_symbols.items():
        if not isinstance(symbol, GuideSymbol):
            continue
        name = str(symbol.name)
        if not name.startswith(_EMOJI_SYMBOL_NAME_PREFIX):
            continue
        trigger = name[len(_EMOJI_SYMBOL_NAME_PREFIX):]
        if (
            len(trigger) > 2
            and trigger.startswith("【")
            and trigger.endswith("】")
            and 0 <= index < len(line.chars)
            and line.chars[index].text == trigger
        ):
            positions.add(index)
    return positions


def _zip_shift_drifted_labels(
    line: TimingLine, labels: list[object], emoji_positions: set[int]
) -> bool:
    """检测中间版本存盘固化的「zip 漂移」逐字角色签名。

    ``dcb1cc0``（@Emoji 标签字符插入）与本对齐修复之间保存过的工程：加载时
    旧的 n_real 条角色被 zip 到新的 n_chars 字符序列上（整体右移、尾部保留
    解析值），错位结果再次存盘固化。签名：前 n_real 条与「剔除标签位后的
    新鲜解析角色」逐位相等，其余条目与新鲜角色相等；超出当前字符数的尾巴
    （行尾空白被后续版本丢弃等）对齐末位新鲜角色。正常用户编辑几乎不可能
    恰好凑出这个形状；命中即视为漂移行，不回放（保留源解析角色）。
    """

    fresh = [char.role_label for char in line.chars]
    if not fresh:
        return False
    real = [
        fresh[index] for index in range(len(fresh)) if index not in emoji_positions
    ]
    for index, label in enumerate(labels):
        if index < len(real):
            if label != real[index]:
                return False
        elif index < len(fresh):
            if label != fresh[index]:
                return False
        elif label != fresh[-1]:
            return False
    return True


def _kept_singer_tag_positions(line: TimingLine) -> set[int]:
    """正文解析保留为可见文本的 ``【…】`` 演唱者标签字符位。

    LRC 源里任何 ``【…】`` 都是角色标签；「保留歌词中【xxx】演唱者名」
    生效后这些字符由加载期自动产生。存量工程按旧「标签剔除」口径保存
    逐字数据，回放时按位跳过它们对齐。
    """
    positions: set[int] = set()
    chars = line.chars
    index = 0
    while index < len(chars):
        if chars[index].text != "【":
            index += 1
            continue
        end = index + 1
        while end < len(chars) and chars[end].text not in ("【", "】"):
            end += 1
        if end < len(chars) and chars[end].text == "】" and end > index + 1:
            positions.update(range(index, end + 1))
            index = end + 1
        else:
            index += 1
    return positions


def _apply_char_role_labels(track: TimingTrack, payload: object) -> bool:
    if not isinstance(payload, list):
        return False
    changed = False
    for line, labels in zip(track.lines, payload):
        if not isinstance(labels, list):
            continue
        emoji_positions = _emoji_label_positions(line)
        auto_positions = emoji_positions | _kept_singer_tag_positions(line)
        if auto_positions and len(labels) == len(line.chars) - len(auto_positions):
            # @Emoji 标签插入 / 【…】标签文本保留特性之前的存量工程：逐字角色
            # 按「无加载期自动字符」的字符序列保存。按位跳过这些字符对齐，
            # 避免整体右移。
            targets = [
                index
                for index in range(len(line.chars))
                if index not in auto_positions
            ]
        elif (
            emoji_positions
            and len(labels) >= len(line.chars)
            and _zip_shift_drifted_labels(line, labels, emoji_positions)
        ):
            # 中间版本已固化的漂移行：跳过回放，保留源解析角色（下次存盘即修复）。
            continue
        else:
            targets = list(range(len(line.chars)))
        for index, label in zip(targets, labels):
            char = line.chars[index]
            new_label = str(label) if label else None
            if char.role_label != new_label:
                char.role_label = new_label
                changed = True
    return changed


def _apply_guide_symbols(
    track: TimingTrack,
    payload: object,
    resolve_row: Callable[[object], object] = lambda value: value,
) -> list[int]:
    if not isinstance(payload, list):
        return []
    mismatches: list[int] = []
    for row, (line, value) in enumerate(zip(track.lines, payload)):
        symbol = guide_symbol_from_dict(resolve_row(value))
        if (
            symbol is not None
            and symbol.replacement_prefix
            and not guide_symbol_replacement_anchored(line, symbol)
        ):
            # 源文件在保存后被改过（换行重排）时行号即错位；行首前缀 + 可见
            # 文字锚点都对得上才回放，否则丢弃并上报，避免静默替换错歌词。
            line.guide_symbol = None
            mismatches.append(row)
            continue
        line.guide_symbol = symbol
    return mismatches


def _apply_inline_guide_symbols(
    track: TimingTrack,
    payload: object,
    resolve_row: Callable[[object], object] = lambda value: value,
) -> None:
    if not isinstance(payload, list):
        return
    for line, value in zip(track.lines, payload):
        parsed: list[tuple[int, GuideSymbol]] = []
        if isinstance(value, dict):
            for raw_index, raw_symbol in value.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                symbol = guide_symbol_from_dict(resolve_row(raw_symbol))
                if guide_symbol_has_visual(symbol):
                    parsed.append((index, symbol))
        emoji_positions = _emoji_label_positions(line)
        payload_has_emoji = any(
            str(symbol.name).startswith(_EMOJI_SYMBOL_NAME_PREFIX)
            for _index, symbol in parsed
        )
        if emoji_positions and not payload_has_emoji:
            # @Emoji 标签插入特性之前的存量工程：行内符号按「无合成标签字符」
            # 的坐标保存（多数行为空）。直接整体替换会把源解析带入的透明头像
            # 一并抹掉，【角色名】标签字符随即以正文文字露出。跳过标签位重映射
            # 旧坐标，并保留源解析的标签头像（与热重载的「源拥有 vs 本地编辑」
            # 口径一致：dcb1cc0 起保存的工程快照里本就含这些符号，走整体替换）。
            remap = [
                index for index in range(len(line.chars)) if index not in emoji_positions
            ]
            symbols = {index: line.inline_guide_symbols[index] for index in emoji_positions}
            symbols.update(
                (remap[index], symbol)
                for index, symbol in parsed
                if 0 <= index < len(remap)
            )
            line.inline_guide_symbols = symbols
            continue
        line.inline_guide_symbols = {
            index: symbol
            for index, symbol in parsed
            if 0 <= index < len(line.chars)
        }


def _apply_display_overrides(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, row in zip(track.lines, payload):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        start, end = row
        line.display_start_override_ms = (
            int(start) if isinstance(start, (int, float)) else None
        )
        line.display_end_override_ms = (
            int(end) if isinstance(end, (int, float)) else None
        )


def _apply_wipe_reverse_overrides(track: TimingTrack, payload: object) -> None:
    """回放手动反向走字覆盖；覆盖值同时改写渲染消费的有效标记。"""
    if not isinstance(payload, list):
        return
    for line, value in zip(track.lines, payload):
        if isinstance(value, bool):
            line.wipe_reverse_override = value
            line.wipe_reverse = value


def _schema_version(value: object) -> int:
    """Best-effort schema stamp; missing/non-numeric counts as legacy ``0``."""
    try:
        return max(int(value), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _sug_axis_singer_ids(value: object) -> Optional[frozenset[str]]:
    """主字幕槽位的 SUG 轴过滤（快照字段）；非列表或缺省 = 未分轴。

    列表允许出现（空 = 空轴，全部行剔除），与「未分轴」用缺省/非列表区分。
    """
    if not isinstance(value, list):
        return None
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _sug_axis_name(value: object) -> Optional[str]:
    """主字幕槽位对应的 SUG 主分组名（快照字段，仅展示用）。"""
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None


def _apply_animation_overrides(track: TimingTrack, payload: object) -> None:
    if not isinstance(payload, list):
        return
    for line, row in zip(track.lines, payload):
        line.animation_override = line_animation_override_from_dict(row)


@dataclass(frozen=True)
class ProjectLoadPlan:
    """Parsed ``.yurika`` state with compatibility defaults resolved once."""

    source_data: dict
    style: Style
    screen: ScreenSettings
    selected_scheme_key: Optional[str]
    output: dict
    subtitle_path: Optional[Path]
    fallback_video_path: Optional[Path]
    audio_path: Optional[Path]
    background: Optional[dict] = None
    subtitle_sug_axis_singer_ids: Optional[frozenset[str]] = None
    subtitle_sug_axis_name: Optional[str] = None
    schema_version: int = 0
    line_breaks_before: Any = None
    line_layout_indices: Any = None
    char_role_labels: Any = None
    line_guide_symbols: Any = None
    line_inline_guide_symbols: Any = None
    line_display_overrides: Any = None
    line_animation_overrides: Any = None
    extra_subtitle_sources: Any = None
    project_role_names: Any = None

    @classmethod
    def from_data(cls, data: dict) -> "ProjectLoadPlan":
        """Parse one project payload without loading files or touching widgets."""
        source = data if isinstance(data, dict) else {}
        style_payload = source.get("style")
        style = style_from_dict(style_payload)
        screen = screen_settings_from_dict(source.get("screen"))
        # Older projects stored resolved pixel sizes without a reference height.
        # Bind those values to the saved canvas before any later screen resize.
        if (
            not isinstance(style_payload, dict)
            or "font_reference_height" not in style_payload
        ):
            style = replace(
                style,
                font_reference_height=max(int(screen.height), 1),
            )
        # 标题条目默认值由 ``style_from_dict`` 统一补齐（缺 key → 一条默认
        # 禁用条目），这里不再额外回填。

        key = source.get("selected_scheme_key")
        selected_scheme_key = key if isinstance(key, str) and key else None
        output = source.get("output")
        paths = split_project_paths(source)
        background = source.get("background")
        return cls(
            source_data=source,
            style=style,
            screen=screen,
            selected_scheme_key=selected_scheme_key,
            output=output if isinstance(output, dict) else {},
            subtitle_path=paths["subtitle_path"],
            fallback_video_path=paths["video_path"],
            audio_path=paths["audio_path"],
            background=background if isinstance(background, dict) else None,
            subtitle_sug_axis_singer_ids=_sug_axis_singer_ids(
                source.get("subtitle_sug_axis_singer_ids")
            ),
            subtitle_sug_axis_name=_sug_axis_name(
                source.get("subtitle_sug_axis_name")
            ),
            schema_version=_schema_version(source.get("schema_version")),
            line_breaks_before=source.get("line_breaks_before"),
            line_layout_indices=source.get("line_layout_indices"),
            char_role_labels=source.get("char_role_labels"),
            line_guide_symbols=source.get("line_guide_symbols"),
            line_inline_guide_symbols=source.get("line_inline_guide_symbols"),
            line_display_overrides=source.get("line_display_overrides"),
            line_animation_overrides=source.get("line_animation_overrides"),
            extra_subtitle_sources=source.get("extra_subtitle_sources"),
            project_role_names=source.get("project_role_names"),
        )

    def deferred_assets(self) -> tuple[DeferredProjectAsset, ...]:
        """Build the existing ordered background/audio/secondary-source queue."""
        loads: list[DeferredProjectAsset] = []
        if self.background is not None:
            loads.append(DeferredProjectAsset("background", deepcopy(self.background)))
        elif (
            self.fallback_video_path is not None
            and self.fallback_video_path.is_file()
        ):
            loads.append(DeferredProjectAsset("video", self.fallback_video_path))
        if self.audio_path is not None:
            loads.append(DeferredProjectAsset("audio", self.audio_path))
        if (
            isinstance(self.extra_subtitle_sources, list)
            and self.extra_subtitle_sources
        ):
            loads.append(
                DeferredProjectAsset(
                    "extra_subtitle_sources",
                    (
                        deepcopy(self.extra_subtitle_sources),
                        deepcopy(self.project_role_names),
                    ),
                )
            )
        return tuple(loads)
