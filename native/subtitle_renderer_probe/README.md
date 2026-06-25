# QPainter Parallel Probe

This is the C0 probe for `docs/字幕渲染核心C++化方案.md`.

It answers one narrow question:

> Can native C++ threads rasterize independent `QImage + QPainter` subtitle frames in parallel?

The probe intentionally does not link to the Python application. It creates a synthetic heavy karaoke scene with:

- multiple CJK lyric lines;
- ruby text;
- per-frame per-glyph transform similar to `utopia`;
- repeated `QPainterPath::addText`, `strokePath`, `fillPath`, and `drawImage` work.

Build example on Windows:

```powershell
$qt = "$env:LOCALAPPDATA\krok-helper\qt\6.10.0\msvc2022_64"
$vs = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
cmd /c "`"$vs\Common7\Tools\VsDevCmd.bat`" -arch=amd64 && cmake -S native\subtitle_renderer_probe -B build\native-probe -G Ninja -DCMAKE_PREFIX_PATH=`"$qt`" -DCMAKE_BUILD_TYPE=Release && cmake --build build\native-probe --config Release"
$env:PATH = "$qt\bin;$env:PATH"
build\native-probe\krok_qpainter_parallel_probe.exe --threads 1,2,4,8 --frames 240
```

Interpretation:

- Near-linear speedup means native renderer threads are worth pursuing.
- Speedup stuck near `1x` means Qt/font rasterization still has a native global bottleneck; C++ may still reduce Python overhead, but not solve parallelism by itself.
