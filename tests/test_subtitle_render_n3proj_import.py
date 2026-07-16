"""NicoKaraMaker3 项目（.n3proj）导入：zip/JSON 解析与字段映射。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from krok_helper.subtitle_render.models import style_from_dict
from krok_helper.subtitle_render.n3proj_import import (
    N3ImportResult,
    is_n3proj_file,
    load_n3proj,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc


def _size(px: int, reference: int = 1080) -> dict:
    return {"Size": px, "Reference": reference, "Ratio": px / reference}


def _dxcolor(r: float, g: float, b: float, a: float = 1) -> dict:
    return {"R": r, "G": g, "B": b, "A": a}


def _solid_brush(name: str, hex6: str, r: float, g: float, b: float) -> dict:
    return {
        "SelectedBrushTypeIndex": 0,
        "SolidColor": {"DxColor": _dxcolor(r, g, b), "Web16": hex6},
        "GradientStops": [],
        "BitmapPath": "",
        "BitmapScale": 100,
        "SettingsName": name,
    }


def _font_info(name: str, font: str, face: str, char: int, edge: int, edge2: int,
               fallback: str, use_edge2=None) -> dict:
    return {
        "FontName": font,
        "FontFaceName": face,
        "CharSize": _size(char),
        "EdgeSize": _size(edge),
        "UseEdge2": use_edge2,
        "EdgeSize2": _size(edge2),
        "FallbackName": fallback,
        "SettingsName": name,
    }


def _lyrics_font(name: str, *, decor_kind: int = 1, decor_size: int = 5,
                 blur_level: int = 0, after_text: dict | None = None,
                 use_edge2=None) -> dict:
    brushes = [
        after_text or _solid_brush("ワイプ後／文字色", "FF0000", 1, 0, 0),
        _solid_brush("縁取り色", "FFFFFF", 1, 1, 1),
        _solid_brush("縁取り 2 色", "000000", 0, 0, 0),
        _solid_brush("飾り色", "000000", 0, 0, 0),
        _solid_brush("ワイプ前／文字色", "FFFFFF", 1, 1, 1),
        _solid_brush("縁取り色", "000000", 0, 0, 0),
        _solid_brush("縁取り 2 色", "FFFFFF", 1, 1, 1),
        _solid_brush("飾り色", "26386A", 0.14901961, 0.21960784, 0.41568628),
    ]
    return {
        "BrushInfos": brushes,
        "FontInfos": [
            _font_info("歌詞／漢字", "UD デジタル 教科書体 N-B", "Bold", 100, 15, 5,
                       "デフォルトフォント", use_edge2),
            _font_info("かな", "", "", 0, 0, 0, "歌詞／漢字"),
            _font_info("英数", "Comic Sans MS", "Negreta", 0, 0, 0, "歌詞／漢字"),
            _font_info("ルビ／漢字", "", "", 45, 10, 3, "歌詞／漢字", use_edge2),
            _font_info("かな", "", "", 0, 0, 0, "ルビ／漢字"),
            _font_info("英数", "", "", 0, 0, 0, "ルビ／漢字"),
        ],
        "DecorKind": decor_kind,
        "DecorSize": _size(decor_size),
        "BlurLevel": blur_level,
        "SettingsName": name,
    }


def _layout(name: str, va: int, aligns: list[int], *, line_space: int = 85,
            v_margin: int = 50, h_margin: int = 50, smart: int = 2,
            lyrics_interval: int = 0) -> dict:
    return {
        "SelectedVerticalAlignmentIndex": va,
        "LineSpace": _size(line_space),
        "SmartHorizon": smart,
        "VerticalMargin": _size(v_margin),
        "HorizontalMargin": _size(h_margin),
        "HorizontalAlignments": [{"HorizontalLayoutAlignment": a} for a in aligns],
        "LyricsInterval": _size(lyrics_interval),
        "AllowBiting": False,
        "RubyInterval": _size(0),
        "RubyAlignment": 0,
        "LyricsAndRubyInterval": _size(0),
        "SettingsName": name,
    }


def _char(ch: str, begin: int, end: int, font_index: int = 0) -> dict:
    return {"Kind": 0, "Char": ch, "BeginTime": begin, "EndTime": end,
            "FontIndex": font_index, "IsRuby": False}


LRC_TEXT = (
    "[00:01:00]あ[00:02:00]い[00:03:00]\n"
    "\n"
    "[00:05:00]う[00:06:00]え[00:07:00]\n"
)


def _line_info(chars: list[dict], layout_index: int = 0,
               action_id: str = "SHINTA.LineFadeInFadeOut") -> dict:
    return {
        "Kind": 1,
        "LyricsCharInfos": chars,
        "SubtitleActionId": action_id,
        "SubtitleActionSettings": {"FadeInTime": 250, "FadeOutTime": 300},
        "LayoutIndex": layout_index,
        "Raw": "",
    }


def _project_payload(tmp_path: Path) -> dict:
    lrc = tmp_path / "demo.lrc"
    lrc.write_text(LRC_TEXT, encoding="utf-8")
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"fake")
    return {
        "SourceInfo": {
            "SourceKind": 0,
            "MoviePath": str(video),
            "MovieRelativePath": "demo.mp4",
            "BackgroundWidth": 1920,
            "BackgroundHeight": 1080,
            "Fps": 60,
            "SoundPath": None,
        },
        "SourceLyricsInfos": [
            {
                "SourceLyricsPath": str(lrc),
                "SourceLyricsRelativePath": "demo.lrc",
                "LineInfos": [
                    _line_info(
                        [_char("あ", 1000, 2000), _char("い", 2000, 3000, font_index=1)],
                        layout_index=1,
                    ),
                    {"Kind": 2, "LyricsCharInfos": [], "LayoutIndex": -1, "Raw": ""},
                    _line_info([_char("う", 5000, 6000), _char("え", 6000, 7000)]),
                ],
                "SettingsName": "メイン",
            },
        ],
        "TitleInfos": [
            {
                "ShowTime": {"Kind": 0, "HeadOffset": 0, "HeadEnd": 5999990,
                             "Interval": 10000, "TailOffset": 0},
                "LayoutIndex": 1,
                "LineInfos": [
                    {"Kind": 5, "LyricsCharInfos": [
                        _char("曲", 5999990, 5999990, font_index=1),
                        _char("名", 5999990, 5999990, font_index=1),
                    ]},
                ],
                "SettingsName": "タイトル1",
            },
        ],
        "LyricsFonts": [
            _lyrics_font("標準配色", decor_kind=1, decor_size=5),
            _lyrics_font(
                "青配色",
                decor_kind=2,
                decor_size=10,
                after_text={
                    "SelectedBrushTypeIndex": 1,
                    "SolidColor": {"DxColor": _dxcolor(0, 0, 1), "Web16": "0000FF"},
                    "GradientStops": [
                        {"Position": 0, "Color": _dxcolor(1, 1, 1)},
                        {"Position": 1, "Color": _dxcolor(0, 0, 1)},
                    ],
                    "BitmapPath": "",
                    "BitmapScale": 100,
                    "SettingsName": "ワイプ後／文字色",
                },
            ),
        ],
        "LyricsLayouts": [
            _layout("下寄せ2行", 2, [0, 2], v_margin=90, line_space=80),
            _layout("タイトル左上", 0, [0], line_space=15),
        ],
        "DestPath": str(tmp_path / "out.mp4"),
        "DestFormat": 1,
    }


def _write_n3proj(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "demo.n3proj"
    raw = "﻿" + json.dumps(payload, ensure_ascii=False)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("0", raw.encode("utf-8"))
    return path


@pytest.fixture()
def imported(tmp_path) -> N3ImportResult:
    return load_n3proj(_write_n3proj(tmp_path, _project_payload(tmp_path)))


def test_is_n3proj_file():
    assert is_n3proj_file("a.n3proj")
    assert is_n3proj_file(Path("A.N3PROJ"))
    assert not is_n3proj_file("a.yurika")


def test_load_n3proj_rejects_non_zip(tmp_path):
    bad = tmp_path / "bad.n3proj"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        load_n3proj(bad)


def test_load_n3proj_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.n3proj"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0", b"{ not json")
    with pytest.raises(ValueError):
        load_n3proj(path)


def test_import_media_and_screen(imported, tmp_path):
    data = imported.project_data
    assert data["subtitle_path"] == str(tmp_path / "demo.lrc")
    assert data["video_path"] == str(tmp_path / "demo.mp4")
    assert data["audio_path"] is None
    assert data["background"] == {
        "kind": "video",
        "path": str(tmp_path / "demo.mp4"),
        "color": "#000000",
        "source_fps": None,
        "sequence_start_number": 0,
        "video_offset_ms": 0,
    }
    assert data["screen"] == {"width": 1920, "height": 1080, "fps": 60, "par": "1:1"}
    assert data["output"]["output_path"] == str(tmp_path / "out.mp4")
    # N3 PageBreak 在 LRC 的第二个歌词行前开启新页（中间空行也保留槽位）。
    assert data["line_breaks_before"] == ["none", "none", "page"]


def test_import_maps_n3_auto_output_name_to_yurika_suffix(tmp_path):
    payload = _project_payload(tmp_path)
    payload["DestPath"] = str(tmp_path / "demo_ニコカラメーカー3出力.mp4")

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    # N3 自动命名换成本模块默认后缀；目录保持不变
    assert result.project_data["output"]["output_path"] == str(
        tmp_path / "demo_yurika出力.mp4"
    )


def test_video_background_ignores_independent_sound_path(tmp_path):
    payload = _project_payload(tmp_path)
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    payload["SourceInfo"]["SoundPath"] = str(audio)
    payload["SourceInfo"]["SoundRelativePath"] = audio.name

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["audio_path"] is None
    assert any("视频背景不使用独立音频" in warning for warning in result.warnings)


def test_import_global_style_font_and_colors(imported):
    style = style_from_dict(imported.project_data["style"])
    assert style.font_family == "UD デジタル 教科書体 N-B"
    assert style.font_family_latin == "Comic Sans MS"
    assert style.font_size_px == 100
    assert style.font_weight == 700
    assert style.stroke_width_px == 15
    # UseEdge2 全链 None → N3 不绘制二重描边
    assert style.stroke2_enabled is False
    assert style.stroke2_width_px == 5
    # 子槽的 0/null 保持为继承状态，不再物化成根槽的有效值。
    assert style.latin_font_size_px is None
    assert style.latin_stroke_width_px is None
    assert style.latin_stroke2_enabled is None
    assert style.latin_stroke2_width_px is None
    # DecorKind.Shadow → 右下偏移 DecorSize
    assert style.decoration_kind == "shadow"
    assert style.shadow_offset_x == 5
    assert style.shadow_offset_y == 5
    colors = style.karaoke_colors
    assert colors is not None
    assert colors.after.text.color == "#FF0000"
    assert colors.before.text.color == "#FFFFFF"
    assert colors.before.shadow.color == "#26386A"
    # ルビ：空字体/字面继续显示 0 并跟随主文字；字号与描边保留显式值。
    assert style.ruby_font_follow_main is True
    assert style.ruby_font_family is None
    assert style.ruby_font_weight is None
    assert style.ruby_font_family_latin is None
    assert style.ruby_font_size_px == 45
    assert style.ruby_stroke_width_px == 10
    assert style.ruby_stroke2_enabled is None
    assert style.ruby_stroke2_width_px == 3
    assert style.ruby_latin_font_size_px is None
    assert style.ruby_latin_font_weight is None
    assert style.ruby_latin_stroke_width_px is None
    assert style.ruby_latin_stroke2_enabled is None
    assert style.ruby_latin_stroke2_width_px is None
    assert style.ruby_karaoke_colors is not None


def test_import_applies_explicit_latin_and_ruby_latin_strokes(tmp_path):
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[2].update(
        CharSize=_size(72),
        EdgeSize=_size(8),
        UseEdge2=True,
        EdgeSize2=_size(4),
    )
    infos[5].update(
        CharSize=_size(30),
        EdgeSize=_size(6),
        UseEdge2=False,
        EdgeSize2=_size(2),
    )

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert style.latin_font_size_px == 72
    assert style.latin_stroke_width_px == 8
    assert style.latin_stroke2_enabled is True
    assert style.latin_stroke2_width_px == 4
    assert style.ruby_latin_font_size_px == 30
    assert style.ruby_latin_stroke_width_px == 6
    assert style.ruby_latin_stroke2_enabled is False
    assert style.ruby_latin_stroke2_width_px == 2


def test_import_ignores_kana_slots_and_uses_japanese_settings(tmp_path):
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    for index in (1, 4):
        infos[index].update(
            FontName="Kana Only Font",
            FontFaceName="Black",
            CharSize=_size(222),
            EdgeSize=_size(33),
            UseEdge2=True,
            EdgeSize2=_size(11),
        )

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert style.font_family == "UD デジタル 教科書体 N-B"
    assert style.font_size_px == 100
    assert style.stroke_width_px == 15
    assert style.stroke2_enabled is False
    assert style.ruby_font_family is None
    assert style.ruby_font_weight is None
    assert style.ruby_font_size_px == 45
    assert style.ruby_stroke_width_px == 10
    assert style.ruby_stroke2_enabled is None
    assert not any("かな" in warning or "假名" in warning for warning in result.warnings)


def test_import_preserves_custom_scheme_local_fallbacks(tmp_path):
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][1]["FontInfos"]
    infos[0].update(FontName="游明朝", FontFaceName="Bold")
    infos[2].update(FontName="", FontFaceName="Black")
    infos[3].update(FontName="", FontFaceName="")
    infos[5].update(FontName="", FontFaceName="Black")

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    scheme = style.custom_style_schemes["青配色"]

    # Empty child slots remain visible as zero/empty while the marker tells the
    # renderer to resolve them inside this scheme, never from global Comic Sans.
    assert scheme.n3_font_inheritance is True
    assert scheme.font_family == "游明朝"
    assert scheme.font_family_latin is None
    assert scheme.ruby_font_family is None
    assert scheme.ruby_font_family_latin is None
    assert scheme.font_weight == 700
    assert scheme.latin_font_weight is None
    assert scheme.ruby_font_weight is None
    assert scheme.ruby_latin_font_weight is None
    assert scheme.italic is False
    assert scheme.font_size_px == 100
    assert scheme.latin_font_size_px is None
    assert scheme.ruby_font_size_px == 45
    assert scheme.ruby_latin_font_size_px is None
    assert scheme.stroke_width_px == 15
    assert scheme.latin_stroke_width_px is None
    assert scheme.ruby_stroke_width_px == 10
    assert scheme.ruby_latin_stroke_width_px is None


def test_import_layouts(imported):
    style = style_from_dict(imported.project_data["style"])
    # LyricsLayouts[0] → 默认布局（Style 本体字段）
    assert style.line_y_position == "bottom"
    assert style.line_y_margin_px == 90
    assert style.line_gap_px == 80
    assert style.horizontal_margin_px == 50
    assert style.smart_horizontal == "equal_margins"
    assert style.line_alignments == ["left", "right"]
    assert style.font_reference_height == 1080
    assert style.layout_reference_height == 1080
    # LyricsLayouts[1:] → Style.layouts
    assert [layout.name for layout in style.layouts] == ["タイトル左上"]
    assert style.layouts[0].line_y_position == "top"
    assert style.layouts[0].line_alignments == ["left"]
    assert style.layouts[0].letter_spacing_px == 0
    assert style.layouts[0].allow_biting is False
    assert style.layouts[0].ruby_interval_px == 0
    assert style.layouts[0].ruby_alignment == "auto"
    assert style.layouts[0].ruby_gap_px == 0
    assert not any("全局设置" in warning for warning in imported.warnings)


def test_import_layout_character_spacing_per_layout(tmp_path):
    payload = _project_payload(tmp_path)
    extra = payload["LyricsLayouts"][1]
    extra["LyricsInterval"] = _size(-6)
    extra["AllowBiting"] = True
    extra["RubyInterval"] = _size(3)
    extra["RubyAlignment"] = 1
    extra["LyricsAndRubyInterval"] = _size(-2)
    payload["SourceLyricsInfos"][0]["LineInfos"][0]["LayoutIndex"] = 1

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    layout = style.layouts[0]

    assert layout.letter_spacing_px == -6
    assert layout.allow_biting is True
    assert layout.ruby_interval_px == 3
    assert layout.ruby_alignment == "center"
    assert layout.ruby_gap_px == -2
    assert result.project_data["line_layout_indices"][0] == 1
    assert not any("全局设置" in warning for warning in result.warnings)


def test_import_custom_scheme_with_gradient(imported):
    style = style_from_dict(imported.project_data["style"])
    assert "青配色" in style.custom_style_schemes
    scheme = style.custom_style_schemes["青配色"]
    # DecorKind.Blur → 发光
    assert scheme.decoration_kind == "glow"
    assert scheme.glow_radius_px == 10
    fill = scheme.karaoke_colors.after.text
    assert fill.mode == "gradient_vertical"
    assert fill.gradient_stops[0] == (0, "#FFFFFF")
    assert fill.gradient_stops[-1] == (100, "#0000FF")


def test_import_preserves_fractional_gradient_stop_positions(tmp_path):
    payload = _project_payload(tmp_path)
    brush = payload["LyricsFonts"][0]["BrushInfos"][0]
    brush["SelectedBrushTypeIndex"] = 1
    brush["GradientStops"] = [
        {"Position": 0.0, "Color": _dxcolor(1, 0, 0)},
        {"Position": 0.333333, "Color": _dxcolor(0, 1, 0)},
        {"Position": 1.0, "Color": _dxcolor(0, 0, 1)},
    ]

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert style.karaoke_colors.after.text.gradient_stops == [
        (0, "#FF0000"),
        (33.3333, "#00FF00"),
        (100, "#0000FF"),
    ]


def test_import_mille_feuille_uses_exact_fractional_hard_bands(tmp_path):
    payload = _project_payload(tmp_path)
    brush = payload["LyricsFonts"][0]["BrushInfos"][0]
    brush["SelectedBrushTypeIndex"] = 2
    brush["GradientStops"] = [
        {"Position": 0.0, "Color": _dxcolor(1, 1, 1)},
        {"Position": 0.333333, "Color": _dxcolor(1, 0, 0)},
        {"Position": 0.777777, "Color": _dxcolor(0, 0, 1)},
        # N3 treats the final source color as a position sentinel only.
        {"Position": 1.0, "Color": _dxcolor(0, 0, 0)},
    ]

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    fill = style.karaoke_colors.after.text

    assert fill.mode == "split_vertical"
    assert fill.split_stops == [
        (0, "#FFFFFF"),
        (33.3333, "#FF0000"),
        (77.7777, "#0000FF"),
        (100, "#0000FF"),
    ]


def test_import_preserves_dxcolor_alpha_for_all_font_brush_layers(tmp_path):
    payload = _project_payload(tmp_path)
    brushes = payload["LyricsFonts"][0]["BrushInfos"]
    for brush, alpha in zip(
        brushes,
        (0.5, 0.25, 0.0, 0.75, 0.6, 0.4, 0.2, 0.1),
    ):
        brush["SolidColor"]["DxColor"]["A"] = alpha

    gradient = payload["LyricsFonts"][1]["BrushInfos"][0]
    gradient["GradientStops"][0]["Color"]["A"] = 0.5
    gradient["GradientStops"][1]["Color"]["A"] = 0.25

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    colors = style.karaoke_colors

    assert colors.after.text.color == "#80FF0000"
    assert colors.after.stroke.color == "#40FFFFFF"
    assert colors.after.stroke2.color == "#00000000"
    assert colors.after.shadow.color == "#BF000000"
    assert colors.before.text.color == "#99FFFFFF"
    assert colors.before.stroke.color == "#66000000"
    assert colors.before.stroke2.color == "#33FFFFFF"
    assert colors.before.shadow.color == "#1A26386A"
    assert style.fill_color == "#80FF0000"
    assert style.stroke_color == "#40FFFFFF"
    assert style.shadow_color == "#BF000000"

    gradient_fill = style.custom_style_schemes["青配色"].karaoke_colors.after.text
    assert gradient_fill.gradient_stops == [
        (0, "#80FFFFFF"),
        (100, "#400000FF"),
    ]


def test_import_blur_concentration_is_scheme_shared_and_reaches_title(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][1]["BlurLevel"] = 2

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    scheme = style.custom_style_schemes["青配色"]

    assert scheme.glow_concentration_level == 2
    assert scheme.ruby_glow_concentration_level is None
    # 标题引用 FontIndex=1（青配色）→ 同步进「标题」方案
    title_scheme = style.custom_style_schemes["标题"]
    assert title_scheme.glow_concentration_level == 2
    assert not any("BlurLevel" in warning or "ブラー浓度" in warning for warning in result.warnings)


def test_import_title_overlay(imported):
    style = style_from_dict(imported.project_data["style"])
    title = style.title_overlay
    assert title is not None and title.enabled
    assert title.text_template == "曲名"
    # LayoutIndex=1 → 引用タイトル左上布局（几何由布局解析，不再展开进 TitleOverlay）
    assert title.layout_index == 1
    assert style.layouts[0].name == "タイトル左上"
    assert style.layouts[0].line_y_position == "top"
    # FontIndex=1（青配色）→ 写入「标题」配色方案；标题永不走字 → 渲染取ワイプ前
    scheme = style.custom_style_schemes["标题"]
    assert scheme.font_family == "UD デジタル 教科書体 N-B"
    assert scheme.karaoke_colors.before.text.color == "#FFFFFF"
    assert scheme.decoration_kind == "glow"
    # Head + HeadEnd 哨兵 → 整段显示
    assert title.show_mode == "whole"
    assert title.fade_in_ms == 0 and title.fade_out_ms == 0


def test_import_title_preserves_per_character_font_roles(tmp_path):
    payload = _project_payload(tmp_path)
    chars = payload["TitleInfos"][0]["LineInfos"][0]["LyricsCharInfos"]
    chars[:] = [
        _char("青", 5999990, 5999990, font_index=1),
        _char("標", 5999990, 5999990, font_index=0),
        _char("青", 5999990, 5999990, font_index=1),
    ]

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    title = style.title_overlay

    assert title is not None
    assert title.text_template == "青標青"
    assert title.char_role_labels == [[None, "標準配色", None]]
    assert style.custom_style_schemes["标题"].decoration_kind == "glow"
    assert "標準配色" in style.custom_style_schemes


def test_import_title_scheme_always_present(tmp_path):
    """N3 项目没有标题时也保底写入默认「标题」方案（渲染/编辑入口恒可用）。"""
    payload = _project_payload(tmp_path)
    payload["TitleInfos"] = []
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    assert style.title_overlay is None
    assert "标题" in style.custom_style_schemes


def test_import_per_line_layout_and_roles(imported):
    data = imported.project_data
    # track 共 3 行（含 1 空行），第一行引用布局 1
    assert data["line_layout_indices"] == [1, 0, 0]
    roles = data["char_role_labels"]
    assert roles[0] == [None, "青配色"]
    assert roles[1] is None
    # FontIndex=0 explicitly clears any role marker parsed from the source LRC.
    assert roles[2] == [None, None]


def test_import_normalizes_bracketed_n3_scheme_names(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][0]["SettingsName"] = "【アクア】"
    payload["LyricsFonts"][1]["SettingsName"] = "【エミリア】"

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert "エミリア" in style.custom_style_schemes
    assert "【エミリア】" not in style.custom_style_schemes
    assert result.project_data["char_role_labels"][0] == [None, "エミリア"]


def test_import_emits_explicit_role_clears_for_all_default_font_indices(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"] = payload["LyricsFonts"][:1]
    payload["TitleInfos"] = []
    for line in payload["SourceLyricsInfos"][0]["LineInfos"]:
        for char in line.get("LyricsCharInfos", []):
            char["FontIndex"] = 0

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["char_role_labels"] == [
        [None, None],
        None,
        [None, None],
    ]


def test_import_line_fade_animation(imported):
    style = style_from_dict(imported.project_data["style"])
    assert style.entry_anim == "fade"
    assert style.entry_lead_ms == 250
    assert style.exit_anim == "fade"
    assert style.exit_fade_ms == 300


def test_import_mixed_line_actions_as_per_line_overrides(tmp_path):
    payload = _project_payload(tmp_path)
    lyric_lines = [
        line for line in payload["SourceLyricsInfos"][0]["LineInfos"]
        if line.get("Kind") == 1
    ]
    lyric_lines[-1]["SubtitleActionId"] = "SHINTA.NoAction"
    lyric_lines[-1]["SubtitleActionSettings"] = {}

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    style = style_from_dict(result.project_data["style"])
    assert style.entry_anim == "fade"
    assert style.exit_anim == "fade"
    overrides = result.project_data["line_animation_overrides"]
    assert overrides[0] is None
    assert overrides[1] is None  # LRC 中的空行占位
    assert overrides[2] == {
        "entry_anim": "none",
        "entry_duration_ms": 0,
        "exit_anim": "none",
        "exit_duration_ms": 0,
    }
    assert not any("多数行" in warning for warning in result.warnings)


def test_mismatched_lyrics_skip_line_payload(tmp_path):
    payload = _project_payload(tmp_path)
    # 少一行歌词记录 → 行数不一致，整体跳过行级导入
    payload["SourceLyricsInfos"][0]["LineInfos"].pop()
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert "line_layout_indices" not in result.project_data
    assert any("行数" in warning for warning in result.warnings)


def test_unsupported_dest_format_warns(tmp_path):
    payload = _project_payload(tmp_path)
    payload["DestFormat"] = 0
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert "output_path" not in result.project_data["output"]
    assert any("输出格式" in warning for warning in result.warnings)


def test_unsupported_fps_falls_back_without_warning(tmp_path):
    payload = _project_payload(tmp_path)
    payload["SourceInfo"]["Fps"] = 30
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert result.project_data["screen"]["fps"] == 60
    assert not any("帧率" in warning for warning in result.warnings)


def test_image_background_is_imported(tmp_path):
    payload = _project_payload(tmp_path)
    image = tmp_path / "background.png"
    image.write_bytes(b"fake")
    payload["SourceInfo"]["SourceKind"] = 1
    payload["SourceInfo"]["ImagePath"] = str(image)
    payload["SourceInfo"]["ImageRelativePath"] = image.name
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert result.project_data["video_path"] is None
    assert result.project_data["background"]["kind"] == "image"
    assert result.project_data["background"]["path"] == str(image)
    assert not any("图片背景" in warning for warning in result.warnings)


def test_sequence_and_solid_background_are_imported(tmp_path):
    payload = _project_payload(tmp_path)
    sequence = tmp_path / "frame_%04d.png"
    sequence.write_bytes(b"fake")
    payload["SourceInfo"].update(
        {"SourceKind": 2, "ImagePath": str(sequence), "ImageRelativePath": sequence.name}
    )
    sequence_result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert sequence_result.project_data["background"] == {
        "kind": "image_sequence",
        "path": str(tmp_path / "frame_%04d.png"),
        "color": "#000000",
        "source_fps": 60,
        "sequence_start_number": 0,
        "video_offset_ms": 0,
    }

    payload["SourceInfo"]["SourceKind"] = 3
    payload["SourceInfo"]["BackgroundColor"] = {"Web16": "123456"}
    solid_result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert solid_result.project_data["background"]["kind"] == "solid"
    assert solid_result.project_data["background"]["color"] == "#123456"


REAL_N3PROJ = Path(r"D:\カラオケ\songs\Marginality\1.n3proj")
TACTIC_N3PROJ = Path(r"D:\カラオケ\songs\TACTIC\1.n3proj")
DARK_SPIRAL_N3PROJ = Path(r"D:\カラオケ\songs\Dark spiral journey\1.n3proj")
ISEKAI_GIRLS_N3PROJ = Path(r"D:\カラオケ\songs\異世界ガールズ♡トーク\1.n3proj")


@pytest.mark.skipif(not REAL_N3PROJ.is_file(), reason="本机样例工程不存在")
def test_import_real_project_smoke():
    result = load_n3proj(REAL_N3PROJ)
    style = style_from_dict(result.project_data["style"])
    assert style.font_family == "UD デジタル 教科書体 N-B"
    assert style.karaoke_colors.after.text.color == "#1C6FB5"
    assert [layout.name for layout in style.layouts] == [
        "下寄せ3行", "上寄せ2行", "コーラス", "タイトル左上", "タイトル中央",
    ]
    assert set(style.custom_style_schemes) == {
        "青配色", "緑配色", "グラデーション配色", "コーラス配色", "情報小", "情報中", "情報大",
    }
    assert style.title_overlay is not None
    assert style.title_overlay.show_mode == "whole"
    assert len(result.project_data["line_layout_indices"]) == 34
    direct_track = load_nicokara_lrc(Path(result.project_data["subtitle_path"]))
    assert [line.break_before for line in direct_track.lines] == result.project_data[
        "line_breaks_before"
    ]


@pytest.mark.skipif(not TACTIC_N3PROJ.is_file(), reason="TACTIC 样例工程不存在")
def test_import_tactic_project_style_parity():
    result = load_n3proj(TACTIC_N3PROJ)
    style = style_from_dict(result.project_data["style"])

    assert style.glow_concentration_level == 1
    assert style.ruby_glow_concentration_level is None
    assert style.letter_spacing_px == 7
    assert style.stroke_width_px == 2
    assert style.stroke2_enabled is False
    assert style.ruby_font_size_px == 45
    assert style.ruby_stroke_width_px == 2
    assert style.ruby_stroke2_enabled is False
    assert style.karaoke_colors is not None
    assert style.karaoke_colors.after.text.color == "#FFF1FB"
    assert style.karaoke_colors.after.stroke.color == "#4EAADE"
    assert style.karaoke_colors.after.stroke2.color == "#000000"
    assert style.karaoke_colors.after.shadow.color == "#4EAADE"


@pytest.mark.skipif(
    not DARK_SPIRAL_N3PROJ.is_file(), reason="Dark spiral journey 样例工程不存在"
)
def test_import_dark_spiral_layout_character_spacing_parity():
    result = load_n3proj(DARK_SPIRAL_N3PROJ)
    style = style_from_dict(result.project_data["style"])

    assert result.warnings == []
    assert style.letter_spacing_px == 4
    assert all(layout.letter_spacing_px == 0 for layout in style.layouts)
    assert set(result.project_data["line_layout_indices"]) == {0}
    assert style.title_overlay is not None
    assert style.title_overlay.layout_index == 4
    assert style.title_overlay.letter_spacing_px == 0


@pytest.mark.skipif(
    not ISEKAI_GIRLS_N3PROJ.is_file(), reason="異世界ガールズ♡トーク样例工程不存在"
)
def test_import_isekai_girls_uses_lrc_space_timing_not_n3_char_snapshot():
    result = load_n3proj(ISEKAI_GIRLS_N3PROJ)
    track = load_nicokara_lrc(Path(result.project_data["subtitle_path"]))
    line = next(
        line
        for line in track.lines
        if "".join(ch.text for ch in line.chars) == "それじゃまたね来週 バーイバーイ"
    )
    week_index = next(index for index, ch in enumerate(line.chars) if ch.text == "週")

    assert [ch.text for ch in line.chars[week_index:week_index + 3]] == ["週", " ", "バ"]
    assert [ch.start_ms for ch in line.chars[week_index:week_index + 3]] == [
        85_070,
        85_370,
        85_760,
    ]
    assert all(ch.source_span_start_ms is None for ch in line.chars)


CHORUS_LRC_TEXT = "[00:10:00]ラ[00:11:00]ラ[00:12:00]\n"


def test_import_multiple_lyrics_sources(tmp_path):
    payload = _project_payload(tmp_path)
    chorus = tmp_path / "chorus.lrc"
    chorus.write_text(CHORUS_LRC_TEXT, encoding="utf-8")
    payload["SourceLyricsInfos"].append(
        {
            "SourceLyricsPath": str(chorus),
            "SourceLyricsRelativePath": "chorus.lrc",
            "LineInfos": [
                _line_info(
                    [_char("ラ", 10000, 11000, font_index=1), _char("ラ", 11000, 12000)],
                    layout_index=1,
                ),
            ],
            "SettingsName": "コーラス1",
        }
    )
    # 空槽位（N3 常见的 コーラス2 占位）应被忽略
    payload["SourceLyricsInfos"].append(
        {"SourceLyricsPath": None, "SourceLyricsRelativePath": "", "LineInfos": [], "SettingsName": "コーラス2"}
    )
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    extras = result.project_data.get("extra_subtitle_sources")
    assert extras is not None and len(extras) == 1
    entry = extras[0]
    assert entry["name"] == "コーラス1"
    assert entry["path"] == str(chorus)
    assert entry["line_layout_indices"] == [1]
    assert entry["char_role_labels"] == [["青配色", None]]
    # 不再出现「仅导入第一个」的提示
    assert not any("仅导入第一个" in w for w in result.warnings)
