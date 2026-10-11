"""字体解析正确性：能力层逐 face 轴检测、实例决策、缺字回退与 IR 下发契约。

本文件是「解析正确 → CPU/GPU 一致 → 尽量保持 v4.2.x 放大表现」这条优先级
的可复现基线：

* 能力层：逐 face 判可变（静态/可变同名共存不再互相误伤）、斜体 face 清单。
* 决策层：weight+italic 联合匹配、合成加粗/倾斜标注、精确性/回退原因。
* 覆盖层：按文本簇判覆盖（组合符/变体选择符/控制符不误判），缺字时以
  Qt(CPU) 实测选字作为回退族名。
* 契约：IR 里 ``*_font_resolved`` 与 ``fallback_family`` 的出现条件。

环境相关断言一律先探测字体是否安装（不同机器字体集不同）。
"""

from __future__ import annotations

from PyQt6.QtGui import QFontDatabase, QFontInfo

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
    clear_capabilities_cache,
    get_capabilities,
)
from krok_helper.subtitle_render.engine.text.weight_resolver import (
    FontRequest,
    resolve_instance,
)


def _require_family(family: str) -> None:
    """跳过条件：字体未安装 **或** 当前 Qt 平台没有字体库。

    offscreen 平台（本仓库 conftest 默认）枚举不到任何字体族——face 级
    断言在那种会话里只能跳过；真实平台（windows）下的端到端校验见
    ``tests/test_subtitle_render_font_resolution_windows.py``（子进程）。
    """
    import pytest

    if family in QFontDatabase.families():
        return
    if not QFontDatabase.families():
        pytest.skip("font database unavailable on this Qt platform (offscreen?)")
    pytest.skip(f"font family not installed: {family}")


def _cap(family: str, weights, italic_weights=(), **kwargs) -> FontCapabilities:
    faces = tuple(
        FontFace(w, f"w{w}", False) for w in sorted(weights)
    ) + tuple(FontFace(w, f"w{w} Italic", True) for w in sorted(italic_weights))
    return FontCapabilities(family=family, faces=faces, **kwargs)


# =========================================================================
# 能力层：逐 face 判可变 / 斜体清单
# =========================================================================


def test_named_instance_variable_family_marks_faces_variable():
    """Noto Sans SC 这类「一个 VF 文件注册多个命名实例」的族：逐 face 标可变。"""
    family = "Noto Sans SC"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    assert cap is not None
    assert cap.axis_present is True
    assert cap.axis_effective is True
    assert cap.variable_style is not None
    variable_face = cap.variable_face
    assert variable_face is not None
    assert variable_face.has_weight_axis
    assert (cap.axis_min, cap.axis_max) == (100.0, 900.0)
    # 族内不应存在「静态却带轴」的 face 判定
    assert all(face.is_variable for face in cap.faces)


def test_static_family_has_no_axis_present():
    """MS Gothic 单 face 静态族：axis_present False、无斜体 face。"""
    family = "MS Gothic"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    assert cap is not None
    assert cap.axis_present is False
    assert cap.axis_effective is False
    assert cap.variable_style is None
    assert cap.has_italic_face is False
    assert all(not face.is_variable for face in cap.faces)


def test_italic_family_reports_italic_faces():
    family = "Segoe UI"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    assert cap is not None
    assert cap.has_italic_face is True
    assert any(face.is_italic for face in cap.faces)


def test_capabilities_cache_invalidates_on_generation_bump():
    family = "MS Gothic"
    _require_family(family)
    first = get_capabilities(family)
    assert get_capabilities(family) is first
    clear_capabilities_cache()
    second = get_capabilities(family)
    assert second is not None
    assert second == first


# =========================================================================
# 决策层：weight + italic 联合匹配
# =========================================================================


def test_static_single_face_bold_italic_requests_both_simulations():
    """单 face 族（MS Gothic {400}）：700+斜体 = 合成加粗 + 合成倾斜。"""
    family = "MS Gothic"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    instance = resolve_instance(
        cap, FontRequest(family=family, weight=700, italic=True)
    )
    assert instance.face_weight == 400
    assert instance.synthetic_bold is True
    assert instance.synthetic_italic is True
    assert instance.exact is False
    assert instance.reason == "synthetic_bold"
    assert instance.axis_value is None


def test_static_real_italic_face_is_not_synthetic():
    """有真实斜体 face 的族：斜体请求命中该 face，不标合成倾斜。"""
    family = "Segoe UI"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    instance = resolve_instance(
        cap, FontRequest(family=family, weight=400, italic=True)
    )
    assert instance.synthetic_italic is False
    assert instance.exact is True
    assert instance.reason is None
    assert instance.face_style is not None


def test_variable_family_italic_request_keeps_axis_instance():
    """真可变族 + 斜体：走轴实例（族内无斜体 face ⇒ 合成倾斜）。"""
    family = "Noto Sans SC"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    instance = resolve_instance(
        cap, FontRequest(family=family, weight=700, italic=True)
    )
    assert instance.is_variable is True
    assert instance.axis_value == 700.0
    assert instance.face_weight == 700
    assert instance.synthetic_italic is True
    assert instance.synthetic_bold is False
    assert instance.exact is True


def test_variable_axis_clamped_marks_out_of_range():
    family = "Noto Sans SC"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    instance = resolve_instance(cap, FontRequest(family=family, weight=1000))
    assert instance.axis_value == 900.0
    assert instance.exact is False
    assert instance.reason == "axis_clamped"
    assert instance.mark == "越界"


def test_missing_family_decision_is_explicit():
    clear_capabilities_cache()
    instance = resolve_instance(None, FontRequest(family="__none__", weight=700))
    assert instance.reason == "missing_family"
    assert instance.axis_value is None
    assert instance.face_style is None
    assert instance.exact is False


def test_axis_declared_but_ineffective_reports_flag_not_axis():
    """表里有 wght 轴但渲染无差异（伪可变）：按静态解析并如实标注。"""
    cap = _cap(
        "PseudoVF",
        weights=(400,),
        axis_min=100.0,
        axis_max=900.0,
        axis_present=True,
        axis_effective=False,
    )
    instance = resolve_instance(cap, FontRequest(family="PseudoVF", weight=700))
    assert instance.axis_value is None
    assert instance.axis_ineffective is True
    assert instance.synthetic_bold is True


def test_identity_is_path_free_and_descriptive():
    cap = _cap("Segoe UI", weights=(400, 700))
    instance = resolve_instance(cap, FontRequest(family="Segoe UI", weight=700))
    identity = instance.identity
    assert "Segoe UI" in identity
    assert "\\" not in identity and ":" not in identity
    assert "w=700" in identity


# =========================================================================
# 决策 ↔ 引擎实测交叉验证
# =========================================================================


def test_italic_synthesis_prediction_matches_engine_styles():
    """斜体合成标注 ↔ Qt 实际命中 face 交叉验证。

    QFontInfo.styleName() 报出的 style 不在族内真实 face 清单 ⇒ Qt 走了
    合成倾斜。实测（2026-10-11）：MS Gothic/Yu Gothic/Microsoft YaHei/
    Noto Sans SC 无斜体 face ⇒ 返回 'Oblique' 系合成名；Segoe UI/Meiryo
    有真实斜体 face ⇒ 返回清单内名字。
    """
    from krok_helper.subtitle_render.engine.text.font_weight import (
        build_weight_font,
    )

    checks = (
        ("MS Gothic", 400),
        ("Yu Gothic", 400),
        ("Microsoft YaHei", 400),
        ("Noto Sans SC", 400),
        ("Segoe UI", 400),
        ("Meiryo", 400),
    )
    seen = 0
    for family, weight in checks:
        if family not in QFontDatabase.families():
            continue
        clear_capabilities_cache()
        cap = get_capabilities(family)
        instance = resolve_instance(
            cap, FontRequest(family=family, weight=weight, italic=True)
        )
        font = build_weight_font(family, 64, weight, italic=True)
        info = QFontInfo(font)
        real_styles = list(QFontDatabase.styles(family))
        engine_synth_italic = info.styleName() not in real_styles
        assert instance.synthetic_italic == engine_synth_italic, (
            f"{family}: predicted synthetic_italic="
            f"{instance.synthetic_italic} but engine reported "
            f"{info.styleName()!r} (faces={real_styles})"
        )
        seen += 1
    if seen == 0:
        import pytest

        pytest.skip("no probe fonts installed")


def test_static_snap_rule_matches_engine_pick():
    """静态就近匹配（平局取更接近 Normal(400) 的 face）↔ QFontInfo 实测一致。"""
    from krok_helper.subtitle_render.engine.text.font_weight import (
        build_weight_font,
    )

    family = "Segoe UI"
    _require_family(family)
    clear_capabilities_cache()
    cap = get_capabilities(family)
    for weight in (350, 500, 650, 800):
        instance = resolve_instance(cap, FontRequest(family=family, weight=weight))
        info = QFontInfo(build_weight_font(family, 48, weight))
        real = {
            style: int(QFontDatabase.weight(family, style))
            for style in QFontDatabase.styles(family)
            if not QFontDatabase.italic(family, style)
        }
        engine_style = info.styleName()
        # 合成加粗时 Qt 报「合成的 Bold」名（不在真实清单里）——只校验非合成档。
        if engine_style in real:
            assert real[engine_style] == instance.face_weight, (
                f"{family}@{weight}: predicted face {instance.face_weight} "
                f"but engine picked {engine_style}({real[engine_style]})"
            )
