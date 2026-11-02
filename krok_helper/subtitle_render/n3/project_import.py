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
- ``DestFormat``：0 UnCompressedAvi（不支持，回退 MP4）/ 1 Mp4 /
  2·3 ArgbPng（含背景 / 仅字幕，映射为本模块 PNG 序列导出）。

不支持的设置（未压缩 AVI 输出、未知字幕动作等）不阻塞导入，
收集为中文 warning 由 UI 一次性展示。

字幕源自适应（2026-10）：字幕文件丢失、解析失败或行数与 N3 记录不一致时，
以 n3proj 内嵌的 ``LineInfos``（逐字文本 + 逐字时间）为准，物化成
``<原文件名>_从N3重建.sug`` 落在 n3proj 同目录并让工程字幕源改指它——
后续保存/重载走 SUG 高保真路径，不再经历 LRC 有损往返。原字幕文件一律不
改动。重建管线：N3 行数据 → 合成 Nicokara LRC 文本（共享块/停顿/行末/标签
段按 N3 实际导出约定，见 :func:`_n3_lrc_body_line`；``AtTagsForSave`` 尾部
原样接回，含 ``@RubyN``）→ SUG 官方 ``NicokaraParser`` → Project → 落盘，
ruby/演唱者/mora 与「SUG 导出 LRC 再导入」同构。重建不可行时退回按行文本
LCS 对齐的兜底路径（能对上的行照常导入行级数据）。
"""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from copy import deepcopy
from dataclasses import dataclass, fields as dataclass_fields, replace
from pathlib import Path
from typing import Any, Callable, Optional

from krok_helper.settings import load_app_settings

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
from krok_helper.subtitle_render.domain.models import (
    DEFAULT_OUTPUT_NAME_SUFFIX,
    LyricsLayout,
    Style,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    default_title_scheme,
    style_to_dict,
)
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_PNG_COMPOSITED,
    OUTPUT_FORMAT_PNG_TRANSPARENT,
)
from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc
from krok_helper.subtitle_render.sources.sug import load_sug_timing_track
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_to_dict,
    line_animation_override_to_dict,
)
from strange_uta_game.backend.domain.entities import Singer
from strange_uta_game.backend.domain.project import Project, ProjectMetadata
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
    NicokaraParser,
    nicokara_result_to_sentences,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

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


ProgressCallback = Callable[[int, str], None]
"""导入进度回调：``(percent, message)``，由 UI 在后台线程里转发。"""


def _sug_export_compensation_ms() -> int:
    """打轴模块（SUG）「设置 → 导出 → 软件导出补偿」的当前值（毫秒）。

    与 ``frontend.main_window._sug_software_compensation_ms`` 同源同逻辑
    （``AppSettings.lyrics_timing["export"]["software_compensation_ms"]``），
    供非 UI 模块复用；读取失败按无补偿处理。
    """
    try:
        sug_settings = load_app_settings().lyrics_timing
    except Exception:  # noqa: BLE001 — 设置读取失败按无补偿处理
        return 0
    export = (
        sug_settings.get("export") if isinstance(sug_settings, dict) else None
    )
    value = (
        export.get("software_compensation_ms")
        if isinstance(export, dict)
        else None
    )
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _wrap_progress_callback(
    progress_cb: Optional[ProgressCallback],
) -> Optional[ProgressCallback]:
    """进度回调不容错失败——UI 侧异常不允许影响导入本身。"""
    if progress_cb is None:
        return None

    def report(percent: int, message: str) -> None:
        try:
            progress_cb(percent, message)
        except Exception:  # noqa: BLE001
            pass

    return report


def load_n3proj(
    path: str | Path,
    *,
    progress_cb: Optional[ProgressCallback] = None,
) -> N3ImportResult:
    """读取并转换 ``.n3proj``。文件不可读/非法时抛 :class:`ValueError`。

    ``progress_cb(percent, message)`` 在各阶段（读取 / 素材解析 / 字幕源
    重建 / 行级对齐）回调，供后台导入任务向前台报告进度；回调异常被吞掉。
    """
    report = _wrap_progress_callback(progress_cb)
    path = Path(path)
    if report is not None:
        report(5, "正在读取工程文件…")
    data = _read_payload(path)
    warnings: list[str] = []
    base_dir = path.parent
    compensation_ms = _sug_export_compensation_ms()

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
    subtitle_track: Optional[TimingTrack] = None
    subtitle_shift_ms = 0
    if lyrics_with_source:
        if report is not None:
            report(20, "正在检查主字幕源…")
        subtitle_path = _resolve_media(
            lyrics_with_source[0].get("SourceLyricsPath"),
            lyrics_with_source[0].get("SourceLyricsRelativePath"),
            base_dir,
            warnings,
            "字幕源",
        )
        subtitle_track = _load_track(subtitle_path, warnings)
        subtitle_path, subtitle_track, subtitle_shift_ms = _ensure_usable_subtitle_source(
            lyrics_with_source[0],
            subtitle_path,
            subtitle_track,
            base_dir,
            warnings,
            "字幕源",
            compensation_ms=compensation_ms,
            report=report,
            rebuild_percent=40,
        )
    lyrics_dir = subtitle_path.parent if subtitle_path is not None else base_dir

    # ---------------------------------------------------------------- 画面
    if report is not None:
        report(55, "正在解析素材与样式…")
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
        # 强制顶底是 N3 的固有行位行为（逆向文档：单行页在 Bottom 模式下按
        # 相邻页重叠强制占最下行/上移），不是用户开关——产品默认已改为关闭，
        # N3 导入必须显式固定开启，否则渲染偏离 N3 10.74。
        "force_top_bottom_n3": True,
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
    title_overlays = _build_title_overlays(title_infos, layouts, font_names, warnings)
    if title_overlays:
        changes["title_overlays"] = title_overlays
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
        track = subtitle_track
        if track is None:
            warnings.append("已跳过每行布局、分页与逐字配色导入")
        if track is not None:
            if report is not None:
                report(90, "正在对齐行级数据…")
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
                # 重建源的 .sug 回读已含软件导出补偿，上屏/消失时刻同步平移
                display_time_shift_ms=subtitle_shift_ms,
            )

        # 副字幕源（コーラス等）：与主字幕同时渲染，逐源导入路径 / 每行布局 / 逐字配色。
        total_extras = len(lyrics_with_source) - 1
        for extra_index, info in enumerate(lyrics_with_source[1:], start=1):
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
            extra_track = _load_track(extra_path, warnings)
            (
                extra_path,
                extra_track,
                extra_shift_ms,
            ) = _ensure_usable_subtitle_source(
                info,
                extra_path,
                extra_track,
                base_dir,
                warnings,
                f"字幕源「{name}」",
                # 同名防撞：副源重建文件名带上源名（主源保持纯 <原名>_从N3重建）
                name_suffix=f"_{_safe_file_name(name)}",
                compensation_ms=compensation_ms,
                report=report,
                rebuild_percent=60 + (25 * extra_index) // max(total_extras, 1),
            )
            extra_payload: dict[str, Any] = {"name": name, "path": str(extra_path)}
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
                    display_time_shift_ms=extra_shift_ms,
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
    # N3 的 ARGB PNG 序列（2 含背景 / 3 仅字幕）与本模块的 PNG 序列导出
    # 同语义，直接映射输出格式；只有未压缩 AVI 与未知值回退 MP4。
    if dest_format == 2:
        output["output_format"] = OUTPUT_FORMAT_PNG_COMPOSITED
    elif dest_format == 3:
        output["output_format"] = OUTPUT_FORMAT_PNG_TRANSPARENT
    elif dest_format != 1:
        format_names = {0: "未压缩 AVI"}
        warnings.append(
            f"N3 输出格式「{format_names.get(dest_format, dest_format)}」不支持，"
            "已回退 MP4，请手动设置输出路径"
        )
    dest_path = str(data.get("DestPath") or "").strip()
    if dest_path and dest_format in (1, 2, 3):
        # N3 自动命名「{视频名}_ニコカラメーカー3出力」换成本模块默认后缀；
        # 用户在 N3 里自定义过的文件名原样保留。PNG 序列只取 stem 作导出名。
        path = Path(dest_path)
        if path.stem.endswith(_N3_AUTO_OUTPUT_SUFFIX):
            stem = path.stem[: -len(_N3_AUTO_OUTPUT_SUFFIX)]
            path = path.with_name(f"{stem}{DEFAULT_OUTPUT_NAME_SUFFIX}{path.suffix}")
        output["output_path"] = str(path)

    project_data: dict[str, Any] = {
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "background": background,
        "style": style_to_dict(style),
        "screen": screen,
        # 打开工程直接停在第一套 N3 方案：字体/颜色卡立即显示导入的描边、
        # 装饰和走字前后配色，而不是全局默认的出厂值。没有字体方案时才回
        # 到全局默认。
        "selected_scheme_key": (
            f"custom:{font_names[0]}" if font_names else "global"
        ),
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
    if report is not None:
        report(100, "导入完成")
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
    # AtTagsForSave 实测有「整段压平成单字符数组」与「按行」两种形态，统一归一。
    lines: list[str] = list(_n3_at_tag_lines(info))
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


def _build_title_overlays(
    title_infos: list[dict],
    layouts: list[dict],
    font_names: list[str],
    warnings: list[str],
) -> list[TitleOverlay]:
    """标题 → 文字 / 布局引用 / 显示时段 / 逐字角色（多标题全量导入）。

    N3 的多个 ``TitleInfos`` 逐条导入为「标题 N」条目，每条独立映射文字 /
    布局引用 / 显示时段 / 逐字角色。字体与颜色不展开进 ``TitleOverlay``，
    也不写进内置的「标题」方案：标题用到的每套 フォント設定 都已经作为
    角色方案 append 进 ``custom_style_schemes``，这里只按 ``FontIndex``
    给每个标题字符贴上对应的角色名 —— 包括标题的主字体，这样内置「标题」
    角色保持出厂值，N3 的配色也不会被改名。
    """
    candidates: list[tuple[dict, list[str]]] = []
    for title in title_infos:
        lines = _title_lines(title)
        if any(line.strip() for line in lines):
            candidates.append((title, lines))
    overlays: list[TitleOverlay] = []
    for number, (title, lines) in enumerate(candidates, start=1):
        kwargs: dict[str, Any] = {
            "name": f"标题 {number}",
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
                labels.append(
                    font_names[index] if 0 <= index < len(font_names) else None
                )
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
                warnings.append(
                    "标题显示时段「開始～終了」带首尾偏移，本模块按整段显示导入"
                )
        else:
            kwargs["show_mode"] = "tail"
            kwargs["duration_ms"] = interval
            kwargs["tail_offset_ms"] = tail_offset
        overlays.append(TitleOverlay(**kwargs))
    return overlays


# ---------------------------------------------------------------------------
# 每行布局 / 逐字配色 / 行动画
# ---------------------------------------------------------------------------


def _load_track(subtitle_path: Optional[Path], warnings: list[str]) -> Optional[TimingTrack]:
    if subtitle_path is None or not subtitle_path.is_file():
        return None
    try:
        return load_nicokara_lrc(subtitle_path)
    except Exception:  # noqa: BLE001 — 主窗口加载时会再次报错，这里先记录再交给重建决策
        warnings.append("字幕源文件解析失败")
        return None


# 重建字幕源的固定后缀：N3 内嵌数据物化出的 .sug 一律带此标记，重复导入
# 重新生成同名文件（内容由 n3proj 决定，可重放），不触碰任何其他文件。
_N3_REBUILT_SUFFIX = "_从N3重建"

# 重建 .sug 的 nicokara_tags 内嵌标记键：值是源字幕文件名。撞名时先看既有
# 文件有没有这个标记——有 = 本模块上次重建的产物，可安全覆写；没有 = 用户
# 自己的文件，绝不覆盖（换名重建并提示）。
_N3_REBUILD_MARKER = "n3_rebuild_source"


def _is_n3_rebuilt_sug(path: Path) -> bool:
    """该 ``.sug`` 是否为本模块此前从 N3 数据重建出的产物。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001 — 读不出来就当未知文件，走保护分支
        return False
    tags = data.get("nicokara_tags") if isinstance(data, dict) else None
    return isinstance(tags, dict) and isinstance(
        tags.get(_N3_REBUILD_MARKER), str
    )


def _n3_lyric_line_count(info: dict) -> int:
    """N3 ``LineInfos`` 里 Kind==1 且有可见字符的行数（对应轨道非空行数）。"""
    count = 0
    for line in _list(info.get("LineInfos")):
        line = _dict(line)
        if _int(line.get("Kind"), -1) != 1:
            continue
        if any(str(char.get("Char") or "") for char in _stripped_n3_chars(line)):
            count += 1
    return count


def _n3_at_tag_lines(info: dict) -> list[str]:
    """归一化 N3 的 ``AtTagsForSave``（@ 标签区）为文本行列表。

    实测 10.74 样例（035/036）：**整段 @ 标签文本直接存成一个字符串**（含换
    行）；也有压平成单字符数组、或按行存整行的变体。三种形态统一归一为行
    列表——内容就是 Nicokara LRC 尾部（``@RubyN`` / ``@Emoji`` / ``@Title``
    …）的权威副本。
    """
    value = info.get("AtTagsForSave")
    if isinstance(value, str):
        lines = value.splitlines()
    else:
        items = [str(item) for item in _list(value)]
        if not items:
            return []
        blob = "".join(items)
        # 按行存储的项本身是完整行、不含换行，无分隔拼接后不会出现换行；
        # 压平形态（字符数组）反之。
        lines = blob.splitlines() if ("\n" in blob or "\r" in blob) else items
    return [line.strip() for line in lines if line.strip()]


def _ms_to_nicokara_ts(ms: int) -> str:
    """毫秒 → ``[MM:SS:CC]`` 厘秒时间戳（向下取整，与 N3 导出口径一致）。"""
    total_cs = max(int(ms), 0) // 10
    minutes, rest = divmod(total_cs, 6000)
    seconds, cs = divmod(rest, 100)
    return f"[{minutes:02d}:{seconds:02d}:{cs:02d}]"


def _safe_file_name(value: str) -> str:
    """把源名压成可进文件名的安全形式。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(value or "").strip())
    return cleaned[:32].strip("_") or "源"


def _n3_lrc_body_line(line: dict, synthetic_begin_ms: int = 0) -> Optional[str]:
    """一条 N3 Kind==1 行 → Nicokara LRC 正文行（共享块形式）。

    按 N3 实际导出约定（对拍 035/037 样例真实 LRC 校准）：

    - ``BeginTime <= 0`` 是「无独立时间」哨兵：该字符并入前一个共享块
      （如 ``O``(219ms) + ``NE``(0) → ``[00:02:19]ONE``）。误当成独立起点
      会造成秒级时间错位（曾致重建轨道与 LRC 往返最大 1982ms 偏差）。
    - ``【…】`` 标签段照抄为纯文本（不打时间戳），SUG 解析器将其识别为
      演唱者切换。
    - 块尾时间戳只在两种情况下打：行内停顿（块末 ``EndTime`` 严格早于下一
      锚点起点）或行末结束（末块 ``EndTime`` 有效）。EndTime 缺失的行末不
      打——加载端按「下一行首锚点」借用，与 LRC 解析路径同语义。
    - **全行无锚点**（装饰性信息行，标题/词曲作者等）：N3 自己导出时合成
      锚点 ``[00:00:00]`` 且连续无锚行按 2 秒步进（037 样例实测 0/2000/
      4000ms）。``synthetic_begin_ms`` 由调用方按此规则传入；不锚点的话
      SUG 加载端会把整行当空行丢掉。
    """
    raw_chars = [
        char
        for char in _list(line.get("LyricsCharInfos"))
        if not _dict(char).get("IsRuby") and str(_dict(char).get("Char") or "")
    ]
    if not raw_chars:
        return None
    chars = [_dict(char) for char in raw_chars]
    text = "".join(str(char.get("Char") or "") for char in chars)
    keep = [True] * len(text)
    for match in _BRACKET_LABEL_RE.finditer(text):
        for position in range(match.start(), match.end()):
            keep[position] = False
    entries: list[tuple[str, int, int, bool]] = []
    position = 0
    for char in chars:
        char_text = str(char.get("Char") or "")
        length = len(char_text)
        is_label = not any(keep[position : position + length])
        entries.append(
            (
                char_text,
                _int(char.get("BeginTime"), -1),
                _int(char.get("EndTime"), -1),
                is_label,
            )
        )
        position += length
    if all(is_label for _t, _b, _e, is_label in entries):
        return None

    def next_anchor_begin(index: int) -> Optional[int]:
        for t, b, _e, is_label in entries[index:]:
            if not is_label and b > 0:
                return b
        return None

    has_real_anchor = any(
        not is_label and b > 0 for _t, b, _e, is_label in entries
    )
    pieces: list[str] = []
    if not has_real_anchor:
        # 全行无锚：N3 导出约定合成锚点（连续无锚行 2 秒步进），否则
        # SUG 加载端会把整行当空行丢掉。
        pieces.append(_ms_to_nicokara_ts(synthetic_begin_ms))
        pieces.append("".join(item[0] for item in entries))
        last_end = entries[-1][2]
        if last_end > 0:
            pieces.append(_ms_to_nicokara_ts(last_end))
        return "".join(pieces)
    index = 0
    total = len(entries)
    while index < total:
        char_text, begin, _end, is_label = entries[index]
        if is_label or begin <= 0:
            # 行首无独立时间的字符：纯文本先行，解析端按「前一区间尾部」处理。
            pieces.append(char_text)
            index += 1
            continue
        block_end_index = index
        while block_end_index + 1 < total:
            next_text, next_begin, _next_end, next_is_label = entries[
                block_end_index + 1
            ]
            if next_is_label or (next_begin > 0 and next_begin != begin):
                break
            block_end_index += 1
        pieces.append(_ms_to_nicokara_ts(begin))
        pieces.append(
            "".join(item[0] for item in entries[index : block_end_index + 1])
        )
        block_end = entries[block_end_index][2]
        is_last_block = all(
            is_label or b <= 0
            for _t, b, _e, is_label in entries[block_end_index + 1 :]
        )
        if block_end > 0:
            anchor = next_anchor_begin(block_end_index + 1)
            if is_last_block or (anchor is not None and block_end < anchor):
                pieces.append(_ms_to_nicokara_ts(block_end))
        index = block_end_index + 1
    return "".join(pieces)


def _n3_lrc_text(info: dict) -> tuple[str, bool]:
    """N3 字幕源 → 完整 Nicokara LRC 文本（正文 + AtTagsForSave 尾部）。

    Kind 0/2/3（空行/分页/段落）不产生 LRC 行——实测 N3 自己导出的 LRC 就
    没有空行（035 样例 0 空行 / 74 行正文），分页语义由本导入的
    ``line_breaks_before`` payload 承载，与 LRC 路径完全一致。
    """
    body: list[str] = []
    has_inline_ruby = False
    unanchored_count = 0
    for line in _list(info.get("LineInfos")):
        line = _dict(line)
        kind = _int(line.get("Kind"), -1)
        if kind != 1:
            continue
        has_inline_ruby = has_inline_ruby or any(
            _dict(char).get("IsRuby")
            for char in _list(line.get("LyricsCharInfos"))
        )
        no_anchor = _n3_line_has_no_real_anchor(line)
        body_line = _n3_lrc_body_line(
            line, synthetic_begin_ms=2000 * unanchored_count
        )
        unanchored_count = unanchored_count + 1 if no_anchor else 0
        if body_line is not None:
            body.append(body_line)
    return "\n".join(body + _n3_at_tag_lines(info)) + "\n", has_inline_ruby


def _n3_line_has_no_real_anchor(line: dict) -> bool:
    """该 Kind==1 行是否没有任何 ``BeginTime > 0`` 的非标签字符。"""
    return not any(
        _int(char.get("BeginTime"), -1) > 0
        for char in _stripped_n3_chars(line)
    )


_N3_META_TAG_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "taggingby": "tagging_by",
    "silencemsec": "silence_ms",
    "headoffset": "head_offset",
}


def _sug_project_from_n3(
    info: dict,
) -> tuple[Optional[Project], Optional[dict[str, object]], bool]:
    """N3 字幕源 → SUG :class:`Project`（走 SUG 官方 Nicokara 导入管线）。

    把 ``LineInfos`` 合成 Nicokara LRC 文本后交给 SUG 自己的
    ``NicokaraParser`` + ``nicokara_result_to_sentences``：ruby（``@RubyN``
    标签，含 mora 时间与位置窗）、演唱者（``【…】`` 标签 / ``@Emoji`` 定义）、
    共享块均分、句中停顿、行末释放全部按 SUG 原生约定落地——与「SUG 导出
    LRC → SUG 再导入」的既有工作流同构，不自造第二套映射。

    返回 ``(project, nicokara_tags, has_inline_ruby)``；N3 行数据里没有歌词
    行时 project 为 None。``has_inline_ruby`` 指字符流里出现的 ``IsRuby``
    内嵌注音字（实测样例均为 0，ruby 实际以 ``@RubyN`` 标签承载——该形态
    无法无损关联基底字，调用方负责提示）。
    """
    lrc_text, has_inline_ruby = _n3_lrc_text(info)
    result = NicokaraParser().parse(lrc_text)
    if not any(line.text for line in result.lines):
        return None, None, has_inline_ruby

    singer_keys: set[str] = set(result.singer_definitions)
    for line in result.lines:
        if line.line_singer_key:
            singer_keys.add(line.line_singer_key)
        for _index, key in line.char_singer_map.items():
            singer_keys.add(key)

    singer_colors = [
        "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
        "#C9B1FF", "#F7DC6F", "#82E0AA", "#F1948A", "#85C1E9",
    ]
    singers: list[Singer] = []
    singer_key_to_id: dict[str, str] = {}
    for index, key in enumerate(sorted(singer_keys)):
        singer = Singer(
            name=result.singer_definitions.get(key, key) or key,
            color=singer_colors[index % len(singer_colors)],
            is_default=index == 0,
        )
        singer_key_to_id[key] = singer.id
        singers.append(singer)
    if not singers:
        # 工作台把「未命名」占位歌手视为无角色标签（与 LRC 路径的裸行一致）。
        placeholder = Singer(
            name="未命名",
            color="#FF6B6B",
            is_default=True,
            is_placeholder=True,
            backend_number=1,
        )
        singers.append(placeholder)
    default_singer_id = singers[0].id

    sentences = nicokara_result_to_sentences(
        result, singer_key_to_id, default_singer_id
    )
    metadata = {
        key.lower(): value for key, value in (result.metadata or {}).items()
    }
    project = Project(
        singers=singers,
        sentences=sentences,
        metadata=ProjectMetadata(
            title=str(metadata.get("title") or ""),
            artist=str(metadata.get("artist") or ""),
        ),
    )

    # .sug 的 nicokara_tags：结构化键（@HeadOffset 会在 .sug 加载端烘焙进行
    # 首时间戳，与 LRC 路径同语义）+ @Emoji 等原文行（加载端按其重插头像）。
    # @RubyN 不进 custom——注音已物化到字符上，避免重复表示。
    tags: dict[str, object] = {}
    for source_key, tag_key in _N3_META_TAG_KEYS.items():
        value = metadata.get(source_key)
        if value not in (None, ""):
            tags[tag_key] = value
    custom = [
        line
        for line in _n3_at_tag_lines(info)
        if not line.upper().startswith("@RUBY")
        and line.split("=", 1)[0].strip("@").lower() not in _N3_META_TAG_KEYS
    ]
    if custom:
        tags["custom"] = custom
    return project, (tags or None), has_inline_ruby


def _rebuild_sug_source(
    info: dict,
    base_dir: Path,
    warnings: list[str],
    label: str,
    *,
    name_suffix: str = "",
    compensation_ms: int = 0,
    report: Optional[ProgressCallback] = None,
    rebuild_percent: int = 0,
) -> Optional[tuple[Path, TimingTrack]]:
    """按 N3 内嵌数据在 n3proj 同目录落盘新 ``.sug``，并返回其解析轨道。

    ``name_suffix`` 用于多字幕源同名防撞：副源传入 ``_<SettingsName>``。
    目标文件名若已存在，先校验它是否也是本模块从 N3 重建出的产物（内嵌
    ``n3_rebuild_source`` 标记）——是则安全覆写（内容可由 n3proj 重放），
    不是（用户自己的同名文件）则绝不覆盖，顺延 ``_2``/``_3``… 换名并提示。
    回读走 :func:`load_sug_timing_track` 并叠加 ``compensation_ms``（SUG
    「软件导出补偿」预设）——与主窗口加载 ``.sug`` 的口径一致，解析后所有
    时间戳即含该补偿。
    """
    project, tags, has_inline_ruby = _sug_project_from_n3(info)
    if project is None:
        return None
    relative_text = str(info.get("SourceLyricsRelativePath") or "").strip()
    absolute_text = str(info.get("SourceLyricsPath") or "").strip()
    source_name = Path(relative_text or absolute_text or "歌词")
    target_dir = base_dir / source_name.parent if relative_text else base_dir
    target = target_dir / (
        f"{source_name.stem}{name_suffix}{_N3_REBUILT_SUFFIX}.sug"
    )
    target, renamed = _claim_rebuild_target(target)
    if renamed:
        warnings.append(
            f"{label}重建文件名与既有文件冲突（该文件不是本模块 N3 重建产物，"
            f"未覆盖），已改用：{target.name}"
        )
    # 内嵌重建标记：既标识产物归属（撞名校验），也记录源字幕名便于追溯。
    tags = dict(tags or {})
    tags[_N3_REBUILD_MARKER] = source_name.name

    def save_stage(stage: str) -> None:
        if report is not None:
            report(rebuild_percent, f"正在重建{label}：{stage}")

    if report is not None:
        report(rebuild_percent, f"正在按 N3 数据重建{label}…")
    try:
        SugProjectParser.save(
            project, str(target), nicokara_tags=tags, progress_cb=save_stage
        )
        # 回读落盘文件（而非内存对象），保证行级 payload 对齐的就是应用后续
        # 加载的同一条轨道；补偿值与主窗口 .sug 加载口径一致。
        track = load_sug_timing_track(
            target, software_compensation_ms=compensation_ms
        )
    except Exception as exc:  # noqa: BLE001 — 重建任何一步失败都退回原行为
        warnings.append(f"{label}无法按 N3 数据重建 .sug 字幕源（{exc}）")
        return None
    if has_inline_ruby:
        warnings.append(
            f"{label}字符流内嵌注音（IsRuby）无法无损关联基底字，已跳过该部分注音"
        )
    return target, track


def _claim_rebuild_target(target: Path) -> tuple[Path, bool]:
    """为重建产物占用目标文件名；撞到非重建产物时顺延换名。

    返回 ``(最终路径, 是否换名)``。同名但带重建标记的旧产物可直接覆写
    （内容由 n3proj 决定，可重放）；连续顺延 99 次仍撞名则放弃（视为异常
    环境交由上层失败处理）。
    """
    if not target.is_file() or _is_n3_rebuilt_sug(target):
        return target, False
    for attempt in range(2, 100):
        candidate = target.with_name(f"{target.stem}_{attempt}{target.suffix}")
        if not candidate.is_file() or _is_n3_rebuilt_sug(candidate):
            return candidate, True
    return target, True


def _ensure_usable_subtitle_source(
    info: dict,
    path: Optional[Path],
    track: Optional[TimingTrack],
    base_dir: Path,
    warnings: list[str],
    label: str,
    *,
    name_suffix: str = "",
    compensation_ms: int = 0,
    report: Optional[ProgressCallback] = None,
    rebuild_percent: int = 0,
) -> tuple[Optional[Path], Optional[TimingTrack], int]:
    """字幕文件缺失或行数与 N3 记录不一致时，以 N3 数据为准重建 ``.sug``。

    N3 的 ``LineInfos`` 本身就是完整歌词快照（逐字文本 + 逐字时间），文件
    丢失或被改动到行数对不上时，原行为是把布局/分页/逐字配色等行级信息
    整体丢掉。这里改为以 N3 数据为准：物化成 ``<原文件名>_从N3重建.sug``
    落在 n3proj 同目录，工程字幕源改指它——后续保存/重载都走 SUG 高保真
    路径，不再经历 LRC 有损往返。原字幕文件一律不改动。无法重建（N3 无
    行数据/写盘失败）时返回原状，行级导入退回按文本对齐的兜底路径。

    返回 ``(path, track, applied_shift_ms)``：重建成功时 ``applied_shift_ms``
    是随 .sug 回读叠加的软件导出补偿值（供行级 display 等绝对时刻同步
    平移），未重建时为 0。
    """
    n3_count = _n3_lyric_line_count(info)
    if n3_count <= 0:
        return path, track, 0
    if track is not None and (
        sum(1 for line in track.lines if not line.is_blank) == n3_count
    ):
        return path, track, 0
    rebuilt = _rebuild_sug_source(
        info,
        base_dir,
        warnings,
        label,
        name_suffix=name_suffix,
        compensation_ms=compensation_ms,
        report=report,
        rebuild_percent=rebuild_percent,
    )
    if rebuilt is None:
        return path, track, 0
    sug_path, sug_track = rebuilt
    if track is None:
        if path is not None and path.is_file():
            warnings.append(f"{label}解析失败，已改用按 N3 数据重建的字幕源：{sug_path}")
        else:
            warnings.append(
                f"{label}文件不存在，已按 N3 内嵌歌词数据重建：{sug_path}"
            )
    else:
        our_count = sum(1 for line in track.lines if not line.is_blank)
        warnings.append(
            f"歌词行数与 N3 项目记录不一致（歌词 {our_count} 行 / N3 记录 "
            f"{n3_count} 行），已按 N3 数据重建字幕源：{sug_path}"
            "（原歌词文件未改动）"
        )
    return sug_path, sug_track, compensation_ms


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
    # NFC 与本模块解析行（组合浊点已合成 デ 等）的码点口径一致：
    # emoji 触发词按拼接串 find 定位，书写形式不同会逐字漂移。
    return unicodedata.normalize(
        "NFC", "".join(str(char.get("Char") or "") for char in chars)
    )


def _is_synthetic_emoji_tag_char(text: str) -> bool:
    """是否是 LRC 解析为行内 emoji 头像插入的合成标签字符。

    正文解析会把 ``【…】`` 剥成角色标签，真实字符不可能是完整标签文本；
    因此 ``chars`` 里出现整段标签文本只能来自 emoji 行内替换的插入。
    """
    return _BRACKET_LABEL_RE.fullmatch(text) is not None


def _source_text_offsets(line: TimingLine) -> dict[int, int]:
    offsets: dict[int, int] = {}
    position = 0
    for index, char in enumerate(line.chars):
        if _is_synthetic_emoji_tag_char(char.text):
            continue
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
    # 源解析（load_nicokara_lrc）已把 emoji 应用到行上：首个标签 → 行首 anchored
    # 头像，其余标签 → 行内插入头像字符。本函数的 payload 会整体覆盖这些字段，
    # 因此先把行上已有状态序列化进来，再用 N3 可见字符触发（如 ♪）叠加。
    guide_row: Optional[dict[str, object]] = (
        guide_symbol_to_dict(our_line.guide_symbol)
        if our_line.guide_symbol is not None
        else None
    )
    inline_row: dict[str, object] = {
        str(index): guide_symbol_to_dict(symbol)
        for index, symbol in our_line.inline_guide_symbols.items()
    }
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
        if (
            guide_row is None
            and raw_text.find(spec.trigger) >= 0
            # 源解析已把该标签原位替换为头像字符（payload 无法造字符，只能兜底
            # 「LRC 缺标签但 N3 字符数据里有」的情况），再给行首头像会双重呈现。
            and not any(char.text == spec.trigger for char in our_line.chars)
        ):
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


def _n3_line_key_text(line: dict) -> str:
    """N3 行的对齐键：去 ruby、去 ``【…】`` 标签段后的可见正文。"""
    return unicodedata.normalize(
        "NFC",
        "".join(
            str(char.get("Char") or "") for char in _stripped_n3_chars(line)
        ),
    )


def _our_line_key_text(line: TimingLine) -> str:
    """解析行的对齐键：去行内 emoji 头像插入的合成标签字符。"""
    return unicodedata.normalize(
        "NFC",
        "".join(
            char.text
            for char in line.chars
            if not _is_synthetic_emoji_tag_char(char.text)
        ),
    )


def _align_line_pairs(
    our_texts: list[str], n3_texts: list[str]
) -> list[tuple[int, int]]:
    """按行文本做 LCS 对齐，返回 ``(our 序, n3 序)`` 匹配对（保持相对顺序）。

    歌词文件在 N3 保存后被增删行时行号即错位，整段全等或整体放弃都会丢数据；
    按文本对齐后，未改动的行（含副歌重复行，按出现顺序配对）仍能带上
    N3 里的布局 / 分页 / 逐字配色。行数规模为数百，O(n·m) DP 足够快。
    """
    our_count, n3_count = len(our_texts), len(n3_texts)
    if not our_count or not n3_count:
        return []
    dp = [[0] * (n3_count + 1) for _ in range(our_count + 1)]
    for i in range(our_count - 1, -1, -1):
        row = dp[i]
        below = dp[i + 1]
        our_text = our_texts[i]
        for j in range(n3_count - 1, -1, -1):
            if our_text == n3_texts[j]:
                row[j] = below[j + 1] + 1
            else:
                row[j] = max(below[j], row[j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < our_count and j < n3_count:
        if our_texts[i] == n3_texts[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _per_line_payloads(
    line_infos: list[dict],
    track: TimingTrack,
    layout_limit: int,
    layout_row_counts: list[int],
    font_names: list[str],
    default_animation: tuple[str, int, str, int],
    warnings: list[str],
    emoji_specs: list[_N3EmojiSpec] | None = None,
    display_time_shift_ms: int = 0,
) -> tuple[
    Optional[list[int]],
    Optional[list[str]],
    Optional[list[Optional[list[Optional[str]]]]],
    Optional[list[Optional[dict[str, object]]]],
    Optional[list[Optional[list[Optional[int]]]]],
    Optional[list[Optional[dict[str, object]]]],
    Optional[list[Optional[dict[str, object]]]],
]:
    """对齐 N3 歌词行与本模块解析行，导出布局，分页与逐字配色。

    N3 ``LineInfos`` 里 Kind==1（Lyrics）的行与 LRC 非空行一一对应（空行/分页
    在 N3 里是 Empty/PageBreak，段落分隔 ParagraphBreak 是运行时插入的）。
    歌词文件在 N3 保存后被改动（行数增减 / 个别行改词）时按行文本 LCS 对齐：
    对齐上的行照常导入，对不上的行保持默认值并提示，不再整段放弃。
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
    pairs = _align_line_pairs(
        [_our_line_key_text(line) for _, line in our_indexed],
        [_n3_line_key_text(line) for line in n3_lines],
    )
    if len(pairs) < len(our_indexed) or len(pairs) < len(n3_lines):
        counts = (
            f"歌词 {len(our_indexed)} 行、N3 记录 {len(n3_lines)} 行"
            if len(our_indexed) != len(n3_lines)
            else f"共 {len(our_indexed)} 行"
        )
        warnings.append(
            f"歌词与 N3 项目记录不一致（{counts}，歌词文件可能已改动）："
            f"已按文本对齐 {len(pairs)} 行并导入其布局与逐字配色，"
            "未对齐的行保持默认值"
        )

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
    for our_position, n3_position in pairs:
        line_index, our_line = our_indexed[our_position]
        n3_line = n3_lines[n3_position]
        break_payload[line_index] = n3_breaks_before[n3_position]
        page_layout_index = page_layout_indices[n3_position]
        n3_chars = _stripped_n3_chars(n3_line)
        n3_text = "".join(str(char.get("Char") or "") for char in n3_chars)
        # 行内 emoji 头像插入的合成标签字符不属于 N3 可见正文，比对与逐字行走都要跳过。
        our_text = "".join(
            char.text
            for char in our_line.chars
            if not _is_synthetic_emoji_tag_char(char.text)
        )
        if n3_text != our_text:
            # LCS 匹配键与这里的比对口径一致，理论上不会触发；留作防线，
            # 宁可单行退回默认值也不让逐字配色错位。
            continue
        layout_payload[line_index] = page_layout_index
        show_begin = n3_line.get("ShowBeginTime")
        show_end = n3_line.get("ShowEndTime")
        if isinstance(show_begin, (int, float)) or isinstance(show_end, (int, float)):
            # 重建源的 .sug 回读叠加了软件导出补偿，上屏/消失时刻同步平移，
            # 保持与轨道时间同一坐标。
            display_payload[line_index] = [
                (
                    int(show_begin) + display_time_shift_ms
                    if isinstance(show_begin, (int, float))
                    else None
                ),
                (
                    int(show_end) + display_time_shift_ms
                    if isinstance(show_end, (int, float))
                    else None
                ),
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
            if _is_synthetic_emoji_tag_char(our_char.text):
                # 合成头像字符不对应 N3 字符：保留解析出的角色标签，不推进 N3 位置。
                labels.append(our_char.role_label)
                continue
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
    return (
        layout_payload,
        break_payload,
        role_payload,
        animation_payload if any(item is not None for item in animation_payload) else None,
        display_payload if any(item is not None for item in display_payload) else None,
        guide_payload if any(item is not None for item in guide_payload) else None,
        inline_guide_payload if any(item is not None for item in inline_guide_payload) else None,
    )
