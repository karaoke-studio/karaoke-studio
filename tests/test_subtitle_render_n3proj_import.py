"""NicoKaraMaker3 项目（.n3proj）导入：zip/JSON 解析与字段映射。"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role
from krok_helper.subtitle_render.domain.models import (
    DEFAULT_OUTPUT_NAME_SUFFIX,
    Style,
    default_title_scheme,
    guide_symbol_from_dict,
    style_from_dict,
)
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_PNG_COMPOSITED,
    OUTPUT_FORMAT_PNG_TRANSPARENT,
)
from krok_helper.subtitle_render.n3.project_import import (
    N3ImportResult,
    is_n3proj_file,
    load_n3proj,
)
from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc
from krok_helper.subtitle_render.sources.sug import load_sug_timing_track


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
               action_id: str = "SHINTA.LineFadeInFadeOut", *,
               action_settings: dict | None = None,
               show_begin: int | None = None, show_end: int | None = None) -> dict:
    result = {
        "Kind": 1,
        "LyricsCharInfos": chars,
        "SubtitleActionId": action_id,
        "SubtitleActionSettings": (
            {"FadeInTime": 250, "FadeOutTime": 300}
            if action_settings is None
            else action_settings
        ),
        "LayoutIndex": layout_index,
        "Raw": "",
    }
    if show_begin is not None:
        result["ShowBeginTime"] = show_begin
    if show_end is not None:
        result["ShowEndTime"] = show_end
    return result


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


@pytest.fixture(autouse=True)
def _isolated_settings_dir(tmp_path_factory, monkeypatch):
    """隔离应用设置目录。

    导入会读 SUG「软件导出补偿」等预设；不隔离的话断言将依赖本机真实
    settings.json，换机器随意失效。
    """
    settings_dir = tmp_path_factory.mktemp("n3-import-settings")
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(settings_dir))
    return settings_dir


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


def test_imports_n3_emoji_role_tag_as_anchored_bitmap_guide(tmp_path):
    """兜底：LRC 正文缺标签、但 N3 字符数据里有 → 行首 anchored 头像近似。"""
    payload = _project_payload(tmp_path)
    lrc = Path(payload["SourceLyricsInfos"][0]["SourceLyricsPath"])
    lrc.write_text(
        "[00:01:00]●[00:02:00]a[00:03:00]\n"
        "\n"
        "@Emoji=【A】,avatar.png,,Zoom=80,NoDecor,MarginRight=20\n",
        encoding="utf-8",
    )
    (tmp_path / "avatar.png").write_bytes(b"fake")
    payload["SourceLyricsInfos"][0]["AtTagsForSave"] = [
        "@Emoji=【A】,avatar.png,,Zoom=80,NoDecor,MarginRight=20"
    ]
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info(
            [
                _char("【A】", 1000, 1000, font_index=1),
                _char("●", 1000, 2000),
                _char("a", 2000, 3000),
            ],
            layout_index=0,
        )
    ]

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    row = result.project_data["line_guide_symbols"][0]
    symbol = guide_symbol_from_dict(row)
    assert symbol is not None
    assert symbol.kind == "bitmap"
    assert symbol.prefix_timing == "anchored"
    assert symbol.bitmap_before_path == str(tmp_path / "avatar.png")
    assert symbol.bitmap_zoom_percent == 80
    assert symbol.bitmap_no_decor is True
    assert symbol.bitmap_margin_right_px == 20


def test_import_keeps_inline_emoji_replacement_for_every_role_tag(tmp_path):
    """一行多个 ``@Emoji`` 触发标签：每个都原位替换，payload 原样带出，逐行数据不错位。"""
    payload = _project_payload(tmp_path)
    lrc = Path(payload["SourceLyricsInfos"][0]["SourceLyricsPath"])
    lrc.write_text(
        "【A】[00:01:00]●[00:02:00]【B】い[00:03:00]\n"
        "\n"
        "@Emoji=【A】,a.png,,NoDecor\n"
        "@Emoji=【B】,b.png,,NoDecor\n",
        encoding="utf-8",
    )
    (tmp_path / "a.png").write_bytes(b"fake")
    (tmp_path / "b.png").write_bytes(b"fake")
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info(
            [
                _char("【A】", 1000, 1000, font_index=1),
                _char("●", 1000, 2000),
                _char("【B】", 2000, 2000, font_index=1),
                _char("い", 2000, 3000, font_index=1),
            ],
            layout_index=0,
        )
    ]

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    data = result.project_data

    # 每个标签都原位替换（不占行前槽位），payload 不再产生 anchored 行首头像
    assert "line_guide_symbols" not in data
    inline_row = data["line_inline_guide_symbols"][0]
    assert set(inline_row) == {"0", "2"}
    first = guide_symbol_from_dict(inline_row["0"])
    second = guide_symbol_from_dict(inline_row["2"])
    assert first is not None and first.bitmap_before_path == str(tmp_path / "a.png")
    assert second is not None and second.bitmap_before_path == str(tmp_path / "b.png")
    # 合成头像字符不参与 N3 正文比对：布局 / 逐字配色照常导入（labels 与 chars 对齐）
    assert data["line_layout_indices"][0] == 0
    assert len(data["char_role_labels"][0]) == 4
    assert not any("不一致" in warning for warning in result.warnings)


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


def test_movie_canvas_uses_n3_size_reference_instead_of_background_placeholder(
    tmp_path,
):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][0]["FontInfos"][0]["CharSize"] = _size(180, 2160)
    payload["LyricsLayouts"][0]["VerticalMargin"] = _size(40, 2160)

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert result.project_data["screen"] == {
        "width": 3840,
        "height": 2160,
        "fps": 60,
        "par": "1:1",
    }
    assert style.custom_style_schemes["標準配色"].font_size_px == 180
    assert style.font_reference_height == 2160
    assert style.line_y_margin_px == 40
    assert style.layout_reference_height == 2160


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


def test_import_decor_kind_none_as_explicit_no_decoration(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][0]["DecorKind"] = 0

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    scheme = style.custom_style_schemes["標準配色"]

    assert scheme.decoration_kind == "none"
    assert scheme.decoration_kind_before == "none"
    assert scheme.shadow_before_offset_x == 0
    assert scheme.shadow_before_offset_y == 0


def test_import_materializes_decoupled_before_decoration_slots(tmp_path):
    """N3 只有一套装饰，走字前/后解耦后两侧都必须落成同一组显式值。"""
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][1]["BlurLevel"] = 2

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    # DecorKind.Shadow：走字前阴影 X/Y 也是 DecorSize，不能留 None 去继承全局。
    shadow_scheme = style.custom_style_schemes["標準配色"]
    assert shadow_scheme.decoration_kind == "shadow"
    assert shadow_scheme.decoration_kind_before == "shadow"
    assert shadow_scheme.shadow_offset_x == 5
    assert shadow_scheme.shadow_before_offset_x == 5
    assert shadow_scheme.shadow_before_offset_y == 5

    # DecorKind.Blur：走字前发光宽度与浓度同样来自 DecorSize / BlurLevel。
    glow_scheme = style.custom_style_schemes["青配色"]
    assert glow_scheme.decoration_kind == "glow"
    assert glow_scheme.decoration_kind_before == "glow"
    assert glow_scheme.glow_radius_px == 10
    assert glow_scheme.glow_before_radius_px == 10
    assert glow_scheme.glow_after_radius_px == 10
    assert glow_scheme.glow_concentration_level == 2
    assert glow_scheme.glow_before_concentration_level == 2


def test_imported_before_decoration_does_not_leak_project_global(tmp_path):
    """把 N3 方案叠到已有工程时，走字前槽位不能被工程全局值顶掉。"""
    payload = _project_payload(tmp_path)
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    style = replace(
        style,
        decoration_kind_before="glow",
        glow_before_concentration_level=2,
        shadow_before_offset_x=-9,
        shadow_before_offset_y=-9,
    )

    resolved = style_for_role(style, "標準配色")

    assert resolved.decoration_kind_before == "shadow"
    assert resolved.shadow_before_offset_x == 5
    assert resolved.shadow_before_offset_y == 5


def test_import_first_font_settings_become_a_named_scheme(imported):
    """第 0 套 フォント設定 也是一个具名角色，不再摊进全局默认。"""
    style = style_from_dict(imported.project_data["style"])
    # 打开工程时直接停在导入的第一套方案，而不是全局默认。
    assert imported.project_data["selected_scheme_key"] == "custom:標準配色"
    assert style.layout_semantics == "n3_1074"
    scheme = style.custom_style_schemes["標準配色"]
    assert scheme.n3_font_inheritance is True
    assert scheme.font_family == "UD デジタル 教科書体 N-B"
    assert scheme.font_family_latin == "Comic Sans MS"
    assert scheme.font_size_px == 100
    assert scheme.font_weight == 700
    assert scheme.stroke_width_px == 15
    # UseEdge2 全链 None → N3 不绘制二重描边
    assert scheme.stroke2_enabled is False
    assert scheme.stroke2_width_px == 5
    # 子槽的 0/null 保持为继承状态，不再物化成根槽的有效值。
    assert scheme.latin_font_size_px is None
    assert scheme.latin_stroke_width_px is None
    assert scheme.latin_stroke2_enabled is None
    assert scheme.latin_stroke2_width_px is None
    # DecorKind.Shadow → 右下偏移 DecorSize
    assert scheme.decoration_kind == "shadow"
    assert scheme.shadow_offset_x == 5
    assert scheme.shadow_offset_y == 5
    colors = scheme.karaoke_colors
    assert colors is not None
    assert colors.after.text.color == "#FF0000"
    assert colors.before.text.color == "#FFFFFF"
    assert colors.before.shadow.color == "#26386A"
    # ルビ：空字体/字面继续显示 0 并跟随主文字；字号与描边保留显式值。
    assert scheme.ruby_font_follow_main is True
    assert scheme.ruby_font_family is None
    assert scheme.ruby_font_weight is None
    assert scheme.ruby_font_family_latin is None
    assert scheme.ruby_font_size_px == 45
    assert scheme.ruby_stroke_width_px == 10
    assert scheme.ruby_stroke2_enabled is None
    assert scheme.ruby_stroke2_width_px == 3
    assert scheme.ruby_latin_font_size_px is None
    assert scheme.ruby_latin_font_weight is None
    assert scheme.ruby_latin_stroke_width_px is None
    assert scheme.ruby_latin_stroke2_enabled is None
    assert scheme.ruby_latin_stroke2_width_px is None
    assert scheme.ruby_colors_follow_main is True
    assert scheme.ruby_karaoke_colors is not None


def test_import_keeps_global_and_title_roles_at_factory_defaults(imported):
    """两个内置角色不被 N3 改写 —— 否则第 0 套配色等于被改名成「全局默认」。

    分色歌里第 0 套通常就是某个具体角色（【アクア】之类），名字一丢，源 LRC
    里同名的 ``【…】`` 标记就再也找不到方案。
    """
    style = style_from_dict(imported.project_data["style"])
    factory = Style()

    for field in ("font_family", "font_family_latin", "font_size_px", "font_weight",
                  "stroke_width_px", "stroke2_enabled", "decoration_kind",
                  "shadow_offset_x", "shadow_offset_y", "karaoke_colors",
                  "ruby_font_size_px", "ruby_stroke_width_px"):
        assert getattr(style, field) == getattr(factory, field), field
    assert style.custom_style_schemes["标题"] == default_title_scheme()
    # 版式（布局域）仍然照 N3 导入，只有配色域不碰全局。
    assert style.layout_semantics == "n3_1074"
    assert style.line_y_margin_px == 90


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
    scheme = style.custom_style_schemes["標準配色"]

    assert scheme.latin_font_size_px == 72
    assert scheme.latin_stroke_width_px == 8
    assert scheme.latin_stroke2_enabled is True
    assert scheme.latin_stroke2_width_px == 4
    assert scheme.ruby_latin_font_size_px == 30
    assert scheme.ruby_latin_stroke_width_px == 6
    assert scheme.ruby_latin_stroke2_enabled is False
    assert scheme.ruby_latin_stroke2_width_px == 2


def test_import_keeps_ruby_stroke2_follow_state(tmp_path):
    """N3 的 ``UseEdge2=null`` 必须保留为“跟随主文字”，不能被物化成开启。"""
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[0]["UseEdge2"] = True
    infos[3]["UseEdge2"] = None
    infos[5]["UseEdge2"] = None

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    scheme = style.custom_style_schemes["標準配色"]

    assert scheme.ruby_stroke2_enabled is None
    assert scheme.ruby_latin_stroke2_enabled is None
    # 合并到角色样式后仍是 None：渲染端按主文字开关解析，而不是写成开。
    resolved = style_for_role(style, "標準配色")
    assert resolved.ruby_stroke2_enabled is None
    assert resolved.stroke2_enabled is True


def test_import_collapses_redundant_child_stroke2_when_text_follows(tmp_path):
    """父组与子组都为开、且子组文字跟随父组时，子组描边2 收成“跟随”。"""
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[0]["UseEdge2"] = True
    infos[2].update(
        FontName="",
        FontFaceName="",
        CharSize=_size(0),
        EdgeSize=_size(0),
        EdgeSize2=_size(4),
        UseEdge2=True,
    )
    infos[3].update(FontName="", FontFaceName="", UseEdge2=True)
    infos[5].update(
        FontName="",
        FontFaceName="",
        CharSize=_size(0),
        EdgeSize=_size(0),
        EdgeSize2=_size(1),
        UseEdge2=True,
    )

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    scheme = style_from_dict(result.project_data["style"]).custom_style_schemes[
        "標準配色"
    ]

    assert scheme.stroke2_enabled is True
    assert scheme.latin_stroke2_enabled is None
    assert scheme.ruby_stroke2_enabled is None
    assert scheme.ruby_latin_stroke2_enabled is None
    # 只收起开关；各槽自己保存的二重描边宽度原样保留。
    assert scheme.latin_stroke2_width_px == 4
    assert scheme.ruby_stroke2_width_px == 3
    assert scheme.ruby_latin_stroke2_width_px == 1


def test_import_keeps_child_stroke2_when_text_does_not_follow(tmp_path):
    """文字没有跟随父组时，子组自己的描边2 开必须保留，不能强行收成跟随。"""
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[0]["UseEdge2"] = True
    infos[2].update(FontName="Comic Sans MS", FontFaceName="Negreta", UseEdge2=True)
    infos[3].update(FontName="Ruby Font", FontFaceName="Bold", UseEdge2=True)
    infos[5].update(
        FontName="Comic Sans MS",
        FontFaceName="Negreta",
        CharSize=_size(30),
        UseEdge2=True,
    )

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    scheme = style_from_dict(result.project_data["style"]).custom_style_schemes[
        "標準配色"
    ]

    assert scheme.latin_stroke2_enabled is True
    assert scheme.ruby_stroke2_enabled is True
    assert scheme.ruby_latin_stroke2_enabled is True


def test_import_collapses_child_stroke2_when_parent_and_child_are_both_closed(tmp_path):
    """false+false 也收成跟随；父/子同为关时不需要两个独立的不勾。"""
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[0]["UseEdge2"] = False
    for index in (2, 3, 5):
        infos[index].update(FontName="", FontFaceName="", UseEdge2=False)

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    scheme = style_from_dict(result.project_data["style"]).custom_style_schemes[
        "標準配色"
    ]

    assert scheme.stroke2_enabled is False
    assert scheme.latin_stroke2_enabled is None
    assert scheme.ruby_stroke2_enabled is None
    assert scheme.ruby_latin_stroke2_enabled is None


def test_import_keeps_child_stroke2_when_state_differs_from_parent(tmp_path):
    """父开子关/父关子开是显式覆盖，不能收成跟随。"""
    payload = _project_payload(tmp_path)
    infos = payload["LyricsFonts"][0]["FontInfos"]
    infos[0]["UseEdge2"] = True
    infos[2].update(FontName="", FontFaceName="", UseEdge2=False)
    infos[3].update(FontName="", FontFaceName="", UseEdge2=False)
    infos[5].update(FontName="", FontFaceName="", UseEdge2=True)

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    scheme = style_from_dict(result.project_data["style"]).custom_style_schemes[
        "標準配色"
    ]

    assert scheme.stroke2_enabled is True
    assert scheme.latin_stroke2_enabled is False
    assert scheme.ruby_stroke2_enabled is False
    # 注音英数的直接父组（注音日文）是关，子组显式开属于覆盖。
    assert scheme.ruby_latin_stroke2_enabled is True


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
    scheme = style.custom_style_schemes["標準配色"]

    assert scheme.font_family == "UD デジタル 教科書体 N-B"
    assert scheme.font_size_px == 100
    assert scheme.stroke_width_px == 15
    assert scheme.stroke2_enabled is False
    assert scheme.ruby_font_family is None
    assert scheme.ruby_font_weight is None
    assert scheme.ruby_font_size_px == 45
    assert scheme.ruby_stroke_width_px == 10
    assert scheme.ruby_stroke2_enabled is None
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
    assert style.layouts[0].name == "タイトル左上"
    assert [layout.name for layout in style.layouts[1:]] == [
        "1 行布局",
        "3 行布局",
        "4 行布局",
        "5 行布局",
        "6 行布局",
        "7 行布局",
        "8 行布局",
    ]
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


def test_import_uses_page_head_layout_for_all_lines_on_n3_page(tmp_path):
    payload = _project_payload(tmp_path)
    line_infos = payload["SourceLyricsInfos"][0]["LineInfos"]
    del line_infos[1]  # remove the explicit PageBreak
    line_infos[0]["LayoutIndex"] = 0  # two-row page
    line_infos[1]["LayoutIndex"] = 1  # ignored by N3 SetOneLineX/Y

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["line_layout_indices"] == [0, 0, 0]


def test_import_preserves_n3_line_display_windows(tmp_path):
    payload = _project_payload(tmp_path)
    first = payload["SourceLyricsInfos"][0]["LineInfos"][0]
    first["ShowBeginTime"] = 250
    first["ShowEndTime"] = 3400

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["line_display_overrides"] == [
        [250, 3400],
        None,
        None,
    ]


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

    stops = style.custom_style_schemes["標準配色"].karaoke_colors.after.text
    assert stops.gradient_stops == [
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
    fill = style.custom_style_schemes["標準配色"].karaoke_colors.after.text

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
    scheme = style.custom_style_schemes["標準配色"]
    colors = scheme.karaoke_colors

    assert colors.after.text.color == "#80FF0000"
    assert colors.after.stroke.color == "#40FFFFFF"
    assert colors.after.stroke2.color == "#00000000"
    assert colors.after.shadow.color == "#BF000000"
    assert colors.before.text.color == "#99FFFFFF"
    assert colors.before.stroke.color == "#66000000"
    assert colors.before.stroke2.color == "#33FFFFFF"
    assert colors.before.shadow.color == "#1A26386A"
    assert scheme.fill_color == "#80FF0000"
    assert scheme.stroke_color == "#40FFFFFF"
    assert scheme.shadow_color == "#BF000000"

    gradient_fill = style.custom_style_schemes["青配色"].karaoke_colors.after.text
    assert gradient_fill.gradient_stops == [
        (0, "#80FFFFFF"),
        (100, "#400000FF"),
    ]


@pytest.mark.parametrize(
    ("bitmap_scale", "expected_scale"),
    [(0, 100), (1, 1), (1000, 1000), (1200, 1000)],
)
def test_import_preserves_missing_bitmap_settings_and_clamps_scale(
    tmp_path, bitmap_scale, expected_scale
):
    payload = _project_payload(tmp_path)
    brush = payload["LyricsFonts"][0]["BrushInfos"][0]
    brush["SelectedBrushTypeIndex"] = 3
    brush["BitmapPath"] = "missing-texture.png"
    brush["BitmapScale"] = bitmap_scale

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    fill = style.custom_style_schemes["標準配色"].karaoke_colors.after.text

    assert fill.mode == "image"
    assert Path(fill.image_path) == tmp_path / "missing-texture.png"
    assert fill.image_scale_pct == expected_scale
    assert any("已保留图片设置" in warning for warning in result.warnings)


def test_import_blur_concentration_is_scheme_shared_and_reaches_title(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][1]["BlurLevel"] = 2

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    scheme = style.custom_style_schemes["青配色"]

    assert scheme.glow_concentration_level == 2
    assert scheme.ruby_glow_concentration_level is None
    # 标题 FontIndex=1 → 逐字角色引用同一套「青配色」，内置「标题」角色不被改写
    assert style.title_overlays[0].char_role_labels == [["青配色", "青配色"]]
    assert style.custom_style_schemes["标题"] == default_title_scheme()
    assert not any("BlurLevel" in warning or "ブラー浓度" in warning for warning in result.warnings)


def test_import_title_overlay(imported):
    style = style_from_dict(imported.project_data["style"])
    assert style.title_overlays
    title = style.title_overlays[0]
    assert title.name == "标题 1"
    assert title.enabled
    assert title.text_template == "曲名"
    # LayoutIndex=1 → 引用タイトル左上布局（几何由布局解析，不再展开进 TitleOverlay）
    assert title.layout_index == 1
    assert style.layouts[0].name == "タイトル左上"
    assert style.layouts[0].line_y_position == "top"
    # FontIndex=1 → 标题逐字引用「青配色」角色；内置「标题」角色保持出厂值
    assert title.char_role_labels == [["青配色", "青配色"]]
    assert style.custom_style_schemes["标题"] == default_title_scheme()
    scheme = style.custom_style_schemes["青配色"]
    assert scheme.font_family == "UD デジタル 教科書体 N-B"
    assert scheme.karaoke_colors.before.text.color == "#FFFFFF"
    assert scheme.decoration_kind == "glow"
    # Head + HeadEnd 哨兵 → 整段显示
    assert title.show_mode == "whole"
    assert title.fade_in_ms == 0 and title.fade_out_ms == 0


def test_import_n3_continuous_head_and_tail_does_not_map_to_two_segments(tmp_path):
    payload = _project_payload(tmp_path)
    payload["TitleInfos"][0]["ShowTime"] = {
        "Kind": 2,
        "HeadOffset": 2500,
        "HeadEnd": 5999990,
        "Interval": 10000,
        "TailOffset": 3500,
    }

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    title = style_from_dict(result.project_data["style"]).title_overlays[0]

    assert title.show_mode == "whole"
    assert any("開始～終了" in warning for warning in result.warnings)


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
    title = style.title_overlays[0]

    assert title.text_template == "青標青"
    # 主字体那两个字符同样贴标签 —— 只有内置「标题」角色留给用户自己改。
    assert title.char_role_labels == [["青配色", "標準配色", "青配色"]]
    assert style.custom_style_schemes["标题"] == default_title_scheme()
    assert {"標準配色", "青配色"} <= set(style.custom_style_schemes)


def test_import_multiple_title_infos_all_imported_with_entry_names(tmp_path):
    """多 TitleInfos 全量导入：逐条命名「标题 1..N」，不再只取第一条。"""
    payload = _project_payload(tmp_path)
    payload["TitleInfos"].append(
        {
            "ShowTime": {"Kind": 1, "HeadOffset": 2000, "HeadEnd": 9000,
                         "Interval": 7000, "TailOffset": 0},
            "LayoutIndex": 0,
            "LineInfos": [
                {"Kind": 5, "LyricsCharInfos": [
                    _char("副", 5999990, 5999990),
                    _char("題", 5999990, 5999990),
                ]},
            ],
            "SettingsName": "タイトル2",
        }
    )

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert [overlay.name for overlay in style.title_overlays] == ["标题 1", "标题 2"]
    assert [overlay.enabled for overlay in style.title_overlays] == [True, True]
    assert [overlay.text_template for overlay in style.title_overlays] == [
        "曲名",
        "副題",
    ]
    assert style.title_overlays[0].show_mode == "whole"
    second = style.title_overlays[1]
    assert second.show_mode == "head"
    assert second.head_offset_ms == 2000
    assert second.duration_ms == 7000
    assert not any("仅" in warning and "标题" in warning for warning in result.warnings)


def test_import_title_scheme_always_present(tmp_path):
    """N3 项目没有标题时也保底写入默认「标题」方案（渲染/编辑入口恒可用）。"""
    payload = _project_payload(tmp_path)
    payload["TitleInfos"] = []
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])
    # 无标题 N3 项目不写 title_overlays：加载后保持默认的一条禁用条目
    assert len(style.title_overlays) == 1
    assert not style.title_overlays[0].enabled
    assert "标题" in style.custom_style_schemes


def test_import_per_line_layout_and_roles(imported):
    data = imported.project_data
    # track 共 3 行（含 1 空行），第一行引用布局 1
    assert data["line_layout_indices"] == [1, 0, 0]
    roles = data["char_role_labels"]
    assert roles[0] == ["標準配色", "青配色"]
    assert roles[1] is None
    # FontIndex=0 也是一套具名配色，覆盖源 LRC 解析出的角色标记。
    assert roles[2] == ["標準配色", "標準配色"]


def test_default_font_chars_still_render_like_n3(imported):
    """外观不变：FontIndex=0 的字符照旧长成 N3 的样子，只是改由角色方案提供。"""
    style = style_from_dict(imported.project_data["style"])
    resolved = style_for_role(style, "標準配色")

    assert resolved.font_family == "UD デジタル 教科書体 N-B"
    assert resolved.font_family_latin == "Comic Sans MS"
    assert resolved.font_size_px == 100
    assert resolved.stroke_width_px == 15
    assert resolved.decoration_kind == "shadow"
    assert resolved.karaoke_colors.after.text.color == "#FF0000"
    assert imported.project_data["char_role_labels"][2] == ["標準配色", "標準配色"]


def test_import_appends_every_font_setting_in_n3_order(imported):
    """N3 的 フォント設定 一套不落地 append 进来（内置「标题」角色除外）。"""
    style = style_from_dict(imported.project_data["style"])

    names = [name for name in style.custom_style_schemes if name != "标题"]
    assert names == ["標準配色", "青配色"]


def test_import_keeps_char_labels_in_sync_with_renamed_duplicate_schemes(tmp_path):
    """两套同名 フォント設定 → 后一套改名，逐字标签必须跟着改，不能指空。"""
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][1]["SettingsName"] = "標準配色"

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert "標準配色（1）" in style.custom_style_schemes
    assert result.project_data["char_role_labels"][0] == ["標準配色", "標準配色（1）"]


def test_import_normalizes_bracketed_n3_scheme_names(tmp_path):
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"][0]["SettingsName"] = "【アクア】"
    payload["LyricsFonts"][1]["SettingsName"] = "【エミリア】"

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert {"アクア", "エミリア"} <= set(style.custom_style_schemes)
    assert "【エミリア】" not in style.custom_style_schemes
    assert result.project_data["char_role_labels"][0] == ["アクア", "エミリア"]


def test_import_labels_every_default_font_index_with_the_first_scheme(tmp_path):
    """整首都用第 0 套时，每个字符也要贴上它的名字（而不是留空吃全局默认）。"""
    payload = _project_payload(tmp_path)
    payload["LyricsFonts"] = payload["LyricsFonts"][:1]
    payload["TitleInfos"] = []
    for line in payload["SourceLyricsInfos"][0]["LineInfos"]:
        for char in line.get("LyricsCharInfos", []):
            char["FontIndex"] = 0

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["char_role_labels"] == [
        ["標準配色", "標準配色"],
        None,
        ["標準配色", "標準配色"],
    ]


def test_import_line_fade_animation(imported):
    style = style_from_dict(imported.project_data["style"])
    assert style.entry_anim == "fade"
    assert style.entry_lead_ms == 250
    assert style.exit_anim == "fade"
    assert style.exit_fade_ms == 300


@pytest.mark.parametrize(
    ("action_id", "settings", "expected"),
    [
        ("SHINTA.NoAction", {}, ("none", 0, "none", 0)),
        ("SHINTA.LineFadeIn", {"FadeInTime": 180}, ("fade", 180, "none", 0)),
        ("SHINTA.LineFadeOut", {"FadeOutTime": 420}, ("none", 0, "fade", 420)),
        (
            "SHINTA.LineFadeInFadeOut",
            {"FadeInTime": 180, "FadeOutTime": 420},
            ("fade", 180, "fade", 420),
        ),
        (
            "SHINTA.CharFadeInFadeOut",
            {"FadeInTime": 250, "FadeOutTime": 300, "IntroDelay": 350},
            ("char_fade", 600, "char_fade", 650),
        ),
        (
            "SHINTA.CharDrip",
            {"FadeInTime": 250, "FadeOutTime": 300, "IntroDelay": 350},
            ("char_drip", 600, "char_drip", 650),
        ),
        (
            "SHINTA.SpinFlip",
            {"FadeInTime": 250, "FadeOutTime": 300, "IntroDelay": 350},
            ("spin_flip", 600, "spin_flip", 650),
        ),
        ("SHINTA.Utopia", {"TailDelay": 250}, ("utopia", 700, "utopia", 750)),
    ],
)
def test_import_maps_supported_n3_actions(tmp_path, action_id, settings, expected):
    payload = _project_payload(tmp_path)
    for line in payload["SourceLyricsInfos"][0]["LineInfos"]:
        if line.get("Kind") == 1:
            line["SubtitleActionId"] = action_id
            line["SubtitleActionSettings"] = settings

    result = load_n3proj(_write_n3proj(tmp_path, payload))
    style = style_from_dict(result.project_data["style"])

    assert (
        style.entry_anim,
        style.entry_lead_ms,
        style.exit_anim,
        style.exit_fade_ms,
    ) == expected
    assert not any("字幕动作" in warning for warning in result.warnings)


def test_import_keeps_slide_up_down_unsupported(tmp_path):
    payload = _project_payload(tmp_path)
    for line in payload["SourceLyricsInfos"][0]["LineInfos"]:
        if line.get("Kind") == 1:
            line["SubtitleActionId"] = "SHINTA.SlideUpDown"

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert any("SHINTA.SlideUpDown" in warning for warning in result.warnings)


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


def test_line_count_mismatch_rebuilds_sug_from_n3(tmp_path):
    """行数不一致以 N3 数据为准：落盘新 .sug、工程改指它，原 LRC 不动。"""
    payload = _project_payload(tmp_path)
    original_lrc = tmp_path / "demo.lrc"
    original_text = original_lrc.read_text(encoding="utf-8")
    # 少一行歌词记录 → 行数不一致（歌词 2 行 / N3 记录 1 行）
    payload["SourceLyricsInfos"][0]["LineInfos"].pop()
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    sug = tmp_path / "demo_从N3重建.sug"
    assert sug.is_file()
    assert result.project_data["subtitle_path"] == str(sug)
    # 原歌词文件保持原样
    assert original_lrc.read_text(encoding="utf-8") == original_text
    assert any("行数" in warning and "重建" in warning for warning in result.warnings)

    # 行级数据按 N3 记录完整带回（第一行 あ/い，含布局与逐字配色）
    data = result.project_data
    assert data["line_layout_indices"][0] == 1
    assert data["char_role_labels"][0] == ["標準配色", "青配色"]
    assert "line_breaks_before" in data


def test_missing_subtitle_file_rebuilds_sug(tmp_path):
    """字幕文件丢失：用 N3 内嵌逐字数据在 n3proj 同目录重建 .sug。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    sug = tmp_path / "demo_从N3重建.sug"
    assert sug.is_file()
    assert result.project_data["subtitle_path"] == str(sug)
    assert any("不存在" in warning and "重建" in warning for warning in result.warnings)

    data = result.project_data
    # Kind2 不产生空行（实测 N3 自己导出的 LRC 就没有空行）；
    # 分页语义由 line_breaks_before payload 落在第二歌词行前
    assert data["line_breaks_before"] == ["none", "page"]
    assert data["line_layout_indices"] == [1, 0]
    assert data["char_role_labels"][0] == ["標準配色", "青配色"]

    # 重建的 .sug 能按 SUG 路径读回：文本、逐字起点、行末时刻与 N3 一致
    track = load_sug_timing_track(sug)
    assert [line.is_blank for line in track.lines] == [False, False]
    first, second = track.lines
    assert [char.text for char in first.chars] == ["あ", "い"]
    assert [char.start_ms for char in first.chars] == [1000, 2000]
    assert first.end_ms == 3000
    assert [char.text for char in second.chars] == ["う", "え"]
    assert [char.start_ms for char in second.chars] == [5000, 6000]
    assert second.end_ms == 7000


def test_missing_subtitle_file_without_lyric_data_keeps_skip(tmp_path):
    """N3 行数据里没有歌词行时无法重建，保持跳过行级导入的原行为。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        {"Kind": 2, "LyricsCharInfos": [], "LayoutIndex": -1, "Raw": ""}
    ]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert not (tmp_path / "demo_从N3重建.sug").exists()
    assert "line_layout_indices" not in result.project_data
    assert any("已跳过每行布局" in warning for warning in result.warnings)


def test_rebuild_failure_falls_back_to_text_alignment(tmp_path, monkeypatch):
    """重建失败（写盘异常）时退回按文本对齐：未改动的行仍带上行级数据。"""
    payload = _project_payload(tmp_path)
    # 歌词文件行数与 N3 记录错开：删掉 N3 的第一行记录，LRC 保留两行
    payload["SourceLyricsInfos"][0]["LineInfos"].pop(0)

    from krok_helper.subtitle_render.n3 import project_import

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(project_import.SugProjectParser, "save", _boom)
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert "line_layout_indices" in result.project_data
    data = result.project_data
    # LRC 里 う/え 一行仍与 N3 记录对齐，布局随行带回；被删记录的行保持默认
    assert data["line_layout_indices"][2] == 0
    assert data["char_role_labels"][2] == ["標準配色", "標準配色"]
    assert any("对齐" in warning for warning in result.warnings)


def test_rebuilt_sug_keeps_inline_pause_release(tmp_path):
    """行内 EndTime 早于下一字起点 → 演唱停顿（pause release）保留。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info([
            _char("あ", 1000, 1500),
            _char("い", 3000, 4000),
        ]),
    ]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    track = load_sug_timing_track(tmp_path / "demo_从N3重建.sug")
    assert [char.pause_release_ms for char in track.lines[0].chars] == [1500, 4000]
    assert not any("ルビ" in warning for warning in result.warnings)


def test_rebuilt_sug_skips_inline_ruby_with_warning(tmp_path):
    """字符流内嵌 IsRuby 注音无关联信息：跳过并明示（ruby 正道是 @RubyN 标签）。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info([
            _char("漢", 1000, 2000),
            {"Kind": 0, "Char": "かん", "BeginTime": 1000, "EndTime": 2000,
             "FontIndex": 0, "IsRuby": True},
            _char("字", 2000, 3000),
        ]),
    ]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    track = load_sug_timing_track(tmp_path / "demo_从N3重建.sug")
    assert [char.text for char in track.lines[0].chars] == ["漢", "字"]
    assert any("IsRuby" in warning for warning in result.warnings)
    # ruby 字不进入逐字配色对位
    assert result.project_data["char_role_labels"][0] == ["標準配色", "標準配色"]


def test_at_tags_save_supports_all_three_storage_forms():
    """AtTagsForSave 实测有整串文本/压平字符数组/按行三种形态，统一归一。"""
    from krok_helper.subtitle_render.n3.project_import import _n3_at_tag_lines

    expected = ["@Ruby1=恋,こ[00:00:28]い", "@Emoji=♪,icon.png,,NoDecor"]
    for form in (
        "\r\n".join(expected) + "\r\n",          # 实测 10.74：整段文本
        list("\n".join(expected) + "\n"),        # 压平字符数组
        expected,                                # 按行
    ):
        assert _n3_at_tag_lines({"AtTagsForSave": form}) == expected
    assert _n3_at_tag_lines({"AtTagsForSave": None}) == []
    assert _n3_at_tag_lines({}) == []


def test_rebuild_materializes_ruby_from_at_tags(tmp_path):
    """@RubyN 标签（含 mora 时间）经 SUG 官方管线物化为 .sug 内的 Ruby。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["AtTagsForSave"] = [
        "@Ruby1=恋,こ[00:00:28]い",
        "@Ruby2=一人,ひ[00:00:21]と[00:00:52]り",
    ]
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info([_char("恋", 1000, 3000), _char("こ", 3000, 5000)]),
        {"Kind": 2, "LyricsCharInfos": [], "LayoutIndex": -1, "Raw": ""},
        # 多字 ruby 基底同块共享起点（真实数据形态）
        _line_info([_char("一", 6000, 8000), _char("人", 0, 10000)]),
    ]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    sug = tmp_path / "demo_从N3重建.sug"
    assert sug.is_file()
    assert not any("ルビ" in warning or "IsRuby" in warning for warning in result.warnings)
    track = load_sug_timing_track(sug)
    by_kanji = {r.kanji: r for r in track.rubies}
    koi = by_kanji["恋"]
    assert (koi.target_line_index, koi.target_char_start, koi.target_char_end) == (0, 0, 1)
    assert koi.reading == "こい"
    assert koi.reading_part_ms == [280]
    hito = by_kanji["一人"]
    assert (hito.target_line_index, hito.target_char_start, hito.target_char_end) == (1, 0, 2)
    assert hito.reading == "ひとり"
    assert "".join(hito.reading_parts) == "ひとり"
    # @RubyN 不进 nicokara_tags.custom（已物化，避免导出重复）
    saved = json.loads(sug.read_text(encoding="utf-8-sig"))
    custom = (saved.get("nicokara_tags") or {}).get("custom") or []
    assert not any(str(line).upper().startswith("@RUBY") for line in custom)


def test_rebuild_merges_sentinel_zero_begin_into_shared_block(tmp_path):
    """BeginTime<=0 是「无独立时间」哨兵：并入共享块，不产生独立起点。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info([
            _char("O", 2000, 3000),
            _char("NE", 0, 0),
            _char(" ", 4000, 5000),
            _char("TWO", 0, 0),
        ]),
    ]
    load_n3proj(_write_n3proj(tmp_path, payload))

    track = load_sug_timing_track(tmp_path / "demo_从N3重建.sug")
    line = track.lines[0]
    assert "".join(char.text for char in line.chars) == "ONE TWO"
    starts = [char.start_ms for char in line.chars]
    # O 锚定 2000；NE/空格间隔并入块内由加载端均分；空格 4000 是独立锚；
    # TWO 并入空格块
    assert starts[0] == 2000
    assert starts[3] == 4000
    assert all(starts[0] < v < starts[3] for v in starts[1:3])


def test_rebuild_applies_head_offset_tag(tmp_path):
    """AtTagsForSave 里的 @Headoffset 在 .sug 加载端烘焙进行首（与 LRC 同语义）。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["AtTagsForSave"] = ["@Title=デモ", "@Headoffset=-100"]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    track = load_sug_timing_track(tmp_path / "demo_从N3重建.sug")
    assert track.meta.title == "デモ"
    assert track.meta.head_offset_ms == -100
    # 行首时间戳被烘焙：1000 → 900，5000 → 4900
    assert track.lines[0].chars[0].start_ms == 900
    assert track.lines[1].chars[0].start_ms == 4900


def test_rebuild_applies_sug_export_compensation(tmp_path, _isolated_settings_dir):
    """预设了 SUG 软件导出补偿时：.sug 回读与行级 display 时刻同步平移。"""
    settings_file = _isolated_settings_dir / "settings.json"
    settings_file.write_text(
        json.dumps(
            {"lyrics_timing": {"export": {"software_compensation_ms": 250}}}
        ),
        encoding="utf-8",
    )
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    payload["SourceLyricsInfos"][0]["LineInfos"] = [
        _line_info(
            [_char("あ", 1000, 2000), _char("い", 2000, 3000)],
            show_begin=500,
            show_end=4000,
        ),
    ]
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    data = result.project_data
    sug = Path(data["subtitle_path"])
    # 导入回读按补偿口径：与主窗口加载 .sug 的行为一致
    track = load_sug_timing_track(sug, software_compensation_ms=250)
    assert [char.start_ms for char in track.lines[0].chars] == [1250, 2250]
    # 行级 display 覆盖（上屏/消失时刻）随轨道同坐标平移
    assert data["line_display_overrides"][0] == [750, 4250]


def test_rebuild_without_compensation_keeps_raw_times(tmp_path):
    """未预设补偿时重建时间戳保持 N3 原值（默认 settings 为空）。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    track = load_sug_timing_track(Path(result.project_data["subtitle_path"]))
    assert [char.start_ms for char in track.lines[0].chars] == [1000, 2000]


def test_rebuild_never_overwrites_foreign_same_name_file(tmp_path):
    """撞名保护：既有同名文件不是 N3 重建产物时换名重建，原文件不动。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    # 预置用户自己的同名 .sug（无重建标记）
    foreign = tmp_path / "demo_从N3重建.sug"
    foreign.write_text('{"version": "0.3.0", "sentences": []}', encoding="utf-8")

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    renamed = tmp_path / "demo_从N3重建_2.sug"
    assert renamed.is_file()
    assert foreign.read_text(encoding="utf-8") == '{"version": "0.3.0", "sentences": []}'
    assert result.project_data["subtitle_path"] == str(renamed)
    assert any("冲突" in warning and "demo_从N3重建_2.sug" in warning for warning in result.warnings)
    # 新产物带重建标记
    saved = json.loads(renamed.read_text(encoding="utf-8-sig"))
    assert saved["nicokara_tags"]["n3_rebuild_source"] == "demo.lrc"


def test_rebuild_regenerates_over_own_previous_output(tmp_path):
    """撞名保护：同名旧产物（带标记）直接覆写，不换名、无告警。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()
    first = load_n3proj(_write_n3proj(tmp_path, payload))
    target = Path(first.project_data["subtitle_path"])
    assert target.name == "demo_从N3重建.sug"

    second = load_n3proj(_write_n3proj(tmp_path, payload))

    assert Path(second.project_data["subtitle_path"]) == target
    assert not (tmp_path / "demo_从N3重建_2.sug").exists()
    assert not any("冲突" in warning for warning in second.warnings)


def test_load_n3proj_reports_progress(tmp_path):
    """progress_cb 在读取与重建阶段回调，回调异常不影响导入。"""
    payload = _project_payload(tmp_path)
    (tmp_path / "demo.lrc").unlink()

    events: list[tuple[int, str]] = []

    def progress(percent: int, message: str) -> None:
        events.append((percent, message))
        if len(events) == 1:
            raise RuntimeError("前台回调炸了也不许影响导入")

    result = load_n3proj(
        _write_n3proj(tmp_path, payload), progress_cb=progress
    )

    assert Path(result.project_data["subtitle_path"]).is_file()
    percents = [percent for percent, _message in events]
    assert percents == sorted(percents)  # 单调推进
    assert percents[-1] >= 90
    assert any("重建" in message for _p, message in events)
    assert any("读取工程" in message for _p, message in events)


def test_extra_source_missing_file_rebuilds_sug(tmp_path):
    """副字幕源（コーラス）文件丢失同样按 N3 数据重建并改指新 .sug。"""
    payload = _project_payload(tmp_path)
    chorus = tmp_path / "chorus.lrc"
    chorus.write_text(
        "[00:11:00]か[00:12:00]ら\n", encoding="utf-8"
    )
    # N3 记录的副源行数比实际 LRC 多一行 → 行数不一致也触发重建
    payload["SourceLyricsInfos"].append(
        {
            "SourceLyricsPath": str(chorus),
            "SourceLyricsRelativePath": "chorus.lrc",
            "SettingsName": "コーラス",
            "LineInfos": [
                _line_info([_char("か", 11000, 12000)], layout_index=0),
                _line_info([_char("ら", 12000, 13000)], layout_index=0),
            ],
        }
    )
    chorus.unlink()
    result = load_n3proj(_write_n3proj(tmp_path, payload))

    extra_sources = result.project_data["extra_subtitle_sources"]
    assert len(extra_sources) == 1
    # 副源重建文件名带源名（同名防撞）
    sug = tmp_path / "chorus_コーラス_从N3重建.sug"
    assert sug.is_file()
    assert extra_sources[0]["path"] == str(sug)
    track = load_sug_timing_track(sug)
    assert [char.text for char in track.lines[0].chars] == ["か"]
    assert [char.text for char in track.lines[1].chars] == ["ら"]


def test_unsupported_dest_format_warns(tmp_path):
    payload = _project_payload(tmp_path)
    payload["DestFormat"] = 0
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    assert "output_path" not in result.project_data["output"]
    assert "output_format" not in result.project_data["output"]
    assert any("输出格式" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("dest_format", "expected_format"),
    [
        (2, OUTPUT_FORMAT_PNG_COMPOSITED),
        (3, OUTPUT_FORMAT_PNG_TRANSPARENT),
    ],
)
def test_png_sequence_dest_format_maps_to_png_export(tmp_path, dest_format, expected_format):
    payload = _project_payload(tmp_path)
    payload["DestFormat"] = dest_format
    payload["DestPath"] = str(tmp_path / "video_ニコカラメーカー3出力.png")
    result = load_n3proj(_write_n3proj(tmp_path, payload))
    output = result.project_data["output"]
    assert output["output_format"] == expected_format
    # N3 自动命名同样替换为本模块默认后缀；PNG 序列只取 stem 作导出名。
    assert Path(output["output_path"]).stem.endswith(DEFAULT_OUTPUT_NAME_SUFFIX)
    assert not any("输出格式" in warning for warning in result.warnings)


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


def test_sequence_background_keeps_fractional_fps(tmp_path):
    payload = _project_payload(tmp_path)
    sequence = tmp_path / "frame_%04d.png"
    sequence.write_bytes(b"fake")
    payload["SourceInfo"].update(
        {"SourceKind": 2, "ImagePath": str(sequence), "ImageRelativePath": sequence.name}
    )
    payload["SourceInfo"]["Fps"] = 29.97

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["background"]["source_fps"] == 29.97


def test_sequence_background_falls_back_for_invalid_fps(tmp_path):
    payload = _project_payload(tmp_path)
    sequence = tmp_path / "frame_%04d.png"
    sequence.write_bytes(b"fake")
    payload["SourceInfo"].update(
        {"SourceKind": 2, "ImagePath": str(sequence), "ImageRelativePath": sequence.name}
    )
    payload["SourceInfo"]["Fps"] = "not-a-number"

    result = load_n3proj(_write_n3proj(tmp_path, payload))

    assert result.project_data["background"]["source_fps"] == 60


# ---------------------------------------------------------------------------
# 本机真实样例（可选回归）
#
# 通过环境变量 ``KARAOKE_STUDIO_N3_SAMPLES`` 指向本地 N3 工程收藏根目录，
# 测试按目录/文件名关键词发现样例；未设置或找不到即整组跳过。固化用例
# 不硬编码任何工作机路径，换环境 / 上 CI 不会失效。
# ---------------------------------------------------------------------------

import os


def _discover_n3_sample(keyword: str) -> Path:
    """在 ``KARAOKE_STUDIO_N3_SAMPLES`` 下按路径关键词找一个 .n3proj 样例。"""
    root = os.environ.get("KARAOKE_STUDIO_N3_SAMPLES", "").strip()
    if not root:
        return Path("__n3_samples_env_not_set__.n3proj")
    base = Path(root)
    if not base.is_dir():
        return Path("__n3_samples_env_not_set__.n3proj")
    for candidate in sorted(base.rglob("*.n3proj")):
        if keyword.lower() in str(candidate).lower():
            return candidate
    return Path(f"__n3_sample_{keyword}_not_found__.n3proj")


def _all_n3_samples(limit: int = 48) -> list[Path]:
    root = os.environ.get("KARAOKE_STUDIO_N3_SAMPLES", "").strip()
    if not root:
        return []
    base = Path(root)
    if not base.is_dir():
        return []
    return [p for p in sorted(base.rglob("*.n3proj")) if p.is_file()][:limit]


REAL_N3PROJ = _discover_n3_sample("marginality")
TACTIC_N3PROJ = _discover_n3_sample("tactic")
DARK_SPIRAL_N3PROJ = _discover_n3_sample("dark spiral")
ISEKAI_GIRLS_N3PROJ = _discover_n3_sample("異世界ガールズ")
ATTCHI_N3PROJ = _discover_n3_sample("035")
UNIVERSE_PAGE_N3PROJ = _discover_n3_sample("036")


def _forced_missing_copy(sample: Path, tmp_path: Path) -> Path:
    """把样例工程复制到 tmp 并让每个字幕源指向各自不存在的文件，强制走重建分支。

    同时把样例目录的图片资产带过去——``@Emoji`` 头像按 .sug 所在目录解析，
    生产环境重建 .sug 与图片同在 n3proj 目录，缺图会把标签退化为可见文本。
    """
    with zipfile.ZipFile(sample) as z:
        payload = json.loads(z.read("0").decode("utf-8-sig"))
    payload = json.loads(json.dumps(payload))
    for index, info in enumerate(payload.get("SourceLyricsInfos") or []):
        # 每个源独立的缺失名：多源工程不能共用一个名字（重建目标同名会互撞）
        info["SourceLyricsPath"] = str(tmp_path / f"missing{index}.lrc")
        info["SourceLyricsRelativePath"] = f"missing{index}.lrc"
    for image in sample.parent.glob("*"):
        if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif"}:
            try:
                if image.stat().st_size <= 2 * 1024 * 1024:
                    shutil.copyfile(image, tmp_path / image.name)
            except OSError:
                pass
    target = tmp_path / "forced.n3proj"
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("0", "\ufeff" + json.dumps(payload, ensure_ascii=False))
    return target


@pytest.mark.skipif(not ATTCHI_N3PROJ.is_file(), reason="035 あっちでこっちで样例不存在")
def test_import_real_ruby_project_rebuild_matches_lrc_round_trip(tmp_path):
    """035 样例（AtTagsForSave 整串文本 + 53 条 @RubyN）：重建 .sug 的 ruby
    与 N3 自己的 LRC 往返逐条一致（行/字符区间/漢字/读音）。"""
    from krok_helper.subtitle_render.n3.project_import import _rebuild_sug_source

    with zipfile.ZipFile(ATTCHI_N3PROJ) as z:
        info = json.loads(z.read("0").decode("utf-8-sig"))["SourceLyricsInfos"][0]
    lrc_track = load_nicokara_lrc(Path(info["SourceLyricsPath"]))

    result = _rebuild_sug_source(info, tmp_path, [], "字幕源")
    assert result is not None
    _sug, rebuilt_track = result

    assert [
        "".join(c.text for c in line.chars)
        for line in rebuilt_track.lines
        if not line.is_blank
    ] == [
        "".join(c.text for c in line.chars)
        for line in lrc_track.lines
        if not line.is_blank
    ]
    assert {(r.kanji, r.reading) for r in rebuilt_track.rubies} == {
        (r.kanji, r.reading) for r in lrc_track.rubies
    }
    for ruby in lrc_track.rubies:
        if ruby.target_line_index is None:
            continue
        match = [
            r
            for r in rebuilt_track.rubies
            if r.kanji == ruby.kanji
            and r.reading == ruby.reading
            and r.target_line_index == ruby.target_line_index
        ]
        assert match, (ruby.kanji, ruby.reading, ruby.target_line_index)
        assert any(
            r.target_char_start == ruby.target_char_start
            and r.target_char_end == ruby.target_char_end
            for r in match
        )


@pytest.mark.skipif(
    not UNIVERSE_PAGE_N3PROJ.is_file(), reason="036 ユニバーページ样例不存在"
)
def test_import_real_missing_lrc_project_rebuilds_with_ruby(tmp_path):
    """036 样例（90 条 @RubyN）：把工程复制到无 LRC 的目录强制走缺失重建。"""
    forced = _forced_missing_copy(UNIVERSE_PAGE_N3PROJ, tmp_path)

    result = load_n3proj(forced)

    sug = tmp_path / "missing0_从N3重建.sug"
    assert sug.is_file()
    assert result.project_data["subtitle_path"] == str(sug)
    track = load_sug_timing_track(sug)
    assert sum(1 for line in track.lines if not line.is_blank) == 42
    assert track.rubies
    assert track.meta.title == "ユニバーページ"
    # @Headoffset=-50 烘焙进行首
    assert track.meta.head_offset_ms == -50
    # 每条 ruby 的基底文本与其目标字符区间严格一致
    for ruby in track.rubies:
        if ruby.target_line_index is None:
            continue
        line = track.lines[ruby.target_line_index]
        span = "".join(
            c.text
            for c in line.chars[ruby.target_char_start : ruby.target_char_end]
        )
        assert span == ruby.kanji, (ruby.kanji, span)


@pytest.mark.parametrize(
    "sample",
    _all_n3_samples(),
    ids=lambda p: p.stem[:40],
)
def test_real_sample_forced_rebuild_matches_embedded_n3_data(sample, tmp_path):
    """通用回归：目录下每个样例强制缺失重建后，行文本与 N3 内嵌数据逐字一致。

    重建契约是「以 n3project 内数据为准」——n3proj 的 ``LineInfos`` 是权威副
    本，部分样例的 LRC 与嵌入副本存在真实漂移（AtTagsForSave 空串丢 ruby、
    标签大小写 / 尾随空格差），因此不与 LRC 对拍；LRC 全量对拍由 035 专属用
    例（实测零漂移样例）锁定。SUG 导入器与 N3 渲染器在同词多音 + 重复行上
    的选条语义差异（039 样例 128 条中 1 条）同样不作为断言。
    """
    from krok_helper.subtitle_render.n3.project_import import (
        _is_synthetic_emoji_tag_char,
        _n3_line_key_text,
    )

    with zipfile.ZipFile(sample) as z:
        payload = json.loads(z.read("0").decode("utf-8-sig"))
    expected_texts = [
        text
        for text in (
            _n3_line_key_text(line)
            for line in payload["SourceLyricsInfos"][0].get("LineInfos") or []
            if line.get("Kind") == 1
        )
        if text
    ]

    rebuilt = load_n3proj(_forced_missing_copy(sample, tmp_path))
    sug_path = Path(rebuilt.project_data["subtitle_path"] or "")
    assert sug_path.is_file()
    track = load_sug_timing_track(sug_path)

    # 比较口径：.sug 加载端按 SUG 生态约定丢弃行尾无时间戳的尾随空白，
    # @Emoji 插入的合成标签字符不算可见正文——两侧 rstrip 并剔除合成字符。
    assert [
        "".join(c.text for c in line.chars if not _is_synthetic_emoji_tag_char(c.text)).rstrip()
        for line in track.lines
        if not line.is_blank
    ] == [text.rstrip() for text in expected_texts]
    # ruby 自洽：基底文本与目标字符区间严格一致
    for ruby in track.rubies:
        if ruby.target_line_index is None:
            continue
        assert 0 <= ruby.target_line_index < len(track.lines)
        line = track.lines[ruby.target_line_index]
        span = "".join(
            c.text
            for c in line.chars[ruby.target_char_start : ruby.target_char_end]
        )
        assert span == ruby.kanji, (ruby.kanji, span)


@pytest.mark.skipif(not REAL_N3PROJ.is_file(), reason="本机样例工程不存在")
def test_import_real_project_smoke():
    result = load_n3proj(REAL_N3PROJ)
    style = style_from_dict(result.project_data["style"])
    assert style.font_family == "UD デジタル 教科書体 N-B"
    assert style.karaoke_colors.after.text.color == "#1C6FB5"
    assert [layout.name for layout in style.layouts[:5]] == [
        "下寄せ3行", "上寄せ2行", "コーラス", "タイトル左上", "タイトル中央",
    ]
    assert set(style.custom_style_schemes) == {
        "青配色", "緑配色", "グラデーション配色", "コーラス配色", "情報小", "情報中", "情報大",
    }
    assert style.title_overlays
    assert style.title_overlays[0].show_mode == "whole"
    assert len(result.project_data["line_layout_indices"]) == 34
    direct_track = load_nicokara_lrc(Path(result.project_data["subtitle_path"]))
    assert all(line.break_before == "none" for line in direct_track.lines)
    assert any(
        value != "none" for value in result.project_data["line_breaks_before"]
    )


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
    assert set(result.project_data["line_layout_indices"]) == {0}
    assert style.title_overlays
    assert style.title_overlays[0].layout_index == 4
    used_layout_indices = {
        *result.project_data["line_layout_indices"],
        *(
            overlay.layout_index
            for overlay in style.title_overlays
            if overlay.layout_index
        ),
    }
    assert all(
        style.layouts[index - 1].letter_spacing_px == 0
        for index in used_layout_indices
        if index > 0
    )
    assert all(overlay.letter_spacing_px == 0 for overlay in style.title_overlays)


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
    assert entry["char_role_labels"] == [["青配色", "標準配色"]]
    # 不再出现「仅导入第一个」的提示
    assert not any("仅导入第一个" in w for w in result.warnings)
