# 字幕渲染核心 C++ 化方案

> 本文是对 [`字幕渲染-管线优化调研.md`](字幕渲染-管线优化调研.md) 与
> [`字幕渲染-预渲染帧缓存方案评估.md`](字幕渲染-预渲染帧缓存方案评估.md) 的后续决策记录。
> 目标是把“是否、为什么、以及怎样把渲染核心 C++ 化”持久化，避免后续接手者重新从探针结果里拼上下文。
>
> 更新时间：2026-06-25

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
- native 侧只消费 Render IR 的基础字段，用于验证协议接缝；当前 `afterText` 字符串拼接只是 toy renderer。
  C2 必须重写为整行 path + 时间/字符 clip 的模型，不能继承 C1 的简单 `addText(afterText)` 路径。

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

### C3：ruby 与缓存迁移

范围：

- ruby layout。
- ruby timing。
- before/after layer bake cache。
- glow cache。

验收：

- 覆盖现有 ruby 测例，特别是多汉字共用 ruby、假名组合、mora timing。
- 重复帧/相邻帧 cache 命中率可观测。

### C4：utopia 迁移

范围：

- entry/wipe/exit `utopia` 动画状态。
- 字符级 transform。
- ruby 与主字联动。
- glow bitmap transform 路径。

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
- Qt 版本应尽量与 PyQt6 wheel 自带 Qt 主版本一致，避免字体/渲染差异过大。
- sidecar 自带 Qt runtime 时，要检查包体积与插件裁剪。

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

1. **Qt 版本差异**
   PyQt6 wheel 与 native Qt 如果版本不同，字体 hinting、路径描边、抗锯齿可能产生像素差异。

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
