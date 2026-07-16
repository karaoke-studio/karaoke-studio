"""NicoKaraMaker3 ``TemplateFont/*.tpl`` discovery and conversion."""

from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest

from krok_helper.subtitle_render.models import StylePreset, SubtitleStyleScheme
from krok_helper.subtitle_render.n3_template_import import (
    N3_TEMPLATE_SOURCE_TYPE,
    default_n3_template_directories,
    find_n3_template_files,
    load_n3_font_template,
    load_n3_font_templates,
    merge_n3_template_presets,
    n3_template_size,
    resolve_n3_template_preset,
)


REAL_TEMPLATE_DIR = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "SHINTA"
    / "NicoKaraMaker3"
    / "TemplateFont"
)


def _size(size: int, ratio: float) -> dict:
    return {"Size": size, "Reference": 1440, "Ratio": ratio}


def _font_info(
    family: str,
    face: str,
    char_size: tuple[int, float],
    edge_size: tuple[int, float],
    edge2_size: tuple[int, float],
    use_edge2: bool | None,
) -> dict:
    return {
        "FontName": family,
        "FontFaceName": face,
        "CharSize": _size(*char_size),
        "EdgeSize": _size(*edge_size),
        "EdgeSize2": _size(*edge2_size),
        "UseEdge2": use_edge2,
    }


def _brush(index: int) -> dict:
    return {
        "SettingsName": f"笔刷{index}",
        "SelectedBrushTypeIndex": 0,
        "SolidColor": {
            "DxColor": {
                "A": 1.0,
                "R": index / 8,
                "G": (8 - index) / 8,
                "B": 0.25,
            }
        },
    }


def _payload(name: str = "标准配色", *, synchronize: bool = True) -> dict:
    return {
        "Guid": str(uuid.uuid4()),
        "SettingsName": name,
        "Synchronize": synchronize,
        "FontInfos": [
            _font_info("游明朝", "Bold", (133, 100 / 1080), (20, 10 / 1080), (12, 6 / 1080), True),
            {},
            _font_info("Comic Sans MS", "Normal", (120, 90 / 1080), (16, 8 / 1080), (8, 4 / 1080), False),
            _font_info("游明朝", "Bold", (60, 45 / 1080), (8, 4 / 1080), (6, 3 / 1080), True),
            {},
            _font_info("Comic Sans MS", "Normal", (48, 36 / 1080), (6, 3 / 1080), (4, 2 / 1080), False),
        ],
        "BrushInfos": [_brush(index) for index in range(8)],
        "DecorKind": 2,
        "DecorSize": _size(13, 5 / 1080),
        "BlurLevel": 1,
    }


def _write_template(directory: Path, payload: dict, *, name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    filename = name or f"{payload['Guid']}.tpl"
    path = directory / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0", b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    return path


def test_template_size_uses_ratio_only_when_height_and_ratio_are_nonzero():
    assert n3_template_size(_size(133, 100 / 1080), 1080) == 100
    assert n3_template_size(_size(133, 100 / 1080), 1440) == 133
    assert n3_template_size(_size(133, 0), 1080) == 133
    assert n3_template_size(_size(133, 100 / 1080), 0) == 133


def test_load_template_scales_every_font_domain_and_decor(tmp_path):
    path = _write_template(tmp_path, _payload())
    result = load_n3_font_template(path, target_height=1080)
    scheme = result.preset.scheme

    assert result.name == "标准配色"
    assert result.preset.group == "N3"
    assert result.preset.source_type == N3_TEMPLATE_SOURCE_TYPE
    assert scheme.font_size_px == 100
    assert scheme.stroke_width_px == 10
    assert scheme.stroke2_width_px == 6
    assert scheme.latin_font_size_px == 90
    assert scheme.latin_stroke_width_px == 8
    assert scheme.latin_stroke2_width_px == 4
    assert scheme.ruby_font_size_px == 45
    assert scheme.ruby_stroke_width_px == 4
    assert scheme.ruby_stroke2_width_px == 3
    assert scheme.ruby_latin_font_size_px == 36
    assert scheme.ruby_latin_stroke_width_px == 3
    assert scheme.ruby_latin_stroke2_width_px == 2
    assert scheme.glow_radius_px == 5
    assert scheme.glow_before_radius_px == 5
    assert scheme.glow_after_radius_px == 5


def test_retained_template_payload_resolves_again_for_new_output_height(tmp_path):
    path = _write_template(tmp_path, _payload())
    original = load_n3_font_template(path, target_height=1080).preset

    resolved, warnings = resolve_n3_template_preset(original, target_height=2160)

    assert not warnings
    assert original.scheme.font_size_px == 100
    assert resolved.scheme.font_size_px == 200
    assert resolved.scheme.ruby_font_size_px == 90
    assert resolved.scheme.glow_radius_px == 10
    assert resolved.source_data == original.source_data


def test_batch_skips_deleted_template_and_isolates_broken_file(tmp_path):
    active = _write_template(tmp_path, _payload("B"))
    _write_template(tmp_path, _payload("A", synchronize=False))
    broken = tmp_path / "broken.tpl"
    broken.write_bytes(b"not a zip")

    result = load_n3_font_templates([tmp_path], target_height=1080)

    assert [item.path for item in result.templates] == [active]
    assert len(result.skipped) == 1
    assert "Synchronize=false" in result.skipped[0][1]
    assert result.failed == ((broken, "不是有效的 NicoKaraMaker3 字体模板（zip 解包失败）"),)


def test_template_requires_entry_zero_and_matching_guid(tmp_path):
    payload = _payload()
    wrong_name = _write_template(tmp_path, payload, name=f"{uuid.uuid4()}.tpl")
    with pytest.raises(ValueError, match="Guid 与文件名不一致"):
        load_n3_font_template(wrong_name, target_height=1080)

    missing_entry = tmp_path / "plain.tpl"
    with zipfile.ZipFile(missing_entry, "w") as archive:
        archive.writestr("font.json", json.dumps(payload))
    with pytest.raises(ValueError, match="缺少固定条目 0"):
        load_n3_font_template(missing_entry, target_height=1080)


def test_discovery_covers_roaming_and_msix_template_directories(tmp_path):
    roaming = tmp_path / "Roaming"
    local = tmp_path / "Local"
    normal = roaming / "SHINTA" / "NicoKaraMaker3" / "TemplateFont"
    msix = local / "Packages" / "SHINTA.N3_abc" / "Settings" / "TemplateFont"
    first = _write_template(normal, _payload("Normal"))
    second = _write_template(msix, _payload("MSIX"))

    directories = default_n3_template_directories(appdata=roaming, localappdata=local)

    assert directories == [normal, msix]
    assert find_n3_template_files(directories) == [first, second]


@pytest.mark.parametrize(
    ("policy", "expected_names", "skipped", "renamed"),
    [
        ("overwrite", {"同名"}, (), ()),
        ("skip", {"同名"}, ("同名",), ()),
        ("rename", {"同名", "同名 (2)"}, (), (("同名", "同名 (2)"),)),
    ],
)
def test_merge_conflict_policies(tmp_path, policy, expected_names, skipped, renamed):
    incoming = load_n3_font_template(
        _write_template(tmp_path, _payload("同名")), target_height=1080
    )
    existing = {
        "同名": StylePreset(
            name="同名",
            group="N3",
            scheme=SubtitleStyleScheme(fill_color="#010203"),
        )
    }

    result = merge_n3_template_presets(existing, [incoming], conflict_policy=policy)

    assert set(result.presets) == expected_names
    assert result.skipped_names == skipped
    assert result.renamed == renamed
    if policy == "overwrite":
        assert result.presets["同名"].source_type == N3_TEMPLATE_SOURCE_TYPE
    elif policy == "skip":
        assert result.presets["同名"].scheme.fill_color == "#010203"


def test_merge_allows_same_name_in_different_group(tmp_path):
    incoming = load_n3_font_template(
        _write_template(tmp_path, _payload("同名")), target_height=1080
    )
    existing = {
        "custom-preset": StylePreset(
            name="同名",
            group="常用",
            scheme=SubtitleStyleScheme(fill_color="#010203"),
        )
    }

    result = merge_n3_template_presets(
        existing, [incoming], conflict_policy="overwrite"
    )

    assert len(result.presets) == 2
    assert {(preset.name, preset.group) for preset in result.presets.values()} == {
        ("同名", "常用"),
        ("同名", "N3"),
    }


@pytest.mark.skipif(not REAL_TEMPLATE_DIR.is_dir(), reason="本机没有 N3 TemplateFont")
def test_real_n3_templates_import_at_1080():
    files = find_n3_template_files([REAL_TEMPLATE_DIR])
    result = load_n3_font_templates([REAL_TEMPLATE_DIR], target_height=1080)

    assert not result.failed
    assert len(result.templates) + len(result.skipped) == len(files)
    assert all("Synchronize=false" in reason for _path, reason in result.skipped)
    assert {
        "【1配色】",
        "【2配色】",
        "【3配色】",
        "標準配色",
    } <= {item.name for item in result.templates}
    for item in result.templates:
        scheme = item.preset.scheme
        assert scheme.font_size_px is not None and scheme.font_size_px > 0
        assert scheme.stroke_width_px is not None and scheme.stroke_width_px > 0
        assert scheme.ruby_font_size_px is not None and scheme.ruby_font_size_px > 0
    standard = next(item for item in result.templates if item.name == "標準配色")
    assert standard.preset.scheme.font_size_px == 100
