from __future__ import annotations

from PyQt6.QtWidgets import QApplication

from krok_helper.gui_qt import ElidedLabel


def test_elided_label_truncates_long_text(qapp) -> None:
    full_title = "RES∞NALIST(アニメ「ゴーストコンサート：missing Songs」主題歌)"
    label = ElidedLabel(full_title)
    label.setMaximumWidth(180)
    QApplication.processEvents()

    assert label.text() != full_title
    assert label.text().endswith("…")
    assert label.toolTip() == full_title
