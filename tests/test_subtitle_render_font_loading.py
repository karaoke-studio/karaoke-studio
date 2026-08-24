"""Font list loading overlay shows only for cold catalogs and visible parents."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip  # noqa: E402
from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

import krok_helper.subtitle_render.frontend.widgets.font_loading as font_loading  # noqa: E402
from krok_helper.subtitle_render.frontend.widgets.font_loading import (  # noqa: E402
    FontListLoadingOverlay,
    font_list_loading_overlay,
)


@pytest.fixture()
def qapp():
    yield QApplication.instance() or QApplication([])


def test_overlay_created_for_cold_catalog_and_destroyed_after(qapp, monkeypatch):
    monkeypatch.setattr(font_loading, "is_n3_font_catalog_ready", lambda: False)
    parent = QWidget()
    parent.resize(400, 300)
    parent.show()

    with font_list_loading_overlay(parent) as overlay:
        assert isinstance(overlay, FontListLoadingOverlay)
        assert overlay.isVisible()
        assert parent.findChild(FontListLoadingOverlay) is overlay

    # processEvents() 不派发 DeferredDelete，须显式冲刷（deleteLater 契约）
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(overlay)


def test_no_overlay_when_catalog_warm(qapp, monkeypatch):
    monkeypatch.setattr(font_loading, "is_n3_font_catalog_ready", lambda: True)
    parent = QWidget()
    parent.show()

    with font_list_loading_overlay(parent) as overlay:
        assert overlay is None
        assert parent.findChild(FontListLoadingOverlay) is None


def test_no_overlay_when_parent_hidden(qapp, monkeypatch):
    monkeypatch.setattr(font_loading, "is_n3_font_catalog_ready", lambda: False)
    parent = QWidget()  # 从未 show：启动构造期路径，占位无意义

    with font_list_loading_overlay(parent) as overlay:
        assert overlay is None
