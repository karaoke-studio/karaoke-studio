"""统一「字重→实例」解析器（engine.text.font_weight）的规则测试。

规则与 native 侧 ``d2d_font_fallback.cpp`` 的 unified weight resolution
逐条对应；改动任一侧时这里的口径断言就是同步契约。
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics

from krok_helper.subtitle_render.engine.text.font_weight import (
    apply_weight_plan,
    bucket_weight,
    build_weight_font,
    family_weight_axis,
    physical_weight_styles,
    resolve_weight_plan,
)

_IWATA_FONT_PATH = (
    r"E:\KaraMaker\StrangeUtaGame\debugsource"
    r"\IwataUDGothic08StdNVFTTF-L\IwataUDGothic08StdNVFTTF-L.ttf"
)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_application_fonts():
    """模块级应用字体注册不泄漏到同进程的后续测试模块（painter 用例对
    字体库状态敏感）。"""
    yield
    QFontDatabase.removeAllApplicationFonts()
    from krok_helper.subtitle_render.engine.text.font_weight import (
        clear_font_weight_cache,
    )

    clear_font_weight_cache()


@pytest.mark.parametrize(
    ("requested", "expected"),
    (
        (100, 100),
        (250, 100),
        (300, 300),
        (350, 300),
        (400, 400),
        (450, 400),
        (500, 500),
        (550, 500),
        (600, 600),
        (650, 600),
        (700, 700),
        (750, 700),
        (800, 800),
        (850, 800),
        (900, 900),
        (950, 900),
        (0, 100),
    ),
)
def test_bucket_weight_matches_standard_hundreds(requested, expected):
    assert bucket_weight(requested) == expected


def _require_family(family: str) -> None:
    if family not in QFontDatabase.families():
        pytest.skip(f"font family not installed: {family}")


_YUGOTH_FONT_PATH = r"C:\Windows\Fonts\YuGothR.ttc"


def _register_yu_gothic() -> str:
    if not os.path.exists(_YUGOTH_FONT_PATH):
        pytest.skip("Yu Gothic font file not present")
    QFontDatabase.addApplicationFont(_YUGOTH_FONT_PATH)
    family = "Yu Gothic"
    _require_family(family)
    return family


def test_static_multiface_family_pins_exact_and_missing():
    family = _register_yu_gothic()
    faces = physical_weight_styles(family)
    weights = [weight for weight, _name in faces]
    assert 400 in weights

    exact = resolve_weight_plan(family, 400)
    assert exact.style_name is not None
    assert exact.base_weight == 400
    assert exact.synthetic_bold is False
    assert exact.mark is None

    # 缺档 600：以实测的 Qt 实际选择为权威（合成或就近随平台字体库而变，
    # 由指纹实测；跨环境一致性由下方的「复现不变量」用例保证）。
    missing = resolve_weight_plan(family, 600)
    assert missing.base_weight in weights
    assert missing.mark in {"模拟", "就近"}
    assert (missing.mark == "模拟") == missing.synthetic_bold


@pytest.mark.skipif(
    not os.path.exists(_YUGOTH_FONT_PATH), reason="Yu Gothic font file not present"
)
def test_missing_weight_plan_reproduces_plain_qt_rendering(qapp):
    """指纹解析的核心不变量：按 plan 构造的字体与「交给 Qt 决定」的字体
    逐字 advance + 墨迹完全一致（否则 CPU/GPU 分叉）。"""
    family = _register_yu_gothic()
    probe = QFont(family)
    probe.setPixelSize(48)
    probe.setWeight(QFont.Weight(600))
    plain_metrics = QFontMetrics(probe)
    plan = resolve_weight_plan(family, 600)
    planned = build_weight_font(family, 48, 600)
    planned_metrics = QFontMetrics(planned)
    for text in ("Ag0Wg指あソ", "歌詞表示"):
        for index, char in enumerate(text):
            assert planned_metrics.horizontalAdvance(
                char
            ) == plain_metrics.horizontalAdvance(char), (text, index)
    assert QFontInfo(planned).styleName() in {
        QFontInfo(probe).styleName(),
        plan.style_name,
    }


def test_static_single_face_family_simulates_bold_only_above_600():
    _require_family("MS Gothic")
    faces = physical_weight_styles("MS Gothic")
    assert len(faces) == 1
    assert faces[0][0] == 400

    light = resolve_weight_plan("MS Gothic", 500)
    assert light.synthetic_bold is False
    assert light.mark == "就近"

    bold = resolve_weight_plan("MS Gothic", 700)
    assert bold.synthetic_bold is True
    assert bold.mark == "模拟"
    assert bold.base_weight == 400

    plain = build_weight_font("MS Gothic", 64, 400)
    simulated = build_weight_font("MS Gothic", 64, 700)
    metrics_plain = QFontMetrics(plain)
    metrics_sim = QFontMetrics(simulated)
    # 合成粗体把 advance 撑大约 1px（DWrite SIMULATIONS_BOLD），
    # 与 native 侧模拟 face 的 advance 口径一致。
    assert metrics_sim.horizontalAdvance("あ") == metrics_plain.horizontalAdvance("あ") + 1


@pytest.mark.skipif(
    not os.path.exists(_IWATA_FONT_PATH), reason="Iwata VF font not present"
)
def test_variable_font_renders_true_axis_instances(qapp):
    QFontDatabase.addApplicationFont(_IWATA_FONT_PATH)
    family = "Iwata UD Gothic 08StdN VF TTF"
    _require_family(family)

    axis = family_weight_axis(family)
    assert axis is not None
    assert (axis.minimum, axis.maximum) == (300.0, 900.0)

    # DWrite 轴值真值（advance units/1000）：300=782, 540=820, 900=856
    for weight, expected_a_px in ((300, 50), (540, 52), (900, 55)):
        plan = resolve_weight_plan(family, weight)
        assert plan.axis_value == float(weight)
        assert plan.mark is None
        metrics = QFontMetrics(build_weight_font(family, 64, weight))
        assert metrics.horizontalAdvance("A") == expected_a_px

    # 中间字重（如 650）是真实插值，不触发任何模拟。
    interpolated = resolve_weight_plan(family, 650)
    assert interpolated.axis_value == 650.0
    assert interpolated.mark is None

    # 轴外请求钳制到端点并标注。
    below = resolve_weight_plan(family, 100)
    assert below.axis_value == 300.0
    assert below.mark == "越界"
    above = resolve_weight_plan(family, 950)
    assert above.axis_value == 900.0
    assert above.mark == "越界"


def test_fake_variable_axis_falls_back_to_static(monkeypatch, qapp):
    """伪可变字体（带 fvar 的 wght 轴但渲染恒定）按静态族处理。

    打桩 ``_wght_axis_is_effective`` 为恒 False，模拟「轴两端指纹一致」
    （无真实变体数据 / 当前环境无法应用轴值），此时不得走轴值路径。
    """
    import krok_helper.subtitle_render.engine.text.font_weight as fw

    monkeypatch.setattr(fw, "_wght_axis_is_effective", lambda family, axis: False)
    fw.clear_font_weight_cache()
    msgothic = r"C:\Windows\Fonts\msgothic.ttc"
    if not os.path.exists(msgothic):
        pytest.skip("MS Gothic font file not present")
    QFontDatabase.addApplicationFont(msgothic)
    _require_family("MS Gothic")
    plan = fw.resolve_weight_plan("MS Gothic", 600)
    assert plan.axis_value is None
    assert plan.style_name == "Regular"
    # 静态语义（就近/模拟随平台字体库而变），绝不标真实轴值。
    assert plan.mark in {"就近", "模拟"}


def test_missing_metadata_family_falls_back_to_plain_weight(monkeypatch):
    # 元数据缺失（字体不存在/headless 枚举为空）时保持旧的纯桶化行为；
    # 直接打桩两条元数据通道，避免平台默认字体回退的干扰。
    import krok_helper.subtitle_render.engine.text.font_weight as fw

    monkeypatch.setattr(fw, "family_weight_axis", lambda family: None)
    monkeypatch.setattr(fw, "physical_weight_styles", lambda family: ())
    plan = fw.resolve_weight_plan("__no_such_family__", 700)
    assert plan.axis_value is None
    assert plan.style_name is None
    assert plan.enum_weight == 700
    font = QFont("__no_such_family__")
    fw.apply_weight_plan(font, plan)
    assert int(font.weight()) == 700


def test_ir_face_decision_resolves_family_like_cpu(monkeypatch):
    # GPU 下发的 face 决策必须与 CPU 同口径：先把 N3/本地化族名（如
    # 「HGP明朝E」→ HGPMinchoE）解析成 Qt 族名再测 face。原始本地化名在
    # QFontDatabase 里查不到 face，会落进「字体缺失」分支下发 face=请求
    # 字重、不模拟，单 face 字体的粗体请求在 GPU 上被渲染成常规体。
    import krok_helper.subtitle_render.engine.text.font_weight as fw
    import krok_helper.subtitle_render.n3.font_catalog as catalog
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces

    aliases = {"本地化名E": "QtFamilyE", "Latin別名": "QtLatin"}
    monkeypatch.setattr(
        catalog, "resolve_qt_font_family", lambda name: aliases.get(name, name)
    )
    seen: list[tuple[str, int]] = []

    def fake_plan(family: str, weight: int, italic: bool = False) -> fw.FontWeightPlan:
        seen.append((family, weight))
        if family in {"QtFamilyE", "QtLatin"} and weight >= 600:
            return fw.FontWeightPlan(
                family=family, requested_weight=weight, base_weight=400,
                synthetic_bold=True, enum_weight=700, mark="模拟",
            )
        return fw.FontWeightPlan(
            family=family, requested_weight=weight, base_weight=weight,
            enum_weight=weight,
        )

    monkeypatch.setattr(fw, "resolve_weight_plan", fake_plan)
    payload = {
        "font_family": "本地化名E",
        "font_weight": 700,
        "latin_font_family": "Latin別名",
    }
    apply_resolved_font_faces(payload)

    assert all(family in {"QtFamilyE", "QtLatin"} for family, _weight in seen)
    assert (payload["font_face_weight"], payload["font_sim_bold"]) == (400, True)
    assert (payload["latin_font_face_weight"], payload["latin_font_sim_bold"]) == (400, True)
    # 原始请求字重不被改写（sidecar Qt 后端与行内覆盖启发式仍消费它）。
    assert payload["font_weight"] == 700
