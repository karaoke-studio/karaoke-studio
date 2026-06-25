param(
    [string]$QtRoot = "$env:LOCALAPPDATA\krok-helper\qt\6.10.0\msvc2022_64",
    [string]$Threads = "1,2,4,8",
    [int]$Frames = 240,
    [switch]$NoGlow,
    [switch]$NoRuby,
    [switch]$NoUtopia,
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
        if (-not (Test-Path $cmake)) {
            throw "cmake.exe not found after installation."
        }
    }
}

$configureAndBuild = "`"$vs\Common7\Tools\VsDevCmd.bat`" -arch=amd64 && " +
    "`"$cmake`" -S native\subtitle_renderer_probe -B build\native-probe -G Ninja " +
    "-DCMAKE_PREFIX_PATH=`"$QtRoot`" -DCMAKE_BUILD_TYPE=Release && " +
    "`"$cmake`" --build build\native-probe --config Release"
cmd /c $configureAndBuild
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:PATH = "$QtRoot\bin;$env:PATH"
$args = @("--threads", $Threads, "--frames", [string]$Frames)
if ($NoGlow) { $args += "--no-glow" }
if ($NoRuby) { $args += "--no-ruby" }
if ($NoUtopia) { $args += "--no-utopia" }

& ".\build\native-probe\krok_qpainter_parallel_probe.exe" @args
