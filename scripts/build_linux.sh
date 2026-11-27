#!/usr/bin/env bash
# Build the Linux x86_64 package (4.5.0+): PyInstaller onedir + Skia sidecar
# + GUI Updater, packed as KaraokeStudio-linux.tar.xz (recommended for manual
# extraction, preserving POSIX modes/symlinks across desktop archive managers)
# and KaraokeStudio-linux.zip (used by the in-app updater).
#
# Build on the oldest supported glibc (CI: ubuntu-22.04); the package runs on
# that distribution or newer. Requirements on the build machine:
#   - Python 3.14 with pip, clang 21.1.8 (skia.lock.json), cmake/ninja
#   - Qt 6.11.0 gcc_64 SDK with qtbase + qtdeclarative (SKIA_QT_ROOT)
#   - libportaudio2 (bundled for users without it)
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_NAME="Lin-K Lyrics"
DIST_PATH="$PROJECT_ROOT/dist/linux"
WORK_PATH="$PROJECT_ROOT/build/pyinstaller-linux"
SPEC_PATH="$PROJECT_ROOT/build/spec-linux"
APP_DIST="$DIST_PATH/$APP_NAME"
APP_INTERNAL="$APP_DIST/_internal"
ASSET_NAME="KaraokeStudio-linux.zip"
TAR_ASSET_NAME="KaraokeStudio-linux.tar.xz"
SUG_SRC="$PROJECT_ROOT/krok_helper/lyrics_timing/src"
SUG_PACKAGE="$SUG_SRC/strange_uta_game"
PYQT6_BINDING_VERSION="6.11.0"
PYQT6_QT_VERSION="6.11.0"
SKIA_QT_ROOT="${SKIA_QT_ROOT:-$HOME/qt-krok/$PYQT6_QT_VERSION/gcc_64}"
SKIA_NATIVE_DIR="$HOME/.cache/krok-helper/skia-deps/native/linux-x64-vulkan"
SKIA_RENDERER_NAME="krok_skia_subtitle_renderer"

if [ "$(uname -m)" != "x86_64" ]; then
  echo "Only x86_64 Linux packages are built (got $(uname -m))."
  exit 1
fi

EXCLUDED_MODULES=(
  krok_helper.updater.macos_controller
  krok_helper.updater.macos_transport
  krok_helper.updater.macos_sparkle
  krok_helper.updater.macos_feed
  krok_helper.updater.macos_metadata
  objc
  Foundation
  AppKit
  winrt
  winrt.windows.globalization
  winrt.windows.foundation
  winrt.windows.foundation.collections
  PySide6
  PyQt5
  PyQt6.Qt3DAnimation
  PyQt6.Qt3DCore
  PyQt6.Qt3DExtras
  PyQt6.Qt3DInput
  PyQt6.Qt3DLogic
  PyQt6.Qt3DRender
  PyQt6.QtCharts
  PyQt6.QtDataVisualization
  PyQt6.QtDesigner
  PyQt6.QtNetworkAuth
  PyQt6.QtPdf
  PyQt6.QtPdfWidgets
  PyQt6.QtQuick3D
  PyQt6.QtQuickControls2
  PyQt6.QtQuickTest
  PyQt6.QtRemoteObjects
  PyQt6.QtScxml
  PyQt6.QtSensors
  PyQt6.QtSerialPort
  PyQt6.QtSql
  PyQt6.QtStateMachine
  PyQt6.QtTest
  PyQt6.QtTextToSpeech
  PyQt6.QtWebEngineQuick
  PyQt6.QtWebSockets
  PyQt6.QtWebView
  sudachidict_core
  sudachidict_full
  scipy
  matplotlib
  pandas
  pytest
  torch
  pymss
  pymss_core
  pip
)

ensure_pkg() {
  local module="$1"
  local pip_name="$2"
  echo "Checking $module..."
  if ! "$PYTHON_BIN" -c "import $module" >/dev/null 2>&1; then
    echo "$module not found, installing $pip_name..."
    "$PYTHON_BIN" -m pip install "$pip_name"
  fi
}

echo "Checking Python..."
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 not found. Please install Python 3.10+ first."
  exit 1
fi

ensure_pkg PyInstaller pyinstaller
ensure_pkg PIL pillow
"$PYTHON_BIN" -m pip install "PyQt6==$PYQT6_BINDING_VERSION" "PyQt6-Qt6==$PYQT6_QT_VERSION" \
  "PyQt6-WebEngine==6.11.0" "PyQt6-WebEngine-Qt6==6.11.0"
ensure_pkg fontTools fonttools
ensure_pkg qfluentwidgets "PyQt6-Fluent-Widgets"
"$PYTHON_BIN" -m pip install "yt-dlp[default]"
"$PYTHON_BIN" scripts/fetch_deno.py
ensure_pkg requests requests
ensure_pkg psutil psutil
ensure_pkg sounddevice sounddevice
ensure_pkg soundfile soundfile
ensure_pkg pedalboard pedalboard
ensure_pkg numpy numpy
ensure_pkg pykakasi pykakasi
ensure_pkg jaconv jaconv
ensure_pkg pyphen pyphen
ensure_pkg sudachipy sudachipy
ensure_pkg sudachidict_small sudachidict_small
ensure_pkg jieba jieba
ensure_pkg pypinyin pypinyin

echo "Checking bundled SUG source path..."
"$PYTHON_BIN" - <<PY
import sys
from pathlib import Path
src = Path(r"$SUG_SRC").resolve()
sys.path.insert(0, str(src))
import strange_uta_game
actual = Path(strange_uta_game.__file__).resolve()
expected = src / "strange_uta_game" / "__init__.py"
print(f"  strange_uta_game: {actual}")
raise SystemExit(0 if actual == expected else f"Expected {expected}, got {actual}")
PY

# sounddevice finds PortAudio through the system; the package ships a copy for
# users who have not installed libportaudio2 (see linux_portaudio_rthook.py).
PORTAUDIO_LIB="${PORTAUDIO_LIB:-$(/sbin/ldconfig -p | awk '/libportaudio\.so\.2 \(.*x86-64/ {print $NF; exit}')}"
if [ -z "$PORTAUDIO_LIB" ] || [ ! -f "$PORTAUDIO_LIB" ]; then
  echo "libportaudio.so.2 not found. Install libportaudio2 or set PORTAUDIO_LIB."
  exit 1
fi
PORTAUDIO_LIB="$(readlink -f "$PORTAUDIO_LIB")"
echo "  PortAudio: $PORTAUDIO_LIB"
# Bundle it under its soname, the name the runtime hook looks for.
PORTAUDIO_STAGE="$PROJECT_ROOT/build/vendor/portaudio"
mkdir -p "$PORTAUDIO_STAGE"
cp -f "$PORTAUDIO_LIB" "$PORTAUDIO_STAGE/libportaudio.so.2"
PORTAUDIO_LIB="$PORTAUDIO_STAGE/libportaudio.so.2"

if [ "${SKIP_SKIA_BUILD:-}" = "1" ]; then
  # Local use: the sidecar in $SKIA_NATIVE_DIR was just built from this tree.
  echo "Using the existing Skia subtitle renderer in $SKIA_NATIVE_DIR"
else
  echo "Building Skia subtitle renderer..."
  if [ ! -f "$SKIA_QT_ROOT/lib/cmake/Qt6Quick/Qt6QuickConfig.cmake" ]; then
    echo "Qt $PYQT6_QT_VERSION SDK not found at $SKIA_QT_ROOT (set SKIA_QT_ROOT)."
    exit 1
  fi
  "$PYTHON_BIN" scripts/build_skia_renderer.py --platform linux --arch x64 --gpu-api vulkan --qt-root "$SKIA_QT_ROOT"
fi

echo "Building GUI Updater..."
"$PYTHON_BIN" krok_helper/updater_app/build_updater.py

mkdir -p "$DIST_PATH" "$WORK_PATH" "$SPEC_PATH"
rm -rf "$APP_DIST"

PYINSTALLER_ARGS=(
  --noconfirm
  --clean
  --windowed
  --onedir
  --name "$APP_NAME"
  --distpath "$DIST_PATH"
  --workpath "$WORK_PATH"
  --specpath "$SPEC_PATH"
  --paths "$SUG_SRC"
  --add-data "$PROJECT_ROOT/krok_helper/assets:krok_helper/assets"
  --add-data "$SUG_PACKAGE/config:strange_uta_game/config"
  --add-data "$SUG_PACKAGE/resource:strange_uta_game/resource"
  --collect-all qfluentwidgets
  --collect-all yt_dlp
  --collect-all yt_dlp_ejs
  --add-binary "$PROJECT_ROOT/build/vendor/deno/deno:tools/deno"
  --add-data "$PROJECT_ROOT/build/vendor/deno/LICENSE:tools/deno"
  --add-binary "$PORTAUDIO_LIB:."
  --runtime-hook "$PROJECT_ROOT/scripts/linux_portaudio_rthook.py"
  --collect-all sounddevice
  --collect-all soundfile
  --collect-all pedalboard
  --collect-all pykakasi
  --collect-all sudachipy
  --collect-all pyphen
  --collect-all jieba
  --collect-all pypinyin
  --collect-data sudachidict_small
  --collect-binaries soundfile
  --collect-submodules strange_uta_game
  --hidden-import sounddevice
  --hidden-import soundfile
  --hidden-import pedalboard
  --hidden-import pedalboard.io
  --hidden-import pedalboard.io.AudioFile
  --hidden-import pedalboard.io.StreamResampler
  --hidden-import pedalboard.time_stretch
  --hidden-import numpy
  --hidden-import pykakasi
  --hidden-import pykakasi.kakasi
  --hidden-import jaconv
  --hidden-import sudachipy
  --hidden-import sudachidict_small
  --hidden-import PyQt6.sip
  --hidden-import PyQt6.QtMultimedia
  --hidden-import PyQt6.QtMultimediaWidgets
  --hidden-import PyQt6.QtWebEngineCore
  --hidden-import PyQt6.QtWebEngineWidgets
  --hidden-import encodings.idna
  --hidden-import colorsys
  --hidden-import krok_helper.updater.posix_zip
)
for module in "${EXCLUDED_MODULES[@]}"; do
  PYINSTALLER_ARGS+=(--exclude-module "$module")
done

echo "Building Linux package..."
"$PYTHON_BIN" -m PyInstaller "${PYINSTALLER_ARGS[@]}" app.py

# The embedded AI timing worker runs SUG sources in an external interpreter
# (runpy), so the bundle needs the real .py tree, as on macOS.
echo "Copying SUG source tree for the AI timing worker..."
SUG_DST="$APP_INTERNAL/strange_uta_game"
mkdir -p "$SUG_DST"
(cd "$SUG_PACKAGE" && find . -type d -name __pycache__ -prune -o -type f ! -name '*.pyc' -print0 \
  | while IFS= read -r -d '' file; do
      mkdir -p "$SUG_DST/$(dirname "$file")"
      cp -f "$file" "$SUG_DST/$file"
    done)

echo "Adding Updater and Skia sidecar..."
install -m 0755 "$PROJECT_ROOT/krok_helper/updater_app/dist/Updater" "$APP_DIST/Updater"
install -m 0755 "$SKIA_NATIVE_DIR/$SKIA_RENDERER_NAME" "$APP_DIST/$SKIA_RENDERER_NAME"

echo "Validating Linux package contents..."
REQUIRED_ROOT=("$APP_NAME" "Updater" "$SKIA_RENDERER_NAME")
REQUIRED_INTERNAL=(
  "libportaudio.so.2"
  "krok_helper/assets/logo/logo.jpg"
  "krok_helper/assets/platforms/youtube.svg"
  "jieba/dict.txt"
  "strange_uta_game/config/config.json"
  "strange_uta_game/config/dictionary.json"
  "strange_uta_game/backend/application/ai_timing/worker/client.py"
  "strange_uta_game/resource/sounds/press.wav"
  "tools/deno/deno"
)
missing=0
for rel in "${REQUIRED_ROOT[@]}"; do
  if [ ! -x "$APP_DIST/$rel" ]; then echo "Missing executable: $APP_DIST/$rel"; missing=1; fi
done
for rel in "${REQUIRED_INTERNAL[@]}"; do
  if [ ! -e "$APP_INTERNAL/$rel" ]; then echo "Missing package file: $APP_INTERNAL/$rel"; missing=1; fi
done
for forbidden_dir in torch functorch torchgen pymss pymss_core pip; do
  if find "$APP_INTERNAL" -type d -name "$forbidden_dir" -print -quit | grep -q .; then
    echo "$forbidden_dir must not be bundled in $APP_NAME"; missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  exit 1
fi
echo "Package content validation passed."

# Build machines are usually headless; the smoke checks need no window.
echo "Validating packaged multiprocessing spawn..."
QT_QPA_PLATFORM=offscreen "$APP_DIST/$APP_NAME" --package-spawn-smoke
echo "Validating packaged Skia subtitle renderer..."
QT_QPA_PLATFORM=offscreen "$APP_DIST/$APP_NAME" --package-skia-smoke
echo "Validating packaged PortAudio..."
QT_QPA_PLATFORM=offscreen "$APP_DIST/$APP_NAME" --package-audio-smoke
echo "Packaged smoke checks passed."

echo "Creating $ASSET_NAME and $TAR_ASSET_NAME..."
rm -f "$PROJECT_ROOT/dist/$ASSET_NAME" "$PROJECT_ROOT/dist/$ASSET_NAME.sha256" \
  "$PROJECT_ROOT/dist/$TAR_ASSET_NAME" "$PROJECT_ROOT/dist/$TAR_ASSET_NAME.sha256"
"$PYTHON_BIN" -m krok_helper.updater.posix_zip "$APP_DIST" "$PROJECT_ROOT/dist/$ASSET_NAME" --prefix "$APP_NAME"
# tar natively preserves symlinks and executable bits in desktop archive managers
# (e.g. KDE Dolphin/Ark, whose libzip plugin turns ZIP symlinks into plain files).
XZ_OPT="${XZ_OPT:--T0}" tar -C "$DIST_PATH" -cJf "$PROJECT_ROOT/dist/$TAR_ASSET_NAME" "$APP_NAME"
(cd "$PROJECT_ROOT/dist" && sha256sum "$ASSET_NAME" > "$ASSET_NAME.sha256" && sha256sum "$TAR_ASSET_NAME" > "$TAR_ASSET_NAME.sha256")

echo
echo "Build complete:"
echo "$PROJECT_ROOT/dist/$TAR_ASSET_NAME"
echo "$PROJECT_ROOT/dist/$ASSET_NAME"
