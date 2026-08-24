"""Fill-editor pages used by the role color section."""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QVBoxLayout, QWidget


class RoleFillPagesBuilder:
    """Build fill editors while color mutations remain on the panel host."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def make_solid_page(self) -> QWidget:
        host = self._host
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        host._paint_solid_btn = host._paint_color_button("color", "#FFFFFF")
        layout.addWidget(host._paint_solid_btn)
        return page
