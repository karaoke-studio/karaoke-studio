param(
    [string]$QtRoot = "$env:LOCALAPPDATA\krok-helper\qt\6.10.0\msvc2022_64",
    [string]$OutputPath = "$env:TEMP\krok-native-renderer-smoke.png",
    [switch]$InstallQtIfMissing
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

if (-not (Test-Path $QtRoot)) {
    if (-not $InstallQtIfMissing) {
        throw "Qt not found at '$QtRoot'. Re-run with -InstallQtIfMissing or install Qt 6.10.0 msvc2022_64 via aqtinstall."
    }
    python -m pip install --user cmake ninja aqtinstall
    python -m aqt install-qt windows desktop 6.10.0 win64_msvc2022_64 --outputdir "$env:LOCALAPPDATA\krok-helper\qt"
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
    "-DCMAKE_PREFIX_PATH=`"$QtRoot`" -DCMAKE_BUILD_TYPE=Release && " +
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

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
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
    rubies=[RubyAnnotation(kanji="K", reading="ka", pos_start_ms=0, pos_end_ms=800)],
)
style = Style(font_size_px=48, ruby_font_size_px=20, line_lead_in_ms=0)
output = Path(os.environ["KROK_NATIVE_SMOKE_OUTPUT"])
with NativeRendererProcess() as renderer:
    print(renderer.configure(track, style, width=640, height=360, fps=60))
    print(renderer.render_frame_png(900, output))
print(output)
'@ | python -
