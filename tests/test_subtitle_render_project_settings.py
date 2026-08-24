"""Focused compatibility checks for project save and backup settings UI."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402
import pytest  # noqa: E402

from krok_helper.subtitle_render.frontend.project.project_settings import (  # noqa: E402
    AutoSaveSettingsDialog,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_auto_save_settings_dialog_preserves_values_and_enablement(qapp) -> None:
    dialog = AutoSaveSettingsDialog(False, 7, 9)

    assert dialog.windowTitle() == "项目保存与备份"
    assert dialog.minimumWidth() == 420
    assert dialog.selection() == (False, 7, 9)
    assert dialog.interval_spin.isEnabled() is False

    dialog.enabled_check.setChecked(True)

    assert dialog.selection() == (True, 7, 9)
    assert dialog.interval_spin.isEnabled() is True
    dialog.close()


def test_auto_save_settings_dialog_clamps_persisted_values(qapp) -> None:
    dialog = AutoSaveSettingsDialog(True, 0, 100)

    assert dialog.selection() == (True, 1, 20)
    dialog.close()
