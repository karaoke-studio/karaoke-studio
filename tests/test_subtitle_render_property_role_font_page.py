"""Focused contracts for one role font-settings page."""

from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.properties.roles.font import (
    FONT_SIZE_MAX_PX,
    RoleFontSettingsPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self._font_controls = {}
        self._font_stroke_controls = {}
        self._font_size_follow_checks = {}
        self.updates = []

    def _refresh_font_weight_combo(self, slot, *, preferred_weight):
        combo = self._font_controls[slot][1]
        combo.addItem("跟随（0）", 0)
        combo.addItem("常规（400）", 400)
        combo.setCurrentIndex(0 if preferred_weight == 0 else 1)

    def _on_font_family_changed(self, _slot, _font):
        pass

    def _update_style(self, **changes):
        self.updates.append(changes)

    def _update_ruby_font_override(self, **changes):
        self.updates.append(changes)

    def _on_font_stroke2_state_changed(self, _field, _spin, _state):
        pass

    def _on_font_stroke2_toggled(self, _field, _spin, _checked):
        pass

    def _on_font_size_follow_toggled(self, _slot, _checked):
        pass


def test_role_font_page_builder_preserves_four_slot_contracts(qapp) -> None:
    host = _Host()
    parent = QWidget()
    builder = RoleFontSettingsPageBuilder(host)
    pages = [
        builder.make_page(subject, script, parent)
        for subject, script in (
            ("main", "japanese"),
            ("main", "latin"),
            ("ruby", "japanese"),
            ("ruby", "latin"),
        )
    ]

    assert len(pages) == 4
    assert set(host._font_controls) == {
        ("main", "japanese"),
        ("main", "latin"),
        ("ruby", "japanese"),
        ("ruby", "latin"),
    }
    assert set(host._font_stroke_controls) == set(host._font_controls)
    assert set(host._font_size_follow_checks) == {
        ("main", "latin"),
        ("ruby", "latin"),
    }
    assert host._font_size_spin.minimum() == 12
    assert host._font_latin_size_spin.minimum() == 0
    assert host._ruby_font_size_spin.minimum() == 8
    assert host._ruby_font_latin_size_spin.minimum() == 0
    assert host._font_size_spin.maximum() == FONT_SIZE_MAX_PX
    assert host._latin_stroke2_enabled_check.isTristate()
    assert host._ruby_stroke2_enabled_check.isTristate()
    assert not host._stroke2_enabled_check.isTristate()


def test_role_font_page_builder_preserves_inheritance_labels(qapp) -> None:
    host = _Host()
    parent = QWidget()
    builder = RoleFontSettingsPageBuilder(host)
    for subject, script in (
        ("main", "japanese"),
        ("main", "latin"),
        ("ruby", "japanese"),
        ("ruby", "latin"),
    ):
        builder.make_page(subject, script, parent)

    assert host._font_controls[("main", "japanese")][2] is None
    assert host._font_controls[("main", "latin")][2] == "跟随主文字日文（0）"
    assert host._font_controls[("ruby", "japanese")][2] == "跟随主文字（0）"
    assert host._font_controls[("ruby", "latin")][2] == "跟随注音日文（0）"
