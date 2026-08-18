@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0\.."

set "PYTHON_BIN=python"
set "BUILD_NAME=LinKLyrics"
set "APP_NAME=Lin-K Lyrics"
rem 改名前的 EXE 名。打包末尾会复制一份同内容副本，供存量客户端的 Updater
rem 校验更新包并在更新后重启（见 docs/auto_update.md §8）。不要删。
set "LEGACY_APP_NAME=Karaoke Studio"
set "DIST_PATH=dist\windows"
set "WORK_PATH=build\pyinstaller-windows"
set "SPEC_PATH=build\spec-windows"
set "APP_DIST=%DIST_PATH%\%APP_NAME%"
set "BUILD_DIST=%DIST_PATH%\%BUILD_NAME%"
set "SUG_SRC=%CD%\krok_helper\lyrics_timing\src"
set "SUG_PACKAGE=%SUG_SRC%\strange_uta_game"
set "PYQT6_BINDING_VERSION=6.11.0"
set "PYQT6_QT_VERSION=6.11.0"
set "IS_CI="
if defined CI set "IS_CI=1"

echo Checking Python...
where %PYTHON_BIN% >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ first.
    if not defined IS_CI pause
    exit /b 1
)

call :ensure_pkg PyInstaller pyinstaller || exit /b 1
call :ensure_pyqt6 || exit /b 1
call :ensure_pkg fontTools fonttools || exit /b 1
call :ensure_pkg qfluentwidgets "PyQt6-Fluent-Widgets" || exit /b 1
call :ensure_pkg yt_dlp yt-dlp || exit /b 1
call :ensure_pkg requests requests || exit /b 1
call :ensure_pkg psutil psutil || exit /b 1
call :ensure_pkg sounddevice sounddevice || exit /b 1
call :ensure_pkg soundfile soundfile || exit /b 1
call :ensure_pkg pedalboard pedalboard || exit /b 1
call :ensure_pkg numpy numpy || exit /b 1
call :ensure_pkg pykakasi pykakasi || exit /b 1
call :ensure_pkg jaconv jaconv || exit /b 1
call :ensure_pkg pyphen pyphen || exit /b 1
call :ensure_pkg sudachipy sudachipy || exit /b 1
call :ensure_pkg sudachidict_small sudachidict_small || exit /b 1
call :ensure_pkg jieba jieba || exit /b 1
call :ensure_pkg pypinyin pypinyin || exit /b 1

echo Checking bundled SUG source path...
%PYTHON_BIN% -c "import sys; from pathlib import Path; src=Path(r'%SUG_SRC%').resolve(); sys.path.insert(0, str(src)); import strange_uta_game; actual=Path(strange_uta_game.__file__).resolve(); expected=src/'strange_uta_game'/'__init__.py'; print('  strange_uta_game:', actual); raise SystemExit(0 if actual == expected else f'Expected {expected}, got {actual}')" || exit /b 1

echo Building and validating Direct2D subtitle renderer...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_native_renderer_smoke.ps1 -InstallQtIfMissing
if errorlevel 1 (
    echo.
    echo Direct2D subtitle renderer build or WARP smoke failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Building GUI Updater.exe...
%PYTHON_BIN% krok_helper\updater_app\build_updater.py
if errorlevel 1 (
    echo.
    echo Updater build failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Fetching aria2c (bundled multi-connection downloader for Bilibili)...
%PYTHON_BIN% scripts\fetch_aria2.py
if errorlevel 1 (
    echo.
    echo aria2c fetch/verify failed.
    if not defined IS_CI pause
    exit /b 1
)

if not exist "%DIST_PATH%" mkdir "%DIST_PATH%"
if not exist "%WORK_PATH%" mkdir "%WORK_PATH%"
if not exist "%SPEC_PATH%" mkdir "%SPEC_PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Get-ChildItem -LiteralPath '%SPEC_PATH%' -Filter '*.spec' -File -ErrorAction SilentlyContinue | Remove-Item -Force"

echo Setting SUG package variant to noWinIME for this build...
copy /Y "%SUG_PACKAGE%\__version__.py" "%SUG_PACKAGE%\__version__.py.bak" >nul
%PYTHON_BIN% -c "import re; q=chr(34); p=r'%SUG_PACKAGE%\__version__.py'; t=open(p,encoding='utf-8').read(); n=re.sub('^(VARIANT *= *)'+q+'[^'+q+']*'+q, r'\1'+q+'noWinIME'+q, t, flags=re.M); open(p,'w',encoding='utf-8').write(n); raise SystemExit(0 if n!=t else 1)"
if errorlevel 1 (
    echo Failed to patch VARIANT in strange_uta_game\__version__.py.
    copy /Y "%SUG_PACKAGE%\__version__.py.bak" "%SUG_PACKAGE%\__version__.py" >nul
    del "%SUG_PACKAGE%\__version__.py.bak" >nul
    if not defined IS_CI pause
    exit /b 1
)

echo Building Windows package...
%PYTHON_BIN% -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onedir ^
    --name "%BUILD_NAME%" ^
    --icon "%CD%\krok_helper\assets\logo\logo.ico" ^
    --distpath "%DIST_PATH%" ^
    --workpath "%WORK_PATH%" ^
    --specpath "%SPEC_PATH%" ^
    --paths "%SUG_SRC%" ^
    --add-data "%CD%\krok_helper\assets;krok_helper\assets" ^
    --add-data "%SUG_PACKAGE%\config;strange_uta_game\config" ^
    --add-data "%SUG_PACKAGE%\resource;strange_uta_game\resource" ^
    --add-data "%SUG_PACKAGE%\bass;strange_uta_game\bass" ^
    --add-binary "%CD%\build\vendor\aria2\aria2c.exe;tools\aria2" ^
    --add-data "%CD%\build\vendor\aria2\COPYING;tools\aria2" ^
    --add-data "%CD%\build\vendor\aria2\LICENSE.OpenSSL;tools\aria2" ^
    --add-data "%CD%\build\vendor\aria2\AUTHORS;tools\aria2" ^
    --upx-exclude "aria2c.exe" ^
    --collect-all qfluentwidgets ^
    --collect-all yt_dlp ^
    --collect-all sounddevice ^
    --collect-all soundfile ^
    --collect-all pedalboard ^
    --collect-all pykakasi ^
    --collect-all sudachipy ^
    --collect-data sudachidict_small ^
    --collect-all pyphen ^
    --collect-all jieba ^
    --collect-all pypinyin ^
    --collect-binaries soundfile ^
    --collect-submodules strange_uta_game ^
    --hidden-import sounddevice ^
    --hidden-import soundfile ^
    --hidden-import pedalboard ^
    --hidden-import pedalboard.io ^
    --hidden-import pedalboard.io.AudioFile ^
    --hidden-import pedalboard.io.StreamResampler ^
    --hidden-import pedalboard.time_stretch ^
    --hidden-import numpy ^
    --hidden-import pykakasi ^
    --hidden-import pykakasi.kakasi ^
    --hidden-import jaconv ^
    --hidden-import PyQt6.sip ^
    --hidden-import PyQt6.QtMultimedia ^
    --hidden-import PyQt6.QtMultimediaWidgets ^
    --hidden-import encodings.idna ^
    --hidden-import colorsys ^
    --hidden-import sudachipy ^
    --hidden-import sudachidict_small ^
    --exclude-module winrt ^
    --exclude-module winrt.windows.globalization ^
    --exclude-module winrt.windows.foundation ^
    --exclude-module winrt.windows.foundation.collections ^
    --exclude-module sudachidict_core ^
    --exclude-module sudachidict_full ^
    --exclude-module scipy ^
    --exclude-module matplotlib ^
    --exclude-module pandas ^
    --exclude-module pytest ^
    --exclude-module torch ^
    --exclude-module pymss ^
    --exclude-module pymss_core ^
    --exclude-module pip ^
    --exclude-module PIL ^
    --exclude-module PySide6 ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6.Qt3DAnimation ^
    --exclude-module PyQt6.Qt3DCore ^
    --exclude-module PyQt6.Qt3DExtras ^
    --exclude-module PyQt6.Qt3DInput ^
    --exclude-module PyQt6.Qt3DLogic ^
    --exclude-module PyQt6.Qt3DRender ^
    --exclude-module PyQt6.QtCharts ^
    --exclude-module PyQt6.QtDataVisualization ^
    --exclude-module PyQt6.QtDesigner ^
    --exclude-module PyQt6.QtNetworkAuth ^
    --exclude-module PyQt6.QtPdf ^
    --exclude-module PyQt6.QtPdfWidgets ^
    --exclude-module PyQt6.QtPositioning ^
    --exclude-module PyQt6.QtQml ^
    --exclude-module PyQt6.QtQuick ^
    --exclude-module PyQt6.QtQuick3D ^
    --exclude-module PyQt6.QtQuickControls2 ^
    --exclude-module PyQt6.QtQuickTest ^
    --exclude-module PyQt6.QtQuickWidgets ^
    --exclude-module PyQt6.QtRemoteObjects ^
    --exclude-module PyQt6.QtScxml ^
    --exclude-module PyQt6.QtSensors ^
    --exclude-module PyQt6.QtSerialPort ^
    --exclude-module PyQt6.QtSql ^
    --exclude-module PyQt6.QtStateMachine ^
    --exclude-module PyQt6.QtTest ^
    --exclude-module PyQt6.QtTextToSpeech ^
    --exclude-module PyQt6.QtWebChannel ^
    --exclude-module PyQt6.QtWebEngineCore ^
    --exclude-module PyQt6.QtWebEngineQuick ^
    --exclude-module PyQt6.QtWebEngineWidgets ^
    --exclude-module PyQt6.QtWebSockets ^
    --exclude-module PyQt6.QtWebView ^
    app.py

set "BUILD_RC=%errorlevel%"
echo Restoring SUG __version__.py...
copy /Y "%SUG_PACKAGE%\__version__.py.bak" "%SUG_PACKAGE%\__version__.py" >nul
del "%SUG_PACKAGE%\__version__.py.bak" >nul

if not "%BUILD_RC%"=="0" (
    echo.
    echo Build failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Trimming Windows package...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$root = Resolve-Path '%BUILD_DIST%\_internal';" ^
    "$qtRoot = @(Get-ChildItem -LiteralPath $root -Directory -Filter 'PyQt6' -ErrorAction SilentlyContinue) + @(Get-ChildItem -LiteralPath $root -Directory -Filter 'PySide6' -ErrorAction SilentlyContinue) | Select-Object -First 1;" ^
    "if (-not $qtRoot) { Write-Host 'PyQt6 directory not found, skipping trim.'; exit 0 };" ^
    "$qt6 = Join-Path $qtRoot.FullName 'Qt6';" ^
    "if (-not (Test-Path $qt6)) { $qt6 = $qtRoot.FullName };" ^
    "$translations = Join-Path $qt6 'translations';" ^
    "if (Test-Path $translations) { Get-ChildItem $translations -File | Where-Object { $_.Name -notin @('qtbase_zh_CN.qm','qtbase_zh_TW.qm','qtbase_ja.qm','qt_zh_CN.qm','qt_zh_TW.qm','qt_ja.qm') } | Remove-Item -Force };" ^
    "$plugins = Join-Path $qt6 'plugins';" ^
    "$removeFiles = @('platforms\qdirect2d.dll','platforms\qminimal.dll','platforms\qoffscreen.dll','imageformats\qwebp.dll','imageformats\qtiff.dll','imageformats\qicns.dll','imageformats\qgif.dll','imageformats\qpdf.dll','imageformats\qtga.dll','imageformats\qwbmp.dll','iconengines\qsvgicon.dll','tls\qopensslbackend.dll','tls\qcertonlybackend.dll','generic\qtuiotouchplugin.dll','networkinformation\qnetworklistmanager.dll','platforminputcontexts\qtvirtualkeyboardplugin.dll');" ^
    "foreach ($rel in $removeFiles) { $path = Join-Path $plugins $rel; if (Test-Path $path) { Remove-Item -LiteralPath $path -Force } };" ^
    "$removeDirs = @('generic','networkinformation','platforminputcontexts');" ^
    "foreach ($rel in $removeDirs) { $path = Join-Path $plugins $rel; if ((Test-Path $path -PathType Container) -and -not (Get-ChildItem $path -Force)) { Remove-Item -LiteralPath $path -Force } };" ^
    "$dlls = @('Qt6Pdf.dll','Qt6VirtualKeyboard.dll','Qt6Quick.dll','Qt6Qml.dll');" ^
    "foreach ($base in @($qt6, (Join-Path $qt6 'bin'), $root)) { if (-not (Test-Path $base)) { continue }; foreach ($name in $dlls) { $path = Join-Path $base $name; if (Test-Path $path) { Remove-Item -LiteralPath $path -Force } } }"
if errorlevel 1 (
    echo.
    echo Package trimming failed.
    if not defined IS_CI pause
    exit /b 1
)

REM Qt 官方 Windows 二进制（如 6.10.2）在 Qt6\bin 自带 VS2019 时代的
REM MSVC 运行时（MSVCP140/VCRUNTIME140 14.26 vcwrkspc）。Qt6 按同目录优先
REM 加载这份旧运行时后，全进程同名唯一，VS2022 编译的 pedalboard_native 等
REM C++ 扩展在运行新代码路径（如 MP3 编码）时会在旧 MSVCP140.dll 内访问冲突
REM （0xc0000005，windowed 应用表现为无声闪退）。这里用构建机 System32 的
REM 新版运行时覆盖，并设版本下限防止未来 PyQt6 升级再次带入旧文件。
echo Refreshing bundled MSVC runtime DLLs...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$qtBin = '%BUILD_DIST%\_internal\PyQt6\Qt6\bin';" ^
    "if (-not (Test-Path -LiteralPath $qtBin -PathType Container)) { Write-Host 'Qt6 bin not found, skipping MSVC runtime refresh.'; exit 0 };" ^
    "$minVersion = [version]'14.38.0.0';" ^
    "$names = @('MSVCP140.dll','MSVCP140_1.dll','MSVCP140_2.dll','VCRUNTIME140.dll','VCRUNTIME140_1.dll');" ^
    "foreach ($name in $names) {" ^
    "  $dst = Join-Path $qtBin $name;" ^
    "  if (-not (Test-Path -LiteralPath $dst -PathType Leaf)) { continue };" ^
    "  $oldRaw = (Get-Item -LiteralPath $dst).VersionInfo.FileVersion;" ^
    "  $oldVer = [version][regex]::Match($oldRaw, '^\d+(\.\d+)+').Value;" ^
    "  if ($oldVer -ge $minVersion) { continue };" ^
    "  $sys = Join-Path $env:SystemRoot ('System32\' + $name.ToLower());" ^
    "  if (-not (Test-Path -LiteralPath $sys -PathType Leaf)) { Write-Host ('System MSVC runtime missing: ' + $sys); exit 1 };" ^
    "  Copy-Item -LiteralPath $sys -Destination $dst -Force;" ^
    "  $newRaw = (Get-Item -LiteralPath $dst).VersionInfo.FileVersion;" ^
    "  $newVer = [version][regex]::Match($newRaw, '^\d+(\.\d+)+').Value;" ^
    "  Write-Host ('  ' + $name + ': ' + $oldVer + ' -> ' + $newVer);" ^
    "  if ($newVer -lt $minVersion) { Write-Host ('MSVC runtime still below floor after refresh: ' + $name); exit 1 }" ^
    "}"
if errorlevel 1 (
    echo.
    echo MSVC runtime refresh failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Renaming Windows package...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$distRoot = Resolve-Path '%DIST_PATH%';" ^
    "$buildDir = Join-Path $distRoot '%BUILD_NAME%';" ^
    "$targetDir = Join-Path $distRoot '%APP_NAME%';" ^
    "if (-not (Test-Path $buildDir -PathType Container)) { throw 'Build output directory not found.' };" ^
    "if (Test-Path $targetDir) { Remove-Item -LiteralPath $targetDir -Recurse -Force };" ^
    "if (Test-Path $targetDir) { throw 'Existing target directory could not be removed.' };" ^
    "$buildExe = Join-Path $buildDir ('%BUILD_NAME%' + '.exe');" ^
    "function Invoke-WithRetry($block, $label) { for ($i = 1; $i -le 8; $i++) { try { & $block; return } catch { if ($i -eq 8) { throw } ; Write-Host (\"  ${label}: locked, retrying ($i/7)...\"); Start-Sleep -Milliseconds 800 } } };" ^
    "if (Test-Path $buildExe -PathType Leaf) { Invoke-WithRetry { Rename-Item -LiteralPath $buildExe -NewName ('%APP_NAME%' + '.exe') -Force -ErrorAction Stop } 'exe' };" ^
    "Invoke-WithRetry { Rename-Item -LiteralPath $buildDir -NewName '%APP_NAME%' -Force -ErrorAction Stop } 'dir'"
if errorlevel 1 (
    echo.
    echo Package rename failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Creating legacy-name EXE copy for existing installs...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$targetDir = Resolve-Path '%APP_DIST%';" ^
    "$primary = Join-Path $targetDir ('%APP_NAME%' + '.exe');" ^
    "$legacy = Join-Path $targetDir ('%LEGACY_APP_NAME%' + '.exe');" ^
    "if (-not (Test-Path $primary -PathType Leaf)) { throw 'Primary EXE not found.' };" ^
    "Copy-Item -LiteralPath $primary -Destination $legacy -Force"
if errorlevel 1 (
    echo.
    echo Failed to create the legacy-name EXE copy.
    echo Existing installs cannot auto-update without it - aborting.
    if not defined IS_CI pause
    exit /b 1
)

echo Copying Updater.exe...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$targetDir = Resolve-Path '%APP_DIST%';" ^
    "$updater = Resolve-Path 'krok_helper\updater_app\dist\Updater.exe';" ^
    "$renderer = Resolve-Path 'build\native-renderer\krok_subtitle_renderer.exe';" ^
    "Copy-Item -LiteralPath $updater -Destination (Join-Path $targetDir 'Updater.exe') -Force;" ^
    "Copy-Item -LiteralPath $renderer -Destination (Join-Path $targetDir 'krok_subtitle_renderer.exe') -Force"
if errorlevel 1 (
    echo.
    echo Failed to copy Updater.exe or the Direct2D subtitle renderer.
    if not defined IS_CI pause
    exit /b 1
)

REM 嵌入的 AI 打轴 worker 以外部解释器（托管 PyMSS runtime）子进程运行，
REM runpy 引导要求 _internal\strange_uta_game 下有真实 .py 源码；KS 的
REM PyInstaller 用 --collect-submodules 把 SUG 编进 PYZ（仅 frozen 应用
REM 自身可用），数据 add-data 只落了 config/resource/bass。打包末尾把
REM submodule 源码树补复制到同相对路径（剔除 __pycache__/.pyc），不改
REM 依赖分析、不改 worker 代码（frozen 应用自身 import 仍走 PYZ，
REM FrozenImporter 优先于路径查找，不受该目录影响）。
echo Copying SUG source tree for the AI timing worker...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$targetDir = Resolve-Path '%APP_DIST%';" ^
    "$src = (Resolve-Path '%SUG_PACKAGE%').Path;" ^
    "$dst = Join-Path $targetDir '_internal\strange_uta_game';" ^
    "New-Item -ItemType Directory -Path $dst -Force | Out-Null;" ^
    "Get-ChildItem -LiteralPath $src -Recurse -File |" ^
    "  Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |" ^
    "  ForEach-Object {" ^
    "    $rel = $_.FullName.Substring($src.Length).TrimStart('\');" ^
    "    $dest = Join-Path $dst $rel;" ^
    "    $destDir = Split-Path -Parent $dest;" ^
    "    if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null };" ^
    "    Copy-Item -LiteralPath $_.FullName -Destination $dest -Force" ^
    "  };" ^
    "$probe = Join-Path $dst 'backend\application\ai_timing\worker\client.py';" ^
    "if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) { throw 'SUG worker source missing after copy.' };" ^
    "Write-Host '  SUG source tree copied (worker bootstrap ready).'"
if errorlevel 1 (
    echo.
    echo Failed to copy the SUG source tree.
    if not defined IS_CI pause
    exit /b 1
)

echo Validating Windows package contents...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$targetDir = Resolve-Path '%APP_DIST%';" ^
    "$internal = Join-Path $targetDir '_internal';" ^
    "$required = @(" ^
    "  'krok_helper\assets\logo\logo.jpg'," ^
    "  'krok_helper\assets\logo\logo.ico'," ^
    "  'krok_helper\assets\logo\start.jpg'," ^
    "  'krok_helper\assets\platforms\youtube.svg'," ^
    "  'strange_uta_game\config\config.json'," ^
    "  'strange_uta_game\config\dictionary.json'," ^
    "  'strange_uta_game\config\cmudict-0.7b'," ^
    "  'strange_uta_game\config\kanji_readings.json'," ^
    "  'strange_uta_game\backend\application\ai_timing\worker\client.py'," ^
    "  'pyphen\dictionaries\hyph_en_US.dic'," ^
    "  'jieba\dict.txt'," ^
    "  'strange_uta_game\resource\icon.ico'," ^
    "  'strange_uta_game\resource\sounds\press.wav'," ^
    "  'strange_uta_game\bass\x64\bass.dll'," ^
    "  'strange_uta_game\bass\x64\bass_fx.dll'," ^
    "  'Updater.exe'," ^
    "  'krok_subtitle_renderer.exe'," ^
    "  '%APP_NAME%.exe'," ^
    "  '%LEGACY_APP_NAME%.exe'" ^
    ");" ^
    "$rootLevel = @('Updater.exe','krok_subtitle_renderer.exe','%APP_NAME%.exe','%LEGACY_APP_NAME%.exe');" ^
    "$missing = @();" ^
    "foreach ($rel in $required) { $base = if ($rel -in $rootLevel) { $targetDir } else { $internal }; $path = Join-Path $base $rel; if (-not (Test-Path $path -PathType Leaf)) { $missing += $path } };" ^
    "$multimediaRequired = @('QtMultimedia.pyd','QtMultimediaWidgets.pyd','Qt6Multimedia.dll','Qt6MultimediaWidgets.dll','ffmpegmediaplugin.dll');" ^
    "foreach ($name in $multimediaRequired) { if (-not (Get-ChildItem -LiteralPath $internal -Recurse -File -Filter $name -ErrorAction SilentlyContinue | Select-Object -First 1)) { $missing += ('Qt Multimedia component: ' + $name) } };" ^
    "$forbiddenRuntimeDirs = @('torch','functorch','torchgen','pymss','pymss_core','pip');" ^
    "foreach ($name in $forbiddenRuntimeDirs) { if (Get-ChildItem -LiteralPath $internal -Recurse -Directory -Filter $name -ErrorAction SilentlyContinue | Select-Object -First 1) { $missing += ($name + ' must not be bundled in the app') } };" ^
    "$forbiddenMetadataPrefixes = @('torch-','pymss-','pymss_core-','pip-');" ^
    "foreach ($dir in Get-ChildItem -LiteralPath $internal -Recurse -Directory -ErrorAction SilentlyContinue) { $lower = $dir.Name.ToLowerInvariant(); foreach ($prefix in $forbiddenMetadataPrefixes) { if ($lower.StartsWith($prefix)) { $missing += ($dir.FullName + ' must not be bundled in the app'); break } } };" ^
    "if ($missing.Count) { Write-Host 'Missing package files:'; $missing | ForEach-Object { Write-Host ('  ' + $_) }; exit 1 };" ^
    "$warnRoot = Join-Path '%WORK_PATH%' '%BUILD_NAME%';" ^
    "$warn = if (Test-Path $warnRoot) { Get-ChildItem -LiteralPath $warnRoot -Recurse -Filter 'warn-*.txt' -File -ErrorAction SilentlyContinue | Select-Object -First 1 } else { $null };" ^
    "if ($warn) { Write-Host ('PyInstaller warnings were written to: ' + $warn.FullName) };" ^
    "Write-Host 'Package content validation passed.'"
if errorlevel 1 (
    echo.
    echo Package validation failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Validating packaged multiprocessing spawn...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$exe = Resolve-Path '%APP_DIST%\%APP_NAME%.exe';" ^
    "$process = Start-Process -FilePath $exe -ArgumentList '--package-spawn-smoke' -WindowStyle Hidden -Wait -PassThru;" ^
    "if ($process.ExitCode -ne 0) { Write-Host ('Packaged spawn smoke failed with exit code ' + $process.ExitCode); exit 1 };" ^
    "Write-Host 'Packaged multiprocessing spawn passed.'"
if errorlevel 1 (
    echo.
    echo Packaged multiprocessing validation failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Validating packaged Direct2D subtitle renderer...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$exe = Resolve-Path '%APP_DIST%\%APP_NAME%.exe';" ^
    "$process = Start-Process -FilePath $exe -ArgumentList '--package-gpu-smoke' -WindowStyle Hidden -Wait -PassThru;" ^
    "if ($process.ExitCode -ne 0) { Write-Host ('Packaged GPU subtitle smoke failed with exit code ' + $process.ExitCode); exit 1 };" ^
    "Write-Host 'Packaged Direct2D subtitle renderer passed.'"
if errorlevel 1 (
    echo.
    echo Packaged Direct2D subtitle renderer validation failed.
    if not defined IS_CI pause
    exit /b 1
)

echo Creating update archives (full zip + incremental parts + manifest)...
if defined IS_CI (
    %PYTHON_BIN% scripts\build_parts.py --require-runtime-reuse
) else (
    %PYTHON_BIN% scripts\build_parts.py
)
if errorlevel 1 (
    echo.
    echo Failed to create update archives.
    if not defined IS_CI pause
    exit /b 1
)

echo.
echo Build complete:
echo %CD%\%APP_DIST%
echo %CD%\%DIST_PATH%\KaraokeStudio-windows.zip
if not defined IS_CI pause
exit /b 0

:ensure_pyqt6
echo Checking PyQt6 %PYQT6_BINDING_VERSION% with Qt %PYQT6_QT_VERSION%...
%PYTHON_BIN% -c "from PyQt6.QtCore import PYQT_VERSION_STR, qVersion; raise SystemExit(0 if PYQT_VERSION_STR == '%PYQT6_BINDING_VERSION%' and qVersion() == '%PYQT6_QT_VERSION%' else 1)" >nul 2>&1
if errorlevel 1 (
    echo Installing PyQt6 %PYQT6_BINDING_VERSION% with Qt %PYQT6_QT_VERSION%...
    %PYTHON_BIN% -m pip install --upgrade "PyQt6==%PYQT6_BINDING_VERSION%" "PyQt6-Qt6==%PYQT6_QT_VERSION%"
    if errorlevel 1 (
        echo Failed to install PyQt6 %PYQT6_BINDING_VERSION% with Qt %PYQT6_QT_VERSION%.
        if not defined IS_CI pause
        exit /b 1
    )
)
exit /b 0

:ensure_pkg
echo Checking %~1...
%PYTHON_BIN% -c "import %~1" >nul 2>&1
if errorlevel 1 (
    echo %~1 not found, installing %~2...
    %PYTHON_BIN% -m pip install %~2
    if errorlevel 1 (
        echo Failed to install %~2.
        if not defined IS_CI pause
        exit /b 1
    )
)
exit /b 0
