"""Role-property page composition isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame, QSizePolicy

from krok_helper.subtitle_render.frontend.theme import palette, themed


class RolePropertyPageBuilder:
    """Compose role navigation, preview, color and font sub-sections."""

    def __init__(
        self,
        host: Any,
        *,
        plain_card_factory: Callable[..., Any],
        role_header_factory: Callable[..., Any],
        font_preview_factory: Callable[..., Any],
        property_pair_factory: Callable[..., Any],
    ) -> None:
        self._host = host
        self._plain_card_factory = plain_card_factory
        self._role_header_factory = role_header_factory
        self._font_preview_factory = font_preview_factory
        self._property_pair_factory = property_pair_factory

    def make_font_color_section(self) -> QFrame:
        host = self._host
        section, layout = self._plain_card_factory()
        host._scheme_section = section
        host._role_header = self._role_header_factory(section)
        role_navigation = host._make_scheme_navigation(host._role_header)
        host._font_preview_requested = True
        host._font_preview_widget = self._font_preview_factory(host._role_header)
        host._role_header.set_widgets(role_navigation, host._font_preview_widget)
        layout.addWidget(host._role_header)

        row = self._property_pair_factory(section)
        host._font_color_row = row
        host._color_section = host._make_color_section(parent=row, inline=True)
        host._font_section = host._make_font_section(parent=row, inline=True)
        for child in (host._color_section, host._font_section):
            child.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )

        divider = QFrame(row)
        divider.setObjectName("SubtitlePropertyInnerDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedWidth(1)
        divider.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        themed(
            divider,
            lambda: (
                "QFrame#SubtitlePropertyInnerDivider { "
                f"background: {palette().card_border}; "
                "border: 0; "
                "}"
            ),
        )
        row.set_widgets(host._color_section, divider, host._font_section)
        layout.addWidget(row)
        return section
