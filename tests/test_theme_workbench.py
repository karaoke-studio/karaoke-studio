from __future__ import annotations

from types import SimpleNamespace

import pytest

from krok_helper.settings import (
    AppSettings,
    UI_THEME_AUTO,
    UI_THEME_DARK,
    UI_THEME_LIGHT,
)
from krok_helper import theme_workbench


@pytest.mark.parametrize(
    ("setting", "is_dark", "expected_mode", "expected_fluent_theme"),
    [
        (
            UI_THEME_AUTO,
            True,
            theme_workbench.ThemeMode.AUTO,
            True,
        ),
        (
            UI_THEME_AUTO,
            False,
            theme_workbench.ThemeMode.AUTO,
            False,
        ),
        (
            UI_THEME_DARK,
            True,
            theme_workbench.ThemeMode.DARK,
            True,
        ),
        (
            UI_THEME_LIGHT,
            False,
            theme_workbench.ThemeMode.LIGHT,
            False,
        ),
    ],
)
def test_apply_settings_theme_always_syncs_fluent_theme(
    monkeypatch,
    setting,
    is_dark,
    expected_mode,
    expected_fluent_theme,
) -> None:
    fake_theme = SimpleNamespace(
        mode=theme_workbench.ThemeMode.AUTO,
        is_dark=is_dark,
    )
    calls = []
    monkeypatch.setattr(theme_workbench, "theme", fake_theme)
    monkeypatch.setattr(
        theme_workbench,
        "_sync_fluent_theme",
        calls.append,
    )

    theme_workbench.apply_settings_theme(AppSettings(ui_theme=setting))

    assert fake_theme.mode is expected_mode
    assert calls == [expected_fluent_theme]


def test_detach_fluent_qss_survives_theme_reapply() -> None:
    """注销托管后，Fluent 主题刷新不得再覆盖自绘 QSS。

    对照组（未注销）复现的正是波形对齐素材卡片的老 bug：``setTheme``
    立即重写可见控件、并给隐藏控件打 ``dirty-qss`` 留到首次 paint 重写。
    """
    from qfluentwidgets import Theme, ToolButton, setTheme, qconfig

    original_theme = qconfig.themeMode.value
    custom = "ToolButton { background: #FFF0F3; }"

    managed = ToolButton()
    managed.setStyleSheet(custom)
    detached = ToolButton()
    detached.setStyleSheet(custom)
    theme_workbench.detach_fluent_qss(detached)

    try:
        setTheme(Theme.DARK, lazy=False)
        assert managed.styleSheet() != custom  # 对照组：被 Fluent 抹掉
        assert detached.styleSheet() == custom

        setTheme(Theme.LIGHT, lazy=True)
        assert detached.styleSheet() == custom
        assert not detached.property("dirty-qss")
    finally:
        setTheme(original_theme, lazy=False)
        managed.deleteLater()
        detached.deleteLater()
