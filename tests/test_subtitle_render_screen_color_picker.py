"""Frozen desktop picking must consume both halves of the mouse gesture."""

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QMouseEvent, QPixmap
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.frontend.properties.color_controls import (
    ScreenColorPicker, clear_recent_colors, recent_colors,
)


@pytest.fixture
def qapp():
    return QApplication.instance()


@pytest.fixture
def picker(qapp):
    clear_recent_colors()
    widget = ScreenColorPicker()
    widget.setGeometry(0, 0, 160, 120)
    image = QImage(160, 120, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    widget._screens = [(QRect(0, 0, 160, 120), image)]
    widget.start()
    qapp.processEvents()
    yield widget
    if not sip.isdeleted(widget):
        widget._pressed_buttons = Qt.MouseButton.NoButton
        widget.cancel()
    clear_recent_colors()


def test_picker_is_fully_opaque_and_paints_the_frozen_frame(picker):
    assert not picker.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not picker.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert picker.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
    rendered = picker.grab().toImage()
    for point in (QPoint(0, 0), QPoint(80, 60), QPoint(rendered.width() - 1, rendered.height() - 1)):
        assert rendered.pixelColor(point) == QColor("#123456")


def test_left_click_stays_captured_until_release_then_records_once(picker):
    picked, finished = [], []
    picker.colorPicked.connect(picked.append)
    picker.finished.connect(lambda: finished.append(True))
    QTest.mousePress(picker, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert picker.isVisible() and picker._active
    assert picked == finished == recent_colors() == []
    QTest.mouseRelease(picker, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert picked == [QColor("#123456")]
    assert finished == [True]
    assert not picker.isVisible() and not picker._active
    assert recent_colors() == [QColor("#123456")]


def test_press_location_is_used_even_if_mouse_moves_before_release(picker):
    picker._screens[0][1].setPixelColor(80, 80, QColor("#ABCDEF"))
    picked = []
    picker.colorPicked.connect(picked.append)
    QTest.mousePress(picker, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    QTest.mouseMove(picker, QPoint(80, 80))
    QTest.mouseRelease(picker, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
    assert picked == [QColor("#123456")]


def test_right_click_cancels_only_after_release(picker):
    finished = []
    picker.finished.connect(lambda: finished.append(True))
    QTest.mousePress(picker, Qt.MouseButton.RightButton, pos=QPoint(30, 30))
    assert picker.isVisible() and picker._active
    assert finished == []
    QTest.mouseRelease(picker, Qt.MouseButton.RightButton, pos=QPoint(30, 30))
    assert finished == [True]
    assert recent_colors() == []


def test_escape_during_mouse_press_drains_release_without_picking(picker):
    QTest.mousePress(picker, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    QTest.keyClick(picker, Qt.Key.Key_Escape)
    assert picker.isVisible() and picker._active
    QTest.mouseRelease(picker, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    assert not picker.isVisible() and not picker._active
    assert recent_colors() == []


def test_escape_without_a_pressed_button_finishes_immediately(picker):
    QTest.keyClick(picker, Qt.Key.Key_Escape)
    assert not picker.isVisible() and not picker._hover_timer.isActive()
    assert recent_colors() == []


def test_extra_buttons_and_shortcuts_do_not_propagate_or_finish_picking(picker):
    QTest.mouseClick(picker, Qt.MouseButton.MiddleButton, pos=QPoint(30, 30))
    assert picker.isVisible() and picker._active
    for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        from PyQt6.QtGui import QKeyEvent
        event = QKeyEvent(event_type, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
        event.ignore()
        QApplication.sendEvent(picker, event)
        assert event.isAccepted()
    assert recent_colors() == []


def test_chorded_cancel_keeps_overlay_until_every_button_is_released(picker):
    def send(event_type, button, buttons):
        event = QMouseEvent(event_type, QPointF(30, 30), QPointF(30, 30), button, buttons,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(picker, event)
        assert event.isAccepted()

    send(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    send(QEvent.Type.MouseButtonPress, Qt.MouseButton.RightButton,
         Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
    send(QEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
    assert picker.isVisible() and picker._active
    send(QEvent.Type.MouseButtonRelease, Qt.MouseButton.RightButton, Qt.MouseButton.NoButton)
    assert not picker.isVisible() and not picker._active
    assert recent_colors() == []


def test_missing_snapshot_never_samples_the_overlay_itself(qapp):
    picker = ScreenColorPicker()
    assert not picker.color_at(QPoint(20, 20)).isValid()
    picker.cancel()


def test_capture_precedes_show_and_keeps_pixmaps_without_full_image_conversion(qapp, monkeypatch):
    order = []

    class Screen:
        def __init__(self, geometry, color, dpr):
            self.rect = geometry
            self.image = QPixmap(geometry.width() * dpr, geometry.height() * dpr)
            self.image.setDevicePixelRatio(dpr)
            self.image.fill(QColor(color))

        def geometry(self):
            return self.rect

        def grabWindow(self, window):
            assert window == 0
            order.append("capture")
            return QPixmap(self.image)

    screens = [Screen(QRect(-160, 0, 160, 120), "#123456", 2),
               Screen(QRect(0, 0, 160, 120), "#ABCDEF", 1)]
    monkeypatch.setattr(QApplication, "screens", staticmethod(lambda: screens))
    picker = ScreenColorPicker()
    real_to_image = QPixmap.toImage

    def small_pixel_conversion(pixmap):
        assert pixmap.width() == pixmap.height() == 1
        return real_to_image(pixmap)

    monkeypatch.setattr(QPixmap, "toImage", small_pixel_conversion)
    monkeypatch.setattr(picker, "show", lambda: order.append("show"))
    for method in ("raise_", "activateWindow", "setFocus", "grabMouse", "grabKeyboard"):
        monkeypatch.setattr(picker, method, lambda *_args: None)
    picker.start()
    assert order == ["capture", "capture", "show"]
    assert all(isinstance(image, QPixmap) for _, image in picker._screens)
    assert picker.color_at(QPoint(-80, 60)) == QColor("#123456")
    assert picker.color_at(QPoint(80, 60)) == QColor("#ABCDEF")
    # A running video behind the overlay cannot change the frozen selection.
    screens[0].image.fill(QColor("#FFFFFF"))
    assert picker.color_at(QPoint(-80, 60)) == QColor("#123456")
    picker.cancel()


def test_capture_failure_cancels_before_showing_a_black_overlay(qapp, monkeypatch):
    class Screen:
        def geometry(self):
            return QRect(0, 0, 160, 120)

        def grabWindow(self, window):
            return QPixmap()

    monkeypatch.setattr(QApplication, "screens", staticmethod(lambda: [Screen()]))
    picker = ScreenColorPicker()
    shown, finished = [], []
    monkeypatch.setattr(picker, "show", lambda: shown.append(True))
    picker.finished.connect(lambda: finished.append(True))
    picker.start()
    assert shown == [] and finished == [True]
    assert not picker._active and not picker._hover_timer.isActive()


def test_pointer_preview_shows_the_sample_color_and_follows_same_color_pixels(picker):
    picker.setGeometry(0, 0, 640, 480)
    image = QImage(640, 480, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    picker._screens = [(QRect(0, 0, 640, 480), image)]
    hovered = []
    picker.colorHovered.connect(hovered.append)
    picker._last_hovered_rgba = None
    picker._emit_hovered_color(QPoint(100, 100))
    first_rect = QRect(picker._preview_rect)
    assert picker._preview_color == QColor("#123456")
    assert not first_rect.isEmpty()
    assert not first_rect.contains(QPoint(100, 100))
    picker._emit_hovered_color(QPoint(120, 120))
    assert picker._preview_rect.topLeft() == first_rect.topLeft() + QPoint(20, 20)
    assert hovered == [QColor("#123456")]  # Moving the card does not emit duplicate colors.
    image.setPixelColor(140, 140, QColor("#ABCDEF"))
    picker._emit_hovered_color(QPoint(140, 140))
    assert picker._preview_color == QColor("#ABCDEF")
    assert hovered == [QColor("#123456"), QColor("#ABCDEF")]


@pytest.mark.parametrize("point", [QPoint(1, 1), QPoint(638, 1), QPoint(1, 478), QPoint(638, 478), QPoint(320, 240)])
@pytest.mark.parametrize("origin", [QPoint(0, 0), QPoint(-640, -480)])
def test_pointer_preview_stays_on_the_sampled_monitor_and_avoids_the_pointer(picker, point, origin):
    screen = QRect(origin.x(), origin.y(), 640, 480)
    picker.setGeometry(screen)
    image = QImage(640, 480, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    picker._screens = [(screen, image)]
    picker._emit_hovered_color(point + origin)
    assert not picker._preview_rect.isEmpty()
    assert picker.rect().contains(picker._preview_rect)
    assert not picker._preview_rect.contains(point)


def test_preview_is_painted_but_never_becomes_the_sample_source(picker):
    picker.setGeometry(0, 0, 640, 480)
    image = QImage(640, 480, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    picker._screens = [(QRect(0, 0, 640, 480), image)]
    picker._emit_hovered_color(QPoint(100, 100))
    rect = picker._preview_rect
    rendered = picker.grab().toImage()
    scale = rendered.devicePixelRatio()
    panel_point = rect.topLeft() + QPoint(8, 8)
    assert rendered.pixelColor(round(panel_point.x() * scale), round(panel_point.y() * scale)) == QColor("#20242C")
    assert picker.color_at(panel_point) == QColor("#123456")
    swatch_point = rect.topLeft() + QPoint(32, 34)
    assert rendered.pixelColor(round(swatch_point.x() * scale), round(swatch_point.y() * scale)) == QColor("#123456")


def test_preview_disappears_outside_captured_screens(picker):
    picker.setGeometry(0, 0, 640, 480)
    image = QImage(640, 480, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123456"))
    picker._screens = [(QRect(0, 0, 640, 480), image)]
    picker._emit_hovered_color(QPoint(100, 100))
    assert not picker._preview_rect.isEmpty()
    picker._emit_hovered_color(QPoint(900, 900))
    assert picker._preview_rect.isEmpty()
