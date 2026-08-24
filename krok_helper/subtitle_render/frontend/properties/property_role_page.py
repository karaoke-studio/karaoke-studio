"""Role-property page composition isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QStackedWidget, QWidget
from qfluentwidgets import CheckBox

from krok_helper.subtitle_render.frontend.properties.property_layout import (
    inline_property_section,
    property_section,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.frontend.properties.property_widgets import FolderTabPanel


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

    def make_font_section(
        self,
        parent: QWidget | None = None,
        *,
        inline: bool = False,
    ) -> QWidget:
        host = self._host
        section, layout = (
            inline_property_section("字体", parent)
            if inline
            else property_section("字体")
        )
        host._font_tab_panel = FolderTabPanel(
            (("japanese", "日文"), ("latin", "英数")),
            (("main", "主文字"), ("ruby", "注音")),
            section,
        )
        host._font_tab_stack = QStackedWidget(host._font_tab_panel)
        host._font_stroke_controls = {}
        host._font_controls = {}
        host._font_size_follow_checks = {}
        for subject, script in (
            ("main", "japanese"),
            ("main", "latin"),
            ("ruby", "japanese"),
            ("ruby", "latin"),
        ):
            host._font_tab_stack.addWidget(
                host._make_font_settings_page(
                    subject,
                    script,
                    host._font_tab_stack,
                )
            )
        host._font_tab_panel.content_layout.addWidget(host._font_tab_stack)
        host._font_tab_panel.leftChanged.connect(host._on_font_script_changed)
        host._font_tab_panel.rightChanged.connect(
            lambda _key: host._sync_font_settings_page()
        )
        layout.addWidget(host._font_tab_panel)

        host._italic_check = CheckBox("斜体", section)
        host._italic_check.toggled.connect(
            lambda checked: host._update_style(italic=checked)
        )
        host._ruby_anchor_check = CheckBox("参与注音高度计算", section)
        host._ruby_anchor_check.setToolTip(
            "关闭后，使用当前角色的字符仍正常绘制和占位，但不会把整行注音向上顶高。"
        )
        host._ruby_anchor_check.toggled.connect(
            lambda checked: host._update_style(affects_ruby_anchor=checked)
        )
        flags_row = QHBoxLayout()
        flags_row.setContentsMargins(0, 0, 0, 0)
        flags_row.setSpacing(12)
        flags_row.addWidget(host._italic_check)
        flags_row.addWidget(host._ruby_anchor_check)
        flags_row.addStretch(1)
        layout.addLayout(flags_row)
        return section
