"""Opt-in checks that the PyQt host can load the native Qt Quick preview module (P03).

Build with ``build_skia_renderer.py --qt-root <Qt 6.11.0 SDK>`` and set
KROK_SKIA_NATIVE_TEST_ROOT to the cache's ``native`` directory.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

LOAD_MODULE = r"""
import sys
from PyQt6.QtCore import QT_VERSION_STR, QUrl
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlComponent, QQmlEngine
app = QGuiApplication(sys.argv)
engine = QQmlEngine()
engine.addImportPath(sys.argv[1])
component = QQmlComponent(engine)
component.setData(b"import Krok.SubtitleSkia\nSkiaSubtitleItem { width: 64; height: 32 }", QUrl())
item = component.create()
print("qt", QT_VERSION_STR)
print("errors", [e.toString() for e in component.errors()])
print("class", item.metaObject().className() if item else None)
print("alpha", item.property("alphaBlending") if item else None)
"""


SHOW_ITEM = r"""
import sys
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QImage
from PyQt6.QtQuick import QQuickItem, QQuickView, QQuickWindow, QSGRendererInterface
from PyQt6.QtWidgets import QApplication, QWidget
from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import (
    select_quick_graphics_api, verify_quick_graphics_api)
if sys.argv[2] == "required":
    select_quick_graphics_api()
else:
    QQuickWindow.setGraphicsApi(getattr(QSGRendererInterface.GraphicsApi, sys.argv[2]))
app = QApplication(sys.argv)
host = QWidget()
host.resize(240, 120)
view = QQuickView()
view.engine().addImportPath(sys.argv[1])
view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
view.setSource(QUrl("data:text/plain,import QtQuick%0Aimport Krok.SubtitleSkia%0A"
                    "Rectangle { color: 'black'; SkiaSubtitleItem { objectName: 'item'; anchors.fill: parent } }"))
container = QWidget.createWindowContainer(view, host)
container.resize(240, 120)
item = view.rootObject().findChild(QQuickItem, "item")

def poll():
    # graphicsBackendChanged is a C++-only signal; reading the property is enough here.
    if not item.property("graphicsBackend"):
        return
    poller.stop()
    try:
        verify_quick_graphics_api(view)
        verified = "ok"
    except RuntimeError:
        verified = "rejected"
    print("backend", item.property("graphicsBackend"))
    print("accepted", item.property("graphicsBackendAccepted"))
    print("host_check", verified)
    # Two grabs 400 ms apart: test-only readback of what the scene graph actually composited.
    QTimer.singleShot(300, lambda: grab(0))

frames = []

def grab(index):
    image = view.grabWindow().convertToFormat(QImage.Format.Format_RGBA8888)
    data = image.constBits().asstring(image.sizeInBytes())
    pixels = [data[i:i + 3] for i in range(0, len(data), 4)]
    frames.append(pixels)
    if index == 0:
        QTimer.singleShot(400, lambda: grab(1))
        return
    coloured = sum(1 for p in frames[1] if max(p) > 24) / len(frames[1])
    moved = sum(1 for a, b in zip(frames[0], frames[1]) if a != b) / len(frames[1])
    print("coloured", f"{coloured:.4f}")
    print("moved", f"{moved:.4f}")
    app.quit()

poller = QTimer()
poller.timeout.connect(poll)
poller.start(20)
QTimer.singleShot(10000, lambda: (print("timeout"), app.quit()))
host.show()
sys.exit(app.exec())
"""


def test_required_quick_graphics_api_per_platform():
    from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import (
        GraphicsApi, required_quick_graphics_api)
    assert required_quick_graphics_api("win32") == GraphicsApi.Vulkan
    assert required_quick_graphics_api("linux") == GraphicsApi.Vulkan
    assert required_quick_graphics_api("darwin") == GraphicsApi.Metal


def test_host_rejects_window_on_another_graphics_api():
    from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import (
        GraphicsApi, verify_quick_graphics_api)

    class Interface:
        def __init__(self, api):
            self.api = api

        def graphicsApi(self):
            return self.api

    class Window:
        def __init__(self, interface):
            self.interface = interface

        def rendererInterface(self):
            return self.interface

    assert verify_quick_graphics_api(Window(Interface(GraphicsApi.Vulkan)), "win32") == GraphicsApi.Vulkan
    with pytest.raises(RuntimeError, match="Direct3D11"):
        verify_quick_graphics_api(Window(Interface(GraphicsApi.Direct3D11)), "win32")
    with pytest.raises(RuntimeError, match="尚未初始化"):
        verify_quick_graphics_api(Window(None), "win32")


@pytest.fixture(scope="module")
def qml_dir():
    configured = os.environ.get("KROK_SKIA_NATIVE_TEST_ROOT")
    if not configured:
        pytest.skip("Set KROK_SKIA_NATIVE_TEST_ROOT after building with --qt-root")
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}[sys.platform]
    arch = os.environ.get("KROK_SKIA_NATIVE_TEST_ARCH", "x64")
    api = "metal" if system == "macos" else "vulkan"
    directory = Path(configured) / f"{system}-{arch}-{api}" / "qml"
    if not (directory / "Krok/SubtitleSkia/qmldir").is_file():
        pytest.skip(f"Preview module not built: {directory}")
    return directory


def test_pyqt_host_loads_native_quick_module(qml_dir):
    # Loading needs no GPU, so the offscreen platform keeps this test headless.
    env = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run([sys.executable, "-c", LOAD_MODULE, str(qml_dir)], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    assert lines["qt"] == "6.11.0", "PyQt runtime must match the SDK the module was built with"
    assert lines["errors"] == "[]", result.stderr
    assert lines["class"].endswith("SkiaSubtitleItem")
    assert lines["alpha"] == "True"


def show_item(qml_dir, api):
    # A real window: Vulkan/Metal need the platform's windowing system, not offscreen.
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    # Without a console, Qt on Windows logs to the debugger instead of the stderr pipe.
    env["QT_FORCE_STDERR_LOGGING"] = "1"
    result = subprocess.run([sys.executable, "-c", SHOW_ITEM, str(qml_dir), api], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert "timeout" not in result.stdout, result.stdout + result.stderr
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line), result.stderr


def test_item_runs_on_the_platform_skia_api(qml_dir):
    lines, stderr = show_item(qml_dir, "required")
    assert lines["backend"] == ("Metal" if sys.platform == "darwin" else "Vulkan")
    assert lines["accepted"] == "True"
    assert lines["host_check"] == "ok"
    if sys.platform == "darwin":
        pytest.skip("Metal texture hand-off is not implemented yet")
    # The shared Skia probe reached the composited window over the black QML background...
    assert float(lines["coloured"]) > 0.05, stderr
    # ...and it is animated by time, so later frames really were drawn by Skia.
    assert float(lines["moved"]) > 0.01, stderr
    assert "Skia" not in stderr, stderr


def test_item_and_host_reject_other_scene_graph_apis(qml_dir):
    other = "Direct3D11" if sys.platform == "win32" else "OpenGL"
    lines, stderr = show_item(qml_dir, other)
    assert lines["backend"] not in ("Vulkan", "Metal")
    assert lines["accepted"] == "False"
    assert lines["host_check"] == "rejected"
    assert "Rejecting scene graph backend" in stderr
