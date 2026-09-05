"""Pure model semantics for resolving and scheduling title overlays."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
    style_for_role,
)
from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.domain.models import (
    TITLE_SCHEME_NAME,
    Style,
    TitleOverlay,
)


def title_layout_source(style: Style, index: Optional[int]):
    """Resolve a title layout reference; dangling references return ``None``."""
    if index is None:
        return None
    index = int(index)
    if index == 0:
        return style
    if 1 <= index <= len(style.layouts):
        return style.layouts[index - 1]
    return None


def resolve_title_overlay(
    style: Style, overlay: Optional[TitleOverlay] = None
) -> Optional[TitleOverlay]:
    """Resolve the title scheme and layout reference into an effective overlay.

    ``overlay`` 缺省时取第一条（兼容单标题时期的调用方）；条目 ``scheme_name``
    引用的方案不存在时回落内置「标题」方案。
    """
    if overlay is None:
        overlays = style.title_overlays
        if not overlays:
            return None
        overlay = overlays[0]
    title = overlay
    scheme_name = title.scheme_name
    if not scheme_name or scheme_name not in style.custom_style_schemes:
        scheme_name = TITLE_SCHEME_NAME
    changes: dict[str, object] = {}
    if scheme_name in style.custom_style_schemes:
        merged = style_for_role(style, scheme_name)
        colors = effective_karaoke_colors(merged).before
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
    source = title_layout_source(style, title.layout_index)
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


def resolve_title_role_overlay(
    style: Style, base: TitleOverlay, role_label: Optional[str]
) -> TitleOverlay:
    """Resolve a title character role into its static title appearance."""
    if not role_label or role_label not in style.custom_style_schemes:
        return base
    merged = style_for_role(style, role_label)
    colors = effective_karaoke_colors(merged).before
    layout_source = title_layout_source(style, base.layout_index)
    return replace(
        base,
        font_family=merged.font_family,
        font_family_latin=merged.font_family_latin,
        font_size_px=int(merged.font_size_px),
        font_weight=int(merged.font_weight),
        italic=bool(merged.italic),
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

_CUSTOM_TAG_RE = re.compile(r"^@([^\s=]+)\s*=\s*(.*)$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}\s]+)\}")


def title_available_tags(track: TimingTrack) -> list[tuple[str, str]]:
    """列出当前字幕源可用于 ``{占位符}`` 的标签，``(名称, 值)`` 有序去重。

    覆盖具名字段（Title / Artist / Album / TaggingBy）与尾部自定义
    ``@Key=Value`` 行（功能性 / 信息性 / 用户自定义一视同仁）；排除
    ``@Emoji*`` 图片标签与 ``@RubyN`` 注音（非文字信息，无可读值）。
    """
    tags: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, value: object) -> None:
        text = str(value or "").strip()
        key = name.lower()
        if not name or not text or key in seen:
            return
        seen.add(key)
        tags.append((name, text))

    meta = track.meta
    add("title", meta.title)
    add("artist", meta.artist)
    add("album", meta.album)
    add("tagging_by", meta.tagging_by)
    for raw in meta.custom:
        match = _CUSTOM_TAG_RE.match(str(raw).strip())
        if match is None:
            continue
        name = match.group(1)
        lowered = name.lower()
        if lowered.startswith("emoji") or lowered.startswith("ruby"):
            continue
        add(name, match.group(2))
    return tags


def resolve_title_text(title: TitleOverlay, track: TimingTrack) -> str:
    """Replace ``{tag}`` placeholders from source tags and clean orphan separators.

    任意标签占位符（``{title}`` / ``{artist}`` / 自定义 ``@Key`` → ``{Key}``）
    都按 :func:`title_available_tags` 的大小写不敏感查表替换；未知占位符
    原样保留。
    """
    template = title.text_template or ""
    if "{" not in template:
        return template.strip("\n")
    lookup = {name.lower(): value for name, value in title_available_tags(track)}
    # 具名字段即使没值也按「已知但为空」替换成空串（沿用旧行为：缺 artist 时
    # ``{title} / {artist}`` 清成 ``曲名``）；只有文件里不存在的未知占位符才
    # 原样保留，方便用户看出哪个标签没解析到。
    meta = track.meta
    for name, raw in (
        ("title", meta.title),
        ("artist", meta.artist),
        ("album", meta.album),
        ("tagging_by", meta.tagging_by),
    ):
        lookup.setdefault(name, str(raw or ""))

    def _substitute(match: "re.Match[str]") -> str:
        return lookup.get(match.group(1).strip().lower(), match.group(0))

    text = _PLACEHOLDER_RE.sub(_substitute, template)
    lines = [
        raw.strip().strip(_TITLE_SEPARATOR_CHARS).strip()
        for raw in text.split("\n")
    ]
    return "\n".join(lines).strip("\n")


def title_show_window(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int]]:
    """Return title visibility windows on the project timeline."""
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
    tail_base_end = max(total - tail_off, 0)
    tail_end = tail_base_end + (10 if total > 0 and tail_off == 0 else 0)
    if title.show_mode == "custom":
        rows = [
            (window.begin_ms, window.end_ms)
            for window in (item.normalized() for item in title.custom_windows)
            if window.end_ms > window.begin_ms
        ]
        if rows:
            return rows
        # 空「自定义」窗口 = 全程显示：新默认条目（custom + 空）不配静态
        # 0-10s 窗口，避免标题在媒体中途凭空消失。
        return [(0, tail_end)]
    if title.show_mode == "whole":
        return [(0, tail_end)]
    if title.show_mode == "head":
        return [(head_start, head_start + duration)]
    if title.show_mode == "tail":
        return [(max(tail_base_end - tail_duration, 0), tail_end)]
    return [
        (head_start, head_start + duration),
        (max(tail_base_end - tail_duration, 0), tail_end),
    ]


def title_show_specs(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int, int, int]]:
    """Return visibility windows with independent head/tail transitions."""
    windows = title_show_window(title, track, duration_ms=duration_ms)
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
    if title.show_mode == "custom":
        rows = [
            (window.begin_ms, window.end_ms, window.fade_in_ms, window.fade_out_ms)
            for window in (item.normalized() for item in title.custom_windows)
            if window.end_ms > window.begin_ms
        ]
        if rows:
            return rows
        # 空「自定义」窗口 = 全程显示且无淡入淡出（直接切显/切隐，与
        # ``title_show_window`` 的空窗口语义配套）。
        return [
            (begin, end, 0, 0)
            for begin, end in title_show_window(title, track, duration_ms=duration_ms)
        ]
    if title.show_mode == "tail":
        return [(begin, end, tail_fade_in, tail_fade_out) for begin, end in windows]
    if title.show_mode == "head_tail" and len(windows) > 1:
        return [
            (*windows[0], head_fade_in, head_fade_out),
            (*windows[1], tail_fade_in, tail_fade_out),
        ]
    return [(begin, end, head_fade_in, head_fade_out) for begin, end in windows]


def title_overlay_opacity(
    title: Optional[TitleOverlay],
    track: TimingTrack,
    t_ms: int,
    *,
    duration_ms: Optional[int] = None,
) -> float:
    """Return title opacity at ``t_ms``, including fade transitions."""
    if title is None or not title.enabled:
        return 0.0
    best = 0.0
    for begin, end, fade_in, fade_out in title_show_specs(
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
