"""音量柱/指示灯（SignalsLits）的开关与参数要记在应用级偏好里。

与标题习惯同一口径：改一次就一直沿用，新建工程直接从记忆播种；随手
打开的旧工程**不**覆盖这份习惯——这些字段不随 ``merge_common_style_
preferences`` 从工程侧带入（见 ``SIGNAL_MODULE_STYLE_FIELDS``），只经
``_remember_style_preferences`` 的编辑差分写入。
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
)
from krok_helper.subtitle_render.domain.models import Style  # noqa: E402
from krok_helper.subtitle_render.settings.preferences import (  # noqa: E402
    merge_common_style_preferences,
)


class _Recorder:
    """一份留在内存里的 subtitle_render 设置命名空间。"""

    def __init__(self) -> None:
        self.data: dict = {}

    def load(self) -> dict:
        return dict(self.data)

    def save(self, data: dict) -> None:
        self.data = dict(data)


@pytest.fixture
def settings() -> _Recorder:
    return _Recorder()


@pytest.fixture
def make_window(settings):
    app = QApplication.instance() or QApplication([])
    built: list[SubtitleRenderWindow] = []

    def factory() -> SubtitleRenderWindow:
        widget = SubtitleRenderWindow.for_embedding(settings_provider=settings)
        built.append(widget)
        return widget

    yield factory
    for widget in built:
        widget.close()
        widget.deleteLater()
    app.processEvents()


def _edit_style(window: SubtitleRenderWindow, **changes) -> None:
    """模拟属性面板提交一次样式编辑（音量柱/指示灯控件走同一通道）。"""
    window._property_panel.set_style(
        replace(window._style, **changes),
        emit=True,
    )
    QApplication.instance().processEvents()


def test_enabling_the_modules_updates_the_app_default(make_window) -> None:
    window = make_window()

    _edit_style(window, volume_enabled=True, lit_enabled=True)

    assert window._app_default_style.volume_enabled is True
    assert window._app_default_style.lit_enabled is True


def test_editing_module_fields_updates_the_app_default(make_window) -> None:
    """设置字段（柱数/尺寸/颜色/时序/外观档/引用）与开关一并记忆。"""
    window = make_window()

    _edit_style(
        window,
        volume_column_count=6,
        volume_fill_color="#FF8800",
        volume_duration_ms=1500,
        volume_appearance_mode="role",
        volume_role_name="主唱",
        lit_style="star",
        lit_size=64,
        lit_offset_y=-40,
    )

    app_style = window._app_default_style
    assert app_style.volume_column_count == 6
    assert app_style.volume_fill_color == "#FF8800"
    assert app_style.volume_duration_ms == 1500
    assert app_style.volume_appearance_mode == "role"
    assert app_style.volume_role_name == "主唱"
    assert app_style.lit_style == "star"
    assert app_style.lit_size == 64
    assert app_style.lit_offset_y == -40


def test_unrelated_edits_keep_the_remembered_values(make_window) -> None:
    """改别的样式不该把信号模块习惯冲回出厂或条目旧值。"""
    window = make_window()
    _edit_style(window, volume_enabled=True, lit_size=64)

    _edit_style(window, line_lead_in_ms=2600)

    assert window._app_default_style.volume_enabled is True
    assert window._app_default_style.lit_size == 64


def test_disabling_is_remembered_too(make_window) -> None:
    window = make_window()
    _edit_style(window, volume_enabled=True)

    _edit_style(window, volume_enabled=False)

    assert window._app_default_style.volume_enabled is False


def test_signal_fields_are_written_to_settings(make_window, settings) -> None:
    window = make_window()
    _edit_style(window, volume_enabled=True, volume_column_count=6, lit_size=64)

    window._save_persisted_state()

    saved_style = settings.data["style"]
    assert saved_style["volume_enabled"] is True
    assert saved_style["volume_column_count"] == 6
    assert saved_style["lit_size"] == 64


def test_a_new_instance_starts_from_the_remembered_values(
    make_window, settings
) -> None:
    """真正要的效果：下次打开新工程，音量柱/指示灯沿用上次的设置。"""
    first = make_window()
    _edit_style(first, volume_enabled=True, volume_column_count=6, lit_size=64)
    first._save_persisted_state()

    second = make_window()

    assert second._app_default_style.volume_enabled is True
    assert second._app_default_style.volume_column_count == 6
    assert second._style.volume_enabled is True
    assert second._style.volume_column_count == 6
    assert second._style.lit_size == 64


def test_opening_another_project_does_not_overwrite_the_habit(
    make_window, settings
) -> None:
    """打开旧工程（音量柱/指示灯还是出厂关）后再落盘，习惯保持记忆。

    装载路径直接赋值 ``_style``、不走 ``_apply_style``；收尾保存经
    ``merge_common_style_preferences`` 投影——信号字段必须在排除集里，
    否则习惯被那个工程的值覆盖。
    """
    window = make_window()
    _edit_style(window, volume_enabled=True, volume_column_count=6)
    window._save_persisted_state()

    window._style = replace(
        window._style,
        volume_enabled=False,
        volume_column_count=4,
        lit_size=30,
    )
    window._save_persisted_state()

    assert settings.data["style"]["volume_enabled"] is True
    assert settings.data["style"]["volume_column_count"] == 6
    # lit_size 从未被编辑记忆：磁盘保持出厂值，不吃工程的 30。
    assert settings.data["style"]["lit_size"] == Style().lit_size
    reopened = make_window()
    assert reopened._app_default_style.volume_enabled is True
    assert reopened._app_default_style.volume_column_count == 6


def test_merge_common_style_preferences_skips_signal_fields() -> None:
    """纯函数口径：工程侧的信号字段不进应用默认样式。"""
    app_default = Style(volume_enabled=True, volume_column_count=6, lit_size=64)
    project = Style(volume_enabled=False, volume_column_count=4, lit_size=30)

    merged = merge_common_style_preferences(app_default, project)

    assert merged.volume_enabled is True
    assert merged.volume_column_count == 6
    assert merged.lit_size == 64


# ---------------------------------------------------------------------------
# 粒子「取色层级」偏好（PARTICLE_MODULE_STYLE_FIELDS，与信号模块同机制）
# ---------------------------------------------------------------------------


def test_particle_color_layers_is_remembered(make_window) -> None:
    """取色层级改一次一直沿用（写进应用默认样式）。"""
    window = make_window()

    _edit_style(window, fx_particle_color_layers="all")

    assert window._app_default_style.fx_particle_color_layers == "all"


def test_opening_another_project_does_not_reset_particle_layers(
    make_window, settings
) -> None:
    """打开旧工程（取色层级还是默认仅实色）后落盘，习惯保持记忆。"""
    window = make_window()
    _edit_style(window, fx_particle_color_layers="decor")
    window._save_persisted_state()

    window._style = replace(window._style, fx_particle_color_layers="solid")
    window._save_persisted_state()

    assert settings.data["style"]["fx_particle_color_layers"] == "decor"
    # 未编辑过的粒子字段（颜色模式等）仍随工程走，不进本集合。
    assert (
        window._app_default_style.fx_particle_color_layers == "decor"
    )


def test_merge_common_style_preferences_skips_particle_layers() -> None:
    """纯函数口径：工程侧的取色层级不进应用默认样式（其余粒子字段照进）。"""
    app_default = Style(fx_particle_color_layers="all")
    project = Style(
        fx_particle_color_layers="solid",
        fx_particle_color_mode="follow_after",
        fx_particle_count=20,
    )

    merged = merge_common_style_preferences(app_default, project)

    assert merged.fx_particle_color_layers == "all"
    # 非习惯字段：跟随工程（merge 语义不变）。
    assert merged.fx_particle_color_mode == "follow_after"
    assert merged.fx_particle_count == 20


def test_particle_speed_is_remembered(make_window) -> None:
    """三类粒子的速度滑条改一次一直沿用（与取色层级同口径，2026-10）。"""
    window = make_window()

    _edit_style(
        window,
        fx_particle_speed_pct=300,
        anim_fx_speed_pct=-200,
        scanline_effect_speed_pct=500,
    )

    assert window._app_default_style.fx_particle_speed_pct == 300
    assert window._app_default_style.anim_fx_speed_pct == -200
    assert window._app_default_style.scanline_effect_speed_pct == 500


def test_opening_another_project_does_not_reset_particle_speed(
    make_window, settings
) -> None:
    """打开速度为 0% 的旧工程后落盘，记忆的速度不被冲回原速。"""
    window = make_window()
    _edit_style(window, fx_particle_speed_pct=250)
    window._save_persisted_state()

    window._style = replace(window._style, fx_particle_speed_pct=0)
    window._save_persisted_state()

    assert settings.data["style"]["fx_particle_speed_pct"] == 250
    assert window._app_default_style.fx_particle_speed_pct == 250


def test_merge_common_style_preferences_skips_particle_speed() -> None:
    """纯函数口径：工程侧的速度不进应用默认样式（尺寸/数量照进）。"""
    app_default = Style(fx_particle_speed_pct=300)
    project = Style(
        fx_particle_speed_pct=-1000,
        fx_particle_size_em=0.8,
        fx_particle_count=20,
    )

    merged = merge_common_style_preferences(app_default, project)

    assert merged.fx_particle_speed_pct == 300
    assert merged.fx_particle_size_em == 0.8
    assert merged.fx_particle_count == 20
