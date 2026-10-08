"""单 face 字体字重迁移（v4.2.x 绝对字重 → v8 归一化 400）的单元测试。"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtGui import QFontDatabase

from krok_helper.subtitle_render.domain.models import Style, TitleOverlay
from krok_helper.subtitle_render.engine.text.font_weight import (
    _migrate_single_face_weight,
    migrate_single_face_font_weights,
)


def _register(*files):
    for name in files:
        QFontDatabase.addApplicationFont(os.path.join(r"C:\Windows\Fonts", name))


@pytest.fixture(autouse=True)
def _fonts(qapp):
    _register("UDDIGIKYOKASHON-B_0.TTC", "msgothic.ttc")
    yield


def _skip_if_missing(family):
    if family not in QFontDatabase.families():
        pytest.skip(f"{family} not installed")


class TestSingleFaceWeightMapping:
    def test_bold_face_any_weight_maps_to_400(self):
        _skip_if_missing("UD Digi Kyokasho NK-B")
        # 粗体单 face（face=700）：任何旧字重都渲染 face 本身 → 400
        for weight in (400, 500, 700, 900):
            assert _migrate_single_face_weight("UD Digi Kyokasho NK-B", weight) == 400

    def test_regular_face_faux_bold_maps_to_700(self):
        _skip_if_missing("MS Gothic")
        # 常规单 face（face=400）：≥600 旧 faux bold → 700，否则 → 400
        assert _migrate_single_face_weight("MS Gothic", 400) == 400
        assert _migrate_single_face_weight("MS Gothic", 500) == 400
        assert _migrate_single_face_weight("MS Gothic", 600) == 700
        assert _migrate_single_face_weight("MS Gothic", 700) == 700
        assert _migrate_single_face_weight("MS Gothic", 900) == 700

    def test_passthrough_when_no_family_or_weight(self):
        # weight=None（未设字重）→ None；family=None（继承，无法判断）→ 保留
        assert _migrate_single_face_weight("KaiTi", None) is None
        assert _migrate_single_face_weight(None, 700) == 700
        assert _migrate_single_face_weight("__no_such__", 700) == 700


class TestMultiFaceAndVariableUntouched:
    def test_multi_face_untouched(self):
        _skip_if_missing("Yu Gothic")
        assert _migrate_single_face_weight("Yu Gothic", 700) == 700

    def test_variable_untouched(self):
        if "Noto Sans JP" not in QFontDatabase.families():
            pytest.skip("Noto Sans JP not installed")
        assert _migrate_single_face_weight("Noto Sans JP", 650) == 650


class TestFullStyleMigration:
    def test_migrates_all_slots_and_is_idempotent(self):
        _skip_if_missing("UD Digi Kyokasho NK-B")
        _skip_if_missing("KaiTi")
        style = Style(
            font_family="UD Digi Kyokasho NK-B", font_weight=700,       # → 400
            font_family_latin="KaiTi", latin_font_weight=700,           # → 700 (faux bold)
            ruby_font_family="UD Digi Kyokasho NK-B", ruby_font_weight=900,  # → 400
            title_overlays=[
                TitleOverlay(
                    enabled=True,
                    font_family="UD Digi Kyokasho NK-B",
                    font_weight=700,
                )
            ],
        )
        migrated = migrate_single_face_font_weights(style)
        assert migrated.font_weight == 400
        assert migrated.latin_font_weight == 700
        assert migrated.ruby_font_weight == 400
        assert migrated.title_overlays[0].font_weight == 400
        # 幂等：再迁移不变
        assert migrate_single_face_font_weights(migrated) == migrated

    def test_untouched_when_no_single_face_change(self):
        _skip_if_missing("Yu Gothic")
        style = Style(font_family="Yu Gothic", font_weight=700)
        migrated = migrate_single_face_font_weights(style)
        assert migrated == style
