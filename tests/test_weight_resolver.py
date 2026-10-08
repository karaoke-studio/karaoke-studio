"""Phase 1 + Phase 2 字体解析与匹配的独立测试。

Phase 1（font_capabilities）：face 枚举、轴检测、别名规范化。
Phase 2（weight_resolver）：W3C CSS §5.2 匹配算法的穷举验证。
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtGui import QFont, QFontDatabase

from krok_helper.subtitle_render.engine.text.font_capabilities import (
    FontCapabilities,
    FontFace,
    get_capabilities,
)
from krok_helper.subtitle_render.engine.text.weight_resolver import (
    ResolvedWeight,
    resolve,
)


def _cap(family: str, weights: tuple[int, ...]) -> FontCapabilities:
    """构造静态测试 capabilities。"""
    faces = tuple(FontFace(w, f"w{w}", False) for w in sorted(weights))
    return FontCapabilities(family=family, faces=faces)


def _vf(family: str, mn: float, mx: float, faces=(300, 700)) -> FontCapabilities:
    """构造可变测试 capabilities。"""
    static = tuple(FontFace(w, f"w{w}", False) for w in faces)
    return FontCapabilities(
        family=family, faces=static,
        axis_min=mn, axis_max=mx, axis_default=400, axis_effective=True,
    )


# =========================================================================
# Phase 2: W3C CSS §5.2 匹配算法——穷举验证
# =========================================================================


class TestStaticExact:
    def test_exact_400(self):
        r = resolve(_cap("t", (400, 700)), 400)
        assert r.render_mode == "face"
        assert r.base_face.weight == 400
        assert r.is_exact

    def test_exact_700(self):
        r = resolve(_cap("t", (400, 700)), 700)
        assert r.render_mode == "face"
        assert r.base_face.weight == 700
        assert r.is_exact

    def test_exact_single(self):
        r = resolve(_cap("t", (400,)), 400)
        assert r.is_exact
        assert r.base_face.weight == 400


class TestStaticMissingBelow500:
    """W ≤ 500 → 先向下取最重。"""

    def test_500_on_400_700(self):
        r = resolve(_cap("t", (400, 700)), 500)
        assert r.render_mode == "face"
        assert r.base_face.weight == 400  # ≤500 → 向下取 Regular

    def test_450_on_300_400_700(self):
        r = resolve(_cap("t", (300, 400, 700)), 450)
        assert r.base_face.weight == 400  # 向下取

    def test_300_on_400_700(self):
        r = resolve(_cap("t", (400, 700)), 300)
        assert r.base_face.weight == 400  # 向下无 → 向上取最近

    def test_200_on_300_400(self):
        r = resolve(_cap("t", (300, 400)), 200)
        assert r.base_face.weight == 300  # 向下无 → 向上取最近


class TestStaticMissingAbove500:
    """W > 500 → 先向上取最轻。"""

    def test_600_on_400_700(self):
        r = resolve(_cap("t", (400, 700)), 600)
        assert r.render_mode == "face"  # 不是 synthetic（face 700 ≥ 600）
        assert r.base_face.weight == 700  # >500 → 向上取 Bold

    def test_600_on_300_400_500_700(self):
        r = resolve(_cap("t", (300, 400, 500, 700)), 600)
        assert r.base_face.weight == 700

    def test_600_on_300_350_400_600_700(self):
        r = resolve(_cap("t", (300, 350, 400, 600, 700)), 600)
        assert r.is_exact
        assert r.base_face.weight == 600

    def test_800_on_400_700(self):
        r = resolve(_cap("t", (400, 700)), 800)
        assert r.base_face.weight == 700  # 向上无 → 向下取最近

    def test_900_on_400_700(self):
        r = resolve(_cap("t", (400, 700)), 900)
        assert r.base_face.weight == 700


class TestSyntheticBold:
    """face < 600 且请求 ≥ 600 → synthetic。"""

    def test_600_on_400(self):
        r = resolve(_cap("t", (400,)), 600)
        assert r.render_mode == "synthetic"
        assert r.base_face.weight == 400
        assert r.needs_synthetic

    def test_900_on_400(self):
        r = resolve(_cap("t", (400,)), 900)
        assert r.render_mode == "synthetic"

    def test_600_on_100(self):
        r = resolve(_cap("t", (100,)), 600)
        assert r.render_mode == "synthetic"
        assert r.base_face.weight == 100

    def test_400_on_100_no_synthetic(self):
        """400 < 600，不触发合成（旧版行为）。"""
        r = resolve(_cap("t", (100,)), 400)
        assert r.render_mode == "face"
        assert not r.needs_synthetic

    def test_900_on_700_no_synthetic(self):
        """face 700 ≥ 600，不触发合成（旧版行为）。"""
        r = resolve(_cap("t", (700,)), 900)
        assert r.render_mode == "face"
        assert not r.needs_synthetic

    def test_600_on_400_700_no_synthetic(self):
        """有 Bold face 时 600 匹配 Bold，不需要合成。"""
        r = resolve(_cap("t", (400, 700)), 600)
        assert r.render_mode == "face"
        assert not r.needs_synthetic


class TestVariableFont:
    def test_in_range(self):
        r = resolve(_vf("t", 300, 900), 600)
        assert r.render_mode == "axis"
        assert r.axis_value == 600.0
        assert r.is_exact

    def test_below_min(self):
        r = resolve(_vf("t", 300, 900), 100)
        assert r.render_mode == "axis"
        assert r.axis_value == 300.0
        assert not r.is_exact

    def test_above_max(self):
        r = resolve(_vf("t", 300, 900), 950)
        assert r.render_mode == "axis"
        assert r.axis_value == 900.0
        assert not r.is_exact


class TestMissing:
    def test_no_faces(self):
        r = resolve(None, 400)
        assert r.render_mode == "missing"

    def test_empty_faces(self):
        r = resolve(FontCapabilities(family="t", faces=()), 400)
        assert r.render_mode == "missing"


# =========================================================================
# Phase 1: Font Capabilities（依赖已安装字体）
# =========================================================================


class TestCapabilities:
    def test_ms_gothic(self):
        if "MS Gothic" not in QFontDatabase.families():
            pytest.skip("MS Gothic not installed")
        cap = get_capabilities("MS Gothic")
        assert cap is not None
        assert not cap.is_variable
        assert cap.face_weights == (400,)

    def test_yu_gothic(self):
        if "Yu Gothic" not in QFontDatabase.families():
            pytest.skip("Yu Gothic not installed")
        cap = get_capabilities("Yu Gothic")
        assert cap is not None
        assert not cap.is_variable
        assert set(cap.face_weights) == {300, 400, 500, 700}

    def test_localized_name(self):
        """日文/本地化族名也能获取 capabilities。"""
        if "MS Gothic" not in QFontDatabase.families():
            pytest.skip("MS Gothic not installed")
        if "ＭＳ ゴシック" not in QFontDatabase.families():
            pytest.skip("ＭＳ ゴシック not listed")
        cap = get_capabilities("ＭＳ ゴシック")
        assert cap is not None
        assert cap.face_weights == (400,)

    def test_nonexistent(self):
        assert get_capabilities("__no_such__") is None
