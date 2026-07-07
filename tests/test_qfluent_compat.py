"""qfluentwidgets host compatibility regressions."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets import RoundMenu  # noqa: E402
from qfluentwidgets.components.widgets.menu import (  # noqa: E402
    MenuAnimationManager,
    MenuAnimationType,
)

from krok_helper.qfluent_compat import apply_qfluent_menu_lifetime_patch  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_deleted_menu_animation_callback_is_ignored(qapp):
    apply_qfluent_menu_lifetime_patch()
    callback = MenuAnimationManager._updateMenuViewport
    assert getattr(callback, "_krok_menu_lifetime_safe", False)

    menu = RoundMenu()
    manager = MenuAnimationManager.make(menu, MenuAnimationType.DROP_DOWN)
    menu.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(menu)

    # This is the exact late valueChanged callback that crashed the process.
    manager._updateMenuViewport()


def test_qfluent_menu_lifetime_patch_is_idempotent():
    apply_qfluent_menu_lifetime_patch()
    callback = MenuAnimationManager._updateMenuViewport
    apply_qfluent_menu_lifetime_patch()
    assert MenuAnimationManager._updateMenuViewport is callback
