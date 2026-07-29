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
