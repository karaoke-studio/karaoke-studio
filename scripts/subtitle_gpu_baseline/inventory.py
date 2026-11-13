"""Auditable model/variant inventory, frozen by a checked-in test contract.

This inventories capabilities; it does not certify that a renderer implements
them. UI/native search locations are evidence, not inferred support guarantees.
"""
from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path
import re
from typing import get_args

from krok_helper.subtitle_render.domain import models, timing, paint
from .policy import COMPARISON_BUDGETS, budget_categories

ROOT = Path(__file__).resolve().parents[2]
MODELS = (models.Style, models.SubtitleStyleScheme, models.TitleOverlay, paint.PaintFill,
    timing.TimingChar, timing.TimingLine, timing.TimingTrackMeta, timing.GuideSymbol,
    timing.RubyAnnotation, timing.LineAnimationOverride, timing.TrackPage,
    timing.TrackPagePlan, timing.TrackSection, timing.TrackDisplayTiming)
VARIANTS = {name: tuple(get_args(getattr(timing, name))) for name in (
    "EntryAnimation", "ExitAnimation", "KaraokeAnimation", "SingFx", "LineSingFx")}
VARIANTS["ColorFillMode"] = tuple(get_args(paint.ColorFillMode))
for _name in ("ScanlineEffect", "ScanlineShape", "LitStyle", "LitAppearanceMode", "VolumeAppearanceMode"):
    VARIANTS[_name] = tuple(get_args(getattr(models, _name)))

FEATURE_LABELS = (
    "字体与 face", "普通横排", "分页/lane/网格", "上下 ruby", "竖排/RTL",
    "before/after 配色", "渐变/图片填充", "描边/发光/阴影", "基础走字",
    "基础入退场", "几何/光效入退场", "Utopia", "zoom_pulse/描边闪光",
    "noise/assemble/dissolve", "装饰粒子全种类", "粒子配色/装饰/速度",
    "普通扫描高亮", "烈焰/水泡走字线", "图片走字线", "扫描线粒子",
    "导唱符", "标题", "指示灯/音量柱", "行背景带", "多轨/角色/viewport",
    "背景与音频", "输出格式", "调度/取消/恢复", "Skia CPU fallback",
)

# Explicit exceptions distinguish persisted compatibility and derived/editor
# state from direct frame fields. They must not silently become new effects.
FIELD_NOTES = {
    "Style.entry_fx": "兼容持久字段；现有 burst 规划不消费，UI 已合入动画档",
    "Style.exit_fx": "兼容持久字段；现有 burst 规划不消费，UI 已合入动画档",
    "Style._lower_romaji_options": "派生的假名转写快照；不是新的用户样式字段",
    "Style.hidden_builtin_layout_ids": "布局编辑器显示状态，不直接改变字幕像素",
    "PaintFill.gradient_path": "编辑器 HSV 路径；渲染消费已烘焙 stops，metadata render=False",
}

_ENGINE = "krok_helper/subtitle_render/engine/"
_NATIVE = "native/subtitle_renderer/src/"
_ELEMENTS = _ENGINE + "render/elements/"
_EFFECTS = _ENGINE + "render/effects/"
FEATURE_SOURCES = {
    "F01": [_ENGINE + "text/font_resource.py", _NATIVE + "backends/direct2d/d2d_font_fallback.cpp"],
    "F02": [_ELEMENTS + "horizontal/layout.py"],
    "F03": [_ENGINE + "layout/plan/orchestrator.py", _ENGINE + "layout/line/qt_geometry.py"],
    "F04": [_ELEMENTS + "horizontal/ruby.py"], "F05": [_ELEMENTS + "vertical.py"],
    "F06": ["krok_helper/subtitle_render/domain/paint.py"],
    "F07": [_NATIVE + "backends/direct2d/d2d_paint_resources.cpp"],
    "F08": [_EFFECTS + "raster.py"], "F09": [_ELEMENTS + "horizontal/wipe.py"],
    "F10": [_ELEMENTS + "horizontal/transitions.py"],
    "F11": [_ELEMENTS + "horizontal/transitions.py", _NATIVE + "backends/direct2d/d2d_fx.cpp"],
    "F12": [_ELEMENTS + "horizontal/utopia.py"],
    "F13": [_ELEMENTS + "horizontal/transitions.py"], "F14": [_EFFECTS + "noise.py"],
    "F15": [_EFFECTS + "particles.py"], "F16": [_EFFECTS + "particles.py"],
    "F17": [_ELEMENTS + "horizontal/scanline.py"], "F18": [_ELEMENTS + "horizontal/scanline.py"],
    "F19": [_ELEMENTS + "scanline_image.py", _ELEMENTS + "scanline_image_asset.py"],
    "F20": [_EFFECTS + "particles.py"], "F21": [_ENGINE + "guide/semantics.py"],
    "F22": [_ELEMENTS + "title.py"], "F23": [_ELEMENTS + "signal.py"],
    "F24": [_ELEMENTS + "band.py"], "F25": [_ENGINE + "render/render_ir.py"],
    "F26": [_ENGINE + "renderer.py", "krok_helper/subtitle_render/project/resources.py"],
    "F27": [_ENGINE + "renderer.py"],
    "F28": ["krok_helper/subtitle_render/native/backend.py", _NATIVE + "runtime/gpu_preview_worker_pool.cpp"],
    "F29": [],  # Planned Raster implementation; never pretend it exists already.
}


def primary_feature(model: str, field: str) -> str:
    if model == "TitleOverlay":
        return "F22"
    if model == "PaintFill":
        return "F07"
    if model == "GuideSymbol":
        return "F21"
    if model == "RubyAnnotation":
        return "F04"
    if model == "LineAnimationOverride":
        return "F10"
    if model in ("TrackPage", "TrackPagePlan", "TrackSection"):
        return "F03"
    if model in ("TimingChar", "TimingTrackMeta", "TrackDisplayTiming"):
        return "F09" if model == "TimingChar" else "F25"
    if model == "TimingLine":
        return "F21" if "guide" in field else "F10" if "animation" in field else "F03"
    if field.startswith("line_band_"):
        return "F24"
    if field.startswith(("lit_", "volume_", "signals_")):
        return "F23"
    if field.startswith("scanline_image_"):
        return "F19"
    if field.startswith("scanline_effect"):
        return "F20"
    if field.startswith("scanline_"):
        return "F17"
    if field.startswith(("fx_", "anim_fx_")) or field == "sing_fx":
        return "F16"
    if field.startswith(("entry_", "exit_", "section_edge_", "section_head_anim", "section_tail_anim")):
        return "F10"
    if field.startswith("zoom_") or field == "karaoke_stroke_flash":
        return "F13"
    if field in ("karaoke_anim", "reverse_karaoke_anim", "ruby_main_progress_mode"):
        return "F09"
    if field == "title_overlays":
        return "F22"
    if field.startswith("viewport_") or field in ("singer_style_overrides", "custom_style_schemes"):
        return "F25"
    if field in ("right_to_left", "vertical"):
        return "F05"
    if "font" in field or field in ("italic", "latin_italic", "ruby_italic", "ruby_latin_italic", "n3_font_inheritance"):
        return "F01"
    if field.startswith(("ruby_", "lower_ruby", "_lower_romaji")) or field == "affects_ruby_anchor":
        return "F04"
    if field.startswith(("stroke", "decoration", "glow", "shadow", "latin_stroke")):
        return "F08"
    if field in ("base_color", "fill_color", "karaoke_colors"):
        return "F06"
    if field.startswith("fill_gradient"):
        return "F07"
    if field in ("letter_spacing_px", "space_width_percent", "allow_biting"):
        return "F02"
    return "F03"  # All remaining Style fields are recorded explicitly in the contract.


def contract() -> dict:
    """Stable source contract. New fields/enums require deliberate inventory review."""
    return {"schema": 1, "models": {
        model.__name__: {field.name: {
            "feature": primary_feature(model.__name__, field.name),
            "render_metadata": field.metadata.get("render", True),
            **({"note": FIELD_NOTES[f"{model.__name__}.{field.name}"]}
               if f"{model.__name__}.{field.name}" in FIELD_NOTES else {}),
        } for field in fields(model)} for model in MODELS},
        "variants": {name: list(values) for name, values in VARIANTS.items()},
        "comparison_budgets": COMPARISON_BUDGETS}


def reference_sites() -> dict[str, list[str]]:
    """Locate exact field spelling in UI and native transport sources (bounded output)."""
    paths = sorted((ROOT / "krok_helper/subtitle_render/frontend/properties").rglob("*.py"))
    paths += sorted((ROOT / "native/subtitle_renderer/src/protocol").glob("*.cpp"))
    paths += [ROOT / "krok_helper/subtitle_render/native/protocol.py"]
    wanted = {field.name for model in MODELS for field in fields(model)}
    sites = {name: [] for name in wanted}
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
            for name in wanted.intersection(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", line)):
                if len(sites[name]) < 6:
                    sites[name].append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    return sites


def inventory(cases) -> dict:
    base = contract()
    sites = reference_sites()
    base["model_sources"] = {model.__name__: Path(inspect.getfile(model)).relative_to(ROOT).as_posix()
                             for model in MODELS}
    base["case_budget_categories"] = {case.case_id: budget_categories(case) for case in cases}
    base["field_reference_sites"] = sites
    base["features"] = {f"F{i:02d}": {"label": label,
        "overlay_cases": [case.case_id for case in cases if f"F{i:02d}" in case.features],
        "scope": "product_suite" if i in (26, 27, 28, 29) else "subtitle_overlay",
        "implemented_by_skia": False,
        "source_entries": FEATURE_SOURCES[f"F{i:02d}"],
        "ui_entry": "krok_helper/subtitle_render/frontend/properties/property_panel.py",
        "ir_entry": "krok_helper/subtitle_render/native/protocol.py",
        "native_render_entry": _NATIVE + "backends/direct2d/d2d_backend_render.cpp" if i <= 25 else None,
        "test_entries": ["tests/test_subtitle_render_gpu_backend.py"] if i <= 25 else [
            "tests/test_subtitle_render_native_export.py" if i == 27 else "tests/test_subtitle_render_native_protocol.py"],
    } for i, label in enumerate(FEATURE_LABELS, 1)}
    base["suites"] = {
        "stills": {"cases": [case.case_id for case in cases]},
        "continuous": {"cases": ["combined_effects"], "duration_ms": 6000, "fps": [60, 120]},
        "random_seek": {"cases": [case.case_id for case in cases], "seed": 20261114},
        "export": {"cases": ["combined_effects", "g3_heavy_roles_fills_title_multisource"],
            "fps": [60, 120], "formats": ["mp4", "png_overlay", "png_composite", "prores4444", "qtrle"],
            "backgrounds": ["video_contain", "image_cover", "image_contain", "sequence", "solid"],
            "status": "specified_not_executed_by_overlay_capture"},
        "long_run": {"cases": ["combined_effects", "g3_heavy_roles_fills_title_multisource"],
            "duration_seconds": 1800, "operations": ["play", "seek", "style", "font", "resize", "cache_eviction"],
            "status": "specified_not_executed_by_overlay_capture"},
    }
    return base
