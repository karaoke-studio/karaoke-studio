# Native Subtitle Renderer Sidecar

This sidecar contains the historical Qt/QPainter CPU renderer and the G0
Direct3D 11 + Direct2D + DirectWrite GPU probe described in
`docs/字幕渲染-GPU后端逆向与实施计划.md`.

Current GPU scope:

- high-performance hardware adapter enumeration plus explicit WARP fallback;
- multithread-protected D3D11 device and multithreaded Direct2D factory;
- transparent BGRA premultiplied GPU target;
- fixed rectangle/glyph Direct2D rendering;
- staging-texture readback and conversion to straight-alpha RGBA8888;
- delivery through the existing `QSharedMemory` frame ring;
- `backend_info` and `render_probe` JSON Lines commands;
- no product preview/export selection yet.

Protocol example:

```json
{"cmd":"configure","ir":{"schema":1,"screen":{"width":640,"height":360,"fps":60},"style":{},"track":{"lines":[],"rubies":[]}}}
{"cmd":"render_frame","t_ms":1000,"output_path":"D:/tmp/native-smoke.png"}
{"cmd":"shutdown"}
```

The process prints one compact JSON object per line. It also prints an initial ready event:

```json
{"event":"ready","gpu_protocol":1,"ok":true,"schema":1}
```

GPU protocol examples:

```json
{"cmd":"backend_info","force_warp":false}
{"cmd":"render_probe","width":256,"height":144,"draw_glyph":true,"force_warp":false}
```

`render_probe` returns a `probe_ready` event carrying the same shared-memory
slot metadata as `frame_ready`. The payload format is RGBA8888 straight alpha.

Build and smoke:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_native_renderer_smoke.ps1 -RequireHardware
C:\Python314\python.exe scripts\probe_gpu_renderer.py --both --frames 1000
```

The Python wrapper lives in `krok_helper/subtitle_render/native_backend.py`.
