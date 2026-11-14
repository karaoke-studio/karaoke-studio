"""Exercise P03 preview resource lifetimes in an isolated, real desktop process."""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qml_dir", type=Path)
    parser.add_argument("--host", choices=("view", "widget"), required=True)
    parser.add_argument("--duration", type=float, default=12)
    parser.add_argument("--no-readback", action="store_true",
                        help="Validate the normal frame graph without test-only Qt screenshot passes")
    options = parser.parse_args(argv)
    faulthandler.enable()
    faulthandler.dump_traceback_later(30)

    from PyQt6.QtCore import QCoreApplication, QEvent, QMetaObject, Qt, QUrl
    from PyQt6.QtGui import QImage
    from PyQt6.QtQuick import QQuickItem, QQuickView
    from PyQt6.QtWidgets import QApplication, QWidget
    import numpy as np
    from krok_helper.subtitle_render.frontend.preview.quick_graphics_api import select_quick_graphics_api

    select_quick_graphics_api()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    extension = {"win32": "subtitle_skia_preview_test_control.dll",
                 "darwin": "libsubtitle_skia_preview_test_control.dylib"}.get(
                     sys.platform, "libsubtitle_skia_preview_test_control.so")
    control = ctypes.CDLL(str(options.qml_dir.parent / extension))
    control.krokSetPreviewTestScreenScale.argtypes = [ctypes.c_double]
    control.krokSetPreviewTestScreenScale.restype = ctypes.c_bool
    qml = ("import QtQuick\nimport Krok.SubtitleSkia\n"
           "Rectangle { color: 'black'; clip: true; SkiaSubtitleItem { "
           "objectName: 'subtitle'; anchors.fill: parent } }")
    counters = {"initialized": 0, "invalidated": 0, "destroyed": 0}
    snapshots = []
    started = time.monotonic()

    def tick():
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)

    def wait_for(predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise RuntimeError(message)
            tick()

    def settle(seconds=0.12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            tick()

    def count(name):
        counters[name] += 1

    for generation in range(2):
        host = QWidget()
        host.resize(320, 180)
        if options.host == "widget":
            # Importing/creating QQuickWidget affects the render loop, so the view control
            # must run in a process which has never imported it.
            from PyQt6.QtQuickWidgets import QQuickWidget
            canvas = QQuickWidget(host)
            canvas.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
            window = canvas.quickWindow()
        else:
            canvas = QQuickView()
            canvas.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
            window = canvas
        canvas.engine().addImportPath(str(options.qml_dir))
        window.setPersistentSceneGraph(False)
        # Match QQuickWidget: keep Qt's device across hide/show, invalidate the scene graph
        # and its borrowed Skia resources. Closing/recreating the host below releases the
        # device as well. Forcing device purge on a surviving QQuickView produced
        # swapchain reacquisition synchronization errors; that mode is not accepted.
        window.setPersistentGraphics(True)
        # Never enter Python from the render thread while the GUI thread blocks in hide().
        window.sceneGraphInitialized.connect(lambda: count("initialized"), Qt.ConnectionType.QueuedConnection)
        window.sceneGraphInvalidated.connect(lambda: count("invalidated"), Qt.ConnectionType.QueuedConnection)
        source = QUrl("data:text/plain," + qml.replace("\n", "%0A"))
        canvas.setSource(source)
        if options.host == "view":
            container = QWidget.createWindowContainer(canvas, host)
        else:
            container = canvas
        container.setGeometry(host.rect())
        host.destroyed.connect(lambda: count("destroyed"))
        item = canvas.rootObject().findChild(QQuickItem, "subtitle")
        if item is None:
            raise RuntimeError("QML subtitle item was not created")
        QMetaObject.invokeMethod(host, "show", Qt.ConnectionType.QueuedConnection)

        def capture(stage):
            print(f"lifecycle-stage {generation} {stage}", file=sys.stderr, flush=True)
            wait_for(lambda: bool(item.property("graphicsBackendAccepted")), "GPU backend not accepted")
            settle()
            if options.no_readback:
                snapshots.append({"stage": stage, "generation": generation,
                                  "dpr": window.effectiveDevicePixelRatio()})
                return None
            if options.host == "widget":
                image = canvas.grabFramebuffer()
            else:
                # Read the item subtree, not the presented swapchain image. Qt's
                # grabWindow() adds swapchain readback transitions unrelated to Skia's
                # offscreen texture handoff, obscuring synchronization validation.
                grab = canvas.rootObject().grabToImage()
                wait_for(lambda: not grab.image().isNull(), "Item readback did not complete")
                image = grab.image()
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            if image.isNull():
                raise RuntimeError(f"Empty preview after {stage}")
            data = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8)
            pixels = data.reshape(image.height(), image.bytesPerLine() // 4, 4)[:, :image.width()]
            coloured = float(np.any(pixels[:, :, :3] > 24, axis=2).mean())
            if coloured < 0.03:
                raise RuntimeError(f"Skia output disappeared after {stage}: {coloured}")
            dpr = window.effectiveDevicePixelRatio()
            if abs(image.width() - container.width() * dpr) > 2:
                raise RuntimeError(f"Wrong physical texture size after {stage}: {image.width()}, DPR {dpr}")
            snapshots.append({"stage": stage, "generation": generation, "dpr": dpr,
                              "width": image.width(), "height": image.height(), "coloured": coloured})
            return pixels.copy()

        first = capture("shown")
        settle(0.35)
        second = capture("animated")
        if not options.no_readback and float(np.any(first != second, axis=2).mean()) < 0.005:
            raise RuntimeError("Skia preview stopped advancing")
        for cycle in range(3):
            host.resize(440 + 20 * cycle, 240 + 10 * cycle)
            container.setGeometry(host.rect())
            capture("resized")
            # Run the native visibility transition from Qt's event loop. Calling hide()
            # directly from a Python frame can hold the GIL while the GUI thread waits
            # for the render thread's cleanup, which may need to reenter the binding.
            QMetaObject.invokeMethod(host, "hide", Qt.ConnectionType.QueuedConnection)
            settle()
            if options.host == "view":
                QMetaObject.invokeMethod(window, "releaseResources", Qt.ConnectionType.QueuedConnection)
                settle()
                # Qt's basic loop only purges caches in releaseResources(), unlike its
                # threaded loop. Replacing the root explicitly covers item/node teardown
                # on both; full scene graph invalidation is also checked on window close.
                canvas.setSource(QUrl())
                canvas.setSource(source)
                item = canvas.rootObject().findChild(QQuickItem, "subtitle")
            QMetaObject.invokeMethod(host, "show", Qt.ConnectionType.QueuedConnection)
            capture("shown-after-invalidation")
        for scale in (2.0, 1.5, 1.0):
            print(f"changing-screen-scale {scale}", file=sys.stderr, flush=True)
            if not control.krokSetPreviewTestScreenScale(scale):
                raise RuntimeError("Process-local screen scaling failed")
            host.resize(host.width() + 1, host.height() + 1)
            container.setGeometry(host.rect())
            capture(f"screen-scale-{scale}")
        dprs = {s["dpr"] for s in snapshots if s["generation"] == generation}
        if len(dprs) < 3:
            raise RuntimeError(f"DPR did not change at runtime: {dprs}")
        QMetaObject.invokeMethod(host, "close", Qt.ConnectionType.QueuedConnection)
        host.deleteLater()
        wait_for(lambda: counters["destroyed"] > generation, "Widget host was not destroyed")
        settle()

    # Keep processing after repeated teardown as well: delayed deletes and queued reports
    # must not access a renderer or GPU device which has already gone away.
    while time.monotonic() - started < options.duration:
        tick()
    print("LIFECYCLE_RESULT " + json.dumps({
        "host": options.host, "platform": app.platformName(), "counters": counters,
        "readback": not options.no_readback,
        "snapshots": snapshots, "elapsed_seconds": time.monotonic() - started}))
    faulthandler.cancel_dump_traceback_later()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
