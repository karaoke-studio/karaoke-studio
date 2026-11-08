"""统一「字重→实例」解析器（engine.text.font_weight）的规则测试。

顺应引擎语义：字重回到绝对字重，模拟加粗交还 Qt/DirectWrite 引擎；
本模块只负责可变轴识别 + 静态引擎就近匹配标注，不做任何「我们膨胀」。
规则与 native 侧 ``d2d_font_fallback.cpp`` 逐条对应，改动任一侧时这里的
口径断言就是同步契约。
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    _axis_is_effective,
)
from krok_helper.subtitle_render.engine.text.font_weight import (
    apply_weight_plan,
    embolden_glyph_path,
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
    from krok_helper.subtitle_render.n3.font_catalog import (
        invalidate_n3_font_caches,
    )

    clear_font_weight_cache()
    # N3 字体目录两级缓存同样按进程字体登记快照：不清会让后续模块（如
    # property_panel）的 font_family 解析读到污染后的目录，把显式设置的
    # 英数字体（如 Arial）误判为「跟随主文字」。
    invalidate_n3_font_caches()


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


def test_static_multiface_family_exact_and_nearest():
    family = "Segoe UI"
    _require_family(family)
    faces = physical_weight_styles(family)
    weights = [weight for weight, _name in faces]
    assert 400 in weights
    assert 700 in weights

    exact = resolve_weight_plan(family, 400)
    assert exact.style_name is not None
    assert exact.base_weight == 400
    assert exact.render_mode == "face"
    assert exact.mark is None

    # 缺档请求走引擎就近（平局取更接近 Normal(400) 的 face：650 对 600/700
    # 等距 → 600）；600 face 不触发合成（拉丁实测 Segoe UI Semibold
    # @600/700 与 @400 同尺寸）。
    nearest = resolve_weight_plan(family, 650)
    assert nearest.render_mode == "snap"
    assert nearest.base_weight == 600
    assert nearest.mark == "就近"
    assert nearest.synthetic_bold is False


def test_static_build_font_sets_request_weight(qapp):
    """顺应引擎：静态 build_weight_font 直接 setWeight(请求)，匹配交引擎。"""
    _require_family("MS Gothic")
    font = build_weight_font("MS Gothic", 48, 600)
    assert int(font.weight()) == 600


def test_static_single_face_family_engine_synthetic():
    """单 face {400}：W≥600 引擎合成，W<600 就近，400 精确。"""
    _require_family("MS Gothic")
    faces = physical_weight_styles("MS Gothic")
    assert len(faces) == 1
    assert faces[0][0] == 400

    exact = resolve_weight_plan("MS Gothic", 400)
    assert exact.render_mode == "face"
    assert exact.mark is None

    snap = resolve_weight_plan("MS Gothic", 500)
    assert snap.render_mode == "snap"
    assert snap.base_weight == 400

    for weight in (600, 700, 800, 900):
        plan = resolve_weight_plan("MS Gothic", weight)
        assert plan.render_mode == "engine_synthetic"
        assert plan.base_weight == 400
        assert plan.mark == "模拟"


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

    # 中间字重（如 650）是真实插值，不触发任何标注。
    interpolated = resolve_weight_plan(family, 650)
    assert interpolated.axis_value == 650.0
    assert interpolated.mark is None

    # 轴下限/上限之下钳制到端点并标「越界」。
    below = resolve_weight_plan(family, 100)
    assert below.axis_value == 300.0
    assert below.mark == "越界"
    above = resolve_weight_plan(family, 950)
    assert above.axis_value == 900.0
    assert above.mark == "越界"


def test_static_family_axis_is_ineffective():
    """静态族两端轴指纹一致（无真变体）→ 不判可变。"""
    msgothic = r"C:\Windows\Fonts\msgothic.ttc"
    if not os.path.exists(msgothic):
        pytest.skip("MS Gothic font file not present")
    QFontDatabase.addApplicationFont(msgothic)
    _require_family("MS Gothic")
    assert _axis_is_effective("MS Gothic", 300.0, 900.0) is False


def test_constant_axis_is_ineffective():
    """恒定轴（min==max 的"可变"包装）不判可变——mn>=mx 短路。"""
    assert _axis_is_effective("MS Gothic", 700.0, 700.0) is False


def test_bold_cut_family_stays_700():
    """粗体单字重族（基 700）：700 就是 700，任何请求都就近到 700，不合成。"""
    ttc = "C:/Windows/Fonts/UDDIGIKYOKASHON-B_0.TTC"
    if not os.path.exists(ttc):
        pytest.skip("UD Digi Kyokasho NK-B font file not present")
    QFontDatabase.addApplicationFont(ttc)
    _require_family("UD Digi Kyokasho NK-B")
    exact = resolve_weight_plan("UD Digi Kyokasho NK-B", 700)
    assert exact.render_mode == "face"
    assert exact.base_weight == 700
    assert exact.mark is None
    for weight in (400, 500, 600, 800, 900):
        plan = resolve_weight_plan("UD Digi Kyokasho NK-B", weight)
        assert plan.render_mode == "snap"
        assert plan.base_weight == 700
        assert plan.synthetic_bold is False


def test_missing_family_falls_back_to_plain_weight():
    """字体不存在/枚举为空时保持纯绝对字重，不标注。"""
    plan = resolve_weight_plan("__no_such_family__", 700)
    assert plan.render_mode == "missing"
    assert plan.axis_value is None
    assert plan.style_name is None
    assert plan.mark is None
    font = QFont("__no_such_family__")
    apply_weight_plan(font, plan)
    assert int(font.weight()) == 700


def test_winding_fill_rule_for_glyph_path(qapp):
    """embolden_glyph_path 现仅设 WindingFill（可变字体中空修复）。"""
    from PyQt6.QtGui import QPainterPath

    path = QPainterPath()
    path.addText(0.0, 0.0, QFont("MS Gothic", 48), "教")
    result = embolden_glyph_path(path)
    assert result.fillRule() == Qt.FillRule.WindingFill


@pytest.mark.parametrize("reflected", [False, True])
def test_glyph_overlap_is_solid_but_counter_stays_transparent(qapp, reflected):
    """Verify actual pixels, including reversed winding after a reflection."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

    path = QPainterPath()
    path.addRect(QRectF(5, 5, 50, 50))
    counter = QPainterPath()
    counter.addRect(QRectF(15, 15, 20, 20))
    path.addPath(counter.toReversed())
    # Same-direction stroke overlaps the outer contour, outside the counter.
    path.addRect(QRectF(40, 25, 30, 15))
    if reflected:
        path = QTransform(-1, 0, 0, 1, 80, 0).map(path)

    def alpha_at(rule, x, y):
        path.setFillRule(rule)
        image = QImage(80, 65, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillPath(path, QColor("white"))
        painter.end()
        return image.pixelColor(80 - x if reflected else x, y).alpha()

    # Control: this fixture reproduces the historical overlap hole.
    assert alpha_at(Qt.FillRule.OddEvenFill, 45, 30) == 0
    rule = embolden_glyph_path(path).fillRule()
    assert alpha_at(rule, 45, 30) == 255
    assert alpha_at(rule, 25, 25) == 0
    assert alpha_at(rule, 60, 30) == 255


def test_get_capabilities_cache_hit_is_qt_free_from_worker_thread(qapp, monkeypatch):
    """缓存命中路径必须零 Qt 调用（渲染线程只允许走这条路）。

    ``apply_resolved_font_faces`` 在渲染线程里逐槽位取能力；冷缓存会枚举
    QFontDatabase/QRawFont——跨线程 Qt 字体访问持字体库锁建引擎，触发 Qt
    告警后还要抢 GIL/写面包屑，等锁的 GUI 线程随之「未响应」（2026-10
    打开工程卡死）。GUI 线程 set_state 先预热缓存（见 preview_async），
    渲染线程只剩这条纯 dict 路径。
    """
    import threading

    from krok_helper.subtitle_render.engine.text import font_capabilities
    from krok_helper.subtitle_render.engine.text.font_capabilities import (
        FontCapabilities,
        FontFace,
    )

    sentinel = FontCapabilities(
        family="CacheHit",
        faces=(FontFace(400, "Regular", False),),
        axis_min=None,
        axis_max=None,
        axis_default=None,
        axis_effective=False,
    )
    font_capabilities._CACHE["CacheHit"] = sentinel

    class _QtMustNotBeTouched:
        def __getattr__(self, name):  # noqa: N802
            raise AssertionError(f"cache hit must not touch Qt: {name}")

    for qt_name in ("QFontDatabase", "QRawFont", "QFont", "QFontInfo"):
        monkeypatch.setattr(font_capabilities, qt_name, _QtMustNotBeTouched())

    results: list = []
    try:
        thread = threading.Thread(
            target=lambda: results.append(
                font_capabilities.get_capabilities("CacheHit")
            )
        )
        thread.start()
        thread.join(timeout=5)
    finally:
        font_capabilities._CACHE.pop("CacheHit", None)

    assert results == [sentinel]


@pytest.mark.parametrize("defect", ["truncated", "duplicate", "tag", "range"])
def test_fvar_rejects_incomplete_or_inconsistent_axis_sets(defect):
    import struct
    from krok_helper.subtitle_render.engine.text.font_capabilities import _parse_fvar_axes
    first = struct.pack(">4s3l2H", b"wght", 100 * 65536, 400 * 65536, 900 * 65536, 0, 256)
    tag = b"wght" if defect == "duplicate" else b"\0bad" if defect == "tag" else b"wdth"
    default = 300 if defect == "range" else 100
    second = struct.pack(">4s3l2H", tag, 50 * 65536, default * 65536, 200 * 65536, 0, 257)
    header = struct.pack(">8H", 1, 0, 16, 2, 2, 20, 0, 12)
    raw = header + first + (second[:10] if defect == "truncated" else second)
    assert _parse_fvar_axes(raw) == {}


def test_fvar_preserves_complete_custom_axis_ranges():
    import struct
    from krok_helper.subtitle_render.engine.text.font_capabilities import _parse_fvar_axes
    raw = struct.pack(">8H", 1, 0, 16, 2, 1, 20, 0, 8)
    raw += struct.pack(">4s3l2H", b"CSTM", -65536, 0, 65536, 0, 256)
    assert _parse_fvar_axes(raw) == {b"CSTM": (-1.0, 0.0, 1.0)}


def test_font_preparation_continues_after_one_failed_family(qapp, monkeypatch):
    from krok_helper.subtitle_render.engine.text import font_preparation as preparation
    from krok_helper.subtitle_render.engine.text import font_capabilities as capabilities
    monkeypatch.setattr(preparation, "collect_referenced_font_families", lambda *args: ["Broken", "Healthy"])
    visited = []

    def load(family):
        visited.append(family)
        if family == "Broken":
            raise ValueError("damaged font")

    monkeypatch.setattr(capabilities, "get_capabilities", load)
    monkeypatch.setattr(preparation, "_iter_prewarm_font_fallback", lambda *args: (visited.append("fallback"),))
    preparation.prepare_fonts(None, None, None)
    assert visited == ["Broken", "Healthy", "fallback"]


def test_incremental_capabilities_restart_after_font_registry_change(monkeypatch):
    from krok_helper.subtitle_render.engine.text import font_capabilities as caps
    caps.clear_capabilities_cache()
    styles = ["Old"]
    monkeypatch.setattr(caps, "canonical_family", lambda family: family)
    monkeypatch.setattr(caps.QFontDatabase, "styles", lambda family: list(styles))
    monkeypatch.setattr(caps.QFontDatabase, "weight", lambda *args: 400)
    monkeypatch.setattr(caps.QFontDatabase, "italic", lambda *args: False)
    monkeypatch.setattr(caps, "_face_axes", lambda *args: None)
    steps = caps.iter_capabilities("Generation Probe")
    next(steps)
    styles[:] = ["New"]
    caps.clear_capabilities_cache()
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            result = finished.value
            break
    assert [face.style_name for face in result.faces] == ["New"]
    assert caps.get_capabilities("Generation Probe") is result
    caps.clear_capabilities_cache()


@pytest.mark.parametrize("raster_changes", [False, True])
def test_incremental_axis_probe_preserves_pixel_only_changes(monkeypatch, raster_changes):
    from krok_helper.subtitle_render.engine.text import font_capabilities as caps
    calls = []
    def cheap(family, style, value, size):
        calls.append(("cheap", value))
        return (10, 10)
    def raster(family, style, value, size):
        calls.append(("raster", value))
        return bytes([int(value) if raster_changes else 1])
    monkeypatch.setattr(caps, "_axis_cheap_signature", cheap)
    monkeypatch.setattr(caps, "_axis_raster_signature", raster)
    steps = caps._iter_axis_is_effective("Probe", 1, 3)
    for count in range(1, 7):
        next(steps)
        assert len(calls) == count
    with pytest.raises(StopIteration) as finished:
        next(steps)
    assert finished.value.value is raster_changes
    assert caps._axis_is_effective("Probe", 1, 3) is raster_changes
