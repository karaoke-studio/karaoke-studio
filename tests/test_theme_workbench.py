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


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scope", ["application", "parent"])
def test_progress_bar_styles_do_not_flatten_progress_rings(qapp, monkeypatch, dark, scope):
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QProgressBar, QVBoxLayout, QWidget
    from qfluentwidgets import ProgressBar, ProgressRing

    monkeypatch.setattr(theme_workbench, "palette", lambda: theme_workbench.WorkbenchPalette(dark))
    original_qss = qapp.styleSheet()
    host = QWidget()
    layout = QVBoxLayout(host)
    bars = [QProgressBar(host), ProgressBar(host)]
    rings = [ProgressRing(host, useAni=False), ProgressRing(host, useAni=False)]
    for ring, size in zip(rings, (48, 100)):
        ring.setFixedSize(size, size)
        ring.setCustomBarColor(QColor("#ff5470"), QColor("#ff5470"))
    for widget in [*bars, *rings]:
        widget.setValue(100)
        layout.addWidget(widget)
    try:
        host.show()
        # Style installation and subsequent repolish both used to turn a 48px
        # ring into a 10px strip, despite its earlier setFixedSize(48, 48).
        for _ in range(2):
            qss = theme_workbench.build_app_qss()
            (qapp if scope == "application" else host).setStyleSheet(qss)
            qapp.processEvents()
            for bar in bars:
                assert bar.height() == 10
            bar_image = bars[0].grab().toImage()
            assert bar_image.pixelColor(bar_image.width() // 4, bar_image.height() // 2) == QColor(
                theme_workbench.palette().progress_chunk
            )
            for ring, size in zip(rings, (48, 100)):
                assert ring.size() == QSize(size, size)
                image = ring.grab().toImage()
                points = [(x, y) for y in range(image.height()) for x in range(image.width())
                          if (color := image.pixelColor(x, y)).red() > 200
                          and color.green() < 140 and color.blue() < 180]
                assert points
                # Check the painted circle, not only its widget's geometry.
                assert max(x for x, _ in points) - min(x for x, _ in points) >= image.width() - 3
                assert max(y for _, y in points) - min(y for _, y in points) >= image.height() - 3
    finally:
        host.close()
        host.deleteLater()
        qapp.setStyleSheet(original_qss)
