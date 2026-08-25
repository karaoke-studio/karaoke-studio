"""NicoKaraMaker3 项目文件（``.n3proj``）导入。

``.n3proj`` 是 zip 包裹的单条目（条目名 ``"0"``）UTF-8(BOM) JSON，内容为 N3
``ProjectDataModel`` 的序列化快照。本模块把其中的素材引用 / フォント設定
（字体 + 配色矩阵）/ レイアウト設定 / 标题（タイトル）/ 每行布局 / 逐字配色 /
输出参数转换为与 ``.yurika`` 同构的项目快照 dict（见 :mod:`project_store`），
由 ``SubtitleRenderWindow._apply_project_data`` 直接套用。

字段语义来自 NicoKaraMaker3 10.74 反编译源码（ilspycmd）确认的枚举：

- ``ColorPage``：``BrushInfos`` 固定 8 项 = ワイプ後（文字 / 縁取り / 縁取り2 /
  飾り）+ ワイプ前（同序）→ ``KaraokeColors.after`` / ``.before``。
- ``FontFacePage``：``FontInfos`` 固定 6 项 = 歌詞（漢字 / かな / 英数）+
  ルビ（漢字 / かな / 英数）。本模块按产品规则忽略两个かな槽，かな始终使用同域
  漢字槽；英数和ルビ漢字的数值 0 / 空串沿 Fallback 链上溯。
- ``BrushType``：0 Solid / 1 Gradient（字符渲染目标内纵向线性渐变）/ 2 MilleFeuille
  （分层硬渐变，N3 通过复制 stop 制造硬边界）/ 3 Bitmap（贴图，相对歌词文件
  所在目录解析，``BitmapScale`` 为百分比）。
- ``DecorKind``：0 None / 1 Shadow（整行向右下平移 ``DecorSize``）/ 2 Blur
  （发光，模糊半径 ``DecorSize``，``BlurLevel``+1 层叠加）。
- ``LyricsLineKind``：0 Empty / 1 Lyrics（携带 ``LayoutIndex`` 与逐字
  ``FontIndex``）/ 2 PageBreak / 3 ParagraphBreak / 4 AtTag / 5 Title。
- ``TitleShowKind``：0 Head（``HeadEnd`` 为 ``5999990`` 哨兵时=整段）/
  1 HeadAndInterval / 2 HeadAndTail（首尾之间连续一段）/ 3 Tail。
- ``SourceKind``：0 Movie / 1 Image / 2 SequenceImage / 3 Background（纯色）。
- ``DestFormat``：0 UnCompressedAvi / 1 Mp4 / 2·3 ArgbPng。

不支持的设置（非 MP4 输出、未知字幕动作等）不阻塞导入，
收集为中文 warning 由 UI 一次性展示。
"""

from __future__ import annotations

import json
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass, fields as dataclass_fields, replace
from pathlib import Path
from typing import Any, Optional

from krok_helper.subtitle_render.n3.font_scheme import (
    convert_n3_font_scheme as _scheme_changes,
    hex_from_colorbind as _hex_from_colorbind,
)
from krok_helper.subtitle_render.domain.background import infer_image_sequence_pattern
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    LineAnimationOverride,
    TimingTrack,
)
from krok_helper.subtitle_render.models import (
    DEFAULT_OUTPUT_NAME_SUFFIX,
    LyricsLayout,
    Style,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    default_title_scheme,
    style_to_dict,
)
from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_to_dict,
    line_animation_override_to_dict,
)

N3_PROJECT_FILE_SUFFIX = ".n3proj"
N3_PROJECT_FILTER = "NicoKaraMaker3 项目 (*.n3proj);;所有文件 (*.*)"

# N3 自动生成的输出文件名后缀（DestPath = {视频名} + 此后缀 + .mp4）
_N3_AUTO_OUTPUT_SUFFIX = "_ニコカラメーカー3出力"

_HEAD_END_MAX_MS = 5_999_990
"""N3 时间标签上限 ``[99:59:99]``，``TitleShowTime.HeadEnd`` 取该值表示“到曲尾”。"""

# 与 frontend.property_panel.SCREEN_FPS_OPTIONS 一致；此处不 import 以免拖入 Qt。
_SUPPORTED_FPS = (60, 120)

_VERTICAL_ALIGN_MAP = {0: "top", 1: "center", 2: "bottom"}
_SMART_HORIZON_MAP = {0: "none", 1: "center_position", 2: "equal_margins"}
_HORIZONTAL_ALIGN_MAP = {0: "left", 1: "center", 2: "right"}
_RUBY_ALIGN_MAP = {0: "auto", 1: "center", 2: "equal_space"}

_NO_ACTION_ID = "SHINTA.NoAction"
_LINE_ACTIONS: dict[str, tuple[str, str]] = {
    "SHINTA.LineFadeIn": ("fade", "none"),
    "SHINTA.LineFadeOut": ("none", "fade"),
    "SHINTA.LineFadeInFadeOut": ("fade", "fade"),
}
_CHAR_ACTIONS: dict[str, str] = {
    "SHINTA.CharFadeInFadeOut": "char_fade",
    "SHINTA.CharDrip": "char_drip",
    "SHINTA.SpinFlip": "spin_flip",
}
_UTOPIA_ACTION_ID = "SHINTA.Utopia"
_UTOPIA_ENTRY_MS = 700
_UTOPIA_EXIT_MS = 750

_BRACKET_LABEL_RE = re.compile(r"【[^】]*】")
_BRACKETED_SCHEME_NAME_RE = re.compile(r"^【([^】]+)】(.*)$")
_EMOJI_TAG_RE = re.compile(r"^@Emoji\d*=(.*)$", re.IGNORECASE)


@dataclass
class N3ImportResult:
    """导入结果：``.yurika`` 同构快照 + 中文提示列表。"""

    project_data: dict
    warnings: list[str]


@dataclass(frozen=True)
class _N3EmojiSpec:
    trigger: str
    before_path: str
    after_path: Optional[str] = None
    zoom_percent: int = 100
    fix_size: bool = False
    no_decor: bool = False
    force_wipe_decor: bool = False
    margin_left_px: int = 0
    margin_right_px: int = 0
    margin_bottom_px: int = 0


def is_n3proj_file(path: object) -> bool:
    return isinstance(path, (str, Path)) and str(path).lower().endswith(N3_PROJECT_FILE_SUFFIX)


def load_n3proj(path: str | Path) -> N3ImportResult:
    """读取并转换 ``.n3proj``。文件不可读/非法时抛 :class:`ValueError`。"""
    path = Path(path)
    data = _read_payload(path)
    warnings: list[str] = []
    base_dir = path.parent

    # ---------------------------------------------------------------- 素材
    source = _dict(data.get("SourceInfo"))
    video_path: Optional[Path] = None
    source_kind = _int(source.get("SourceKind"), 0)
    background: dict[str, Any]
    if source_kind == 0:
        video_path = _resolve_media(
            source.get("MoviePath"), source.get("MovieRelativePath"), base_dir, warnings, "背景视频"
        )
        background = {
            "kind": "video", "path": str(video_path) if video_path else None,
            "color": "#000000", "source_fps": None, "sequence_start_number": 0,
            "video_offset_ms": 0,
        }
    elif source_kind in (1, 2):
        image_path = _resolve_media(
            source.get("ImagePath"), source.get("ImageRelativePath"), base_dir, warnings,
            "背景图片序列" if source_kind == 2 else "背景图片",
        )
        sequence_path, sequence_start = (
            infer_image_sequence_pattern(image_path)
            if source_kind == 2 and image_path is not None
            else (image_path, 0)
        )
        background = {
            "kind": "image_sequence" if source_kind == 2 else "image",
            "path": str(sequence_path) if sequence_path else None,
            "color": "#000000",
            "source_fps": _int(source.get("Fps"), 60) if source_kind == 2 else None,
            "sequence_start_number": sequence_start,
            "video_offset_ms": 0,
        }
    else:
        color = _hex_from_colorbind(source.get("BackgroundColor"), "#000000")
        background = {
            "kind": "solid", "path": None, "color": color,
            "source_fps": None, "sequence_start_number": 0, "video_offset_ms": 0,
        }

    audio_path = _resolve_media(
        source.get("SoundPath"), source.get("SoundRelativePath"), base_dir, warnings, "音频"
    )
    if source_kind == 0 and audio_path is not None:
        warnings.append("视频背景不使用独立音频，已忽略 SoundPath 并沿用视频内嵌音轨")
        audio_path = None

    lyrics_infos = [_dict(item) for item in _list(data.get("SourceLyricsInfos"))]
    lyrics_with_source = [
        info
        for info in lyrics_infos
        if str(info.get("SourceLyricsPath") or "").strip()
        or str(info.get("SourceLyricsRelativePath") or "").strip()
    ]
    subtitle_path: Optional[Path] = None
    if lyrics_with_source:
        subtitle_path = _resolve_media(
            lyrics_with_source[0].get("SourceLyricsPath"),
            lyrics_with_source[0].get("SourceLyricsRelativePath"),
            base_dir,
            warnings,
            "字幕源",
        )
    lyrics_dir = subtitle_path.parent if subtitle_path is not None else base_dir

    # ---------------------------------------------------------------- 画面
    fonts = [_dict(item) for item in _list(data.get("LyricsFonts"))]
    layouts = [_dict(item) for item in _list(data.get("LyricsLayouts"))]
    width = _int(source.get("BackgroundWidth"), 1920)
    height = _int(source.get("BackgroundHeight"), 1080)
    font_reference_height = _font_reference_height(fonts, height)
    layout_reference_height = _layout_reference_height(layouts, height)
    # For movie projects these dimensions belong to the optional solid
    # background. N3 updates SizeAndRatio.Reference from MovieInfo.Height, so
    # the shared reference is the reliable saved video height when the media
    # file itself cannot be probed.
    if source_kind == 0:
        inferred_height = font_reference_height or layout_reference_height
        if inferred_height > 0 and height > 0 and inferred_height != height:
            width = max(int(round(width * inferred_height / height)), 1)
            height = inferred_height
    fps = _int(source.get("Fps"), 60)
    if fps not in _SUPPORTED_FPS:
        # The renderer only supports 60/120 fps. Unsupported N3 values are a
        # hard compatibility boundary, so normalize to 60 without prompting.
        fps = 60
    screen = {"width": width, "height": height, "fps": fps, "par": "1:1"}

    # ---------------------------------------------------------------- 样式
    changes: dict[str, Any] = {
        "layout_semantics": "n3_1074",
        "font_reference_height": font_reference_height,
        "layout_reference_height": layout_reference_height,
    }

    if layouts:
        geometry = _layout_geometry(layouts[0])
        geometry.pop("name")
        changes.update(geometry)
        changes["upper_line_left_margin_px"] = geometry["horizontal_margin_px"]
        changes["lower_line_right_margin_px"] = geometry["horizontal_margin_px"]
        changes.update(_layout_char_domain(layouts[0]))
        changes["layouts"] = [
            LyricsLayout(
                **_layout_geometry(item),
                **_layout_char_domain(item),
            )
            for item in layouts[1:]
        ]

    # N3 的每一套 フォント設定 都原样落成一个角色方案（含 FontIndex 0 那套）。
    # 早期版本把第 0 套摊进全局默认，等于把它改名成「全局默认」：分色歌里这套
    # 通常是某个具体角色（【アクア】之类），名字一丢，LRC 里同名的 ``【…】``
    # 标记就再也找不到对应方案。全局默认与「标题」两个内置角色因此保持出厂值，
    # N3 的配色一律 append 进来，由逐字角色标签去引用。
    font_names: list[str] = []
    if fonts:
        scheme_field_names = {item.name for item in dataclass_fields(SubtitleStyleScheme)}
        custom: dict[str, SubtitleStyleScheme] = {}
        for index, font in enumerate(fonts):
            name = _n3_scheme_name(font.get("SettingsName"), f"配色{index}")
            if name in custom or name == TITLE_SCHEME_NAME:
                name = f"{name}（{index}）"
            scheme_changes = _scheme_changes(
                font,
                lyrics_dir,
                warnings,
                name,
                preserve_inheritance=True,
            )
            custom[name] = SubtitleStyleScheme(
                **{
                    key: value
                    for key, value in scheme_changes.items()
                    if key in scheme_field_names
                },
                n3_font_inheritance=True,
            )
            # 逐字标签只认这里定下的最终名字（重名会被加后缀），所以两边同源。
            font_names.append(name)
        changes["custom_style_schemes"] = custom

    title_infos = [_dict(item) for item in _list(data.get("TitleInfos"))]
    title_overlay = _build_title_overlay(title_infos, layouts, font_names, warnings)
    if title_overlay is not None:
        changes["title_overlay"] = title_overlay
    # 「标题」方案恒存在。N3 标题引用的 フォント設定 已经在上面 append 过，标题
    # 逐字角色去引用它，所以这里保持出厂标题外观、不被 N3 覆写。
    custom_schemes = changes.get("custom_style_schemes")
    if isinstance(custom_schemes, dict):
        custom_schemes[TITLE_SCHEME_NAME] = default_title_scheme()

    # ---------------------------------------------------------- 每行布局 / 逐字配色 / 动画
    line_layout_indices: Optional[list[int]] = None
    line_breaks_before: Optional[list[str]] = None
    char_role_labels: Optional[list[Optional[list[Optional[str]]]]] = None
    line_animation_overrides: Optional[list[Optional[dict[str, object]]]] = None
    line_display_overrides: Optional[list[Optional[list[Optional[int]]]]] = None
    line_guide_symbols: Optional[list[Optional[dict[str, object]]]] = None
    line_inline_guide_symbols: Optional[list[Optional[dict[str, object]]]] = None
    extra_sources: list[dict[str, Any]] = []
    if lyrics_with_source:
        layout_limit = len(changes.get("layouts") or [])
        layout_row_counts = [
            max(len(_list(layout.get("HorizontalAlignments"))), 1)
            for layout in layouts
        ]

        line_infos = [_dict(item) for item in _list(lyrics_with_source[0].get("LineInfos"))]
        animation_changes, default_animation = _animation_changes(line_infos, warnings)
        changes.update(animation_changes)
        track = _load_track(subtitle_path, warnings)
        if track is not None:
            emoji_specs = _parse_emoji_tags(
                _emoji_tag_lines(lyrics_with_source[0], track),
                subtitle_path.parent if subtitle_path is not None else base_dir,
                warnings,
            )
            (
                line_layout_indices,
                line_breaks_before,
                char_role_labels,
                line_animation_overrides,
                line_display_overrides,
                line_guide_symbols,
                line_inline_guide_symbols,
            ) = _per_line_payloads(
                line_infos,
                track,
                layout_limit,
                layout_row_counts,
                font_names,
                default_animation,
                warnings,
                emoji_specs,
            )

        # 副字幕源（コーラス等）：与主字幕同时渲染，逐源导入路径 / 每行布局 / 逐字配色。
        for info in lyrics_with_source[1:]:
            name = str(info.get("SettingsName") or "").strip() or "コーラス"
            extra_path = _resolve_media(
                info.get("SourceLyricsPath"),
                info.get("SourceLyricsRelativePath"),
                base_dir,
                warnings,
                f"字幕源「{name}」",
            )
            if extra_path is None:
                continue
            extra_payload: dict[str, Any] = {"name": name, "path": str(extra_path)}
            extra_track = _load_track(extra_path, warnings)
            if extra_track is not None:
                extra_line_infos = [_dict(item) for item in _list(info.get("LineInfos"))]
                extra_emoji_specs = _parse_emoji_tags(
                    _emoji_tag_lines(info, extra_track),
                    extra_path.parent,
                    warnings,
                )
                (
                    extra_layouts,
                    extra_breaks,
                    extra_roles,
                    extra_animations,
                    extra_display,
                    extra_guides,
                    extra_inline_guides,
                ) = _per_line_payloads(
                    extra_line_infos,
                    extra_track,
                    layout_limit,
                    layout_row_counts,
                    font_names,
                    default_animation,
                    warnings,
                    extra_emoji_specs,
                )
                if extra_layouts is not None:
                    extra_payload["line_layout_indices"] = extra_layouts
                if extra_breaks is not None:
                    extra_payload["line_breaks_before"] = extra_breaks
                if extra_roles is not None:
                    extra_payload["char_role_labels"] = extra_roles
                if extra_animations is not None:
                    extra_payload["line_animation_overrides"] = extra_animations
                if extra_display is not None:
                    extra_payload["line_display_overrides"] = extra_display
                if extra_guides is not None:
                    extra_payload["line_guide_symbols"] = extra_guides
                if extra_inline_guides is not None:
                    extra_payload["line_inline_guide_symbols"] = extra_inline_guides
            extra_sources.append(extra_payload)

    style = replace(Style(), **changes)

    # ---------------------------------------------------------------- 输出
    output: dict[str, Any] = {}
    dest_format = _int(data.get("DestFormat"), 1)
    dest_path = str(data.get("DestPath") or "").strip()
    if dest_format == 1:
        if dest_path:
            # N3 自动命名「{视频名}_ニコカラメーカー3出力」换成本模块默认后缀；
            # 用户在 N3 里自定义过的文件名原样保留。
            path = Path(dest_path)
            if path.stem.endswith(_N3_AUTO_OUTPUT_SUFFIX):
                stem = path.stem[: -len(_N3_AUTO_OUTPUT_SUFFIX)]
                path = path.with_name(f"{stem}{DEFAULT_OUTPUT_NAME_SUFFIX}{path.suffix}")
            output["output_path"] = str(path)
    else:
        format_names = {0: "未压缩 AVI", 2: "ARGB PNG 序列", 3: "ARGB PNG 序列（仅字幕）"}
        warnings.append(
            f"N3 输出格式「{format_names.get(dest_format, dest_format)}」不支持，请手动设置输出路径（本模块输出 MP4）"
        )

    project_data: dict[str, Any] = {
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "background": background,
        "style": style_to_dict(style),
        "screen": screen,
        "selected_scheme_key": "global",
        "output": output,
    }
    if line_layout_indices is not None:
        project_data["line_layout_indices"] = line_layout_indices
    if line_breaks_before is not None:
        project_data["line_breaks_before"] = line_breaks_before
    if char_role_labels is not None:
        project_data["char_role_labels"] = char_role_labels
    if line_animation_overrides is not None:
        project_data["line_animation_overrides"] = line_animation_overrides
    if line_display_overrides is not None:
        project_data["line_display_overrides"] = line_display_overrides
    if line_guide_symbols is not None:
        project_data["line_guide_symbols"] = line_guide_symbols
    if line_inline_guide_symbols is not None:
        project_data["line_inline_guide_symbols"] = line_inline_guide_symbols
    if extra_sources:
        project_data["extra_subtitle_sources"] = extra_sources
    return N3ImportResult(project_data=project_data, warnings=warnings)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _read_payload(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not names:
                raise ValueError("压缩包为空")
            entry = "0" if "0" in names else names[0]
            raw = archive.read(entry)
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 NicoKaraMaker3 项目文件（zip 解包失败）") from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"项目内容不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("项目内容不是对象")
    return data


# ---------------------------------------------------------------------------
# 基础转换
# ---------------------------------------------------------------------------


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _n3_scheme_name(value: object, fallback: str) -> str:
    """Return the canonical internal name for an N3 font/color scheme.

    N3 projects commonly name schemes ``【アクア】`` because the same name is
    emitted into Nicokara LRC as a ``【...】`` role marker. LRC and SUG adapters
    store the semantic role name without those syntax delimiters, so normalize
    the N3 side as well. A duplicate suffix remains intact
    (``【アクア】2`` -> ``アクア2``).
    """
    name = str(value or "").strip()
    match = _BRACKETED_SCHEME_NAME_RE.fullmatch(name)
    if match is not None:
        name = f"{match.group(1)}{match.group(2)}".strip()
    return name or fallback


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _size(value: object) -> int:
    """``SizeAndRatio`` → 当前像素值（N3 渲染同样直接用 ``Size``）。"""
    return _int(_dict(value).get("Size"), 0)


def _size_reference(value: object, fallback: int) -> int:
    """Return N3's current media-height reference for a ``SizeAndRatio``."""
    reference = _int(_dict(value).get("Reference"), 0)
    return reference if reference > 0 else max(int(fallback), 1)


def _font_reference_height(fonts: list[dict], fallback: int) -> int:
    if fonts:
        infos = _list(fonts[0].get("FontInfos"))
        if infos:
            return _size_reference(_dict(infos[0]).get("CharSize"), fallback)
    return max(int(fallback), 1)


def _layout_reference_height(layouts: list[dict], fallback: int) -> int:
    if layouts:
        return _size_reference(layouts[0].get("VerticalMargin"), fallback)
    return max(int(fallback), 1)


def _resolve_media(
    absolute: object,
    relative: object,
    base_dir: Path,
    warnings: list[str],
    label: str,
) -> Optional[Path]:
    """N3 存绝对 + 相对双路径；绝对不存在时回退到 n3proj 同目录相对路径。"""
    absolute_text = str(absolute or "").strip()
    relative_text = str(relative or "").strip()
    if not absolute_text and not relative_text:
        return None
    if absolute_text:
        candidate = Path(absolute_text)
        if candidate.is_file():
            return candidate
    if relative_text:
        candidate = base_dir / relative_text
        if candidate.is_file():
            return candidate
    missing = absolute_text or relative_text
    warnings.append(f"{label}文件不存在：{missing}")
    return Path(absolute_text) if absolute_text else base_dir / relative_text


def _emoji_tag_lines(info: dict, track: Optional[TimingTrack]) -> list[str]:
    lines: list[str] = []
    for item in _list(info.get("AtTagsForSave")):
        text = str(item).strip()
        if text:
            lines.append(text)
    if track is not None:
        lines.extend(
            str(item).strip() for item in track.meta.custom if str(item).strip()
        )
    return lines


def _resolve_emoji_image_path(path_text: str, base_dir: Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else base_dir / path


def _parse_emoji_tags(
    lines: list[str],
    base_dir: Path,
    warnings: list[str],
) -> list[_N3EmojiSpec]:
    specs: list[_N3EmojiSpec] = []
    seen: set[str] = set()
    for line in lines:
        match = _EMOJI_TAG_RE.match(line.strip())
        if match is None:
            continue
        parts = [
            part.strip()
            for part in match.group(1).replace("，", ",").split(",")
        ]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            warnings.append(f"N3 Emoji 标签格式无法识别：{line}")
            continue
        trigger = parts[0]
        if trigger in seen:
            continue
        seen.add(trigger)
        before = _resolve_emoji_image_path(parts[1], base_dir)
        after = (
            _resolve_emoji_image_path(parts[2], base_dir)
            if len(parts) >= 3 and parts[2]
            else None
        )
        if not before.is_file():
            warnings.append(f"N3 Emoji 图片不存在：{before}")
        if after is not None and not after.is_file():
            warnings.append(f"N3 Emoji 后图片不存在：{after}")
        zoom_percent = 100
        fix_size = False
        no_decor = False
        force_wipe_decor = False
        margin_left = 0
        margin_right = 0
        margin_bottom = 0
        for raw_option in parts[3:]:
            option = raw_option.strip()
            if not option:
                continue
            key, sep, raw_value = option.partition("=")
            key_lower = key.strip().lower()
            value = raw_value.strip().rstrip("%")
            if sep and key_lower == "zoom":
                zoom_percent = max(_int(value, zoom_percent), 1)
            elif key_lower == "fix":
                fix_size = True
            elif key_lower == "nodecor":
                no_decor = True
            elif key_lower == "forcewipedecor":
                force_wipe_decor = True
            elif sep and key_lower == "marginleft":
                margin_left = _int(value, margin_left)
            elif sep and key_lower == "marginright":
                margin_right = _int(value, margin_right)
            elif sep and key_lower == "marginbottom":
                margin_bottom = _int(value, margin_bottom)
        specs.append(
            _N3EmojiSpec(
                trigger=trigger,
                before_path=str(before),
                after_path=str(after) if after is not None else None,
                zoom_percent=zoom_percent,
                fix_size=fix_size,
                no_decor=no_decor,
                force_wipe_decor=force_wipe_decor,
                margin_left_px=margin_left,
                margin_right_px=margin_right,
                margin_bottom_px=margin_bottom,
            )
        )
    return specs


def _emoji_guide_symbol(spec: _N3EmojiSpec, *, anchored: bool) -> GuideSymbol:
    return GuideSymbol(
        name=f"N3 Emoji {spec.trigger}",
        kind="bitmap",
        bitmap_before_path=spec.before_path,
        bitmap_after_path=spec.after_path,
        bitmap_zoom_percent=spec.zoom_percent,
        bitmap_fix_size=spec.fix_size,
        bitmap_no_decor=spec.no_decor,
        bitmap_force_wipe_decor=spec.force_wipe_decor,
        bitmap_margin_left_px=spec.margin_left_px,
        bitmap_margin_right_px=spec.margin_right_px,
        bitmap_margin_bottom_px=spec.margin_bottom_px,
        prefix_timing="anchored" if anchored else "pre_roll",
    )


# ---------------------------------------------------------------------------
# レイアウト設定
# ---------------------------------------------------------------------------


def _layout_geometry(layout: dict) -> dict[str, Any]:
    alignments = [
        _HORIZONTAL_ALIGN_MAP.get(_int(_dict(item).get("HorizontalLayoutAlignment"), 0), "left")
        for item in _list(layout.get("HorizontalAlignments"))
    ]
    return {
        "name": str(layout.get("SettingsName") or "布局"),
        "line_y_position": _VERTICAL_ALIGN_MAP.get(
            _int(layout.get("SelectedVerticalAlignmentIndex"), 2), "bottom"
        ),
        "line_y_margin_px": _size(layout.get("VerticalMargin")),
        "line_gap_px": _size(layout.get("LineSpace")),
        "smart_horizontal": _SMART_HORIZON_MAP.get(_int(layout.get("SmartHorizon"), 2), "equal_margins"),
        "horizontal_margin_px": _size(layout.get("HorizontalMargin")),
        "line_alignments": (alignments or ["left"])[:8],
    }


def _layout_char_domain(layout: dict) -> dict[str, Any]:
    """N3 布局的字符排版字段（字间距 / 咬合 / ルビ间隔）。"""
    return {
        "letter_spacing_px": _size(layout.get("LyricsInterval")),
        "allow_biting": bool(layout.get("AllowBiting")),
        "ruby_interval_px": _size(layout.get("RubyInterval")),
        "ruby_alignment": _RUBY_ALIGN_MAP.get(_int(layout.get("RubyAlignment"), 0), "auto"),
        "ruby_gap_px": _size(layout.get("LyricsAndRubyInterval")),
    }


# ---------------------------------------------------------------------------
# 标题（タイトル）
# ---------------------------------------------------------------------------


def _title_lines(title: dict) -> list[str]:
    lines: list[str] = []
    for line in _list(title.get("LineInfos")):
        line = _dict(line)
        if _int(line.get("Kind"), -1) != 5:
            continue
        lines.append(
            "".join(
                str(_dict(char).get("Char") or "")
                for char in _list(line.get("LyricsCharInfos"))
                if not _dict(char).get("IsRuby")
            )
        )
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _title_character_rows(title: dict) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for line in _list(title.get("LineInfos")):
        line = _dict(line)
        if _int(line.get("Kind"), -1) != 5:
            continue
        rows.append(
            [
                _dict(char)
                for char in _list(line.get("LyricsCharInfos"))
                if not _dict(char).get("IsRuby")
            ]
        )
    while rows and not any(str(char.get("Char") or "").strip() for char in rows[-1]):
        rows.pop()
    return rows


def _build_title_overlay(
    title_infos: list[dict],
    layouts: list[dict],
    font_names: list[str],
    warnings: list[str],
) -> Optional[TitleOverlay]:
    """标题 → 文字 / 布局引用 / 显示时段 / 逐字角色。

    字体与颜色不展开进 ``TitleOverlay``，也不写进内置的「标题」方案：标题用到
    的每套 フォント設定 都已经作为角色方案 append 进 ``custom_style_schemes``，
    这里只按 ``FontIndex`` 给每个标题字符贴上对应的角色名 —— 包括标题的主字体，
    这样内置「标题」角色保持出厂值，N3 的配色也不会被改名。
    位置改为 ``layout_index`` 引用（与 N3 ``TitleInfoModel.LayoutIndex`` 同序）。
    """
    candidates = [(title, _title_lines(title)) for title in title_infos]
    candidates = [(title, lines) for title, lines in candidates if any(line.strip() for line in lines)]
    if not candidates:
        return None
    if len(candidates) > 1:
        skipped = "、".join(str(title.get("SettingsName") or "?") for title, _lines in candidates[1:])
        warnings.append(f"N3 项目包含多个标题设置，仅导入第一个（{skipped} 被忽略）")
    title, lines = candidates[0]
    kwargs: dict[str, Any] = {
        "enabled": True,
        "text_template": "\n".join(lines),
        # N3 标题无淡入淡出动作，直接显示/消失。
        "fade_in_ms": 0,
        "fade_out_ms": 0,
    }

    role_rows: list[list[Optional[str]]] = []
    for row in _title_character_rows(title):
        labels: list[Optional[str]] = []
        for char in row:
            index = _int(char.get("FontIndex"), 0)
            # 索引越界（N3 删过配色）时留空，落回内置「标题」角色。
            labels.append(font_names[index] if 0 <= index < len(font_names) else None)
        role_rows.append(labels)
    kwargs["char_role_labels"] = role_rows

    layout_index = _int(title.get("LayoutIndex"), 0)
    if not (0 <= layout_index < len(layouts)):
        layout_index = 0
    kwargs["layout_index"] = layout_index

    show_time = _dict(title.get("ShowTime"))
    kind = _int(show_time.get("Kind"), 0)
    head_offset = _int(show_time.get("HeadOffset"), 0)
    head_end = _int(show_time.get("HeadEnd"), _HEAD_END_MAX_MS)
    interval = _int(show_time.get("Interval"), 10000)
    tail_offset = _int(show_time.get("TailOffset"), 0)
    if kind == 0:
        if head_offset <= 0 and head_end >= _HEAD_END_MAX_MS:
            kwargs["show_mode"] = "whole"
        else:
            kwargs["show_mode"] = "head"
            kwargs["head_offset_ms"] = head_offset
            kwargs["duration_ms"] = max(head_end - head_offset, 0)
    elif kind == 1:
        kwargs["show_mode"] = "head"
        kwargs["head_offset_ms"] = head_offset
        kwargs["duration_ms"] = interval
    elif kind == 2:
        # N3 HeadAndTail 是从开始偏移连续显示到片尾偏移；本模块的
        # head_tail 是“开始和片尾各一段”，不能直接映射。
        kwargs["show_mode"] = "whole"
        if head_offset or tail_offset:
            warnings.append("标题显示时段「開始～終了」带首尾偏移，本模块按整段显示导入")
    else:
        kwargs["show_mode"] = "tail"
        kwargs["duration_ms"] = interval
        kwargs["tail_offset_ms"] = tail_offset
    return TitleOverlay(**kwargs)


# ---------------------------------------------------------------------------
# 每行布局 / 逐字配色 / 行动画
# ---------------------------------------------------------------------------


def _load_track(subtitle_path: Optional[Path], warnings: list[str]) -> Optional[TimingTrack]:
    if subtitle_path is None or not subtitle_path.is_file():
        return None
    try:
        return load_nicokara_lrc(subtitle_path)
    except Exception:  # noqa: BLE001 — 主窗口加载时会再次报错，这里只跳过行级导入
        warnings.append("字幕源解析失败，已跳过每行布局与逐字配色导入")
        return None


def _stripped_n3_chars(line: dict) -> list[dict]:
    """去掉 ruby 字符和 ``【…】`` 标签段（本模块把标签解析为角色而非字符）。"""
    chars = [_dict(char) for char in _list(line.get("LyricsCharInfos")) if not _dict(char).get("IsRuby")]
    text = "".join(str(char.get("Char") or "") for char in chars)
    keep = [True] * len(text)
    for match in _BRACKET_LABEL_RE.finditer(text):
        for position in range(match.start(), match.end()):
            keep[position] = False
    result: list[dict] = []
    position = 0
    for char in chars:
        length = len(str(char.get("Char") or ""))
        if length and keep[position]:
            result.append(char)
        position += length
    return result


def _line_animation_signature(line: dict) -> Optional[tuple[str, int, str, int]]:
    """N3 单行动作 → 本模块逐行动画值；未知动作返回 None。"""
    action_id = str(line.get("SubtitleActionId") or "")
    if not action_id or action_id == _NO_ACTION_ID:
        return ("none", 0, "none", 0)
    settings = _dict(line.get("SubtitleActionSettings"))
    if action_id in _LINE_ACTIONS:
        entry, exit_ = _LINE_ACTIONS[action_id]
        return (
            entry,
            max(0, _int(settings.get("FadeInTime"), 250)) if entry != "none" else 0,
            exit_,
            max(0, _int(settings.get("FadeOutTime"), 250)) if exit_ != "none" else 0,
        )
    if action_id in _CHAR_ACTIONS:
        effect = _CHAR_ACTIONS[action_id]
        intro_delay = max(0, _int(settings.get("IntroDelay"), 350))
        return (
            effect,
            intro_delay + max(0, _int(settings.get("FadeInTime"), 250)),
            effect,
            intro_delay + max(0, _int(settings.get("FadeOutTime"), 250)),
        )
    if action_id == _UTOPIA_ACTION_ID:
        return ("utopia", _UTOPIA_ENTRY_MS, "utopia", _UTOPIA_EXIT_MS)
    return None


def _raw_n3_non_ruby_text(line: dict) -> str:
    chars = [
        _dict(char)
        for char in _list(line.get("LyricsCharInfos"))
        if not _dict(char).get("IsRuby")
    ]
    return "".join(str(char.get("Char") or "") for char in chars)


def _source_text_offsets(line: TimingLine) -> dict[int, int]:
    offsets: dict[int, int] = {}
    position = 0
    for index, char in enumerate(line.chars):
        offsets[position] = index
        position += len(char.text)
    return offsets


def _emoji_payload_for_line(
    n3_line: dict,
    n3_text: str,
    our_line: TimingLine,
    emoji_specs: list[_N3EmojiSpec],
) -> tuple[Optional[dict[str, object]], Optional[dict[str, object]]]:
    if not emoji_specs:
        return None, None
    raw_text = _raw_n3_non_ruby_text(n3_line)
    text_offsets = _source_text_offsets(our_line)
    guide_row: Optional[dict[str, object]] = None
    inline_row: dict[str, object] = {}
    for spec in emoji_specs:
        if not spec.trigger:
            continue
        visible_at = n3_text.find(spec.trigger)
        if visible_at >= 0:
            char_index = text_offsets.get(visible_at)
            key = str(char_index) if char_index is not None else ""
            if (
                char_index is not None
                and key not in inline_row
                and char_index < len(our_line.chars)
                and our_line.chars[char_index].text == spec.trigger
            ):
                inline_row[key] = guide_symbol_to_dict(
                    _emoji_guide_symbol(spec, anchored=False)
                )
            continue
        if guide_row is None and raw_text.find(spec.trigger) >= 0:
            role_label = our_line.singer_label or next(
                (char.role_label for char in our_line.chars if char.role_label),
                None,
            )
            symbol = _emoji_guide_symbol(spec, anchored=True)
            if role_label:
                symbol = replace(
                    symbol,
                    role_label=role_label,
                    role_labels=(role_label,),
                )
            guide_row = guide_symbol_to_dict(symbol)
    return guide_row, (inline_row or None)


def _signature_changes(signature: tuple[str, int, str, int]) -> dict[str, Any]:
    entry, entry_ms, exit_, exit_ms = signature
    return {
        "entry_anim": entry,
        "entry_lead_ms": entry_ms,
        "exit_anim": exit_,
        "exit_fade_ms": exit_ms,
    }


def _animation_changes(
    line_infos: list[dict], warnings: list[str]
) -> tuple[dict[str, Any], tuple[str, int, str, int]]:
    """选出全局默认动作；差异行由 ``_per_line_payloads`` 保存为 override。"""
    lyric_lines = [line for line in line_infos if _int(line.get("Kind"), -1) == 1]
    if not lyric_lines:
        signature = ("none", 0, "none", 0)
        return {}, signature
    counts: dict[tuple[str, int, str, int], int] = {}
    unknown: set[str] = set()
    for line in lyric_lines:
        signature = _line_animation_signature(line)
        if signature is None:
            unknown.add(str(line.get("SubtitleActionId") or "?"))
            continue
        counts[signature] = counts.get(signature, 0) + 1
    for action_id in sorted(unknown):
        warnings.append(f"字幕动作「{action_id}」暂不支持，这些行将继承全局特效")
    default = max(counts, key=counts.get) if counts else ("none", 0, "none", 0)
    return _signature_changes(default), default


def _per_line_payloads(
    line_infos: list[dict],
    track: TimingTrack,
    layout_limit: int,
    layout_row_counts: list[int],
    font_names: list[str],
    default_animation: tuple[str, int, str, int],
    warnings: list[str],
    emoji_specs: list[_N3EmojiSpec] | None = None,
) -> tuple[
    Optional[list[int]],
    Optional[list[str]],
    Optional[list[Optional[list[Optional[str]]]]],
    Optional[list[Optional[dict[str, object]]]],
    Optional[list[Optional[list[Optional[int]]]]],
    Optional[list[Optional[dict[str, object]]]],
    Optional[list[Optional[dict[str, object]]]],
]:
    """对齐 N3 歌词行与本模块解析行，导出布局、分页与逐字配色。

    N3 ``LineInfos`` 里 Kind==1（Lyrics）的行与 LRC 非空行一一对应（空行/分页
    在 N3 里是 Empty/PageBreak，段落分隔 ParagraphBreak 是运行时插入的）。
    逐行校验字符文本，一致才导入该行数据，避免歌词文件被改动后错位。
    """
    n3_lines: list[dict] = []
    n3_breaks_before: list[str] = []
    pending_break = "none"
    for line in line_infos:
        kind = _int(line.get("Kind"), -1)
        if kind in (2, 3):  # PageBreak / ParagraphBreak 都结束当前显示页
            pending_break = "page" if kind == 2 else "paragraph"
        elif kind == 1:
            n3_lines.append(line)
            n3_breaks_before.append(pending_break)
            pending_break = "none"
    our_indexed = [(index, line) for index, line in enumerate(track.lines) if not line.is_blank]
    if not n3_lines:
        return None, None, None, None, None, None, None
    if len(n3_lines) != len(our_indexed):
        warnings.append(
            "歌词行数与 N3 项目记录不一致（歌词文件可能已改动），"
            "已跳过每行布局、分页与逐字配色导入"
        )
        return None, None, None, None, None, None, None

    raw_layout_indices = [
        index if 0 <= index <= layout_limit else 0
        for line in n3_lines
        for index in [_int(line.get("LayoutIndex"), 0)]
    ]
    page_layout_indices = list(raw_layout_indices)
    has_explicit_breaks = any(value != "none" for value in n3_breaks_before[1:])
    page_start = 0
    while page_start < len(n3_lines):
        head_layout = raw_layout_indices[page_start]
        rows = (
            layout_row_counts[head_layout]
            if 0 <= head_layout < len(layout_row_counts)
            else 1
        )
        page_end = (
            len(n3_lines)
            if has_explicit_breaks
            else min(page_start + max(rows, 1), len(n3_lines))
        )
        for candidate in range(page_start + 1, page_end):
            if n3_breaks_before[candidate] != "none":
                page_end = candidate
                break
        for index in range(page_start, page_end):
            page_layout_indices[index] = head_layout
        page_start = page_end

    layout_payload = [0] * len(track.lines)
    break_payload = ["none"] * len(track.lines)
    role_payload: list[Optional[list[Optional[str]]]] = [None] * len(track.lines)
    animation_payload: list[Optional[dict[str, object]]] = [None] * len(track.lines)
    display_payload: list[Optional[list[Optional[int]]]] = [None] * len(track.lines)
    guide_payload: list[Optional[dict[str, object]]] = [None] * len(track.lines)
    inline_guide_payload: list[Optional[dict[str, object]]] = [None] * len(track.lines)
    emoji_specs = emoji_specs or []
    mismatched = 0
    for (line_index, our_line), n3_line, break_before, page_layout_index in zip(
        our_indexed, n3_lines, n3_breaks_before, page_layout_indices
    ):
        break_payload[line_index] = break_before
        n3_chars = _stripped_n3_chars(n3_line)
        n3_text = "".join(str(char.get("Char") or "") for char in n3_chars)
        our_text = "".join(char.text for char in our_line.chars)
        if n3_text != our_text:
            mismatched += 1
            continue
        layout_payload[line_index] = page_layout_index
        show_begin = n3_line.get("ShowBeginTime")
        show_end = n3_line.get("ShowEndTime")
        if isinstance(show_begin, (int, float)) or isinstance(show_end, (int, float)):
            display_payload[line_index] = [
                int(show_begin) if isinstance(show_begin, (int, float)) else None,
                int(show_end) if isinstance(show_end, (int, float)) else None,
            ]
        signature = _line_animation_signature(n3_line)
        if signature is not None and signature != default_animation:
            animation_payload[line_index] = line_animation_override_to_dict(
                LineAnimationOverride(
                    entry_anim=signature[0],
                    entry_duration_ms=signature[1],
                    exit_anim=signature[2],
                    exit_duration_ms=signature[3],
                )
            )
        # 逐字配色：FontIndex → 对应 フォント設定 名称作为角色标签。第 0 套
        # 同样是一个具名角色（不再摊进全局默认），所以也贴标签；这样 LRC 里
        # ``【…】`` 解析出的同名标记正好对得上，而不是被清成 None。
        # 索引越界（N3 删过配色）才留 None，与字符对齐写回。
        offset_to_char: dict[int, dict] = {}
        position = 0
        for char in n3_chars:
            offset_to_char[position] = char
            position += len(str(char.get("Char") or ""))
        labels: list[Optional[str]] = []
        position = 0
        for our_char in our_line.chars:
            n3_char = offset_to_char.get(position)
            font_index = _int(n3_char.get("FontIndex"), 0) if n3_char is not None else 0
            label = (
                font_names[font_index]
                if 0 <= font_index < len(font_names) and font_names[font_index]
                else None
            )
            labels.append(label)
            position += len(our_char.text)
        role_payload[line_index] = labels
        guide_row, inline_row = _emoji_payload_for_line(
            n3_line, n3_text, our_line, emoji_specs
        )
        if guide_row is not None:
            guide_payload[line_index] = guide_row
        if inline_row is not None:
            inline_guide_payload[line_index] = inline_row
    if mismatched:
        warnings.append(f"{mismatched} 行歌词文本与 N3 项目记录不一致，这些行的布局与逐字配色未导入")
    return (
        layout_payload,
        break_payload,
        role_payload,
        animation_payload if any(item is not None for item in animation_payload) else None,
        display_payload if any(item is not None for item in display_payload) else None,
        guide_payload if any(item is not None for item in guide_payload) else None,
        inline_guide_payload if any(item is not None for item in inline_guide_payload) else None,
    )
