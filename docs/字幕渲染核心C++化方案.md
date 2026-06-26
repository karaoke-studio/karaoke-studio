# 字幕渲染核心 C++ 化方案

> 本文是对 [`字幕渲染-管线优化调研.md`](字幕渲染-管线优化调研.md) 与
> [`字幕渲染-预渲染帧缓存方案评估.md`](字幕渲染-预渲染帧缓存方案评估.md) 的后续决策记录。
> 目标是把“是否、为什么、以及怎样把渲染核心 C++ 化”持久化，避免后续接手者重新从探针结果里拼上下文。
>
> 更新时间：2026-06-26

---

## 0. TL;DR

- 当前性能瓶颈已经不是“再加一个 Python 线程”能解决的问题：`bench_parallel_paint.py` 实测 QThread 池约
  `1.07x`，说明 PyQt 调用 QPainter 光栅化期间仍受 GIL 串行化约束。
- `utopia + glow` 的核心成本集中在逐帧 `strokePath` / `fillPath` / glow / `addText` 等 Qt C++ 调用；
  cProfile 显示约 61% 时间在 Qt C++ 光栅化调用内，其中 `strokePath` 单独约 33%。
- 单纯帧缓存已经实测回退：`utopia` 每帧都在抖动、缩放、扫光，按帧桶复用会造成时间吸附，出现错位/重影。
- 最新 S2' 探针已证明 **multiprocess + shared memory ring buffer** 可绕开 GIL 并把净吞吐拉过实时。
- “C++ 化渲染核心”的价值不只是“单帧更快”，更重要是：
  - 去掉 GIL，使多线程帧渲染真正并行；
  - 减少每帧 Python layout / 对象分配 / 跨语言调用开销；
  - 形成可被预览与导出共用的 native renderer，长期替代巨型 `painter.py` 热路径。

**建议路线**：短期先产品化 S2' 多进程共享内存预览池；C++ 化作为中长期 native 后端推进。C++ 首选
**sidecar renderer 进程**，而不是一开始做 `.pyd` Python extension。

**C0 探针状态（2026-06-25）**：已新增 `native/subtitle_renderer_probe/`，并在本机 Qt 6.10.0
MSVC Release 构建下重跑。默认重负载（2400×1350、utopia+glow+ruby、240 帧）按每档 3 次取中位：
1 线程约 37.4fps，8 线程约 121.9fps，16 线程约 133.7fps。结论更准确地说是：C++ 同进程线程池
没有像 PyQt QThread 一样卡死在 1x，具备继续推进价值；但在 8C/16T 笔记本 CPU 上 8→16 线程收益已经很小，
探针结果应视为“可行性门槛通过”，不是产品 renderer 吞吐承诺。

**C1 骨架状态（2026-06-25）**：已新增 `native/subtitle_renderer/` sidecar、Python
`native_protocol.py` / `native_backend.py`、`scripts/run_native_renderer_smoke.ps1`。当前协议为 JSON Lines，
支持 `configure` / `render_frame` / `shutdown`，Render IR v1 由 Python 从 `TimingTrack + Style`
生成。C1 只做 PNG smoke 输出，尚未接入预览、导出或 shared memory ring buffer。

**当前进度快照（2026-06-26）**：
- C2/C3 已迁移普通横排、ruby、PaintFill、glow/shadow、singer/role override 等主路径，并建立 Python-vs-native
  bounded pixel diff。
- C4 已迁移 `utopia` 主文本、ruby group/reading transition 与 transformed glow cache；修复了 ruby utopia glow cache
  误用 transformed path 造成的 content miss，60 帧样本阶段 miss 已从 `+219` 降到 `+0`。
- C4-4 已建立真实工程 benchmark：`render_frame_stats` 用于单帧不落盘计时，`render_range_stats` 用于 C++ thread pool
  吞吐测试。A stain 60 帧 cache-on 初测显示 `range:1/2/4/8` 约 `8.12/4.07/2.40/1.67ms/frame`，证明 sidecar
  多线程方向有效。
- 产品协议雏形已落地：`render_range` / `cancel_generation` 支持 generation、取消、后台 worker、逐帧 `frame_ready`；
  `frame_ready` 已携带 `QSharedMemory` ring slot metadata，native 会写入 RGBA8888 payload。Python 侧
  `SharedFrameRingReader` 已可 attach shared memory，校验 slot header，并复制 RGBA8888 payload / 转 `QImage`。
- C5 预览初始接线已落地：`KROK_SUBTITLE_NATIVE_RENDER=1` 且 sidecar 可用时，graphics preview 会使用 native
  `render_range` + shared memory 帧；generation / 当前时间会过滤过期帧，native 失败时回退后台 Python renderer。
- C5 稳定化已完成第一轮：native preview 支持播放态 look-ahead、frame-bucket cache key、generation cancel、
  waiting bucket 与 ring slot overwrite 降级处理。`A stain` 720p/60fps/5s 对比中 native 已达到
  `ready=296/296`、`dup=0`、`steady_drop=0`，抽样像素差为 0；seek+resize+style churn 压力 probe 为 `fail=0`。

**下一步**：把 C5 probe 输出沉淀为可长期对比的 summary/CSV，并跑更长时间真实素材压力回归；若继续稳定，可进入
C6，把 native range render 接入导出路径。

---

## 1. 背景与已确认事实

### 1.1 当前渲染路径

预览与导出共用 Python 侧 `paint_frame_to_painter`：

- 预览：`frontend/preview_async.py` 中单个 QThread worker 渲 `QImage`，GUI 线程 blit。
- 导出：`engine/renderer.py` 逐帧生成 RGBA rawvideo，喂给 ffmpeg pipe；导出侧已有多进程、条带、空帧短路。
- 核心绘制：`engine/painter.py`，包含 layout、animation、paint、缓存、ruby、Sayatoo signal、`utopia` 等全部逻辑。

### 1.2 Python 多线程不可行

调研结论来自 `scripts/bench_parallel_paint.py`：

| 线程数 | 结论 |
|---|---|
| 1 | 基准 |
| 2/4/8 | 吞吐几乎不涨，8 线程约 1.07x |

含义：

- 多个 QThread 各画自己的 QImage 也没有线性加速。
- PyQt 在关键 QPainter C++ 调用期间没有释放 GIL，导致 Python 线程池被串行化。
- 因此不应再投入“QThread worker 池”路线。

### 1.3 多进程共享内存可行

调研结论来自 `scripts/probe_multiprocess_preview.py`：

- discard 模式证明多进程能真正绕开 GIL。
- pickle 回传整帧 bytes 会被 IPC 吃掉收益。
- shared memory slot/ring buffer 能显著恢复吞吐，2 进程起即有机会超过实时。

含义：

- 若继续使用 Python + QPainter，唯一能越过 GIL 墙的是 multiprocess。
- 预览端应该优先落地“多进程渲染池 + 共享内存 ring buffer + generation 丢弃过期帧”。

### 1.4 帧缓存不适合 utopia 主痛点

已尝试并回退的 P-A 帧缓存说明：

- 首次连续播放 `utopia` 时每 tick 基本都是新帧，缓存全 miss。
- tick 抖动导致命中同一帧桶时，`utopia` 当前动画状态会错 0-16ms，肉眼可见错位/重影。
- 未来若恢复帧缓存，应只对静止保持段/非动画段生效，不应缓存正在扫光、入退场或 `utopia` 动态帧。

---

## 2. 为什么还要 C++ 化

S2' 多进程是近期最短路径，但它仍然是“绕开 GIL”。C++ 化的目标是“拆掉 GIL 墙”：

1. **真正多线程**
   C++ worker 线程可以在一个进程内并行渲不同帧，不需要每个 worker 一份 Python 解释器、QApplication、缓存与大对象。

2. **减少 Python 热路径成本**
   当前每帧仍有大量 Python 层 layout、dataclass 访问、临时对象、函数分派。即使 Qt 光栅化仍然贵，Python 部分也能被 native 化吃掉。

3. **预览/导出共用后端**
   预览不需要低延迟时，native renderer 可以用 ring buffer 交帧；导出可以用 range render 直接给 ffmpeg。

4. **长期维护性**
   `painter.py` 已经承载 layout、动画、特效、缓存、边界估算、导出优化等职责。C++ 化时可以顺手把结构切成：
   `layout -> animation -> layer/composite -> raster`。

5. **为 GPU 后端留口**
   C++ renderer 的 IR 与 layer model 稳定后，未来可以加 Skia/GPU/MSDF 后端，而不是把 GPU 逻辑塞回 Python 巨石。

---

## 3. 路线选择：sidecar 优先

### 3.1 不建议第一步做 `.pyd`

`.pyd` / pybind / SIP 方案的优点是调用方便、分发像 Python 模块；但第一阶段风险更高：

- 崩溃会直接带崩主 GUI 进程。
- Qt ABI、PyQt6 wheel 自带 Qt、系统 Qt/CMake Qt 的版本关系需要非常谨慎。
- GIL 仍需在 binding 边界显式释放，调试复杂。
- PyInstaller 收集 extension + Qt 依赖的失败面更大。

### 3.2 建议第一步做 native sidecar renderer

形态：

```text
Karaoke Studio.exe
  └─ krok-subtitle-renderer.exe
       ├─ stdin/stdout 或 named pipe 控制命令
       ├─ shared memory ring buffer 返回帧
       └─ 内部 C++ thread pool 渲染帧
```

优点：

- 和 S2' 探针结论一致：进程边界 + shared memory 是已经被验证的 IPC 形态。
- native 崩溃不会直接带崩主 GUI，可 fallback 到 Python renderer。
- 可独立跑 benchmark、pixel test、压力测试。
- PyInstaller 只需要带一个 exe/dll 集合，工程边界清楚。
- 后续如果 sidecar 稳定，再收敛成 `.pyd` 或保留 sidecar 都可以。

---

## 4. 目标架构

### 4.1 Python 继续负责的部分

- GUI、项目管理、设置持久化、文件选择。
- Nicokara LRC 解析与现有 `TimingTrack` / `Style` 模型维护。
- 与 workflow context、导出任务、取消/进度 UI 集成。
- fallback Python renderer。

### 4.2 Native renderer 负责的部分

- 接收版本化 render IR。
- 帧级可见内容计算。
- layout cache。
- animation state。
- layer bake cache。
- QImage/QPainter 光栅化。
- C++ thread pool / job scheduler。
- shared memory ring buffer 写帧。

### 4.3 Render IR

不要让 C++ 直接理解 Python dataclass。Python 侧生成一个稳定、版本化的中间格式：

```json
{
  "schema": 1,
  "screen": {"width": 1920, "height": 1080, "fps": 60},
  "style": {...},
  "roles": {...},
  "track": {
    "meta": {...},
    "lines": [
      {
        "singer_id": 0,
        "singer_label": "A",
        "chars": [
          {"text": "君", "start_ms": 1234, "pause_release_ms": null, "role_label": null}
        ],
        "end_ms": 2345
      }
    ],
    "rubies": [
      {"kanji": "君", "reading": "きみ", "reading_part_ms": [1234, 1340], "pos_start_ms": 1234, "pos_end_ms": 1500}
    ]
  }
}
```

注意：

- IR 必须带 `schema`，后续样式字段变更可兼容。
- 样式字段按“渲染语义”整理，不必 1:1 暴露 UI 表单字段。
- 路径类资源，如图片填充，应传绝对路径 + 文件 mtime/hash，便于 native cache 失效。

### 4.4 控制协议

最小命令集：

```text
configure(render_ir, target_size, dpr, shm_name, ring_slots)
render_frame(frame_index, t_ms, generation)
render_range(start_frame, count, generation)
cancel_generation(generation)
shutdown
```

返回事件：

```text
ready
frame_ready(frame_index, t_ms, generation, slot_index, width, height, stride, format)
range_progress(done, total)
error(code, message)
```

### 4.5 Shared memory ring buffer

每个 slot 存一帧 RGBA premultiplied 或 straight alpha，格式必须明确：

```text
slot_header:
  generation
  frame_index
  t_ms
  width
  height
  stride
  pixel_format
  state: empty | writing | ready
payload:
  rgba bytes
```

预览端规则：

- GUI 只消费当前 generation。
- seek/style/尺寸变化时 generation++，旧帧直接丢。
- 当前帧优先级最高，look-ahead 帧低优先级。
- ring 满时丢最远未来帧，不能阻塞当前帧。

导出端规则：

- 可以不用 ring，也可以 range render 后主进程按序读 slot 写 ffmpeg。
- 后续可让 native 直接写 ffmpeg stdin，减少 Python 中转。

---

## 5. 分阶段落地计划

### C0：C++ 并行可行性探针

目标：确认“同进程 C++ 多线程 QPainter 光栅化”是否存在可用扩展空间，以及在本机 CPU 上大概从哪里开始撞到收益天花板。

做法：

- 写最小 C++ Qt console 程序。
- 合成 `utopia + glow + ruby` 近似场景。
- 每个线程独立 `QImage + QPainter`，渲染不同帧。
- 测 1/2/4/8/16 线程吞吐、进程 CPU 采样、单帧耗时。
- 每档至少跑 3 次，正式引用中位数；单跑只作为烟测信号。

验收：

- 4 线程至少接近 2.5x，8 线程至少明显超过 4 线程，才说明 native 多线程值得继续投入。
- 8→16 线程若收益很小，说明已经接近本机天花板或遇到 Qt/font/memory 争用，不能按物理核/逻辑核线性外推。
- 若仍接近 1x，说明 Qt 内部或字体栈还有全局锁；C++ 化仍可减少 Python 开销，但不能指望多线程主收益。

#### C0 实测结果（2026-06-25）

实现位置：

- [`native/subtitle_renderer_probe/CMakeLists.txt`](../native/subtitle_renderer_probe/CMakeLists.txt)
- [`native/subtitle_renderer_probe/src/main.cpp`](../native/subtitle_renderer_probe/src/main.cpp)
- [`scripts/run_native_qpainter_probe.ps1`](../scripts/run_native_qpainter_probe.ps1)

本机环境：

- CPU：AMD Ryzen 9 6900HX，8 物理核 / 16 逻辑核；笔记本平台，结果可能受 boost、温度、后台任务影响。
- Visual Studio Build Tools 2026 / MSVC 19.50
- Qt 6.10.0 `win64_msvc2022_64`，由 `aqtinstall` 下载到 `%LOCALAPPDATA%\krok-helper\qt`
- CMake 4.3.4 + Ninja 1.13.0

默认重负载：2400×1350、240 帧、`utopia + glow + ruby`。以下为每档 3 次运行的中位数，不是置信区间。
CPU 列来自进程 CPU 时间采样；它可辅助判断本进程占用，但不一定覆盖 Windows 字体服务等进程外成本。

| threads | runs | median fps | median ms/frame | median speedup | median process CPU cores | median all-core CPU |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 37.4 | 26.7 | 1.0x | 0.1 | 0.6% |
| 2 | 3 | 65.0 | 15.4 | 1.7x | 0.1 | 0.8% |
| 4 | 3 | 100.0 | 10.0 | 2.7x | 0.2 | 1.5% |
| 8 | 3 | 121.9 | 8.2 | 3.3x | 0.3 | 1.9% |
| 16 | 3 | 133.7 | 7.5 | 3.6x | 11.2 | 69.9% |

早期有一组单跑数据曾显示 8 线程约 133.6fps / 4.2x，因只跑一次且未覆盖 16 线程，不再作为结论引用。

判读：

- 与 Python QThread 池 8 线程约 1.07x 的结果相比，C++ 同进程线程池确实绕开了 PyQt/GIL 那堵墙。
- 4 线程约 2.7x，8 线程约 3.3x，通过“值得继续做 native 探索”的门槛；但 8→16 只从 121.9fps 到 133.7fps，已经明显边际递减。
- 这组探针负载偏向“并行光栅化上限”：每帧大块 `addText`/stroke/fill/glow，layout、Python 调度、真实工程状态同步等还没放进去。真实 renderer 的可并行占比大概率更低。
- 因此 C0 不能解读成“native renderer 能稳定跑 133fps”。更稳妥的读法是：sidecar 线程池在这台 8C/16T 机器上具备约 3-4x 封顶空间，且后续优化重点应转向减少共享资源争用、缓存字体/path/layout、降低 per-frame QImage 分配。
- 下一步可以进入 C1：native sidecar renderer 骨架 + Render IR v1。

### C1：native sidecar 骨架

新增建议目录：

```text
native/
  subtitle_renderer/
    CMakeLists.txt
    src/
      main.cpp
      protocol.*
      shared_memory.*
      renderer.*
      models.*
```

Python 侧新增：

```text
krok_helper/subtitle_render/native_backend.py
krok_helper/subtitle_render/native_protocol.py
```

功能：

- sidecar 启动/关闭。
- `configure` 命令。
- 单帧纯色/简单文字渲染。
- 后续接 shared memory 返回帧；C1 先用 PNG smoke 验证协议。
- 环境变量开关：`KROK_SUBTITLE_NATIVE_RENDER=1`。
- native 不可用时自动 fallback Python。

#### C1 实装状态（2026-06-25）

实现位置：

- [`native/subtitle_renderer/CMakeLists.txt`](../native/subtitle_renderer/CMakeLists.txt)
- [`native/subtitle_renderer/src/main.cpp`](../native/subtitle_renderer/src/main.cpp)
- [`krok_helper/subtitle_render/native_protocol.py`](../krok_helper/subtitle_render/native_protocol.py)
- [`krok_helper/subtitle_render/native_backend.py`](../krok_helper/subtitle_render/native_backend.py)
- [`scripts/run_native_renderer_smoke.ps1`](../scripts/run_native_renderer_smoke.ps1)
- [`tests/test_subtitle_render_native_protocol.py`](../tests/test_subtitle_render_native_protocol.py)

当前协议：

```json
{"cmd":"configure","ir":{"schema":1,"screen":{"width":640,"height":360,"fps":60},"style":{},"track":{"lines":[],"rubies":[]}}}
{"cmd":"render_frame","t_ms":900,"output_path":"C:/Temp/native-smoke.png"}
{"cmd":"shutdown"}
```

sidecar 启动后会先输出：

```json
{"event":"ready","ok":true,"qt":"6.10.0","schema":1}
```

已验证：

- `C:\Python314\python.exe -m pytest tests\test_subtitle_render_native_protocol.py`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_native_renderer_smoke.ps1`

通信硬化状态：

- Python wrapper 后台排空 stderr，避免 sidecar 大量 Qt/font 日志把 stderr pipe 写满后互锁。
- stdout 响应读取带 timeout；sidecar 卡住时会抛 `NativeRendererError`，不再无限 `readline()`。
- stdout 上的非协议杂行会被记录为 noise 并跳过；正式响应仍要求是带 `event` 字段的 JSON object。
- `close()` 发送 shutdown 后会 `wait(timeout=...)`，必要时 terminate/kill，避免 Windows 上遗留僵尸进程或 pipe 句柄。
- 自动化测试使用 fake sidecar 覆盖 noisy stdout、大量 stderr、正常 round-trip 和卡死 timeout，不依赖本机 Qt/native 构建。

smoke 输出示例：

```text
{'event': 'configured', 'fps': 60, 'height': 360, 'line_count': 1, 'ok': True, 'ruby_count': 1, 'width': 640}
{'checksum': '1371571811376282518', 'event': 'frame_ready', 'height': 360, 'ok': True, ...}
```

刻意未做：

- 尚未接入 `preview_async.py` 或 `renderer.py`。
- 尚未实现 shared memory ring buffer。
- native 侧只消费 Render IR 的基础字段，用于验证协议接缝；C2 已替换 C1 的 `afterText` 字符串拼接，
  但仍未覆盖完整 Python painter 语义。

### C2：普通横排路径迁移

范围：

- 单行/双行横排。
- before/after 填充。
- stroke/stroke2。
- 基础扫光 clip。
- 无 ruby、无 signal、无 `utopia`。

验收：

- 增加 Python renderer vs native renderer 的像素回归测试。
- 普通工程预览/导出能用 native 路径。
- 输出与 Python 旧路径视觉差异可解释，最好逐像素一致。

#### C2 实装状态（2026-06-25）

已完成第一段普通横排 native 迁移：

- native 侧不再用 `afterText` 前缀字符串重建已唱路径。
- before/after 共用同一条整行 `QPainterPath`，已唱层只通过当前时间计算出的横向 clip 叠加。
- clip 依据每个 `TimingChar` 的 `start_ms` / `pause_release_ms` / 下一字符起点 / 行尾时间做线性推进。
- 已解析并使用 `letter_spacing_px`、`stroke2_width_px`、`line_y_margin_px`、`line_gap_px`、`line_y_position`、`dual_line_layout`、`right_to_left` 等基础横排字段。
- after 填充使用纯色 `fill_color`，font weight 读取 `font_weight`，stroke2 颜色跟随 Render IR 的 `karaoke_colors` 矩阵，不再沿用 C1 smoke 的蓝色渐变、写死 DemiBold 或固定黑色 stroke2。
- `render_frame` 临时返回 `line_x`、`line_width`、`baseline_y`、`after_clip_left/right/top/height`、`visible_lines` 诊断字段，用于 smoke 阶段验证 clip 行为。
- `render_frame` 同时返回 `line_diagnostics[]`，覆盖双行 lane 0 / lane 1 的 x、width、baseline 与 after clip 诊断；旧的首行字段继续保留，便于 smoke 与旧测试兼容。
- `scripts/run_native_renderer_smoke.ps1` 会渲染 0ms / 200ms / 900ms / 1800ms 四帧，并断言 after clip 单调推进、
  末帧接近整行宽度，且 `line_x` / `line_width` / `baseline_y` / `after_clip_right/top/height` 与 Python `_layout_line` / `_fill_clip_band` / C3a visual extent 的几何结果接近。
- `tests/test_subtitle_render_native_protocol.py` 已加入同类 pytest：本机或 CI 里存在 native exe 时自动跑几何回归，不存在则 `pytest.skip`。
- 已补 Python renderer vs native renderer 的普通横排像素级回归，覆盖单行与双行；双行测试明确验证 lane 1 下行 x 与 baseline 堆叠。
- 已修复 `afterStroke2Color` fallback：`karaoke_colors.after.stroke2` 缺省时回到 after stroke2 自身默认，不再错误继承 before stroke2。
- 已建立 direct vs bake 的 painter 侧像素基线：`KROK_SUBTITLE_HORIZONTAL_LAYER=0` 可禁用横排 layer bake，作为后续 native parity 的矢量 oracle。

仍未完成：

- 尚未接入 `preview_async.py` 或导出路径。
- ruby 不在 C2 绘制，完整 ruby layout/timing 放到 C3。
- 基础 glow / shadow 纯色装饰已在 C3b-6 迁移；ruby PaintFill 的 gradient/split/image 已在 C3b-7a/C3b-7b 迁移。role/singer override 已在 C3b-8 迁移；signal、entry/exit animation、`utopia` 尚未迁移。

##### C2 已知偏差 / 待办

1. **glow/shadow 本体与 glow bitmap cache 已迁移。**
   C3a 已把 native after-clip 纵向 extent 改为复刻 painter 的 visual extent 口径：
   `max(_visual_stroke_extent, after_glow_extent, |shadow_dy|, 2) + 4`，并把 smoke / pytest 的 `after_clip_top/height`
   期望值从旧的 stroke-only 自比升级为 painter 公式真比。C3b-6 已补 native 实际绘制 glow/shadow 的路径与像素 diff；
   C3a 后续补上 native 侧 glow blur bitmap LRU：按待模糊 source bitmap 内容 hash、尺寸与 radius 复用
   `QGraphicsBlurEffect` 的输出，`frame_ready` 暂时返回 `glow_cache_hits` / `glow_cache_misses` /
   `glow_cache_size` 诊断字段用于验收。

### C3：ruby 与缓存迁移

范围：

- ruby layout。
- ruby timing。
- before/after layer bake cache。
- glow cache。

#### C3a：visual extent parity（2026-06-25 已启动）

已完成第一刀：

- native 解析 `decoration_kind`、`glow_radius_px`、`glow_before_radius_px`、`glow_after_radius_px`、`shadow_offset_x/y`。
- native after-clip 的纵向 top/height 使用 painter 同口径 visual extent：
  `max(stroke/stroke2 extent, after glow extent, |shadow_dy|, 2) + 4`。
- smoke 与 pytest 均改用 painter 公式作为期望值，新增 glow / shadow 参数化测试，避免再次退回 stroke-only 自比。
- C3b-6 已在 native 中实际绘制基础 glow / shadow 纯色装饰，并覆盖 ruby 场景的 Python-vs-native 像素 diff。
- native 已迁移 glow blur bitmap LRU 缓存；重复渲染同一 glow frame 时 `glow_cache_hits` 增加、miss 不增加，
  并保持输出 checksum 不变。

仍未完成：

- `utopia` 上正 glyph run 级 glow bitmap transform 路径尚未迁移；当前 native 缓存先覆盖普通 glow blur 结果复用。

#### C3b：横排 ruby layout/timing（2026-06-25 已启动）

已完成第一刀：

- native 复刻横排 ruby 的目标字匹配：时间范围命中、`kanji` 文本 span 匹配、preferred indices 选择。
- native 复刻 ruby effective timing：当 `@Ruby` 时间范围与实际目标字范围不一致时，把 reading timing rebased 到目标字范围。
- native 复刻 ruby reading progress：支持普通 `reading_part_ms` 分段与 pause pair 形式。
- native `render_frame` 新增 `ruby_diagnostics[]`，包含 `kanji`、`reading`、`indices`、`x`、`baseline_y`、`target_width`、`reading_width`、`progress`、after clip 几何。
- native 普通横排路径已实际绘制 ruby before/after 两层。
- C3b-2 已迁移 ruby 专属配色：`ruby_color` legacy 路径、`ruby_karaoke_colors` 优先级，以及无独立 ruby matrix 时回退主 `karaoke_colors`。
- pytest 已覆盖 ruby layout/timing 几何 parity，并增加 native 带 ruby / 不带 ruby 输出不同的像素 smoke。
- pytest 已覆盖 `ruby_color` 与 `ruby_karaoke_colors` 会改变 native ruby 像素输出。
- C3b-3 已建立受限 ruby Python-vs-native 像素 diff：纯色、无 stroke/glow/shadow、普通横排、全唱完时刻，native 输出落入现有 bounded diff 容差。
- C3b-4 已建立 ruby 中间扫光态 Python-vs-native 像素 diff：native 主字 after clip 改为复刻 painter 的 fill segment 逻辑，
  ruby 覆盖的主字组会作为一个连续 segment 按 rebased ruby progress 推进，而不是继续按单字 timing 推进。
- C3b-5 已建立 ruby stroke/stroke2 的 Python-vs-native 像素 diff：覆盖中间扫光态与全唱完时刻，验证 ruby 缩放后的描边宽度、
  stroke2 宽度和 clip padding 均落入严格 bounded diff 容差。
- C3b-6 已建立 ruby glow/shadow 的 Python-vs-native 像素 diff：覆盖中间扫光态与全唱完时刻；native 解析 `shadow` layer 颜色，
  并按 Python `_paint_text_layer_stack()` 顺序绘制 glow/shadow、stroke/stroke2 与填充层。
- C3b-7a 已建立 ruby PaintFill 的 gradient/split Python-vs-native 像素 diff：覆盖 `gradient_horizontal` 与
  `split_vertical` 的中间扫光态与全唱完时刻；native 改用 `PaintFillSpec` + `QBrush` 绘制 text/stroke/stroke2/shadow 层，
  不再把这些模式降级为纯色 `color`。
- C3b-7b 已建立 ruby PaintFill 的 image Python-vs-native 像素 diff：覆盖中间扫光态与全唱完时刻；native 解析
  `image_path` / `image_scale_pct`，并将 ruby before/after 绘制切到与 Python 一致的局部 `QImage` 层后再贴回主画布，
  同时关闭 ruby 局部层的 `SmoothPixmapTransform`，避免纹理 tile 被插值成混色。native 侧已对解码后的 image fill
  `QImage` 做 64 项 LRU 缓存，避免每个 fill/stroke/ruby 层重复从磁盘读取纹理。
- C3b-8 已迁移横排 native 的 `singer_style_overrides` 与 `custom_style_schemes`：native 会先按行套歌手方案，再在带
  `role_label` 的行内按字套角色方案，重算逐字 font/width/path 并按角色样式绘制 before/after 层；ruby 继续跟随
  行级（含 singer override）样式。实现上已把 `(singer_id, role_label)` 的解析结果提升到 `configure` 作用域的
  `ResolvedStyle` cache，`render_frame` / `layoutLine` 只查表并存轻量指针，不再逐字复制整份 `RenderConfig`。
  pytest 已覆盖 singer override + ruby、inline role override + ruby 的 Python-vs-native 像素 diff。

仍未完成：

- ruby 的 utopia transition 与竖排路径尚未迁移。

验收：

- 覆盖现有 ruby 测例，特别是多汉字共用 ruby、假名组合、mora timing。
- 重复帧/相邻帧 cache 命中率可观测。

### C4：utopia 迁移

范围：

- entry/wipe/exit `utopia` 动画状态。
- 字符级 transform。
- ruby 与主字联动。
- glow bitmap transform 路径。
- 按 `字幕渲染-管线优化调研.md` §9.6 的原语契约落地：effect 只产出 `opacity` /
  `translate` / `clip` / `affine` / 栅格参数，绘制与缓存共用底层 path/layer 逻辑，避免做成
  utopia-only 死路。

#### C4 分步计划（2026-06-25）

- **C4-1：native 主文本 utopia primitive/vector 路径。** 解析 `entry_anim` / `exit_anim` /
  `entry_lead_ms` / `exit_fade_ms`，在 native 侧复刻主文本 entry / wipe / exit 的
  `AnimationState`（opacity + affine），先以逐字/单行 group 的矢量 path 绘制保证 parity；
  不在本刀迁移 ruby transition，也不引入 body bitmap/量子化缓存。
- **C4-2：ruby 与主字 group 联动。** 把 ruby 覆盖的主字组、ruby reading units、退场整组行为
  接入同一 scope；scope_id 继续按单行拆分，不做跨行联合 group。
- **C4-3：utopia glow transform cache。** 在 C3a blur LRU 基础上迁移 Python 的上正 run/scope glow
  bitmap transform 语义，避免 utopia 下重复高斯，同时保留 body 矢量锐利路径。
- **C4-4：真实重工程 benchmark 与调优。** 用 `utopia + glow + ruby` 场景比较 Python 与 native，
  再决定是否需要量子化重栅或更粗粒度 composite cache。

#### C4-1 实装状态（2026-06-25 进行中）

- 已实现：native 解析 `entry_anim` / `exit_anim` / `entry_lead_ms` / `exit_fade_ms`。
- 已实现：主文本 utopia active window 与 entry / wipe / exit 的 `AnimationState` 原语
  （opacity + affine）计算。
- 已实现：普通横排、非 inline role 的主文本 utopia 矢量绘制分支；普通路径、inline role 路径继续走旧逻辑。
- 已实现：普通行主文本 entry / wipe / exit 三个关键帧的 Python-vs-native bounded 像素 diff；
  测试使用无 ruby、无 glow、无描边的主文本场景，先锁定动画几何与 opacity。
- 暂不纳入：ruby transition、inline role utopia、utopia glow transform cache。

#### C4-2 实装状态（2026-06-25 进行中）

- 已实现：native 复用现有 ruby target/effective timing，在 utopia 退场阶段把 ruby 覆盖的多字主文本
  作为单行内 group 动画单元；非退场阶段仍逐字处理，避免改变正常扫光语义。
- 已实现：native ruby reading 按 visual units 逐单元套 utopia entry / wipe / exit transform；ruby 继续复用
  现有 target/effective timing 与 ruby 专属 PaintFill。
- 已实现：主字 group + ruby reading 的 entry / wipe / exit 三个关键帧 Python-vs-native bounded 像素 diff；
  测试使用无 glow、无描边场景，先锁定 group/reading 动画几何与 opacity。
- 暂不纳入：跨行 group、inline role utopia。

#### C4-3 实装状态（2026-06-25 已完成第一刀）

- 已实现：native transformed text stack 支持传入上正 `QPainterPath` / `QRectF` / `QTransform`，
  在 `utopia` 路径下先按上正 glyph/group/ruby path 构建 glow bitmap，再用同一 transform blit；
  主体文字、描边、扫光填充仍保持矢量绘制。
- 已实现：主文本单字/多字 ruby group、ruby reading visual unit、ruby 退场 group 均接入同一
  transformed glow cache 路径；非 `utopia` 路径继续使用原有逐帧绘制/基础 blur LRU。
- 已实现：`KROK_SUBTITLE_NATIVE_GLOW_CACHE=0` 或 `KROK_SUBTITLE_GLOW_CACHE=0` 时回退到旧的
  transformed path glow 绘制，便于 A/B 与紧急回退。
- 已验证：新增 native 测试覆盖不同 `utopia` transform 帧复用同一上正 glow layer，第二帧
  `glow_cache_hits` 增加且 `glow_cache_misses` / cache size 不增长。
- 已验证：新增 `utopia + ruby group + glow + stroke` Python-vs-native bounded 像素 diff，确保缓存
  只改变 glow 生成位置，不改变可见语义。
- 暂不纳入：跨行 group、inline role utopia、真实重工程 benchmark、预览/导出接线。

#### C4-4 实装状态（2026-06-26 已启动）

- 已新增 `scripts/bench_native_renderer.py`，用同一个 `.yurika` 工程、`TimingTrack` 与 `Style`
  对比 Python `paint_frame()` 和 native sidecar 的逐帧耗时，并输出 CSV。
- 默认项目沿用本机 `D:\カラオケ\songs\A stain\A stain.yurika`，默认强制覆盖为
  `entry_anim=utopia` / `exit_anim=utopia` / `decoration_kind=glow`，可用
  `--keep-project-style` 保留项目原样式。
- 已新增 native `render_frame_stats` 协议与 Python `NativeRendererProcess.render_frame_stats()`：sidecar 渲染一帧并返回
  `render_ms`、checksum、line/ruby diagnostics 与 glow cache 计数，但不做 PNG 编码/磁盘写入。旧的
  `render_frame` + `output_path` PNG smoke 协议继续保留，并同样返回 `render_ms` 便于拆分“渲染成本”和“落盘成本”。
- `scripts/bench_native_renderer.py` 默认改用 `--native-mode stats`；需要旧 smoke 路径时可显式传
  `--native-mode png`。CSV 现在同时输出 native roundtrip 与 sidecar 内部 render-only 耗时。
- C4-4b 已把 benchmark 升级为可定位慢帧的工具：`--cache on|off|both` 可自动跑 glow cache 对照；
  summary CSV 一行对应一个 cache 模式；新增 samples CSV，逐帧输出 `frame_index`、`t_ms`、Python/native 耗时、
  native render-only 耗时、glow cache 累计值与逐帧 delta。默认 samples 文件与 summary 同名加 `_samples`，
  也可用 `--samples-out` 指定。
- 旧 PNG smoke（2026-06-26，本机 A stain，1920x1080，4 帧 + 2 帧 warmup）：
  Python mean 约 9.36ms，native mean 约 95.28ms，native glow cache hits=61 / misses=12。
  该结果主要反映 C1 PNG 输出协议开销，不代表最终 preview/export throughput。
- stats smoke（2026-06-26，本机 A stain，1920x1080，4 帧 + 2 帧 warmup）：Python mean 约 7.69ms，
  native roundtrip mean 约 6.07ms，native render-only mean 约 5.37ms，native glow cache hits=61 / misses=12。
  这说明 C4-3 后 native 单帧渲染成本已经能低于当前 Python 单进程路径；旧 PNG smoke 的 80-95ms 主要来自
  PNG 编码/磁盘写入，不应作为 preview/export throughput 判据。
- PNG compat smoke（2026-06-26，本机 A stain，1920x1080，2 帧 + 1 帧 warmup）：native roundtrip mean
  约 80.70ms，但同一响应里的 render-only mean 约 5.19ms，进一步确认落盘协议是主要污染项。
- C4-4b smoke（2026-06-26，本机 A stain，1920x1080，3 帧 + 1 帧 warmup，`--cache both`）：
  cache on 时 Python mean 约 7.44ms、native render-only mean 约 5.95ms、sample cache delta `+30/+6`；
  cache off 时 Python mean 约 7.74ms、native render-only mean 约 6.96ms。该短窗口只作工具烟测，
  正式判断仍应使用 60 帧以上窗口和 samples CSV 排查慢帧。
- C4-4b 60 帧初测（2026-06-26，本机 A stain，1920x1080，60 帧 + 10 帧 warmup，`--cache both`）：
  cache on 时 Python mean 约 10.07ms、native render-only mean 约 7.72ms（render-only 约 1.30x）、
  sample cache delta `+1563/+219`；cache off 时 native render-only mean 约 13.26ms。samples CSV 显示
  cache on 的慢帧集中在 92200-92470ms 附近，慢帧仍伴随每帧约 6-7 个 glow miss；cache off 同窗口会升到
  16-17ms。下一步优化应优先分析这些 miss 的 key 稳定性与可复用粒度，而不是继续看 PNG/IPC。
- 已新增 native glow miss 诊断：`frame_ready` / `frame_stats` 返回 `glow_cache_shape_misses`、
  `glow_cache_content_variant_misses`、`glow_cache_evicted_key_misses` 和最近 miss 摘要；benchmark 的 summary
  与 samples CSV 也输出三类 miss delta。分类含义：`shape` 是 radius/尺寸/格式首次出现；`content` 是同 shape
  但源 bitmap checksum 变化；`evicted` 是曾见过的完整 key 被 LRU 淘汰后再 miss。
- C4-4c 60 帧诊断（2026-06-26，本机 A stain，1920x1080，60 帧 + 10 帧 warmup，`--cache both`）：
  cache on 时 sample miss `+219` 拆为 `shape=51`、`content=168`、`evicted=0`；cache off 仍约
  14.12ms render-only。结论：当前 miss 不是 LRU 容量不足，主要是同尺寸/radius 的 glow source bitmap 内容持续变化。
  下一刀应优先看 utopia/ruby group 的 glow source 内容为何不稳定，或改为缓存更上游的路径/轮廓层，而不是单纯扩大
  `kGlowBitmapCacheMax`。
- 已新增 glow miss scope 诊断：benchmark samples CSV 输出 `cache_scope_miss_delta`，native 响应输出
  `glow_cache_misses_by_scope`。实测确认 content variant 几乎全部来自 `ruby_utopia_reading`，主字仅 1 个 miss。
- 已修复 ruby utopia glow cache 的上正 path 传参：之前 ruby group / ruby reading 先把 path 套上 transform，
  又把 transformed path 作为 cache source 传入，导致每帧 transform 变化都会产生 content miss；现改为绘制仍使用
  transformed path，但 glow cache 使用未变换的 upright path，与主文本路径一致。修复后 60 帧 `--cache both`：
  cache on 时 sample miss 从 `+219` 降到 `+0`，总 miss 仅 warmup 的 `main_utopia_char=1` 与
  `ruby_utopia_reading=1`；native render-only mean 约 8.23ms，cache off render-only mean 约 14.37ms。
- 已新增 benchmark 专用 `render_range_stats` 协议：Python 传入一组 timestamps 与 `threads`，sidecar 内部用
  C++ worker 并行渲染，不落盘，返回 range 总耗时、每帧 render_ms/checksum 与 cache 诊断。`bench_native_renderer.py`
  可用 `--native-mode range --range-threads N` 调用该路径。
- C4-4d range thread pool 初测（2026-06-26，本机 A stain，1920x1080，60 帧 + 10 帧 warmup，cache on）：
  `range:1` 约 8.12ms/帧，`range:2` 约 4.07ms/帧，`range:4` 约 2.40ms/帧，`range:8` 约 1.67ms/帧。
  worker 内单帧 render_ms 会随并发升高（8 线程约 11.97ms），说明存在 Qt/font/cache/CPU 争用，但总吞吐已经明显
  受益于 C++ 多线程。该结果把后续方向重新拉回 range render / preview scheduler，而不是继续深挖 utopia 专项缓存。
- 已新增产品协议雏形 `render_range` / `cancel_generation`：`render_range` 带 `generation`、timestamps 或
  `start_frame/count`、`threads`、`shm_key`、`ring_slots`，sidecar 后台开 C++ worker 渲染，按输出顺序写入
  `QSharedMemory` ring slot，并逐帧输出 `frame_ready` 事件；事件包含 `slot_index`、`slot_count`、`slot_offset`、
  `slot_bytes`、`payload_offset`、`payload_bytes`、`width`、`height`、`stride`、`pixel_format=rgba8888` 等 slot
  信息，最后输出 `range_done`。`cancel_generation` 会标记 generation 取消，worker 后续停止并以 `cancelled=true`
  收尾。Python 侧已新增 `SharedFrameRingReader` / `SharedFrameSlot`：按 `frame_ready` attach `QSharedMemory`，
  校验 64-byte slot header（ready state、generation、frame_index、t_ms、尺寸、stride、format、payload bounds），复制
  RGBA8888 payload，并可生成 detached `QImage`。下一步应把该 consumer 接进 C5 预览调度，补 generation 过滤、
  丢帧策略与 Python renderer 回退。

### C4-5：Python shared memory consumer（2026-06-26）

已完成：

- `krok_helper/subtitle_render/native_backend.py` 新增 `SharedFrameRingReader` / `SharedFrameSlot`。
- `SharedFrameRingReader.from_event(frame_ready)` 会使用事件里的 `shm_key` attach native sidecar 创建的 `QSharedMemory`。
- `read_frame(frame_ready)` 会校验事件必须是 `payload=shared_memory`，再按 `slot_offset` / `payload_offset` /
  `payload_bytes` 复制当前 slot 的 RGBA8888 payload。
- slot header 协议固定为 64 bytes，前 10 个 little-endian int32：`state`、`generation`、`frame_index`、`t_ms`、
  `width`、`height`、`stride`、`format_id`、`payload_offset_in_slot`、`payload_bytes`。当前 `state=2` 表示 ready，
  `format_id=1` 表示 `rgba8888`。
- reader 会对 header 与 `frame_ready` metadata 做一致性校验，避免 ring slot 被覆盖后 Python 误读成当前帧。
- `SharedFrameSlot.to_qimage()` 返回 detached `QImage`，不会把 UI 生命周期绑在 shared memory 指针上。

验证：

```powershell
C:\Python314\python.exe -m pytest tests/test_subtitle_render_native_protocol.py tests/test_subtitle_render_native_benchmark.py -q
```

当前验证结果：`55 passed`。

接手注意：

- fake sidecar 只验证 JSON 协议，不创建真实 shared memory；consumer 的端到端测试必须走真实
  `build/native-renderer/krok_subtitle_renderer.exe`。
- 真实测试是 `tests/test_subtitle_render_native_protocol.py::test_native_render_range_shared_memory_reader_reads_slot_when_exe_exists`。
- 这一步只完成“Python 能读 native ring slot”；尚未接入 `frontend/preview_async.py` / `preview_graphics.py`。
- 下一刀不要继续做 utopia 专项缓存；应进入 C5，把 `render_range` + `SharedFrameRingReader` 接到预览调度。

验收：

- 用真实 `A stain` 或同等重工程对比。
- 预览目标：重场景能稳定接近 60fps，或至少明显高于 Python S2' 同配置。
- 画质重点看正在扫光的字：不能有时间吸附、重影、错位。

### C5：接入预览调度

替换或旁路 `AsyncSubtitleRenderer`：

- Python GUI 发 `request(t_ms)`。
- native 立即调度当前帧。
- 暂停/播放时调度 look-ahead。
- seek 时 generation++，取消旧任务。

保留开关：

- `KROK_SUBTITLE_ASYNC_PREVIEW=0`：同步 Python 回退。
- `KROK_SUBTITLE_NATIVE_RENDER=0`：禁用 native。
- `KROK_SUBTITLE_PREVIEW_MP_FILL=1`：若 native 未启用，可继续使用 S2' Python 多进程方案。

#### C5 初始接线状态（2026-06-26）

已完成：

- `frontend/preview_async.py` 新增 opt-in `NativeAsyncSubtitleRenderer`。当 `KROK_SUBTITLE_NATIVE_RENDER=1` 且能解析到
  sidecar exe 时，预览可通过 native `render_range` 读取 shared-memory `frame_ready` 并向 UI 发 `QImage`。
- `frontend/preview_graphics.py` 会在 async preview 开启时优先选择 native renderer，否则继续使用原 Python
  `AsyncSubtitleRenderer`。
- native preview 使用 generation 过滤过期帧；UI 侧 `_on_async_frame(image, t_ms)` 也会丢弃与当前时间不一致的晚到帧。
- native sidecar 启动、configure、render_range、shared-memory 读取任一阶段失败时，会回退到后台 Python QPainter
  单帧渲染，不阻塞 GUI 线程。
- 播放态 look-ahead 的第一步已接入：`PreviewGraphicsView.set_playing()` 会透传播放状态，native renderer 在播放中
  用 `render_range` 请求当前帧 + 默认 4 个未来帧。
- look-ahead 帧基础缓存已接入：未来帧到达后会按 `t_ms` 存入 `NativePreviewFrameCache`，后续 request 命中时先即时
  emit cached `QImage`，并继续调度新的前瞻窗口。
- 主动取消已接入：`NativeRendererProcess.send_cancel_generation()` 只写入 cancel 命令、不消费 stdout；native preview
  在 seek / style / size / playing 状态导致 generation 前进时，会对当前活跃 generation 发送 cancel。
- 轻量诊断计数已接入：`NativePreviewStats` 可通过 `stats_snapshot()` 读取 cache hit/miss、未来帧缓存数、过期帧丢弃数、
  主动 cancel 次数，用于后续真实工程压测。
- look-ahead 缓存 key 已从裸 `t_ms` 调整为按预览 fps 归一化的 frame bucket，避免 `1016ms` / `1017ms` 这类同一
  视觉帧因为毫秒抖动重复 miss。`A stain` 真素材 3s 高频 seek probe 中，最终统计从约 `hit=49 miss=46` 改善到
  `hit=74 miss=21`。
- 主动取消压测入口已扩展：`probe_native_preview_stats.py` 支持高频 seek、resize churn、style churn，并输出
  sidecar `generation_cancelled` / `range_done` 事件计数。`A stain` 真素材 4s 高频 seek+resize+style probe 可正常退出，
  最终约 `hit=98 miss=37 stale=55 cancel=39 native_cancel=35 done=125`。
- 预览后端对比入口已落地：`compare_preview_backends.py` 可直接驱动 Python `AsyncSubtitleRenderer` 与 native
  `NativeAsyncSubtitleRenderer`，输出请求帧、唯一回帧、重复回帧事件、p95 延迟，并抽样比较两者 subtitle layer 画质。
  `A stain` 真素材 720p/60fps/5s probe 中，Python 为 `ready=296/296 p95=1.57ms`，native 为
  `ready=292/296 p95=0.93ms`，4 个抽样时间点像素差均为 0。
- native preview 已跳过 cache hit 后的当前帧重复渲染：cache 命中会即时 emit cached frame，同时只向 sidecar 请求未来
  look-ahead 帧。`A stain` 720p/60fps/5s 对比中 native 重复回帧事件从约 `291` 降到 `1`，p95 延迟约 `0.77ms`。

#### C5 稳定化状态（2026-06-26，提交 `bf3d9e5`）

本轮目标是追踪并修复 native preview 少量未回帧问题。结论与落地如下：

- `compare_preview_backends.py` 已新增逐 `t_ms` 明细 CSV 与汇总字段：`missing_t_ms`、`duplicate_t_ms`、
  `settle_ready_t_ms`、`leading_missing_frames`、`steady_dropped_frames` 等，用于区分冷启动前导缺帧、稳态丢帧、
  末尾 settle 窗口和重复回帧。
- 首轮诊断确认旧的 `ready=292/296` 全部集中在开头 `0/17/34/51ms`，不是 range 末尾调度，也不是 settle 窗口过短。
- `NativeAsyncSubtitleRenderer` 已调整播放态调度：连续近邻播放 tick 不再每帧推进 generation、取消当前
  look-ahead range；seek / style / size / playing 状态变化仍会推进 generation 并取消旧任务。
- 已新增 waiting bucket：cache 未命中但等待 native look-ahead 兑现的请求会按 preview fps frame bucket 记录；
  native 返回同一视觉帧时，用实际请求的 `t_ms` emit，避免 `1033ms`/`1034ms` 这类毫秒抖动造成假丢帧。
- 已新增 emitted bucket 去重：cache hit、waiting 兑现、当前帧 native 回调共用同一套已兑现 bucket，避免同一视觉帧重复 emit。
- resize 改变渲染目标尺寸时会重启 native sidecar，再 configure 新尺寸，避免尺寸 churn 后复用旧 shared-memory/ring
  状态导致协议边界不稳。
- ring slot 在 Python 读取前被后续帧覆盖时，`SharedFrameRingReader` 的一致性校验会抛 `NativeRendererError`；
  预览调度已将这类单帧错误降级为 `stale_frames_dropped` 并继续读取后续事件，不再把整个 native renderer 标为失败并回退。
- `NativePreviewStats` 新增 `native_renderer_failures`，`probe_native_preview_stats.py` 会输出 `fail=...`，用于确认
  压力测试中是否真正触发 native fallback。

验证结果（本机 `A stain` 真素材，720p/60fps/5s）：

```text
python: ready=296/296 events=296 dup=0 leading_miss=0 steady_drop=0
native: ready=296/296 events=296 dup=0 leading_miss=0 steady_drop=0 settle=1
quality: changed=0 max_delta=0
```

其中 `settle=1` 来自最后 `5000ms` 终点帧在脚本停止播放后返回，属于脚本边界口径，不是实际丢帧。

压力 probe（seek + resize + style churn，`--seek-every-ms 120 --resize-every-ms 250 --style-every-ms 400`）已确认
`fail=0` 且 stats 全程推进。典型 5s 结果约为：

```text
hit=182 miss=149 future=868 stale=87 cancel=19 native_cancel=15 done=259 fail=0
```

同配置 15s 回归也保持 `fail=0`，summary/CSV 输出示例：

```text
requests=884 cache_hits=556 cache_misses=426 cache_hit_rate=0.5662
future=2684 stale=204 cancel=48 native_cancel=34 done=779 fail=0
```

当前测试：

```powershell
C:\Python314\python.exe -m pytest tests\test_subtitle_render_transport.py tests\test_subtitle_render_native_benchmark.py tests\test_subtitle_render_native_protocol.py -q
```

结果：`115 passed`。

待继续：

- C5 native preview 基本闭环；发布前若需加固，可跑 30s 正常播放、seek-only、seek+resize+style 三组长时回归，
  记录 fail、stale、cancel、done 是否持续推进。
- 诊断工具已有 summary/CSV；后续若再次出现 `steady_drop > 0`，优先查看 details CSV 的 `missing_t_ms` 分布与
  `native_renderer_failures`。
- 可选增强：补内存占用统计或 per-window 可视化诊断，用于后续调优 look-ahead 缓存大小。

### C6：接入导出

第一阶段：

- Python `renderer.py` 仍负责 ffmpeg 命令与进度。
- native 渲染 RGBA frame/range。
- Python 按序写 ffmpeg stdin。

第二阶段：

- native renderer 可选直接写 rawvideo pipe。
- Python 只负责启动 ffmpeg、取消、日志、错误上报。

### C7：构建与发布

需要新增：

- CMake 构建脚本。
- Windows/macOS CI native build。
- PyInstaller `--add-binary` sidecar。
- 包内容校验。
- smoke test：native sidecar 可启动、可 configure、可渲一帧。

注意：

- 当前 Windows/macOS build script 以 Python/PyInstaller 为主，尚无 native toolchain。
- Qt 版本应与 PyQt6 wheel 自带 Qt **完全一致**（已由版本指纹强制，见 §7.1），避免字体/渲染差异。
- sidecar 自带 Qt runtime 时，要检查包体积与插件裁剪。

#### 谁需要构建 native（重要：不是所有人）

native renderer 是**可选 sidecar**，Python 侧始终保留 fallback 渲染器，native 路径由 env 开关 opt-in。
因此构建前置分人群：

| 人群 | 是否需要 native toolchain / Qt 6.x dev |
|---|---|
| 只改 Python 侧的贡献者（多数） | **不需要**。装 PyQt6 跑 fallback 即可，app 正常工作 |
| 要动 native（C++）的开发者 | 需要：MSVC x64 + CMake/Ninja + 与 PyQt6 同版本的可构建 Qt（aqt 或官方安装器），构建出 exe |
| 发布 / CI 流水线 | 在 CI 里构建一次 sidecar，PyInstaller 把 exe 打进包 |
| 终端用户 | **不需要**。拿到的是预编译 exe，不装 Qt、不构建任何东西 |

> 即：版本匹配（如当前 6.11）只约束「构建 native 的人 + CI」。以后 PyQt6 升版，也只有这两类需要重建 native，
> 不波及纯 Python 贡献者与用户。这正是选 sidecar 而非 `.pyd` 的收益之一。

#### native 构建 toolchain 前置（2026-06-25 实测）

构建 native（上面第 2、3 类人）需要：

- MSVC x64（Visual Studio Build Tools，C++ 工作负载）、CMake、Ninja。
- **与 PyQt6 完全同版本的可构建 Qt**（dev 文件 + cmake config，PyQt6 wheel 自带的 runtime DLL 不含这些，无法用于构建）。
- 推荐用 `scripts/run_native_renderer_smoke.ps1`：它读 PyQt6 `QT_VERSION_STR`，自动推导/安装对应 Qt、传指纹给 CMake、构建并跑 parity。

> ⚠️ **aqtinstall 版本坑（当前必读）**：Qt 6.11 改了下载仓库结构（内层目录
> `qt6_6110/qt6_6110/` → `qt6_6110/qt6_6110_msvc2022_64/`），**aqtinstall 3.3.0（PyPI 最新）按旧结构拼
> URL 会 404，装不了 6.11/6.12**。修复已在 aqt git main。两种解法：
> 1. 临时用 dev 版：`pip install --user --upgrade "git+https://github.com/miurahr/aqtinstall.git"`（本机已用
>    3.3.1.dev115 成功装 6.11.0）；待 aqt 正式发版带此修复后回退稳定版。
> 2. 用 Qt 官方在线安装器装 6.11.0 / MSVC2022 64-bit，再 `run_native_renderer_smoke.ps1 -QtRoot <路径>`。
>
> 旧版 Qt（如 6.10）不受影响，aqt 3.3.0 仍能装——但那会触发版本指纹 FATAL，不能用于本项目构建。

---

## 6. 与 S2' 多进程方案的关系

两者不是互斥关系：

| 方案 | 定位 | 优点 | 缺点 |
|---|---|---|---|
| S2' Python 多进程 + shm | 短期产品化 | 已探针验证；改动集中在预览调度和 IPC；复用现有 painter | 每进程一套 Python/Qt/cache；工程复杂；仍保留 Python 热路径 |
| C++ sidecar renderer | 中长期主线 | 真多线程潜力；去 Python 热路径；预览/导出统一 native 后端 | 需要 native 构建链；要迁移复杂特效；像素一致性验收重 |

建议顺序：

1. 先落地 S2'，解决当前重 `utopia` 预览卡顿。
2. 同时做 C0 C++ 探针，不影响主线。
3. C0 结果好，再推进 C1-C4。
4. native 路径稳定后，逐步替换 S2'。

---

## 7. 风险清单

1. **Qt 版本差异（已加版本指纹强约束）**
   PyQt6 wheel 与 native Qt 如果版本不同，字体 hinting、路径描边、抗锯齿会产生像素差异。
   **不变量：native Qt 必须 == PyQt6 `QT_VERSION_STR`。** 落地方式：
   - `scripts/run_native_renderer_smoke.ps1` 在构建前读 PyQt6 的 `QT_VERSION_STR`，据此推导 `QtRoot`、
     安装/使用对应版本，并把版本号通过 `-DKROK_EXPECTED_QT_VERSION` 传给 CMake。
   - `native/subtitle_renderer/CMakeLists.txt` 断言 `Qt6_VERSION VERSION_EQUAL KROK_EXPECTED_QT_VERSION`，
     不一致直接 `FATAL_ERROR`（已实测：6.10 配 expected=6.11 会构建失败）。
   - 含义：以后升 PyQt6 时，**必须同步把 native 重建到相同 Qt 版本**，否则构建直接挡下；
     parity 测试的容差因此只用于吸收舍入噪声，而不是悄悄掩盖跨版本度量差。
   - 历史背景：C0/C1/C2 早期 native 按 6.10.0 构建，而 PyQt6 自带 6.11.0；几何 parity 当时是靠 4px
     容差吸收了 6.10↔6.11 的差异。指纹约束就是为了堵住这种静默漂移。

2. **字体一致性**
   Windows/macOS 字体枚举、fallback、CJK/ruby 字形 fallback 都要与现有 Python 路径尽量一致。

3. **像素回归成本高**
   `utopia` 是时间连续动画，不适合宽松缓存；测试必须覆盖关键时间点与真实工程。

4. **shared memory 生命周期**
   Windows/macOS API 不同，必须处理异常退出、slot 泄漏、generation 过期、ring 满、取消任务。

5. **sidecar 打包体积**
   Qt runtime 可能显著增加包体积。需要评估能否复用 PyQt6 bundled Qt，或接受 sidecar 自带 Qt。

6. **跨平台构建复杂度**
   当前发版流程没有 CMake/native extension。CI、local build、release runbook 都要更新。

7. **崩溃与 fallback**
   native renderer 必须有 watchdog。崩溃后回退 Python，不应让整个工作台闪退。

---

## 8. 近期可执行任务

1. 新增 `scripts/probe_native_qpainter_parallel/` 或 `native/subtitle_renderer_probe/`，实现 C0 探针。
2. 固定一个真实重工程 benchmark：`utopia + glow + ruby`，记录 Python 单线程、Python S2'、C++ 探针三组数据。
3. 设计 Render IR v1，先覆盖普通横排 + ruby + 基础 style。
4. 新增 `native_backend.py` 空壳与 env 开关，确保 fallback 语义先定下来。
5. 更新 `docs/字幕渲染-管线优化调研.md` 的 §10.7：把“C++ native renderer”列为 S4/L 路线，明确它与 S2' 的关系。

---

## 9. 当前决策

**不建议立刻重写整个 `engine/painter.py`。**

推荐决策：

- 产品短期：继续推进 S2' 多进程共享内存预览池，尽快解决首播重特效掉帧。
- 技术中期：开 C++ sidecar renderer 探针，若多线程扩展成立，再逐步迁移热路径。
- 工程原则：Python renderer 永远保留 fallback，native 路径用 env/设置开关灰度启用。

一句话版本：**S2' 先救火，C++ sidecar 拆墙；不要用一次性大重写赌整个字幕模块。**
