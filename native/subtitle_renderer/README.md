# Native Subtitle Renderer Sidecar

This is the C1 skeleton for `docs/字幕渲染核心C++化方案.md`.

Scope:

- long-running sidecar process;
- JSON Lines control protocol over stdin/stdout;
- Render IR v1 ingestion;
- minimal smoke `render_frame` command that writes a PNG;
- no shared-memory ring buffer yet.

Protocol example:

```json
{"cmd":"configure","ir":{"schema":1,"screen":{"width":640,"height":360,"fps":60},"style":{},"track":{"lines":[],"rubies":[]}}}
{"cmd":"render_frame","t_ms":1000,"output_path":"D:/tmp/native-smoke.png"}
{"cmd":"shutdown"}
```

The process prints one compact JSON object per line. It also prints an initial ready event:

```json
{"event":"ready","ok":true,"schema":1}
```

The Python wrapper lives in `krok_helper/subtitle_render/native_backend.py`.
