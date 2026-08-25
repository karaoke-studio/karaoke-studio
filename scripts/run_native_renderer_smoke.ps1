param(
    [string]$QtRoot = "",
    [string]$OutputPath = "$env:TEMP\krok-native-renderer-smoke.png",
    [switch]$InstallQtIfMissing,
    [switch]$RequireHardware
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

# Native and PyQt must use the same Qt minor version or glyph metrics and
# antialiasing can silently diverge.
$qtVersion = (python -c "from PyQt6.QtCore import qVersion; print(qVersion())").Trim()
if (-not $qtVersion) {
    throw "Could not read the PyQt6 Qt version."
}
if (-not $QtRoot) {
    $QtRoot = "$env:LOCALAPPDATA\krok-helper\qt\$qtVersion\msvc2022_64"
}
if (-not (Test-Path $QtRoot)) {
    if (-not $InstallQtIfMissing) {
        throw "Qt not found at '$QtRoot'. Re-run with -InstallQtIfMissing."
    }
    # aqtinstall 3.3.0 from PyPI cannot read the Qt 6.11 repository layout.
    # Pin the exact upstream commit that is verified with Qt 6.11.0 instead of
    # following a mutable development branch.
    $aqtCommit = "bbfb1f7c0590b9eb3fa91356e75bb64fb15d3643"
    $aqtRequirement = "aqtinstall @ git+https://github.com/miurahr/aqtinstall.git@$aqtCommit"
    python -m pip install --user cmake ninja $aqtRequirement
    python -m aqt install-qt windows desktop $qtVersion win64_msvc2022_64 `
        --archives qtbase --outputdir "$env:LOCALAPPDATA\krok-helper\qt"
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    throw "vswhere.exe not found. Install Visual Studio Build Tools with MSVC x64 tools."
}
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) {
    throw "MSVC x64 tools not found."
}

$cmake = Join-Path $env:APPDATA "Python\Python314\Scripts\cmake.exe"
if (-not (Test-Path $cmake)) {
    $cmakeCommand = Get-Command cmake.exe -ErrorAction SilentlyContinue
    if ($cmakeCommand) {
        $cmake = $cmakeCommand.Source
    } else {
        python -m pip install --user cmake ninja
    }
}

$configureAndBuild = "`"$vs\Common7\Tools\VsDevCmd.bat`" -arch=amd64 && " +
    "`"$cmake`" -S native\subtitle_renderer -B build\native-renderer -G Ninja " +
    "-DCMAKE_PREFIX_PATH=`"$QtRoot`" -DQt6_DIR=`"$QtRoot\lib\cmake\Qt6`" " +
    "-DQt6Core_DIR=`"$QtRoot\lib\cmake\Qt6Core`" " +
    "-DQt6Gui_DIR=`"$QtRoot\lib\cmake\Qt6Gui`" " +
    "-DQt6Widgets_DIR=`"$QtRoot\lib\cmake\Qt6Widgets`" " +
    "-DCMAKE_BUILD_TYPE=Release " +
    "-DKROK_EXPECTED_QT_VERSION=`"$qtVersion`" && " +
    "`"$cmake`" --build build\native-renderer --config Release"
cmd /c $configureAndBuild
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:QT_QPA_PLATFORM = "offscreen"
$env:KROK_SUBTITLE_NATIVE_RENDERER = (Resolve-Path ".\build\native-renderer\krok_subtitle_renderer.exe").Path
$env:KROK_NATIVE_SMOKE_OUTPUT = $OutputPath
$env:KROK_GPU_REQUIRE_HARDWARE = if ($RequireHardware) { "1" } else { "0" }

@'
import os
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, SharedFrameRingReader


def pixel(slot, x, y):
    offset = y * slot.stride + x * 4
    return tuple(slot.payload[offset : offset + 4])


track = TimingTrack(
    lines=[TimingLine(chars=[TimingChar("GPU", 0), TimingChar("探针", 500)], end_ms=1000)]
)
style = Style(font_size_px=48, line_lead_in_ms=0)
output = Path(os.environ["KROK_NATIVE_SMOKE_OUTPUT"])
app = QApplication.instance() or QApplication([])

with NativeRendererProcess(response_timeout_s=15.0) as renderer:
    configured = renderer.configure(track, style, width=640, height=360, fps=60)
    frame = renderer.render_frame_png(500, output)
    assert configured["ok"] and frame["ok"] and output.is_file()

    hardware = renderer.backend_info()
    if os.environ.get("KROK_GPU_REQUIRE_HARDWARE") == "1":
        assert hardware["available"] and hardware["hardware"] and not hardware["warp"], hardware
    print(hardware)

    for force_warp in (False, True):
        info = renderer.backend_info(force_warp=force_warp)
        if not info["available"]:
            if force_warp:
                raise AssertionError(info)
            print({"hardware_probe_skipped": info.get("error")})
            continue
        event = renderer.render_probe(
            width=64,
            height=48,
            force_warp=force_warp,
            draw_glyph=True,
            rgba=(51, 102, 204, 128),
        )
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)
        assert pixel(slot, 0, 0) == (0, 0, 0, 0)
        sample = pixel(slot, 16, 24)
        assert all(abs(a - b) <= 1 for a, b in zip(sample, (51, 102, 204, 128))), sample
        print(event)

print(output)
'@ | python -
