param(
    [string]$QtRoot = "",
    [string]$OutputPath = "$env:TEMP\krok-native-renderer-smoke.png",
    [switch]$InstallQtIfMissing
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

# Pin the native Qt to whatever Qt PyQt6 bundles, so the sidecar and the Python
# fallback renderer rasterize identically. This is the single source of truth for
# the version fingerprint asserted in native/subtitle_renderer/CMakeLists.txt.
$qtVersion = (python -c "from PyQt6.QtCore import QT_VERSION_STR; print(QT_VERSION_STR)").Trim()
if (-not $qtVersion) {
    throw "Could not read PyQt6 Qt version (is PyQt6 installed in this interpreter?)."
}
if (-not $QtRoot) {
    $QtRoot = "$env:LOCALAPPDATA\krok-helper\qt\$qtVersion\msvc2022_64"
}

if (-not (Test-Path $QtRoot)) {
    if (-not $InstallQtIfMissing) {
        throw "Qt not found at '$QtRoot'. Re-run with -InstallQtIfMissing or install Qt $qtVersion msvc2022_64 via aqtinstall (must match PyQt6 Qt $qtVersion)."
    }
    python -m pip install --user cmake ninja aqtinstall
    python -m aqt install-qt windows desktop $qtVersion win64_msvc2022_64 --outputdir "$env:LOCALAPPDATA\krok-helper\qt"
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    throw "vswhere.exe not found. Install Visual Studio Build Tools with MSVC x64 tools."
}
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) {
    throw "MSVC x64 tools not found. Install Visual Studio Build Tools with the C++ workload."
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
    "-DCMAKE_PREFIX_PATH=`"$QtRoot`" -DCMAKE_BUILD_TYPE=Release " +
    "-DKROK_EXPECTED_QT_VERSION=`"$qtVersion`" && " +
    "`"$cmake`" --build build\native-renderer --config Release"
cmd /c $configureAndBuild
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:PATH = "$QtRoot\bin;$env:PATH"
$env:KROK_SUBTITLE_NATIVE_RENDERER = (Resolve-Path ".\build\native-renderer\krok_subtitle_renderer.exe").Path
$env:KROK_NATIVE_SMOKE_OUTPUT = $OutputPath

@'
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.engine.painter import (
    _fill_clip_band,
    _horizontal_after_path_clip_rect,
    _layout_line,
    _resolve_display_baselines,
    _resolve_sayatoo_line_layouts,
    _resolve_visible_content,
)
from krok_helper.subtitle_render.models import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native_backend import NativeRendererProcess

track = TimingTrack(
    lines=[
        TimingLine(
            chars=[
                TimingChar("K", 0),
                TimingChar("a", 400),
                TimingChar("r", 800),
                TimingChar("a", 1200),
            ],
            end_ms=1800,
        )
    ],
)
style = Style(
    font_size_px=48,
    ruby_font_size_px=20,
    line_lead_in_ms=0,
    stroke_width_px=10,
    stroke2_width_px=6,
    karaoke_colors=KaraokeColors(
        before=KaraokeColorState(
            text=PaintFill(color="#FFFFFF"),
            stroke=PaintFill(color="#222222"),
            stroke2=PaintFill(color="#202020"),
        ),
        after=KaraokeColorState(
            text=PaintFill(color="#FF5A6F"),
            stroke=PaintFill(color="#222222"),
            stroke2=PaintFill(color="#303030"),
        ),
    ),
)
output = Path(os.environ["KROK_NATIVE_SMOKE_OUTPUT"])
app = QApplication.instance() or QApplication([])
track_t_ms, display_style, display_lines, _signal_lines, _title_opacity = (
    _resolve_visible_content(track, 900, style)
)
baselines = _resolve_display_baselines(360, track, display_lines, display_style)
line_layouts = _resolve_sayatoo_line_layouts(
    640,
    360,
    track,
    display_lines,
    baselines,
    track_t_ms,
    display_style,
)
display_line = display_lines[0]
line_layout = line_layouts[display_line.lane]
py_layout = _layout_line(
    track,
    display_line.line,
    display_style,
    640,
    360,
    baseline_y=line_layout.baseline_y,
    line_x=line_layout.text_x,
    lane=display_line.lane,
)
assert py_layout is not None


def py_clip_right(t_ms):
    band = _fill_clip_band(py_layout.fill_segments, t_ms, py_layout.rtl)
    return py_layout.x0 if band is None else band[1]


def assert_close(actual, expected, label, tolerance=4.0):
    assert abs(float(actual) - float(expected)) <= tolerance, (label, actual, expected)


expected_clip = _horizontal_after_path_clip_rect(
    py_layout.fill_segments,
    py_layout.baseline_y,
    py_layout.metrics,
    900,
    py_layout.rtl,
    style.stroke_width_px,
)
assert expected_clip is not None


with NativeRendererProcess() as renderer:
    print(renderer.configure(track, style, width=640, height=360, fps=60))
    frame0 = renderer.render_frame_png(0, output.with_name(output.stem + "-000.png"))
    frame200 = renderer.render_frame_png(200, output.with_name(output.stem + "-200.png"))
    frame900 = renderer.render_frame_png(900, output)
    frame1800 = renderer.render_frame_png(1800, output.with_name(output.stem + "-1800.png"))
    print(frame900)

    line_x = frame900["line_x"]
    line_end = line_x + frame900["line_width"]
    assert_close(line_x, py_layout.x0, "line_x")
    assert_close(frame900["line_width"], py_layout.total_w, "line_width")
    assert_close(frame900["baseline_y"], py_layout.baseline_y, "baseline_y")
    assert_close(frame900["after_clip_top"], expected_clip.top(), "clip_top")
    assert_close(frame900["after_clip_height"], expected_clip.height(), "clip_height")
    clips = [
        frame0["after_clip_right"],
        frame200["after_clip_right"],
        frame900["after_clip_right"],
        frame1800["after_clip_right"],
    ]
    assert clips[0] == line_x, clips
    assert line_x < clips[1] < clips[2] < clips[3], clips
    assert abs(clips[3] - line_end) < 1.0, (clips[3], line_end)
    assert_close(frame0["after_clip_right"], py_clip_right(0), "clip@0")
    assert_close(frame200["after_clip_right"], py_clip_right(200), "clip@200")
    assert_close(frame900["after_clip_right"], py_clip_right(900), "clip@900")
    assert_close(frame1800["after_clip_right"], py_clip_right(1800), "clip@1800")
print(output)
'@ | python -
