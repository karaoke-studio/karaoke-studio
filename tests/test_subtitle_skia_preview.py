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

def desktop_environment(platform=sys.platform):
    # Strip the application's headless default, but retain explicit X11/Wayland selection.
    return {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"
            or (platform == "linux" and v in {"xcb", "wayland"})}


@pytest.mark.parametrize("platform,selection,expected", [
    ("linux", "xcb", "xcb"), ("linux", "wayland", "wayland"),
    ("linux", "offscreen", None), ("win32", "offscreen", None), ("darwin", "offscreen", None)])
def test_desktop_environment_preserves_explicit_linux_window_system(monkeypatch, platform, selection, expected):
    monkeypatch.setenv("QT_QPA_PLATFORM", selection)
    assert desktop_environment(platform).get("QT_QPA_PLATFORM") == expected
    assert os.environ["QT_QPA_PLATFORM"] == selection


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
print("platform", app.platformName())
host = QWidget()
host.resize(240, 120)
view = QQuickView()
view.engine().addImportPath(sys.argv[1])
view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
view.setSource(QUrl("data:text/plain,import QtQuick%0Aimport Krok.SubtitleSkia%0A"
                    "Rectangle { color: 'black'; SkiaSubtitleItem { objectName: 'item'; motionProbeEnabled: true; anchors.fill: parent } }"))
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


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_preview_surface_format_does_not_mutate_global_defaults(platform):
    from PyQt6.QtGui import QSurfaceFormat
    from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import _preview_surface_format
    before = QSurfaceFormat.defaultFormat().swapInterval()
    requested = _preview_surface_format(platform)
    assert requested.swapInterval() == (0 if platform == "linux" else before)
    assert QSurfaceFormat.defaultFormat().swapInterval() == before


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


REAL_SCENE = r"""
import json
import sys
from pathlib import Path
from PyQt6.QtCore import QByteArray, QObject, QTimer, QUrl, pyqtProperty
from PyQt6.QtGui import QGuiApplication, QImage
from PyQt6.QtQuick import QQuickView
from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import select_quick_graphics_api
from krok_helper.subtitle_render.native.skia_scene import prepare_skia_scene_ir

select_quick_graphics_api()
app = QGuiApplication(sys.argv)
binary = Path(sys.argv[1]).parent / ('krok_skia_subtitle_renderer.exe' if sys.platform == 'win32' else 'krok_skia_subtitle_renderer')
track = TimingTrack(lines=[TimingLine(chars=[TimingChar('A', 400), TimingChar('B', 700)], end_ms=1000,
    display_start_override_ms=0, display_end_override_ms=2000)])
style = Style(font_family='DejaVu Sans' if sys.platform == 'linux' else 'Arial', font_size_px=170,
    karaoke_colors=KaraokeColors(before=KaraokeColorState(text=PaintFill(color='#FF1414')),
        after=KaraokeColorState(text=PaintFill(color='#14FF14'))), decoration_kind='none',
    stroke_width_px=0, stroke2_enabled=False, dual_line_layout=False, karaoke_anim='none', entry_anim='none',
    exit_anim='none', line_horizontal_layout='center', line_y_position='center', smart_horizontal='none')
ir = prepare_skia_scene_ir(track, style, executable=binary, width=640, height=360, fps=60)

class Source(QObject):
    @pyqtProperty(QByteArray, constant=True)
    def good(self):
        return QByteArray(json.dumps(ir, ensure_ascii=False).encode())

    @pyqtProperty(QByteArray, constant=True)
    def bad(self):
        return QByteArray(b'[]')

source = Source()
validate_only = 'validation' in sys.argv
view = QQuickView()
view.engine().addImportPath(sys.argv[1])
view.rootContext().setContextProperty('sceneSource', source)
qml = r'''
import QtQuick
import Krok.SubtitleSkia
Rectangle {
    width: 640; height: 360; color: 'black'
    property int lastRevision: 0
    property bool accepted: false
    property string failure: ''
    property string receipts: ''
    function start() {
        return renderer.configureScene(sceneSource.bad, 1) && renderer.configureScene(sceneSource.good, 2)
    }
    function seek(revision, generation, timeMs) { return renderer.requestFrame(revision, generation, timeMs) }
    function seekPair() { return renderer.requestFrame(2, 2, 1250) && renderer.requestFrame(2, 2, 1350) }
    function rejectScene() { return renderer.configureScene(sceneSource.bad, 3) }
    function clearScene() {
        return renderer.configureScene(sceneSource.good, 4) && renderer.clearScene(5)
    }
    SkiaSubtitleItem {
        id: renderer; anchors.fill: parent
        onScenePrepared: function(revision, ok, error) {
            parent.lastRevision = revision
            parent.accepted = ok
            parent.failure = error
        }
        onFrameSubmitted: function(revision, generation, timeMs) {
            parent.receipts += revision + '/' + generation + '/' + timeMs + ';'
        }
    }
}'''
from PyQt6.QtQml import QQmlComponent
component = QQmlComponent(view.engine())
component.setData(qml.encode(), QUrl())
root = component.create()
assert root is not None, [e.toString() for e in component.errors()]
view.setContent(QUrl(), component, root)
view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
view.resize(640, 360)
view.show()
assert root.start()
assert not root.seek(1, 1, 100)

def capture(done):
    if validate_only:
        done()
        return
    # Capture the item's offscreen texture. Window grabs touch the swapchain
    # and introduce Qt's own presentation/readback barriers into this check.
    grab = root.grabToImage()
    assert grab is not None
    grabs.append(grab)
    def ready():
        image = grab.image().convertToFormat(QImage.Format.Format_RGBA8888)
        frames.append(image.constBits().asstring(image.sizeInBytes()))
        done()
    grab.ready.connect(ready)

def first_frame():
    if root.property('lastRevision') != 2:
        return
    timer.stop()
    assert root.property('accepted'), root.property('failure')
    assert root.seek(2, 1, 100)
    QTimer.singleShot(250, lambda: capture(capture_before))

def capture_before():
    assert root.seekPair()
    assert not root.seek(2, 1, 1400)
    QTimer.singleShot(250, lambda: capture(capture_after))

def capture_after():
    assert root.rejectScene()
    timer.timeout.disconnect()
    timer.timeout.connect(verify_rollback)
    timer.start(20)

def verify_rollback():
    if root.property('lastRevision') != 3:
        return
    timer.stop()
    assert not root.property('accepted') and root.property('failure')
    assert not root.seek(3, 3, 100)
    assert root.seek(2, 3, 100)
    QTimer.singleShot(250, lambda: capture(capture_revisit))

def capture_revisit():
    assert root.property('receipts') == '2/1/100;2/2/1350;2/3/100;', root.property('receipts')
    assert root.clearScene()
    assert not root.seek(2, 4, 100) and not root.seek(4, 4, 100)
    QTimer.singleShot(250, lambda: capture(verify_clear))

def verify_clear():
    assert root.property('lastRevision') == 3
    if validate_only:
        print('validated scene', flush=True)
        app.quit()
        return
    assert frames[0] == frames[2]
    assert frames[0] != frames[1]
    assert not any(frames[3][0::4]) and not any(frames[3][1::4])
    red = sum(r > 180 and g < 80 for r, g in zip(frames[0][0::4], frames[0][1::4]))
    green = sum(r < 80 and g > 180 for r, g in zip(frames[1][0::4], frames[1][1::4]))
    print('scene', red, green, flush=True)
    app.quit()

frames = []
grabs = []
timer = QTimer()
timer.timeout.connect(first_frame)
timer.start(20)
QTimer.singleShot(12000, app.quit)
sys.exit(app.exec())
"""


def test_quick_native_scene_uses_explicit_clock_and_keeps_last_good_scene(qml_dir):
    result = subprocess.run([sys.executable, '-c', REAL_SCENE, str(qml_dir)],
                            env=desktop_environment(), capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r'^scene (\d+) (\d+)$', result.stdout, re.M)
    assert match and min(map(int, match.groups())) > 40, result.stdout + result.stderr


def test_quick_native_scene_handoff_under_vulkan_validation(qml_dir, tmp_path):
    if sys.platform == 'darwin' or not os.environ.get('KROK_VULKAN_VALIDATION'):
        pytest.skip('Needs an active Khronos Vulkan validation layer')
    log = tmp_path / 'validation.log'
    env = desktop_environment() | vulkan_validation_environment(log) | {
        'QT_LOGGING_RULES': 'krok.subtitle.skia.preview.debug=true',
        'QT_FORCE_STDERR_LOGGING': '1',
    }
    result = subprocess.run([sys.executable, '-c', REAL_SCENE, str(qml_dir), 'validation'],
                            env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0 and 'validated scene' in result.stdout, result.stdout + result.stderr
    frames = [(int(t), int(g), int(r)) for t, g, r in re.findall(
        r'\bscene-time (\d+) generation (\d+) revision (\d+)\b', result.stderr)]
    assert list(dict.fromkeys(frames)) == [(100, 1, 2), (1350, 2, 2), (100, 3, 2)], result.stderr
    assert_vulkan_validation_clean(log)


def test_pyqt_host_loads_native_quick_module(qml_dir):
    # Loading needs no GPU, so the offscreen platform keeps this test headless.
    env = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run([sys.executable, "-c", LOAD_MODULE, str(qml_dir)], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    assert lines["qt"] == "6.11.0", "PyQt runtime must match the SDK the module was built with"
    assert lines["errors"] == "[]", result.stderr
    assert lines["class"].endswith("SkiaSubtitleItem")
    assert lines["alpha"] == "True"


def show_item(qml_dir, api, extra_env=None):
    # A real window: Vulkan/Metal need the platform's windowing system, not offscreen.
    env = desktop_environment()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    # Without a console, Qt on Windows logs to the debugger instead of the stderr pipe.
    env["QT_FORCE_STDERR_LOGGING"] = "1"
    env.update(extra_env or {})
    result = subprocess.run([sys.executable, "-c", SHOW_ITEM, str(qml_dir), api], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    assert "timeout" not in result.stdout, result.stdout + result.stderr
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    if sys.platform == "linux" and env.get("QT_QPA_PLATFORM"):
        assert lines["platform"] == env["QT_QPA_PLATFORM"], result.stdout
    return lines, result.stderr


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
                    "Rectangle { color: 'black'; SkiaSubtitleItem { motionProbeEnabled: true; anchors.fill: parent } }"))
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


def vulkan_validation_environment(log):
    env = {
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
    return env


def assert_vulkan_validation_clean(log):
    text = log.read_text(encoding="utf-8", errors="replace")
    # Positive control: the layer really ran, with synchronization checks enabled.
    current = "CURRENT-VALIDATION-ENABLED" in text and "Synchronization" in text
    older = ("Khronos Validation Layer Active" in text
             and "VK_VALIDATION_FEATURE_ENABLE_SYNCHRONIZATION_VALIDATION_EXT" in text)
    assert current or older, text[:2000]
    reported = re.findall(r"^Validation (?:Error|Warning): \[ ([^\]]+?) \]", text, re.M)
    assert not [r for r in reported if r.startswith("SYNC-")], text[:4000]
    assert set(reported) <= QT_OWNED_VALIDATION_IDS, text[:4000]


def test_texture_handoff_is_clean_under_vulkan_validation(qml_dir, tmp_path):
    if sys.platform == "darwin":
        pytest.skip("Vulkan validation; Metal uses its own API validation")
    if not os.environ.get("KROK_VULKAN_VALIDATION"):
        pytest.skip("Set KROK_VULKAN_VALIDATION=1 with the Khronos validation layer installed")
    log = tmp_path / "validation.log"
    env = desktop_environment() | {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    } | vulkan_validation_environment(log)
    result = subprocess.run([sys.executable, "-c", HANDOFF, str(qml_dir)], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stderr
    frames = re.search(r"^frames (\d+)$", result.stdout, re.M)
    assert frames and int(frames[1]) > 30, result.stdout
    assert_vulkan_validation_clean(log)


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
    # The failure must come from the validation layer's MTLDebug* proxies, not from checks the
    # Metal framework always performs.
    assert "MTLDebug" in checked.stdout + checked.stderr, checked.stdout + checked.stderr
    # The real preview under that same environment: any Metal API error would abort it.
    lines, stderr = show_item(qml_dir, "required", layer)
    assert lines["backend"] == "Metal" and lines["accepted"] == "True", stderr
    assert float(lines["coloured"]) > 0.05 and float(lines["moved"]) > 0.01, stderr


# P03.6: the direct preview is one opaque Quick canvas (letterbox, background, Skia subtitles)
# inside the widget hierarchy. A transparent stack-on-top overlay over the old QGraphicsView was
# rejected: it either hides the background (normal stacking) or covers overlapping widgets such
# as the floating player's controls (WA_AlwaysStackOnTop, which ignores widget masks).
QUICK_CANVAS = r"""
import shutil, subprocess, sys
from pathlib import Path
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.frontend.preview.playback import PlaybackController
from krok_helper.subtitle_render.frontend.preview.preview_view import TransportBar
from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import select_quick_graphics_api
from krok_helper.subtitle_render.frontend.preview.skia_quick_canvas import SkiaQuickCanvas
from krok_helper.subtitle_render.frontend.preview.skia_quick_clock import SkiaQuickClock
from krok_helper.subtitle_render.frontend.preview.skia_quick_scene import SkiaQuickScene
select_quick_graphics_api()
from PyQt6.QtCore import QPoint, QRect, QTimer
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QWidget
app = QApplication(sys.argv)
print("platform", app.platformName())
qml_dir, work, case = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
W, H = (640, 240) if case == "crop" else (640, 360)
background = QImage(640, 360, QImage.Format.Format_RGB32)
painter = QPainter(background)
gradient = QLinearGradient(0, 0, 640, 0)
gradient.setColorAt(0, QColor(0, 160, 60))
gradient.setColorAt(1, QColor(160, 0, 160))
painter.fillRect(background.rect(), gradient)
painter.end()
background.save(str(work / "bg.png"))
source = BackgroundSource(kind='image', path=str(work / 'bg.png'))
if case == 'contain':
    assert background.copy(0, 0, 480, 360).save(str(work / 'narrow.png'))
    source = BackgroundSource(kind='image', path=str(work / 'narrow.png'), image_fit='contain')
elif case == 'solid':
    source = BackgroundSource(kind='solid', color='#204080')
elif case == 'sequence':
    for index, color in enumerate(('#203060', '#1C6834')):
        background.fill(QColor(color))
        assert background.save(str(work / f'frame_{index:02}.png'))
    source = BackgroundSource(kind='image_sequence', path=str(work / 'frame_%02d.png'), source_fps=1)
if case == "video":
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    if not ffmpeg:
        print("skip no-ffmpeg"); sys.exit(0)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=640x480:rate=30:duration=3", "-pix_fmt", "yuv420p", str(work / "v.mp4")], check=True)
    source = BackgroundSource(kind='video', path=str(work / 'v.mp4'))
host = QWidget(); host.resize(W, H)
canvas = SkiaQuickCanvas(Path(qml_dir), host)
canvas.setGeometry(0, 0, W, H)
canvas.set_output_size(640, 360)
canvas.set_background_source(source)
binary = Path(qml_dir).parent / ('krok_skia_subtitle_renderer.exe' if sys.platform == 'win32' else 'krok_skia_subtitle_renderer')
track = TimingTrack(lines=[TimingLine(chars=[TimingChar('█', i*100) for i in range(8)], end_ms=1000,
    display_start_override_ms=0, display_end_override_ms=4000)])
color = KaraokeColorState(text=PaintFill(color='#FF8822'))
style = Style(font_family='DejaVu Sans' if sys.platform == 'linux' else 'Arial', font_size_px=160,
    karaoke_colors=KaraokeColors(before=color, after=color), decoration_kind='none',
    stroke_width_px=0, stroke2_enabled=False, dual_line_layout=False, karaoke_anim='none', entry_anim='none',
    exit_anim='none', line_horizontal_layout='center', line_y_position='center', smart_horizontal='none')
blocker = QFrame(host)                      # stands in for overlaid controls (transport bar)
blocker.setStyleSheet("background: #ff0000;")
band = QRect(0, int(H * 0.45), W, 12)
blocker.setGeometry(band); blocker.raise_()
player = None
clock = SkiaQuickClock(canvas, fps=30)
scene = SkiaQuickScene(canvas)
errors = []
scene.preparation_failed.connect(errors.append)
transport = TransportBar(host)
transport.hide()
transport.set_duration(4000)
transport.set_preview_fps(30)
transport_times = []
transport.timeChanged.connect(transport_times.append)
transport.timeChanged.connect(clock.set_time)
transport.seekRequested.connect(clock.seek)
transport.playbackStateChanged.connect(clock.set_playing)
receipts = []
canvas.frame_submitted.connect(lambda revision, generation, t: receipts.append((revision, generation, t)))
if case == 'video':
    player = PlaybackController(host)
    player.set_video_output(canvas.video_output)
    player.set_media(work / 'v.mp4')
    transport.attach_playback_controller(player)

def prepared(revision, accepted, error):
    assert revision == 2 and accepted, error
    if player:
        transport.play()
    else:
        transport.set_time(100)
        transport.set_time(1300)
    QTimer.singleShot(1300, report)

def report():
    image = host.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    sx, sy = image.width() / W, image.height() / H
    px = lambda x, y: QColor(image.pixel(int(x * sx), int(y * sy)))
    area = [px(x, y) for x in range(0, W, 4) for y in range(H // 4, 3*H // 4, 4)]
    reds = [px(x, band.center().y()) for x in range(0, W, 2)]
    print("accepted", canvas.renderer.property("graphicsBackendAccepted"))
    print("orange", sum(1 for c in area if c.red() > 245 and 125 < c.green() < 145 and 25 < c.blue() < 45))
    print("blocker_red", sum(1 for c in reds if c.red() > 200 and c.green() < 60 and c.blue() < 60), len(reds))
    print("corner", px(3, 3).name())
    hit = QApplication.widgetAt(host.mapToGlobal(QPoint(W // 2, int(H * 0.25))))
    print("mouse_reaches_canvas", hit is canvas)
    if player:
        print("video_playing", player.is_playing() and player.position() > 0)
        assert 3 < len(receipts) <= 44, receipts
        emitted = iter([0, *transport_times])
        for revision, generation, t in receipts:
            assert revision == 2 and generation == 1 and t in emitted, (receipts, transport_times)
        transport.pause()
        transport.set_time(500)
        QTimer.singleShot(200, verify_paused_seek)
    else:
        assert receipts == [(2, 3, 1300)], receipts
        if case == 'sequence':
            scene.clear()
            transport.set_time(100)
            QTimer.singleShot(450, verify_background_only)
        else:
            app.quit()

def verify_background_only():
    image = host.grab().toImage()
    assert QColor(image.pixel(3, 3)).name() == '#203060'
    assert receipts == [(2, 3, 1300)], receipts
    app.quit()

def verify_paused_seek():
    assert not player.is_playing()
    assert receipts[-1] == (2, 2, 500), receipts
    assert sum(generation == 2 for _, generation, _ in receipts) == 1, receipts
    if player:
        player.shutdown()
    app.quit()

QTimer.singleShot(18000, lambda: (print("timeout", errors), app.quit()))
canvas.scene_prepared.connect(prepared)
assert scene.set_scene(track, style, executable=binary, width=640, height=360, fps=60) == 1
assert scene.set_scene(track, style, executable=binary, width=640, height=360, fps=60) == 2
host.show()
sys.exit(app.exec())
"""


def run_canvas(qml_dir, tmp_path, case):
    env = desktop_environment() | {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "QT_FORCE_STDERR_LOGGING": "1",
        "KROK_SUBTITLE_RENDER_BACKEND": "skia"}
    result = subprocess.run([sys.executable, "-c", QUICK_CANVAS, str(qml_dir), str(tmp_path), case], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    if "skip no-ffmpeg" in result.stdout:
        pytest.skip("ffmpeg not on PATH to generate the test video")
    assert "timeout" not in result.stdout, result.stdout + result.stderr
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    if sys.platform == "linux" and env.get("QT_QPA_PLATFORM"):
        assert lines["platform"] == env["QT_QPA_PLATFORM"], result.stdout
    return lines


@pytest.mark.parametrize("case", ["image", "crop", "contain", "solid", "sequence", "video"])
def test_quick_canvas_composites_background_and_subtitles_under_widgets(qml_dir, tmp_path, case):
    lines = run_canvas(qml_dir, tmp_path, case)
    assert lines["accepted"] == "True"
    assert int(lines["orange"]) > 20, lines              # Skia subtitles drawn over the background
    red, total = map(int, lines["blocker_red"].split())
    assert red == total, lines                            # overlaid widgets stay above the canvas
    assert lines["mouse_reaches_canvas"] == "False"       # input passes through to the panel
    if case in {"video", "contain"}:
        assert lines["corner"] == "#000000", lines        # 4:3 in 16:9: contain + black bars
    if case == "video":
        assert lines["video_playing"] == "True", lines
    elif case == "sequence":
        assert lines["corner"] == "#1c6834", lines
    elif case == "solid":
        assert lines["corner"] == "#204080", lines
    elif case != "contain":
        assert lines["corner"] != "#000000", lines        # background, not a black hole, shows


FLOATING_WINDOW = r"""
import sys
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.domain.roll import RollSegmentConfig, RollTimelinePlan
from krok_helper.subtitle_render.frontend.preview.playback import PlaybackController
from krok_helper.subtitle_render.frontend.preview.player_window import PreviewPlayerWindow
from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import select_quick_graphics_api

select_quick_graphics_api()
app = QApplication([])
owner = QWidget()
owner.resize(900, 600)
owner.show()
window = PreviewPlayerWindow(owner)
window.resize(640, 450)
panel, transport = window.preview_panel, window.transport_bar
player = PlaybackController(window)
assert panel.use_external_player(player)
transport.attach_playback_controller(player)
transport.timeChanged.connect(panel.set_time)
transport.seekRequested.connect(panel.seek)
transport.playbackStateChanged.connect(panel.set_playing)
frames, errors = [], []
panel.canvas.framePainted.connect(lambda: frames.append(transport.current_time_ms))
panel.gpuFallback.connect(errors.append)

def wait_for(predicate):
    for _ in range(160):
        if predicate():
            break
        QTest.qWait(100)
    else:
        raise AssertionError((frames, errors, panel.canvas.render_backend_label()))

assert panel.uses_skia_quick_preview
panel.set_output_size(640, 360)
panel.set_background_source(BackgroundSource(kind='solid', color='#204080'))
transport.set_duration(2000)
style = Style(font_family='DejaVu Sans' if sys.platform == 'linux' else 'Arial', font_size_px=160,
    karaoke_colors=KaraokeColors(before=KaraokeColorState(text=PaintFill(color='#FF1010')),
        after=KaraokeColorState(text=PaintFill(color='#10FF10'))), decoration_kind='none',
    stroke_width_px=0, stroke2_enabled=False, dual_line_layout=False, karaoke_anim='none', entry_anim='none',
    exit_anim='none', line_horizontal_layout='center', line_y_position='center', smart_horizontal='none')
panel.set_style(style)
panel.set_track(TimingTrack(lines=[TimingLine(chars=[TimingChar('A', 400), TimingChar('B', 700)],
    end_ms=1000, display_start_override_ms=0, display_end_override_ms=2000)]))
window.show()
if '--require-hardware' in sys.argv:
    wait_for(lambda: errors or panel.skia_quick_target_established)
    if panel.skia_quick_target_established:
        print('real hardware available', flush=True)
    else:
        wait_for(lambda: panel.current_subtitle_frame() is not None)
        assert not panel.uses_skia_quick_preview
        assert panel.canvas._async_renderer.current_backend_mode == 'cpu'
        assert 'Skia Raster CPU' in panel.canvas.render_backend_label()
        assert errors[0].startswith('Skia 直画设备不可用'), errors
        print('Skia Raster fallback verified', flush=True)
    window.close()
    owner.close()
    player.shutdown()
    sys.exit(0)
wait_for(lambda: panel.skia_quick_target_established and frames)
assert panel.current_subtitle_frame() is None
transport.set_time(1350)
wait_for(lambda: frames[-1] == 1350)
image = panel.canvas.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
pixels = image.constBits().asstring(image.sizeInBytes())
assert sum(r < 60 and g > 210 for r, g in zip(pixels[0::4], pixels[1::4])) > 50
for index, size in enumerate([(720, 520), (860, 580), (640, 450)]):
    window.hide()
    window.resize(*size)
    frame_count = len(frames)
    time_ms = 100 if index % 2 == 0 else 1350
    transport.set_time(time_ms)
    QTest.qWait(200)
    window.show()
    wait_for(lambda: len(frames) > frame_count and frames[-1] == time_ms)
panel.set_track(None)
QTest.qWait(250)
cleared = panel.canvas.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
pixels = cleared.constBits().asstring(cleared.sizeInBytes())
assert not any(r < 60 and g > 210 for r, g in zip(pixels[0::4], pixels[1::4]))
assert not any(r > 210 and g < 60 for r, g in zip(pixels[0::4], pixels[1::4]))
assert not errors, errors
assert panel.set_gpu_preview_enabled(False) and not panel.uses_skia_quick_preview
assert panel.canvas._async_renderer.selection.target == 'raster'
assert panel.set_gpu_preview_enabled(True) and panel.uses_skia_quick_preview
roll = RollTimelinePlan(pre=RollSegmentConfig(), post=RollSegmentConfig(), pre_ms=600)
panel.set_roll_plan(roll)
assert not panel.uses_skia_quick_preview and panel.canvas._async_renderer.selection.target == 'gpu'
panel.set_roll_plan(None)
assert panel.uses_skia_quick_preview
window.close()
owner.close()
player.shutdown()
print('floating preview verified', flush=True)
"""


def run_floating_preview(qml_dir, *, require_hardware=False):
    binary = qml_dir.parent / ('krok_skia_subtitle_renderer.exe' if sys.platform == 'win32' else 'krok_skia_subtitle_renderer')
    env = desktop_environment() | {
        'KROK_SUBTITLE_RENDER_BACKEND': 'skia', 'KROK_SUBTITLE_SKIA_TARGET': 'gpu',
        'KROK_SUBTITLE_SKIA_DIRECT_PREVIEW': '1', 'KROK_SUBTITLE_GPU_PREVIEW': '1',
        'KROK_SUBTITLE_SKIA_REQUIRE_HARDWARE': '1' if require_hardware or sys.platform == 'win32' else '0',
        'KROK_SUBTITLE_SKIA_FALLBACK': 'raster',
        'KROK_SUBTITLE_SKIA_RENDERER': str(binary), 'QT_FORCE_STDERR_LOGGING': '1',
    }
    args = ['--require-hardware'] if require_hardware else []
    result = subprocess.run([sys.executable, '-c', FLOATING_WINDOW, *args], env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_floating_preview_selects_opted_in_skia_quick_path(qml_dir):
    assert 'floating preview verified' in run_floating_preview(qml_dir)


@pytest.mark.skipif(sys.platform != 'linux', reason='Software Vulkan fallback on Linux')
def test_floating_preview_uses_skia_raster_when_hardware_required(qml_dir):
    output = run_floating_preview(qml_dir, require_hardware=True)
    if 'real hardware available' in output:
        pytest.skip('Needs a software Vulkan device')
    assert 'Skia Raster fallback verified' in output, output
