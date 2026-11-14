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
