"""Opt-in checks that the PyQt host can load the native Qt Quick preview module (P03).

Build with ``build_skia_renderer.py --qt-root <Qt 6.11.0 SDK>`` and set
KROK_SKIA_NATIVE_TEST_ROOT to the cache's ``native`` directory.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
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


def show_item(qml_dir, api, extra_env=None):
    # A real window: Vulkan/Metal need the platform's windowing system, not offscreen.
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    # Without a console, Qt on Windows logs to the debugger instead of the stderr pipe.
    env["QT_FORCE_STDERR_LOGGING"] = "1"
    env.update(extra_env or {})
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


HANDOFF = r"""
import sys
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtQuick import QQuickView
from PyQt6.QtWidgets import QApplication, QWidget
from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import select_quick_graphics_api
select_quick_graphics_api()
app = QApplication(sys.argv)
host = QWidget()
host.resize(320, 160)
view = QQuickView()
view.engine().addImportPath(sys.argv[1])
view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
view.setSource(QUrl("data:text/plain,import QtQuick%0Aimport Krok.SubtitleSkia%0A"
                    "Rectangle { color: 'black'; SkiaSubtitleItem { anchors.fill: parent } }"))
container = QWidget.createWindowContainer(view, host)
container.resize(320, 160)
frames = [0]
view.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))
# Resizing recreates the item texture, so a second wrap/transition cycle is validated too.
QTimer.singleShot(1500, lambda: (host.resize(400, 200), container.resize(400, 200)))
QTimer.singleShot(3000, lambda: (print("frames", frames[0]), app.quit()))
host.show()
sys.exit(app.exec())
"""

# Qt 6.11.0's QRhiVulkan creates QQuickRhiItem's colour texture (usage COLOR_ATTACHMENT |
# SAMPLED | TRANSFER_SRC | TRANSFER_DST, item-sized) with PREINITIALIZED + OPTIMAL tiling.
# Attributed with the API dump layer; Skia only wraps that image and never creates it.
QT_OWNED_VALIDATION_IDS = {"VUID-VkImageCreateInfo-initialLayout-12478"}


def test_texture_handoff_is_clean_under_vulkan_validation(qml_dir, tmp_path):
    if sys.platform == "darwin":
        pytest.skip("Vulkan validation; Metal uses its own API validation")
    if not os.environ.get("KROK_VULKAN_VALIDATION"):
        pytest.skip("Set KROK_VULKAN_VALIDATION=1 with the Khronos validation layer installed")
    log = tmp_path / "validation.log"
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"} | {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        # Third-party overlays (OBS, Steam, vendor hooks) must not add noise or hide errors.
        "VK_LOADER_LAYERS_DISABLE": "~implicit~",
        "VK_INSTANCE_LAYERS": "VK_LAYER_KHRONOS_validation",
        "VK_KHRONOS_VALIDATION_VALIDATE_SYNC": "true",
        "VK_KHRONOS_VALIDATION_REPORT_FLAGS": "info,warn,error",
        "VK_KHRONOS_VALIDATION_DEBUG_ACTION": "VK_DBG_LAYER_ACTION_LOG_MSG",
        "VK_KHRONOS_VALIDATION_LOG_FILENAME": str(log),
    }
    if os.environ.get("VULKAN_SDK"):
        layers = "Bin" if sys.platform == "win32" else "share/vulkan/explicit_layer.d"
        env["VK_ADD_LAYER_PATH"] = str(Path(os.environ["VULKAN_SDK"]) / layers)
    result = subprocess.run([sys.executable, "-c", HANDOFF, str(qml_dir)], env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.split()[-1]) > 30, result.stdout
    text = log.read_text(encoding="utf-8", errors="replace")
    # Positive control: the layer really ran, with synchronization checks enabled.
    assert "CURRENT-VALIDATION-ENABLED" in text and "Synchronization" in text, text[:2000]
    reported = re.findall(r"^Validation (?:Error|Warning): \[ ([^\]]+?) \]", text, re.M)
    assert not [r for r in reported if r.startswith("SYNC-")], text[:4000]
    assert set(reported) <= QT_OWNED_VALIDATION_IDS, text[:4000]


def test_metal_preview_under_active_api_validation(qml_dir):
    if sys.platform != "darwin":
        pytest.skip("Metal API validation")
    if not os.environ.get("KROK_METAL_VALIDATION"):
        pytest.skip("Set KROK_METAL_VALIDATION=1 to run under Apple's Metal API validation")
    probe = qml_dir.parent / "krok_metal_validation_probe"
    base = {k: v for k, v in os.environ.items() if not k.startswith("MTL_")}
    layer = {"MTL_DEBUG_LAYER": "1", "MTL_DEBUG_LAYER_ERROR_MODE": "assert",
             "MTL_DEBUG_LAYER_WARNING_MODE": "nslog"}
    plain = subprocess.run([str(probe)], env=base, capture_output=True, text=True, timeout=60)
    assert plain.returncode == 0 and "validation inactive" in plain.stdout, plain.stdout + plain.stderr
    # Positive control: the same invalid call must be stopped once the layer is requested.
    checked = subprocess.run([str(probe)], env=base | layer, capture_output=True, text=True, timeout=60)
    assert checked.returncode != 0, checked.stdout + checked.stderr
    assert "validation inactive" not in checked.stdout
    assert "assert" in (checked.stdout + checked.stderr).lower(), checked.stdout + checked.stderr
    # The real preview under that same environment: any Metal API error would abort it.
    lines, stderr = show_item(qml_dir, "required", layer)
    assert lines["backend"] == "Metal" and lines["accepted"] == "True", stderr
    assert float(lines["coloured"]) > 0.05 and float(lines["moved"]) > 0.01, stderr
