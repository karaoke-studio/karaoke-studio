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
  ルビ（漢字 / かな / 英数）；数值 0 / 空串沿 Fallback 链上溯
  （かな・英数 → 漢字，ルビ漢字 → 歌詞漢字）。
- ``BrushType``：0 Solid / 1 Gradient（整画面纵向线性渐变）/ 2 MilleFeuille
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

from krok_helper.subtitle_render.models import (
    KaraokeColorState,
    KaraokeColors,
    LineAnimationOverride,
    LyricsLayout,
    PaintFill,
    Style,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TimingTrack,
    TitleOverlay,
    _paint_fill,
    default_title_scheme,
    normalize_glow_concentration_level,
    line_animation_override_to_dict,
    style_to_dict,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

N3_PROJECT_FILE_SUFFIX = ".n3proj"
N3_PROJECT_FILTER = "NicoKaraMaker3 项目 (*.n3proj);;所有文件 (*.*)"

_HEAD_END_MAX_MS = 5_999_990
"""N3 时间标签上限 ``[99:59:99]``，``TitleShowTime.HeadEnd`` 取该值表示“到曲尾”。"""

# 与 frontend.property_panel.SCREEN_FPS_OPTIONS 一致；此处不 import 以免拖入 Qt。
_SUPPORTED_FPS = (60, 120)

_VERTICAL_ALIGN_MAP = {0: "top", 1: "center", 2: "bottom"}
_SMART_HORIZON_MAP = {0: "none", 1: "center_position", 2: "equal_margins"}
_HORIZONTAL_ALIGN_MAP = {0: "left", 1: "center", 2: "right"}
_RUBY_ALIGN_MAP = {0: "auto", 1: "center", 2: "equal_space"}

# FontFacePage 的 Fallback 链（かな/英数 → 同域漢字；ルビ漢字 → 歌詞漢字）。
_FONT_FALLBACK_INDEX = {1: 0, 2: 0, 3: 0, 4: 3, 5: 3}

_LINE_FADE_ACTION_ID = "SHINTA.LineFadeInFadeOut"
_NO_ACTION_ID = "SHINTA.NoAction"

# FontFaceName 是本地化字重/斜体名（"Bold" / "Negreta" / "太字" …），按关键字匹配。
_FACE_WEIGHT_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("extrabold", 800),
    ("ultrabold", 800),
    ("semibold", 600),
    ("demibold", 600),
    ("black", 900),
    ("heavy", 900),
    ("medium", 500),
    ("light", 300),
    ("thin", 100),
    ("bold", 700),
    ("negreta", 700),
    ("negrita", 700),
    ("grassetto", 700),
    ("fett", 700),
    ("gras", 700),
    ("太字", 700),
    ("ボールド", 700),
)
_FACE_ITALIC_KEYWORDS = ("italic", "oblique", "cursiva", "kursiv", "corsivo", "斜体", "イタリック")

_BRACKET_LABEL_RE = re.compile(r"【[^】]*】")


@dataclass
class N3ImportResult:
    """导入结果：``.yurika`` 同构快照 + 中文提示列表。"""

    project_data: dict
    warnings: list[str]


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
    width = _int(source.get("BackgroundWidth"), 1920)
    height = _int(source.get("BackgroundHeight"), 1080)
    fps = _int(source.get("Fps"), 60)
    if fps not in _SUPPORTED_FPS:
        warnings.append(f"N3 项目帧率 {fps} fps 不在支持列表（60/120），已调整为 60 fps")
        fps = 60
    screen = {"width": width, "height": height, "fps": fps, "par": "1:1"}

    # ---------------------------------------------------------------- 样式
    fonts = [_dict(item) for item in _list(data.get("LyricsFonts"))]
    layouts = [_dict(item) for item in _list(data.get("LyricsLayouts"))]
    changes: dict[str, Any] = {"layout_reference_height": height}

    if layouts:
        geometry = _layout_geometry(layouts[0])
        geometry.pop("name")
        changes.update(geometry)
        changes["upper_line_left_margin_px"] = geometry["horizontal_margin_px"]
        changes["lower_line_right_margin_px"] = geometry["horizontal_margin_px"]
        changes.update(_layout_char_domain(layouts[0]))
        changes["layouts"] = [LyricsLayout(**_layout_geometry(item)) for item in layouts[1:]]
        diff_names = [
            str(item.get("SettingsName") or "?")
            for item in layouts[1:]
            if _layout_char_domain(item) != _layout_char_domain(layouts[0])
        ]
        if diff_names:
            warnings.append(
                "以下布局的歌詞間隔 / ルビ間隔等字段与默认布局不同，本模块这些字段为全局设置，"
                f"已按默认布局导入：{'、'.join(diff_names)}"
            )

    if fonts:
        changes.update(_scheme_changes(fonts[0], lyrics_dir, warnings, str(fonts[0].get("SettingsName") or "標準配色")))
        scheme_field_names = {item.name for item in dataclass_fields(SubtitleStyleScheme)}
        custom: dict[str, SubtitleStyleScheme] = {}
        for index, font in enumerate(fonts[1:], start=1):
            name = str(font.get("SettingsName") or "").strip() or f"配色{index}"
            if name in custom or name == TITLE_SCHEME_NAME:
                name = f"{name}（{index}）"
            scheme_changes = _scheme_changes(font, lyrics_dir, warnings, name)
            custom[name] = SubtitleStyleScheme(
                **{key: value for key, value in scheme_changes.items() if key in scheme_field_names}
            )
        changes["custom_style_schemes"] = custom

    title_infos = [_dict(item) for item in _list(data.get("TitleInfos"))]
    title_overlay, title_scheme, title_role_schemes = _build_title_overlay(
        title_infos, fonts, layouts, lyrics_dir, warnings
    )
    if title_overlay is not None:
        changes["title_overlay"] = title_overlay
    # 「标题」方案恒存在：N3 标题引用的 フォント設定 优先，否则保持默认标题外观。
    custom_schemes = changes.get("custom_style_schemes")
    if isinstance(custom_schemes, dict):
        custom_schemes.update(
            {
                name: scheme
                for name, scheme in title_role_schemes.items()
                if name not in custom_schemes
            }
        )
        custom_schemes[TITLE_SCHEME_NAME] = title_scheme or default_title_scheme()

    # ---------------------------------------------------------- 每行布局 / 逐字配色 / 动画
    line_layout_indices: Optional[list[int]] = None
    line_breaks_before: Optional[list[str]] = None
    char_role_labels: Optional[list[Optional[list[Optional[str]]]]] = None
    line_animation_overrides: Optional[list[Optional[dict[str, object]]]] = None
    extra_sources: list[dict[str, Any]] = []
    if lyrics_with_source:
        layout_limit = len(changes.get("layouts") or [])
        font_names = [str(font.get("SettingsName") or "") for font in fonts]

        line_infos = [_dict(item) for item in _list(lyrics_with_source[0].get("LineInfos"))]
        animation_changes, default_animation = _animation_changes(line_infos, warnings)
        changes.update(animation_changes)
        track = _load_track(subtitle_path, warnings)
        if track is not None:
            line_layout_indices, line_breaks_before, char_role_labels, line_animation_overrides = _per_line_payloads(
                line_infos, track, layout_limit, font_names, default_animation, warnings
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
                extra_layouts, extra_breaks, extra_roles, extra_animations = _per_line_payloads(
                    extra_line_infos, extra_track, layout_limit, font_names, default_animation, warnings
                )
                if extra_layouts is not None:
                    extra_payload["line_layout_indices"] = extra_layouts
                if extra_breaks is not None:
                    extra_payload["line_breaks_before"] = extra_breaks
                if extra_roles is not None:
                    extra_payload["char_role_labels"] = extra_roles
                if extra_animations is not None:
                    extra_payload["line_animation_overrides"] = extra_animations
            extra_sources.append(extra_payload)

    style = replace(Style(), **changes)

    # ---------------------------------------------------------------- 输出
    output: dict[str, Any] = {}
    dest_format = _int(data.get("DestFormat"), 1)
    dest_path = str(data.get("DestPath") or "").strip()
    if dest_format == 1:
        if dest_path:
            output["output_path"] = dest_path
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


def _hex_from_dxcolor(value: object, fallback: str) -> str:
    color = _dict(value)
    if not color:
        return fallback

    def channel(key: str, default: float = 0.0) -> int:
        try:
            number = float(color.get(key, default))
        except (TypeError, ValueError):
            number = default
        return max(0, min(255, round(number * 255)))

    alpha = channel("A", 1.0)
    rgb = "%02X%02X%02X" % (channel("R"), channel("G"), channel("B"))
    return f"#{alpha:02X}{rgb}" if alpha < 255 else f"#{rgb}"


def _hex_from_colorbind(value: object, fallback: str = "#FFFFFF") -> str:
    """``ColorBindModel`` → ``#RRGGBB`` or ``#AARRGGBB``."""
    bind = _dict(value)
    if "DxColor" in bind:
        return _hex_from_dxcolor(bind.get("DxColor"), fallback)
    # BackgroundColor 等场合外层就是 DxColor 字段集
    if {"R", "G", "B"} <= bind.keys():
        return _hex_from_dxcolor(bind, fallback)
    web = str(bind.get("Web16") or "").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", web):
        return f"#{web.upper()}"
    return fallback


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


# ---------------------------------------------------------------------------
# 颜色 / 笔刷
# ---------------------------------------------------------------------------


def _gradient_stop_list(brush: dict) -> list[tuple[int, str]]:
    stops: list[tuple[int, str]] = []
    for stop in _list(brush.get("GradientStops")):
        stop = _dict(stop)
        try:
            position = float(stop.get("Position", 0.0))
        except (TypeError, ValueError):
            position = 0.0
        pct = max(0, min(100, round(position * 100)))
        stops.append((pct, _hex_from_dxcolor(stop.get("Color"), "#FFFFFF")))
    stops.sort(key=lambda item: item[0])
    return stops


def _mille_feuille_stops(stops: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """复刻 N3 MilleFeuille 的 stop 复制（硬色带）。

    N3 在相同 Position 上放两个 stop 制造硬边界；QGradient 侧按位置去重，
    因此把「上一色带的收尾 stop」放到边界前 1%，边界处 1% 的过渡肉眼不可见。
    N3 忽略最后一个 stop 自身的颜色（末段色带 = 倒数第二个 stop 的颜色）。
    """
    if len(stops) < 2:
        return stops
    result: list[tuple[int, str]] = []
    for index in range(len(stops) - 1):
        position, color = stops[index]
        next_position = stops[index + 1][0]
        result.append((position, color))
        edge = max(position, next_position - 1)
        if edge > position:
            result.append((edge, color))
    result.append((100, stops[-2][1]))
    return result


def _fill_from_brush(
    brush: object,
    lyrics_dir: Path,
    warnings: list[str],
    context: str,
) -> PaintFill:
    brush = _dict(brush)
    solid = _hex_from_colorbind(brush.get("SolidColor"))
    brush_type = _int(brush.get("SelectedBrushTypeIndex"), 0)
    if brush_type == 0:
        return _paint_fill(solid)
    if brush_type in (1, 2):
        stops = _gradient_stop_list(brush) or [(0, solid), (100, solid)]
        if brush_type == 2:
            stops = _mille_feuille_stops(stops)
        start_color = stops[0][1]
        end_color = stops[-1][1]
        return PaintFill(
            mode="gradient_vertical",
            color=start_color,
            start_color=start_color,
            end_color=end_color,
            gradient_stops=stops,
            split_top_color=start_color,
            split_bottom_color=end_color,
        )
    if brush_type == 3:
        image = str(brush.get("BitmapPath") or "").strip()
        candidate = Path(image) if image else None
        if candidate is not None and not candidate.is_absolute():
            candidate = lyrics_dir / image
        if candidate is None or not candidate.is_file():
            warnings.append(f"{context}：贴图填充素材不存在（{image or '未设置'}），已回退纯色")
            return _paint_fill(solid)
        fill = _paint_fill(solid)
        fill.mode = "image"
        fill.image_path = str(candidate)
        fill.image_scale_pct = max(1, _int(brush.get("BitmapScale"), 100))
        return fill
    warnings.append(f"{context}：未知笔刷类型（{brush_type}），已回退纯色")
    return _paint_fill(solid)


def _karaoke_colors_from_brushes(
    brushes: list, lyrics_dir: Path, warnings: list[str], context: str
) -> KaraokeColors:
    """``ColorPage`` 顺序：after(文字/縁/縁2/飾り) + before(同序)。"""
    fills: list[PaintFill] = []
    for index in range(8):
        brush = brushes[index] if index < len(brushes) else None
        name = str(_dict(brush).get("SettingsName") or f"笔刷{index}")
        fills.append(_fill_from_brush(brush, lyrics_dir, warnings, f"{context}·{name}"))
    return KaraokeColors(
        before=KaraokeColorState(text=fills[4], stroke=fills[5], stroke2=fills[6], shadow=fills[7]),
        after=KaraokeColorState(text=fills[0], stroke=fills[1], stroke2=fills[2], shadow=fills[3]),
    )


# ---------------------------------------------------------------------------
# フォント設定（字体 + 配色方案）
# ---------------------------------------------------------------------------


def _font_chain(font_infos: list, index: int) -> list[dict]:
    """按 ``FontFacePage`` 位置沿 Fallback 链展开（かな→漢字 等）。"""
    chain: list[dict] = []
    seen: set[int] = set()
    current: Optional[int] = index
    while current is not None and current not in seen and 0 <= current < len(font_infos):
        seen.add(current)
        chain.append(_dict(font_infos[current]))
        current = _FONT_FALLBACK_INDEX.get(current)
    return chain


def _resolve_font_name(chain: list[dict]) -> Optional[str]:
    for info in chain:
        name = str(info.get("FontName") or "").strip()
        if name:
            return name
    return None


def _resolve_font_size(chain: list[dict], key: str) -> int:
    """数值 0 表示未设置，沿链上溯（与 N3 ``Nkm3Common`` 解析一致）。"""
    for info in chain:
        value = _size(info.get(key))
        if value > 0:
            return value
    return 0


def _resolve_use_edge2(chain: list[dict]) -> bool:
    for info in chain:
        value = info.get("UseEdge2")
        if isinstance(value, bool):
            return value
    return False


def _face_weight_italic(face_name: Optional[str]) -> tuple[int, bool]:
    if not face_name:
        return 400, False
    lowered = face_name.lower()
    weight = 400
    for keyword, value in _FACE_WEIGHT_KEYWORDS:
        if keyword in lowered:
            weight = value
            break
    italic = any(keyword in lowered for keyword in _FACE_ITALIC_KEYWORDS)
    return weight, italic


def _resolve_face_name(chain: list[dict]) -> Optional[str]:
    for info in chain:
        name = str(info.get("FontFaceName") or "").strip()
        if name:
            return name
    return None


def _scheme_changes(
    font: dict, lyrics_dir: Path, warnings: list[str], context: str
) -> dict[str, Any]:
    """一个 フォント設定 → ``Style`` / ``SubtitleStyleScheme`` 通用字段字典。"""
    font_infos = _list(font.get("FontInfos"))
    kanji = _font_chain(font_infos, 0)
    alnum = _font_chain(font_infos, 2)
    kana_own = str(_dict(font_infos[1] if len(font_infos) > 1 else {}).get("FontName") or "").strip()
    alnum_own = str(_dict(font_infos[2] if len(font_infos) > 2 else {}).get("FontName") or "").strip()
    ruby_kanji = _font_chain(font_infos, 3)

    changes: dict[str, Any] = {}
    family = _resolve_font_name(kanji)
    if family:
        changes["font_family"] = family
    if kana_own and family and kana_own != family:
        warnings.append(f"{context}：かな独立字体（{kana_own}）暂不支持，已沿用漢字字体")
    if alnum_own and alnum_own != (family or ""):
        changes["font_family_latin"] = alnum_own

    size = _resolve_font_size(kanji, "CharSize")
    if size > 0:
        changes["font_size_px"] = size
    weight, italic = _face_weight_italic(_resolve_face_name(kanji))
    changes["font_weight"] = weight
    changes["italic"] = italic
    changes["stroke_width_px"] = _resolve_font_size(kanji, "EdgeSize")
    changes["stroke2_enabled"] = _resolve_use_edge2(kanji)
    changes["stroke2_width_px"] = _resolve_font_size(kanji, "EdgeSize2")

    latin_size = _resolve_font_size(alnum, "CharSize")
    if latin_size > 0:
        changes["latin_font_size_px"] = latin_size
    latin_weight, _latin_italic = _face_weight_italic(_resolve_face_name(alnum))
    changes["latin_font_weight"] = latin_weight
    changes["latin_stroke_width_px"] = _resolve_font_size(alnum, "EdgeSize")
    changes["latin_stroke2_enabled"] = _resolve_use_edge2(alnum)
    changes["latin_stroke2_width_px"] = _resolve_font_size(alnum, "EdgeSize2")

    colors = _karaoke_colors_from_brushes(_list(font.get("BrushInfos")), lyrics_dir, warnings, context)
    changes["karaoke_colors"] = colors
    changes["base_color"] = colors.before.text.color
    changes["fill_color"] = colors.after.text.color
    changes["stroke_color"] = colors.after.stroke.color
    changes["shadow_color"] = colors.after.shadow.color

    decor_kind = _int(font.get("DecorKind"), 0)
    decor_size = _size(font.get("DecorSize"))
    if decor_kind == 2:
        concentration = normalize_glow_concentration_level(font.get("BlurLevel"))
        changes["decoration_kind"] = "glow"
        changes["glow_radius_px"] = decor_size
        changes["glow_before_radius_px"] = decor_size
        changes["glow_after_radius_px"] = decor_size
        changes["glow_concentration_level"] = concentration
    elif decor_kind == 1:
        changes["decoration_kind"] = "shadow"
        changes["shadow_offset_x"] = decor_size
        changes["shadow_offset_y"] = decor_size
    else:
        # N3「飾りなし」：以 0 偏移阴影表达（视觉上不可见）。
        changes["decoration_kind"] = "shadow"
        changes["shadow_offset_x"] = 0
        changes["shadow_offset_y"] = 0

    # N3 のルビ字体/字号独立于歌詞（未设置时沿 Fallback 链取歌詞漢字字体，但
    # 字号始终是ルビ自己的值）；本模块默认「注音跟随主文字」会连字号一起跟随
    # 主文字，导入时显式解除跟随并落地链上解析结果。
    changes["ruby_font_follow_main"] = False
    ruby_family = _resolve_font_name(ruby_kanji)
    if ruby_family:
        changes["ruby_font_family"] = ruby_family
    ruby_kana_own = str(_dict(font_infos[4] if len(font_infos) > 4 else {}).get("FontName") or "").strip()
    ruby_alnum_own = str(_dict(font_infos[5] if len(font_infos) > 5 else {}).get("FontName") or "").strip()
    ruby_alnum = _font_chain(font_infos, 5)
    if ruby_kana_own and ruby_family and ruby_kana_own != ruby_family:
        warnings.append(f"{context}：ルビかな独立字体（{ruby_kana_own}）暂不支持，已沿用ルビ漢字字体")
    if ruby_alnum_own and ruby_alnum_own != (ruby_family or ""):
        changes["ruby_font_family_latin"] = ruby_alnum_own
    ruby_weight, ruby_italic = _face_weight_italic(_resolve_face_name(ruby_kanji))
    changes["ruby_font_weight"] = ruby_weight
    if ruby_italic != changes["italic"]:
        warnings.append(f"{context}：注音斜体与主文字不同，本模块注音斜体跟随主文字，已沿用主文字设置")
    ruby_size = _resolve_font_size(ruby_kanji, "CharSize")
    if ruby_size > 0:
        changes["ruby_font_size_px"] = ruby_size
    changes["ruby_stroke_width_px"] = _resolve_font_size(ruby_kanji, "EdgeSize")
    changes["ruby_stroke2_enabled"] = _resolve_use_edge2(ruby_kanji)
    changes["ruby_stroke2_width_px"] = _resolve_font_size(ruby_kanji, "EdgeSize2")
    ruby_latin_size = _resolve_font_size(ruby_alnum, "CharSize")
    if ruby_latin_size > 0:
        changes["ruby_latin_font_size_px"] = ruby_latin_size
    ruby_latin_weight, _ruby_latin_italic = _face_weight_italic(
        _resolve_face_name(ruby_alnum)
    )
    changes["ruby_latin_font_weight"] = ruby_latin_weight
    changes["ruby_latin_stroke_width_px"] = _resolve_font_size(
        ruby_alnum, "EdgeSize"
    )
    changes["ruby_latin_stroke2_enabled"] = _resolve_use_edge2(ruby_alnum)
    changes["ruby_latin_stroke2_width_px"] = _resolve_font_size(
        ruby_alnum, "EdgeSize2"
    )
    changes["ruby_color"] = colors.after.text.color
    changes["ruby_karaoke_colors"] = deepcopy(colors)
    return changes


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
    """N3 布局里属于本项目「字体/全局」域的字段（字间距 / 咬合 / ルビ间隔）。"""
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


def _title_base_font_index(title: dict, font_count: int) -> int:
    """取非空白标题字符中出现最多的 FontIndex；平票按首次出现。"""
    counts: dict[int, int] = {}
    order: list[int] = []
    first_valid: Optional[int] = None
    for row in _title_character_rows(title):
        for char in row:
            index = _int(char.get("FontIndex"), 0)
            if not 0 <= index < font_count:
                continue
            if first_valid is None:
                first_valid = index
            if not str(char.get("Char") or "").strip():
                continue
            if index not in counts:
                counts[index] = 0
                order.append(index)
            counts[index] += 1
    if counts:
        return max(order, key=lambda index: counts[index])
    return first_valid if first_valid is not None else 0


def _build_title_overlay(
    title_infos: list[dict],
    fonts: list[dict],
    layouts: list[dict],
    lyrics_dir: Path,
    warnings: list[str],
) -> tuple[
    Optional[TitleOverlay],
    Optional[SubtitleStyleScheme],
    dict[str, SubtitleStyleScheme],
]:
    """标题 → (文字/布局引用/显示时段, 「标题」配色方案)。

    字体/颜色不再展开进 ``TitleOverlay``：标题引用的 フォント設定 折算成
    ``SubtitleStyleScheme`` 由调用方写入 ``custom_style_schemes[TITLE_SCHEME_NAME]``；
    位置改为 ``layout_index`` 引用（与 N3 ``TitleInfoModel.LayoutIndex`` 同序）。
    """
    candidates = [(title, _title_lines(title)) for title in title_infos]
    candidates = [(title, lines) for title, lines in candidates if any(line.strip() for line in lines)]
    if not candidates:
        return None, None, {}
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

    scheme: Optional[SubtitleStyleScheme] = None
    font_index = _title_base_font_index(title, len(fonts))
    font = fonts[font_index] if 0 <= font_index < len(fonts) else (fonts[0] if fonts else None)
    if font is not None:
        context = f"标题·{font.get('SettingsName') or '?'}"
        scheme_changes = _scheme_changes(font, lyrics_dir, warnings, context)
        field_names = {item.name for item in dataclass_fields(SubtitleStyleScheme)}
        scheme = SubtitleStyleScheme(
            **{key: value for key, value in scheme_changes.items() if key in field_names}
        )

    title_role_schemes: dict[str, SubtitleStyleScheme] = {}
    role_rows: list[list[Optional[str]]] = []
    for row in _title_character_rows(title):
        labels: list[Optional[str]] = []
        for char in row:
            index = _int(char.get("FontIndex"), 0)
            if index == font_index or not 0 <= index < len(fonts):
                labels.append(None)
                continue
            role_font = fonts[index]
            name = str(role_font.get("SettingsName") or "").strip() or f"配色{index}"
            labels.append(name)
            if name not in title_role_schemes:
                context = f"标题字符·{name}"
                changes = _scheme_changes(role_font, lyrics_dir, warnings, context)
                field_names = {item.name for item in dataclass_fields(SubtitleStyleScheme)}
                title_role_schemes[name] = SubtitleStyleScheme(
                    **{key: value for key, value in changes.items() if key in field_names}
                )
        role_rows.append(labels)
    kwargs["char_role_labels"] = role_rows

    layout_index = _int(title.get("LayoutIndex"), 0)
    if not (0 <= layout_index < len(layouts)):
        layout_index = 0
    kwargs["layout_index"] = layout_index
    layout = layouts[layout_index] if 0 <= layout_index < len(layouts) else None
    if layout is not None and scheme is not None:
        # N3 标题字间距来自布局的歌詞間隔；本模块字间距归配色方案域。
        scheme = replace(
            scheme, letter_spacing_px=_layout_char_domain(layout)["letter_spacing_px"]
        )

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
        # N3 HeadAndTail = 从开头偏移到片尾偏移的连续一段；最接近整段显示。
        kwargs["show_mode"] = "whole"
        if head_offset or tail_offset:
            warnings.append("标题显示时段「開始～終了」带首尾偏移，本模块按整段显示导入")
    else:
        kwargs["show_mode"] = "tail"
        kwargs["duration_ms"] = interval
        kwargs["tail_offset_ms"] = tail_offset
    return TitleOverlay(**kwargs), scheme, title_role_schemes


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
    if action_id != _LINE_FADE_ACTION_ID:
        return None
    settings = _dict(line.get("SubtitleActionSettings"))
    return (
        "fade",
        max(0, _int(settings.get("FadeInTime"), 250)),
        "fade",
        max(0, _int(settings.get("FadeOutTime"), 250)),
    )


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
    font_names: list[str],
    default_animation: tuple[str, int, str, int],
    warnings: list[str],
) -> tuple[
    Optional[list[int]],
    Optional[list[str]],
    Optional[list[Optional[list[Optional[str]]]]],
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
        return None, None, None, None
    if len(n3_lines) != len(our_indexed):
        warnings.append(
            "歌词行数与 N3 项目记录不一致（歌词文件可能已改动），"
            "已跳过每行布局、分页与逐字配色导入"
        )
        return None, None, None, None

    layout_payload = [0] * len(track.lines)
    break_payload = ["none"] * len(track.lines)
    role_payload: list[Optional[list[Optional[str]]]] = [None] * len(track.lines)
    animation_payload: list[Optional[dict[str, object]]] = [None] * len(track.lines)
    mismatched = 0
    any_role = False
    for (line_index, our_line), n3_line, break_before in zip(
        our_indexed, n3_lines, n3_breaks_before
    ):
        break_payload[line_index] = break_before
        n3_chars = _stripped_n3_chars(n3_line)
        n3_text = "".join(str(char.get("Char") or "") for char in n3_chars)
        our_text = "".join(char.text for char in our_line.chars)
        if n3_text != our_text:
            mismatched += 1
            continue
        layout_index = _int(n3_line.get("LayoutIndex"), 0)
        layout_payload[line_index] = layout_index if 0 <= layout_index <= layout_limit else 0
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
        # 逐字配色：FontIndex > 0 → 对应 フォント設定 名称作为角色标签。
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
                if 0 < font_index < len(font_names) and font_names[font_index]
                else None
            )
            labels.append(label)
            position += len(our_char.text)
        if any(labels):
            any_role = True
            role_payload[line_index] = labels
    if mismatched:
        warnings.append(f"{mismatched} 行歌词文本与 N3 项目记录不一致，这些行的布局与逐字配色未导入")
    return (
        layout_payload,
        break_payload,
        role_payload if any_role else None,
        animation_payload if any(item is not None for item in animation_payload) else None,
    )
