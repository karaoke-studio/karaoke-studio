"""Phase 1 + Phase 2 字体解析与匹配的独立测试。

Phase 1（font_capabilities）：face 枚举、轴检测、别名规范化。
Phase 2（weight_resolver）：v4.2.x 比例 + 紧邻放大语义的穷举验证。
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
# Phase 2: 紧邻放大语义
# =========================================================================


class TestVariableFont:
    def test_in_range_exact(self):
        r = resolve(_vf("t", 300, 900), 600)
        assert r.render_mode == "axis"
        assert r.axis_value == 600.0
        assert r.is_exact

    def test_below_min_snaps(self):
        r = resolve(_vf("t", 300, 900), 100)
        assert r.render_mode == "axis"
        assert r.axis_value == 300.0
        assert not r.is_exact

    def test_above_max_emboldens(self):
        r = resolve(_vf("t", 300, 900), 950)
        assert r.render_mode == "embolden"
        assert r.embolden_delta == 50


class TestStaticSingleFace:
    """单 face：无论实际字重都归一化为 400 档。"""

    def test_regular_400_exact(self):
        r = resolve(_cap("t", (400,)), 400)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_regular_500_embolden(self):
        r = resolve(_cap("t", (400,)), 500)
        assert r.render_mode == "embolden"
        assert r.embolden_delta == 100

    def test_regular_700_embolden(self):
        r = resolve(_cap("t", (400,)), 700)
        assert r.embolden_delta == 300

    def test_regular_900_embolden(self):
        r = resolve(_cap("t", (400,)), 900)
        assert r.embolden_delta == 500

    def test_regular_300_snaps(self):
        r = resolve(_cap("t", (400,)), 300)
        assert r.render_mode == "snap"
        assert not r.is_exact

    def test_bold_700_normalized_to_400(self):
        """NK-B {700} 视为 400 档：@700 = Δ300，@400 = 精确。"""
        r700 = resolve(_cap("t", (700,)), 700)
        assert r700.render_mode == "embolden"
        assert r700.embolden_delta == 300
        r400 = resolve(_cap("t", (700,)), 400)
        assert r400.render_mode == "face"
        assert r400.is_exact

    def test_bold_700_at_900(self):
        r = resolve(_cap("t", (700,)), 900)
        assert r.render_mode == "embolden"
        assert r.embolden_delta == 500

    def test_extralight_100_normalized_to_400(self):
        """{100} 视为 400 档：@600 = Δ200，@400 = 精确。"""
        r600 = resolve(_cap("t", (100,)), 600)
        assert r600.render_mode == "embolden"
        assert r600.embolden_delta == 200
        r400 = resolve(_cap("t", (100,)), 400)
        assert r400.render_mode == "face"
        assert r400.is_exact


class TestStaticMultiFace:
    """多 face：精确命中直接用；缺档从紧邻较小 face 放大。"""

    def test_exact_400(self):
        r = resolve(_cap("t", (400, 700)), 400)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_exact_700(self):
        r = resolve(_cap("t", (400, 700)), 700)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_500_embolden_from_400(self):
        r = resolve(_cap("t", (400, 700)), 500)
        assert r.render_mode == "embolden"
        assert r.base_face.weight == 400
        assert r.embolden_delta == 100

    def test_600_embolden_from_400(self):
        """600 的紧邻较小是 400（不是 700），从 400 放大 Δ200。"""
        r = resolve(_cap("t", (400, 700)), 600)
        assert r.render_mode == "embolden"
        assert r.base_face.weight == 400
        assert r.embolden_delta == 200

    def test_800_embolden_from_700(self):
        r = resolve(_cap("t", (400, 700)), 800)
        assert r.render_mode == "embolden"
        assert r.base_face.weight == 700
        assert r.embolden_delta == 100

    def test_900_embolden_from_700(self):
        r = resolve(_cap("t", (400, 700)), 900)
        assert r.embolden_delta == 200

    def test_300_snaps_to_400(self):
        r = resolve(_cap("t", (400, 700)), 300)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 400

    def test_yu_gothic_600_embolden_from_500(self):
        """Yu Gothic {300,400,500,700}@600 → 紧邻较小 500，Δ100。"""
        r = resolve(_cap("t", (300, 400, 500, 700)), 600)
        assert r.render_mode == "embolden"
        assert r.base_face.weight == 500
        assert r.embolden_delta == 100


class TestMissing:
    def test_no_faces(self):
        r = resolve(None, 400)
        assert r.render_mode == "missing"

    def test_empty(self):
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

    def test_nonexistent(self):
        assert get_capabilities("__no_such__") is None
