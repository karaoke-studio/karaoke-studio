"""Phase 1 + Phase 2 字体解析与匹配的独立测试。

Phase 1（font_capabilities）：face 枚举、轴检测、别名规范化。
Phase 2（weight_resolver）：顺应引擎语义——可变=轴值（钳制到端点），
静态=引擎就近匹配标注（精确命中 / 引擎合成粗体 / 引擎就近），不做任何
「我们膨胀」。
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
# Phase 2: 顺应引擎语义
# =========================================================================


class TestVariableFont:
    def test_in_range_exact(self):
        r = resolve(_vf("t", 300, 900), 600)
        assert r.render_mode == "axis"
        assert r.axis_value == 600.0
        assert r.is_exact

    def test_below_min_clamps(self):
        r = resolve(_vf("t", 300, 900), 100)
        assert r.render_mode == "axis"
        assert r.axis_value == 300.0
        assert not r.is_exact
        assert r.mark == "越界"

    def test_above_max_clamps(self):
        r = resolve(_vf("t", 300, 900), 950)
        assert r.render_mode == "axis"
        assert r.axis_value == 900.0
        assert not r.is_exact
        assert r.mark == "越界"


class TestStaticSingleFace:
    """单 face：不归一化，引擎就近 + 合成（face<600 且 W≥600 时）。"""

    def test_exact_400(self):
        r = resolve(_cap("t", (400,)), 400)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_500_snaps_to_400(self):
        r = resolve(_cap("t", (400,)), 500)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 400
        assert not r.is_exact

    def test_700_engine_synthetic(self):
        r = resolve(_cap("t", (400,)), 700)
        assert r.render_mode == "engine_synthetic"
        assert r.base_face.weight == 400
        assert r.mark == "模拟"

    def test_900_engine_synthetic(self):
        r = resolve(_cap("t", (400,)), 900)
        assert r.render_mode == "engine_synthetic"
        assert r.base_face.weight == 400

    def test_300_snaps(self):
        r = resolve(_cap("t", (400,)), 300)
        assert r.render_mode == "snap"
        assert not r.is_exact

    def test_bold_700_stays_700(self):
        """NK-B {700}：700 就是 700（业界一致），不再归一化到 400。"""
        r700 = resolve(_cap("t", (700,)), 700)
        assert r700.render_mode == "face"
        assert r700.is_exact
        assert r700.base_face.weight == 700
        r400 = resolve(_cap("t", (700,)), 400)
        assert r400.render_mode == "snap"
        assert r400.base_face.weight == 700

    def test_bold_700_cannot_further_bold(self):
        """粗 face（≥600）不可再加粗：@900 只就近到 700，无合成。"""
        r = resolve(_cap("t", (700,)), 900)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 700

    def test_extralight_100_engine_synthetic_at_600(self):
        """{100}：@600 引擎合成（100<600），@400 就近到 100。"""
        r600 = resolve(_cap("t", (100,)), 600)
        assert r600.render_mode == "engine_synthetic"
        assert r600.base_face.weight == 100
        r400 = resolve(_cap("t", (100,)), 400)
        assert r400.render_mode == "snap"
        assert r400.base_face.weight == 100


class TestStaticMultiFace:
    """多 face：精确命中直接用；缺档引擎就近（平局取轻），face<600 且
    请求≥600 时引擎合成粗体。"""

    def test_exact_400(self):
        r = resolve(_cap("t", (400, 700)), 400)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_exact_700(self):
        r = resolve(_cap("t", (400, 700)), 700)
        assert r.render_mode == "face"
        assert r.is_exact

    def test_500_snaps_to_400(self):
        r = resolve(_cap("t", (400, 700)), 500)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 400

    def test_600_nearest_700_no_tie(self):
        """600 对 {400,700} 无平局：就近 700（粗 face，不再合成）。"""
        r = resolve(_cap("t", (400, 700)), 600)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 700

    def test_800_snaps_to_700(self):
        r = resolve(_cap("t", (400, 700)), 800)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 700

    def test_900_snaps_to_700(self):
        r = resolve(_cap("t", (400, 700)), 900)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 700

    def test_300_snaps_to_400(self):
        r = resolve(_cap("t", (400, 700)), 300)
        assert r.render_mode == "snap"
        assert r.base_face.weight == 400

    def test_yu_gothic_600_tie_prefers_lighter_with_synth(self):
        """Yu Gothic {300,400,500,700}@600：500/700 平局取轻 → 500+合成。
        2026-10-08 实测校准（v4.2.x 像素一致）：引擎对平局取更轻 face 并
        施加 faux bold，而不是上吸到 700。"""
        r = resolve(_cap("t", (300, 400, 500, 700)), 600)
        assert r.render_mode == "engine_synthetic"
        assert r.base_face.weight == 500
        assert r.mark == "模拟"

    def test_semibold_single_face_never_synthesizes(self):
        """{600} 族（Segoe UI Semibold 型）：600 face 不合成——@600 精确、
        其余就近，全档恒定（拉丁实测 152x113 不变；早先观测到的变粗实为
        CJK 回退 face(400) 的合成）。"""
        exact = resolve(_cap("t", (600,)), 600)
        assert exact.render_mode == "face"
        assert exact.is_exact
        assert exact.mark is None
        for w in (400, 500, 700, 900):
            plan = resolve(_cap("t", (600,)), w)
            assert plan.render_mode == "snap"
            assert plan.base_face.weight == 600
            assert plan.synthetic_bold is False


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
