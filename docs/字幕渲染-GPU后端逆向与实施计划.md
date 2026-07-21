# 字幕渲染 GPU 后端：NicoKaraMaker3 逆向结论与实施计划

> 状态：G0～G5 已完成，Windows 交互会话默认使用 G5；G6 DirectComposition 已停止且产品入口硬关闭；G7 render-core 性能专项
> 第 1～3 项（发光行级化、scratch 常驻化、导出流水线化）已完成并达标，第 4 项预览质量档位待产品排期
> 最后更新：2026-07-20  
> 逆向基准：NicoKaraMaker3 10.74.80.0 x64  
> 产品基线：Windows 默认启用 G5 shared-memory/QImage 预览与 GPU 字幕导出，Python QPainter 永久作为 oracle 与 fallback

本文用于把 2026-07-18～2026-07-19 对 NicoKaraMaker3（下称 N3）GPU
预览/导出管线的逆向结论，以及 Karaoke Studio 后续 C++ Direct2D 后端的实施方法持久化。
新会话若要继续 GPU 工作，应先读本文，再读：

1. [`字幕渲染核心C++化方案.md`](字幕渲染核心C++化方案.md)：已有 CPU QPainter sidecar、Render IR、共享内存 ring 与历史 benchmark；
2. [`字幕渲染-管线优化调研.md`](字幕渲染-管线优化调研.md)：Python/GIL、LayerCompositor、预览同步与 GPU/CPU 差异；
3. [`字幕渲染模块-需求设计.md`](字幕渲染模块-需求设计.md)：当前正式功能、产品边界与导出约束；
4. [`NicokaraMaker3字幕布局逆向.md`](NicokaraMaker3字幕布局逆向.md)：N3 字符布局、描边与 Direct2D glow 语义。

---

## 0. 决策摘要

### 0.1 已确认决策

- 若目标是从根本上绕过 Python GIL，并获得类似 N3 的 GPU 字幕预览，采用
  **Windows C++ sidecar + Direct3D 11 + Direct2D + DirectWrite**。
- 不重写 Python 编辑器、工程模型、导入导出、属性面板与撤销/重做；C++ 只负责
  Render IR 消费、布局/资源缓存、逐帧动画求值、GPU 绘制和帧交付。
- 复用现有 native sidecar 的协议、共享内存、取消、generation、benchmark 与 fallback
  基础设施，但**不把现有 C++ QPainter CPU renderer 直接改名当作 GPU renderer**。
- CPU QPainter 永久保留为正确性 oracle、无 GPU 环境 fallback 和 macOS 正式路径。
- 第一阶段只做“透明 GPU 字幕层 + CPU 回读到已有 shared-memory ring”，验证收益后才做
  D3D11 shared texture / 原生 SwapChain 零回读预览。
- Windows 交互会话默认开启稳定的 G5 shared-memory/QImage GPU 预览与 GPU 字幕导出；G6 DirectComposition
  不再继续，`gpu_native_preview_enabled()` 硬返回 `False`，旧环境变量也不能进入 G6。
  `KROK_SUBTITLE_GPU_PREVIEW=0` 可关闭 G5，能力检查或运行失败时自动回退 Painter。

### 0.2 当前仍未完成的事情

- G6 原生预览已停止且硬关闭；后续预览优化只在 G5/Painter 路径进行；
- 尚未完成 AMD/Intel、多显示器/DPR 切换、真实 device-removed 与 30 分钟视频播放矩阵；
- G7 第 4 项预览质量档位（0.25/0.5/1.0 预览缩放）尚未实施，待产品侧确认交互后排期；
  第 1～3 项已完成：4K utopia+发光+ruby render mean 23.87ms→2.74ms（60.3fps），4K GPU
  导出吞吐 1.54x（见 §5 G7 与进度日志第四十三批）；
- 尚未承诺 GPU 与 CPU 逐像素完全一致；
- 尚未改变“QPainter 离屏 + ffmpeg rawvideo pipe”为当前唯一正式路径的产品事实；
- 尚未选择跨平台 GPU 方案；首期明确 Windows-only，macOS 继续 CPU。

---

## 1. 已核实的 N3 GPU 架构

以下结论来自 `NicoKaraMaker3.dll` 的 .NET 反编译结果，并由本机 N3 日志交叉验证。
反编译材料当前保存在仓库 gitignored 的 `.reverse/n3_decomp/`，不能将反编译源码复制进产品；
实现必须采用 clean-room：只复现公开 API 可实现的架构和可观察行为。

### 1.1 技术栈

N3 10.74.80.0 使用：

- .NET 8 / C# / WinUI 3；
- Vortice.Direct3D11 3.5.0；
- Vortice.Direct2D1 3.5.0；
- Vortice.DirectWrite、DXGI、Media Foundation、XAudio2、WIC；
- ffmpeg 负责 MP4 编码。

它不是 MSDF/SDF 字形图集或自定义文字 shader，而是**GPU-backed Direct2D 矢量路径渲染器**。

### 1.2 设备初始化

`DirectXResources` 的行为：

1. 创建 D3D11 Feature Level 11.1/11.0 设备；
2. 默认按 `GpuPreference.HighPerformance` 选择非软件适配器；
3. 硬件设备创建失败时回退 D3D11 WARP；
4. 创建 multithreaded Direct2D Factory 和 Direct2D Device；
5. 创建 DirectWrite Factory、系统字体集合与 WIC Factory；
6. 调用 `ID3D11Multithread.SetMultithreadProtected(true)`；
7. Media Foundation DXGI Device Manager 与同一个 D3D11 Device 绑定。

本机运行日志确认 N3 选择了 `NVIDIA GeForce RTX 3070 Ti Laptop GPU`，预览/导出实际报告
“映像スレッド数: 8”。

### 1.3 字形和样式资源

N3 的主要预计算路径：

```text
DirectWrite FontFace
  -> GetGlyphIndices
  -> GetGlyphRunOutline
  -> ID2D1PathGeometry
  -> 字符位置变换后的 ID2D1TransformedGeometry
```

- 字符宽度会结合 glyph outline bounds、left/right side bearing 与 advance width 计算；
- 字体缺字时遍历 fallback FontFace；
- 纯色、渐变、千层渐变和图片填充会变成对应的 Direct2D Brush；
- MP4/AVI 导出开始前额外生成 filled/stroked/stroked2 `GeometryRealization`；
- 预览至少复用已经生成的字形 PathGeometry/TransformedGeometry，逐帧不重新解析字体文件。

### 1.4 预览线程模型

N3 没有 Python GIL。CLR 的 `TaskCreationOptions.LongRunning` 会运行在可并行的 OS 线程上。
一个播放器包含：

```text
WinUI 主线程
  ├─ UI、按钮、尺寸、100ms 进度条更新
  ├─ XAudio2 长驻音频线程（主时钟）
  ├─ VideoPreviewer 长驻调度线程（约 5ms 轮询）
  └─ 1～8 个 ProcessImageAsync 长驻渲染 worker
       ├─ 每 worker 独立 ID2D1DeviceContext1
       ├─ 每 worker 独立 RGBA premultiplied GPU target
       ├─ 每 worker 独立 blur/shadow work bitmap
       └─ 每 worker 一个 AutoResetEvent + frame ring slot
```

线程数为：

```text
UseMultiThread ? min(Environment.ProcessorCount / 2, 8) : 1
```

D3D11 multithread protection 和单 GPU command queue 仍可能串行化一部分提交；8 个 worker 的价值主要是：

- CPU 侧没有全局解释器锁；
- 解码、字幕命令生成、GPU 执行和呈现形成流水线；
- 同时保留多张正在处理/待呈现的帧；
- 某个线程等待驱动或 GPU 时，其他线程可继续准备后续帧。

### 1.5 预览逐帧数据流

```text
XAudio2 + Stopwatch 提供 Position
  -> VideoPreviewer.SetRingIfNeeded()
  -> Media Foundation SourceReader.ReadSample()
  -> IMFMediaBuffer / IMFDXGIBuffer / IDXGISurface
  -> ID2D1Bitmap shared bitmap（无 CPU 视频帧拷贝）
  -> 唤醒对应 ProcessImageAsync worker
  -> worker BeginDraw
       1. DrawBitmap(video)
       2. 遍历当前可见歌词/标题
       3. SubtitleAction.DrawLineInfo
       4. blur/shadow/描边/正文/ruby
     EndDraw
  -> IsBitmapTaskDone = true
  -> 调度线程检查 audio Position >= frame PTS
  -> DrawBitmap(worker target -> swap-chain target)
  -> IDXGISwapChain1.Present(0)
```

音频是主时钟。若音频已经超过某帧的结束时间，N3 会跳过落后视频 sample，直到重新追上音频；
它不会为了等字幕而阻塞音频。

### 1.6 预览没有 GPU 回读

N3 预览使用 WinUI `SwapChainPanel`：

- 两个 swap-chain buffer；
- `R8G8B8A8_UNorm`；
- `FlipSequential`；
- `Present(0)`；
- worker GPU target 最终直接画入 swap-chain GPU surface。

预览路径没有 WIC/BMP/`byte[]`。这是 N3 预览效率高的重要原因。

### 1.7 预览质量档位

N3 的三档 preview scale 是 `0.25 / 0.5 / 1.0`。源视频和字幕在缩放后的 GPU target 上统一合成。
因此 4K 工程的低/中档实际只渲染 540p/1080p，流畅度不能全部归因于 GPU。

### 1.8 导出不是零拷贝

N3 导出仍然是：

```text
GPU 完整视频帧
  -> WIC BMP 内存编码
  -> 跳过 54-byte BMP header
  -> CPU byte[] + 翻转行
  -> ffmpeg stdin（bgra rawvideo）
```

即“GPU 光栅化/合成 + CPU 全帧回读 + ffmpeg 编码”。我们不应照搬 WIC BMP 中转；首期 GPU
导出/预览回读应使用 staging texture + `Map`，并继续利用现有字幕 strip/bands 减少传输量。

---

## 2. 当前 Karaoke Studio 的事实基线

### 2.1 正式产品路径

- 预览：`AsyncSubtitleRenderer` 在单个 QThread 中把字幕画到 QImage，GUI blit；
- 导出：Python QPainter 产生透明 RGBA 字幕帧，经 rawvideo pipe 交给 ffmpeg；
- 导出已有空帧短路、buffer 复用、单条 strip、多 bands 和 multiprocessing；
- 视频解码/播放由 Qt Multimedia，导出背景缩放/overlay/编码由 ffmpeg；
- native preview 仅有环境变量实验入口且默认关闭；native export 在产品代码中硬返回 false。

### 2.2 已证明的 GIL 边界

PyQt QThread 池实测约 1.07x，关键 QPainter/PyQt 调用期间无法获得有效线程并行；导出使用
multiprocessing 可以绕开 GIL，但预览用多进程会增加调度、shared memory、seek generation 和销毁复杂度。

### 2.3 已有 native 资产

可复用：

- `krok_helper/subtitle_render/native_protocol.py`：Render IR v1；
- `krok_helper/subtitle_render/native_backend.py`：JSON-lines sidecar client；
- `krok_helper/subtitle_render/engine/native_export.py`：range render/shared-memory adapter；
- `native/subtitle_renderer/`：CMake、Qt C++ sidecar、共享内存 frame ring；
- `native/subtitle_renderer/src/backends/`：`RenderBackend` 合同与 G0 Direct2D/D3D11 backend；
- native protocol/benchmark/export/transport 测试；
- generation、取消、乱序 frame ready 重排、超时和进程退出诊断。

仍不可直接复用为正式 GPU 字幕实现：

- 历史字幕语义核心仍是 QImage + QPainter CPU raster；G0 Direct2D 当前只画固定探针；
- 其缓存 parity 不完整，且 vertical/title/signal 等没有达到当前 Python 功能全集；
- 2026-07-11 后产品策略已硬关闭 native 路径；
- `NativeAsyncSubtitleRenderer` 的 range 调度策略存在积压失控缺陷（§2.5），G2 不得原样照搬。

### 2.4 当前性能参考

`.bench/bench_render_20260717_162928.csv` 的 1080p `full` 微基准：

- paint mean：约 4.63ms；
- alloc/fill：约 1.22ms；
- copy：约 1.06ms；
- total mean：约 6.91ms，纯字幕理论约 144.7fps。

这不是端到端视频预览/导出速度。GPU 项目必须证明它改善的不是一个已经足够快的微基准，而是
真实重工程中的预览刷新、主线程响应、4K/120fps 或动态重特效瓶颈。

### 2.5 CPU native 预览 2～3fps 崩溃的根因（2026-07-19 复盘）

历史事实：CPU native 预览在真实应用中播放只有 2～3fps、几乎不可用，这是当时放弃 native
路径的直接原因。2026-07-19 代码复盘的结论是：**底层基础设施（JSON-lines 协议、shared memory
slot 校验、进程封装、generation/取消原语）健康；崩溃来自预览调度策略层的结构性缺陷。**
QPainter 渲染慢只是把系统推过临界点的诱因；把约 1.5x 的慢放大成约 20x 崩溃的是调度器。

失控机制（死亡螺旋，三个设计的交互）：

1. `NativeAsyncSubtitleRenderer._render_native` 以「当前帧 + 6 帧前瞻」为一个 range，
   **阻塞消费**整个 range 直到 `range_done`。前瞻窗口固定 7 帧 ≈ 117ms（60fps）。
   一旦 range 端到端耗时超过该窗口，range 完成时 GUI 当前时间已跑出缓存覆盖范围，
   整个 range 的产出全部作废。
2. miss 的请求进入 `_waiting_request_by_key`，`_include_waiting_timestamps` 会把所有未兑现
   的旧请求追加进下一个 range，**没有上限、没有过期淘汰**。range 越滚越大，每轮更慢。
3. 唯一清空积压的途径是 generation 推进，但连续播放的 tick（+16ms）永远不满足
   `_should_advance_generation_for_request_locked` 的推进条件——播放不停，螺旋不退出。

稳态表现：每个 range 花数百毫秒渲十几个陈旧时间戳，只有少数帧作为迟到的 waiting 请求
发给 UI（内容已过期数百毫秒），用户看到 2～3fps 的过期画面。

四个放大器：

- **ring 覆写丢帧**：sidecar 发射器按 `nextEmit % slotCount` 写槽，从不等 Python 读完；
  积压使 range 超过槽数后覆写未读槽 → Python 一致性校验失败 → 帧被当 stale 丢弃 →
  waiting 永远兑现不了，反哺螺旋。
- **每个 range 新建一批 `std::thread`**，而 layout cache 是 `thread_local`——预览每
  ~100ms 一个 range，布局缓存随线程死亡永远是冷的。benchmark 用单个 range 跑 60 帧，
  因此测不出来。
- **每个 range 新建整块 shared memory**：`shm_key` 带 uuid，每次 create + memset 一块
  ~66MB（1080p × 8 槽）段，Python 侧反复 attach/detach。
- **Python 消费路径每帧拷贝 3～4 次** 8MB（shm→bytes→QImage→copy→缓存 store/take copy），
  且消费与渲染串行。

为什么当时探针全绿、真实应用崩溃：这是一个**双稳态系统**。720p 离屏探针单帧快、无视频
解码争核，range 耗时始终小于前瞻窗口，螺旋不启动，命中率 0.9594；真实应用 1080p + 真实
样式 + Qt Multimedia 解码争核 + 上述放大器，一旦越过临界点即单向失控且无法自愈。
离屏探针测到的是好的那个稳态。

结论：当时「native 比 Python 还卡」**不构成 native 渲染能力不行的证据**（render-only
benchmark 达标，range:8 约 1.67ms/帧）；作废的是调度策略。该复盘转化为 §5 G2 的调度硬性
要求。

---

## 3. 目标架构

### 3.1 进程边界

首选 sidecar，不首选 `.pyd`：

```text
Python / PyQt 主进程
  ├─ 工程模型、导入、编辑、UI
  ├─ 生成 Render IR
  ├─ play/pause/seek/style generation
  ├─ CPU renderer oracle + fallback
  └─ NativeRendererProcess client
               │ JSON-lines control + shared memory / shared texture
               ▼
C++ renderer sidecar
  ├─ RenderRuntime / generation
  ├─ LayoutCache / FontCache / GeometryCache / BrushCache
  ├─ RenderBackend interface
  │    ├─ QPainterCpuBackend（仅实验/对照，可后续拆出）
  │    └─ Direct2DGpuBackend（新）
  ├─ frame scheduler / ring / cancellation
  └─ D3D11 device-loss recovery
```

sidecar 的收益：native/驱动崩溃不直接带崩 PyQt；可独立重启；CPU fallback 清晰；不把 COM/D3D
生命周期塞进 Python 解释器与 Qt teardown 顺序。

### 3.2 C++ 目录建议

不要继续把实现堆进 `native/subtitle_renderer/src/main.cpp`。G0 开始先拆骨架：

```text
native/subtitle_renderer/
├─ CMakeLists.txt
├─ src/
│  ├─ main.cpp
│  ├─ protocol/
│  │  ├─ json_protocol.cpp/.h
│  │  └─ render_ir.cpp/.h
│  ├─ runtime/
│  │  ├─ render_runtime.cpp/.h
│  │  ├─ frame_scheduler.cpp/.h
│  │  └─ shared_frame_ring.cpp/.h
│  └─ backends/
│     ├─ render_backend.h
│     └─ direct2d/
│        ├─ d2d_backend.cpp/.h
│        ├─ d2d_device.cpp/.h
│        ├─ d2d_text_geometry.cpp/.h
│        ├─ d2d_layer_renderer.cpp/.h
│        └─ d2d_readback.cpp/.h
└─ tests/
```

第一刀不要求完整搬迁旧 `main.cpp`，但新增 Direct2D 代码必须进入独立目录和接口，避免形成第二个巨石。

### 3.3 Backend 最小合同

伪接口：

```cpp
struct RenderSurface {
    int width;
    int height;
    int stride;
    PixelFormat format;       // 首期 RGBA8888 straight alpha
    std::span<const std::byte> cpuBytes;
    // 后续：D3D11 shared handle / fence metadata
};

class RenderBackend {
public:
    virtual ~RenderBackend() = default;
    virtual BackendCaps capabilities() const = 0;
    virtual void configure(const RenderIR& ir) = 0;
    virtual void invalidate(const InvalidationSet& changes) = 0;
    virtual RenderSurface renderFrame(int64_t tMs, uint64_t generation) = 0;
    virtual void cancel(uint64_t generation) = 0;
};
```

首期 `renderFrame` 可以同步返回已回读数据；外层 scheduler 负责多槽、乱序完成和取消。后期共享 GPU
纹理时扩展 surface metadata，不改变 Python 高层协议语义。

### 3.4 数据更新原则

- 工程加载、字体/样式/布局变化：发送完整 IR 或可验证的增量 invalidation；
- 播放 tick：只发送 `t_ms + generation`，不重复发送每个字符和 path；
- native configure 阶段生成 font face、glyph geometry、line layout、静态 layer key；
- 每帧只求 clip、opacity、transform、当前颜色状态和需要动态绘制的 signal/action；
- seek/style change 增加 generation，旧帧即使完成也不得呈现。

---

## 4. Direct2D backend 设计

### 4.1 图形资源

- 单个 D3D11 Device + DXGI Adapter；优先高性能 adapter，可配置；
- Direct2D multithreaded Factory/Device；
- 每个 worker 独立 `ID2D1DeviceContext` 和 target texture；
- 所有 target 统一 `R8G8B8A8_UNorm`；Direct2D 内部使用 premultiplied alpha；
- CPU 交付前必须显式转换为现有协议要求的 straight RGBA8888，或协议增加 alpha mode，禁止隐式混用；
- 设备创建失败时返回结构化错误，由 Python 切回 CPU，而不是静默黑屏。

### 4.2 字体和几何缓存

建议缓存层级：

```text
FontKey -> IDWriteFontFace
GlyphKey(font, glyph, size, orientation) -> PathGeometry
GlyphStyleKey(glyph geometry, stroke widths) -> GeometryRealization
LineLayoutKey(track/style/layout/DPR) -> positioned glyphs + ruby + wipe segments
LayerKey(fill/stroke/glow/state) -> reusable GPU bitmap/effect input
```

首期只做字体、glyph path、line layout 和静态 glow/source cache；每增加一种缓存必须有：

- 完整 key；
- style/font/DPR/viewport invalidation；
- 项目切换和 renderer restart 清理；
- hit/miss/bytes/eviction 诊断。

### 4.3 绘制顺序

必须沿用 Python/N3 已验证的层顺序，而不是按 API 方便程度重排：

1. shadow/glow；
2. outer stroke2；
3. stroke；
4. body fill；
5. before/after karaoke clip；
6. ruby 对应层；
7. signal/title/action overlay。

复杂 alpha fill 下要保留 body-protect/clip 语义，避免描边透入半透明正文。

### 4.4 Glow

- Direct2D `GaussianBlur.StandardDeviation` 对齐 N3 语义；
- 三档浓度复用同一个 glow source，以不同 sigma 多次模糊并 SourceOver；
- 大半径使用 Direct2D 默认 `Balanced` 作为首个 N3 对照基准；
- 静态 before/after glow 单独缓存；
- utopia 等变换效果优先变换已缓存的上正 glow source/bitmap，body 保持矢量清晰；
- 每种优化都必须通过 N3/Python 对照帧，不能只看速度。

### 4.5 首期 readback

不使用 N3 的 WIC BMP 中转：

1. GPU target texture；
2. `CopyResource`/`CopySubresourceRegion` 到 staging texture；
3. fence/query 确认完成；
4. `Map`；
5. 按 row pitch 写入 shared-memory slot；
6. premultiplied/straight alpha 转换；
7. 发布 `frame_ready`。

优先支持字幕 strip/bands，避免固定回读完整 1080p/4K 透明帧。

### 4.6 后期零回读预览

仅在 G0～G3 达标后研究：

- D3D11 shared texture handle；
- keyed mutex 或 fence；
- PyQt/native QWidget 中的 D3D swap chain；
- 或一个嵌入式 native child HWND；
- resize/DPR/device lost/窗口销毁协议。

不要在 G0 阶段同时解决 Qt 视频解码、统一音频主时钟和共享纹理，否则无法隔离收益与故障。

---

## 5. 分阶段实施计划

所有阶段都必须保持 CPU 产品路径可运行。每阶段完成后更新本文“进度日志”。

### G0：环境与最小 GPU 探针（3～5 天，已完成）

目标：证明当前构建机、PyInstaller 目录和 RTX/AMD/Intel 环境能稳定启动 Direct2D sidecar。

交付物：

- `RenderBackend` 接口和 `Direct2DGpuBackend` 最小骨架；
- adapter 枚举、硬件设备、WARP fallback；
- 透明 texture + 矩形/单个固定 glyph；
- staging readback 到 RGBA shared-memory slot；
- `backend_info`/`render_probe` 协议事件；
- adapter、feature level、WARP、render/readback ms 日志；
- 不接产品 UI。

验收：

- NVIDIA/当前机器 hardware path smoke；
- 强制 WARP smoke；
- 1000 帧无泄漏、无 device error；
- 输出尺寸、stride、alpha、颜色通道测试通过；
- sidecar 异常退出时 Python 获得明确错误。

### G1：横排字幕核心（1～2 周，已完成）

覆盖：

- DirectWrite 字体选择和 fallback；
- 横排主字、Latin 独立字体；
- solid before/after fill；
- stroke/stroke2；
- 逐字 wipe clip；
- line alignment、letter spacing、N3 bearing/width；
- 基础 glow；
- geometry/layout cache。

暂不覆盖 ruby、角色混排、图片填充、动画、标题、signal、vertical/RTL。

验收：

- 固定字体集合下与 N3 关键帧做视觉对照；
- 与 Python oracle 做 bounded pixel diff；
- 1080p60 连续 10 秒无 frame protocol 错误；
- render/readback 分项计时可导出 CSV。

### G2：实验预览接线（1～2 周，已完成）

覆盖：

- 复用 `NativeRendererProcess`/generation/cancel/ring；
- Python 只发送 timestamp；
- latest-wins + look-ahead；
- GPU 失败自动回退 Python `AsyncSubtitleRenderer`；
- 仅开发环境显式开关，产品默认硬关闭；
- seek、resize、style churn、工程切换、关闭窗口 teardown。

首期允许 GPU readback -> shared memory -> QImage。此阶段测量读回是否吞掉 GPU 收益。

调度硬性要求（源自 §2.5 CPU 预览崩溃复盘，逐条必须满足，不满足不得进入验收）：

1. **积压有上限**：永远不渲染早于当前请求时间的帧；waiting/backlog 必须有容量上限和
   过期淘汰，任何情况下 range/队列长度不得无界增长；
2. **最新优先**：调度以最新请求时间戳为最高优先级，不得按 range 顺序阻塞消费旧帧；
3. **ring 流控**：单次在途帧数 ≤ ring 槽数，或发射端等待消费确认后才可覆写槽位；
   不允许"覆写未读槽 → 校验失败 → 当 stale 丢弃"作为常态路径；
4. **资源常驻**：worker 池、shared memory 段、（GPU 侧）device/context 跨 range 常驻，
   不随单次请求重建；所有缓存不得绑定在短命线程的 `thread_local` 上；
5. **消费轻量**：Python 侧每帧从 shared memory 到可显示 QImage 至多一次完整像素拷贝；
6. **失控自愈**：若端到端 ready latency 连续超过前瞻窗口，调度器必须能主动降级
   （缩小前瞻/丢弃积压/推进 generation），不得进入不可自愈的慢稳态。

验收：

- 1080p60 普通横排稳定 60fps；
- 重 glow 场景字幕 ready rate、p95 latency、steady drop 优于 Python；
- 连续播放 30 分钟、seek 500 次、resize/style churn 无崩溃；
- Python GUI 主线程响应不随字幕 paint 增长；
- renderer kill/restart/fallback 可恢复；
- **以上指标必须在真实 GUI + 真实视频播放下测量**；离屏探针只作回归信号，不作验收
  依据（§2.5：C5 离屏探针全绿但真实应用 2～3fps 的教训）；
- 人为注入慢帧（如强制单帧 sleep 200ms）后，恢复时间有界，不进入 §2.5 式慢稳态。

### G3：常用功能达到可用 MVP（2～4 周，已完成）

覆盖：

- ruby layout/timing/wipe；
- singer/role override、行内混合字体/字号/配色；
- gradient/split/image fill；
- shadow、三档 glow、stroke2 `UseEdge2`；
- 标题和多个字幕源的常用横排路径；
- strip/bands readback。

验收：

- TACTIC 等 N3 样例关键帧；
- A stain 等真实重工程；
- Python/GPU raw subtitle overlay 抽帧 diff；
- cache hit/miss/bytes 和 GPU memory 稳态；
- 4K60 common path 达到本文性能门槛。

### G4：高级功能 parity（1～3 个月，已完成）

按风险顺序逐个迁移：

1. char fade / line fade / slide；
2. spin/flip；
3. utopia 主字；
4. utopia ruby/group/transformed glow；
5. Sayatoo signal；
6. vertical + vertical ruby；
7. RTL；
8. viewport transform、跨行 scope、全部标题路径。

每项必须单独有 feature capability；未实现项让整个 frame/project 回退 CPU，禁止 GPU/CPU 在同一字幕层里
悄悄混画造成顺序和 alpha 差异。

### G5：GPU 导出实验（2～4 周，已完成；默认关闭）

- GPU 只渲染透明 subtitle strip/bands；
- staging readback 后仍交现有 ffmpeg overlay/encoder；
- 不在第一版重写背景视频解码；
- 不把“GPU 字幕渲染”和“NVENC/QSV/AMF 编码”混为一个开关；
- 保留 60/120fps、取消、进度、半成品删除和 CPU fallback。

达到收益门槛后，再评估 D3D11 texture -> hardware encoder/filter 的 vendor-specific 零拷贝方案。

### G6：共享 GPU 纹理/原生预览（可选，2～4 周起）

目标是去掉 G2 的 GPU readback/QImage blit，接近 N3 的 SwapChain 路径。必须单独立项，因为它会涉及
PyQt/native HWND、Qt Multimedia、统一视频时钟和 teardown，不是 Direct2D 字幕 backend 的必要前置。

首批实现采用 **sidecar-owned DirectComposition child HWND**：Qt viewport 只提供父 HWND 与物理几何，
Direct2D frame target 通过同一 D3D11 device GPU→GPU copy 到 premultiplied composition swap chain，
不经过 staging texture、shared memory 或 QImage。现有统一播放器/音频时钟仍是唯一时间源，Python 每个
tick 只提交最新 `t_ms`。本节保留历史实现记录；产品入口现已硬关闭，环境变量不能重新启用 G6。

首批验收覆盖：

- 跨进程父子 HWND、resize、负坐标裁切、透明 premultiplied swap chain 与显式 teardown；
- 单在途 + 单 pending latest-wins；原生呈现不创建 shared-memory reader/QImage；
- sidecar kill 后 Painter 当前帧回退、一秒有界冷却、GPU device/window/cache 重建；
- GPU↔Painter 重复切换不遗留 worker/child HWND；
- 独立记录 render/present/roundtrip，原生路径 `readback_ms` 必须恒为 0。

剩余验收：真实视频 30 分钟连续播放、多显示器/DPR/最小化恢复、AMD/Intel、真实 device removed/reset，
以及复杂 Utopia/signal/viewport 组合的 render-core 优化（后者已单独立项为 G7）。G6 只移除了回读/QImage
瓶颈，不会掩盖复杂场景本身超过帧预算的问题。

### G7：render-core 性能专项（2026-07-20 立项；第 1～3 项已完成，第 4 项待产品排期）

背景：4K + utopia + 发光在真实工程中约 20fps、几乎不可播放。2026-07-20 的隔离基准
（RTX 3070 Ti 硬件、bands 回读、`scripts/benchmark_gpu_renderer.py`）把差距精确拆开：

| 4K 场景 | fps | render mean/p95 |
|---|---|---|
| 仅 utopia | 57.7 | 2.19 / 7.45ms |
| 仅发光 | 54.8 | 3.59 / 6.51ms |
| utopia + 发光 | 23.6 | 21.73 / 43.91ms |
| utopia + 发光 + ruby | 20.5 | 23.87 / 50.50ms |
| 普通走字 + ruby | 79.7 | 1.95 / 2.40ms |
| 普通走字 + ruby + 发光 | 54.0 | 3.15 / 3.72ms |

根因（与 N3 逆向对照，均已在反编译源中核实）：

- **我们**：utopia/spin × 发光组合走 per-char `InlineGlowLayer`——每字符 × {before, after} 每帧
  `CreateBitmap`（4K 单张 33MB）+ `CreateEffect` + 全画布 Clear + 至多 3 次全画布高斯模糊；一行
  10 字 + ruby 即每帧 30～60 次 4K 模糊与 20+ 次显存分配（warmup 本地显存峰值 759MB 即此洪流）。
  行级 glow source / ruby glow source 在普通路径同样每帧重建（`renderFrame` 内约 3803/3979/4191 行）。
- **N3**：每 worker 启动时 `CreateCompatibleRenderTarget` 一张**常驻** work bitmap（尺寸为预览缩放后的
  `ScaledMovieInfo`），全帧全行复用；utopia 逐字符动画只是 `CreateTransformedGeometry` 把变换烘进几何、
  `PushOpacityLayer` 控透明度，零位图成本；发光按**整行**处理（`DrawOneLineDecorBlurMulti`），每行
  `BlurLevel+1`（≤3）次模糊，与字符数无关。同一行我们做 30～60 次模糊 + 20+ 次分配，N3 做 3 次 + 0 次。

普通走字结论：render 核心 4K 已达标（1.9～3.2ms，120fps 预算内），与 N3 无实质差距；基准环路
18.5ms/帧中约 9ms 是 JSON IPC/共享内存消费/Python 拷贝，预览侧已被 G6 绕过，仅导出（G5 串行
render→readback→展开→ffmpeg）仍受其约束。

目标与顺序（clean-room 对齐 N3，不复制反编译源码）：

1. **utopia/spin 发光行级化**（已完成，2026-07-20）：实施中发现"把动画变换烘进几何后整行统一模糊"
   会改变缩放动画期的发光外扩（Painter 语义是"先模糊上正字形、再变换模糊结果"，oracle 18px 容差
   下 spin/utopia 入出场均越界），因此最终架构为：**本帧动画矩阵为恒等的字符共享行级 glow source**
   （普通行走合并源，行内混排/ruby 按样式分组），**仅本帧确实带变换的字符/ruby 单元**（通常是正在
   bounce 的 1～2 个，入出场窗口内更多）保留逐字符"模糊后变换"层。稳态唱字帧因此不再逐字符摊开。
   验收达成：4K utopia+发光+ruby render mean 2.74ms/p95 5.82ms、60.3fps（门槛 ≤6ms/≥55fps）；
   GPU oracle 套件 126 passed 全绿。
2. **glow source / GaussianBlur effect 常驻化**（已完成，2026-07-20）：scratch 位图与 blur effect
   池挂在 impl_ 上（上限 8 张，逐行回卷复用，突发超额帧后释放），scene 尺寸变化时失效重建；同时
   scratch Clear 用裁剪限定、合成用 `DrawImage(effect, targetOffset, imageRectangle)` 只请求内容
   矩形，利用 Direct2D effect 图按需求值把每次模糊成本从全画布降到行/字符邻域。验收达成：显存
   稳态增长归零（基准 growth 0～33MB 即至多一张突发 scratch），普通发光 p95 2.68ms（门槛 ≤5ms）。
3. **导出 readback 流水线化**（已完成，2026-07-20）：`gpu_render_frame` 协议新增 `slot_count`
   （1～4，按 `frame_index % slot_count` 写槽并回报真实槽位），Python 客户端拆分 begin/finish，
   `iter_gpu_rgba_frames` 在双槽 ring 上一帧深度流水——sidecar 渲染/回读第 N+1 帧与 Python 侧
   band 展开/RGBA 转换第 N 帧重叠，并有真实 sidecar 逐字节一致性测试钉住语义。验收达成：4K
   utopia+发光+ruby 导出 34.5→53.0fps（1.54x ≥ 1.5x 门槛）。
4. **预览质量档位**（产品功能，可独立排期）：对齐 N3 的 0.25/0.5/1.0 预览缩放，4K 工程低档只渲 540p；
   与渲染优化正交，是弱 GPU 用户成本最低的收益。待产品侧确认交互后排期。

---

## 6. 性能与正确性门槛

### 6.1 必测工程

至少固定三类 corpus：

1. 普通双行 + ruby + glow；
2. TACTIC/N3 风格：三档 glow、蓝白 after、stroke2 开关、7px spacing；
3. 重特效：utopia + ruby group + glow + 多角色。

每类测试：1080p60、4K60；若目标是 120fps，再加 1080p120/4K120。

### 6.2 计时必须拆分

禁止只报总 FPS。至少输出：

- Python request/IPC；
- native layout/animation；
- GPU command recording；
- GPU execution/fence wait；
- readback；
- shared-memory copy；
- QImage 构造/blit；
- end-to-end ready latency；
- missing/duplicate/stale/drop；
- CPU/GPU memory 和 cache hit/miss。

### 6.3 继续投入门槛

G2/G3 后同时满足才进入完整迁移：

- 复杂 4K60 纯字幕 render throughput 至少为当前 CPU 的 2x；
- 含 readback/IPC 的端到端预览或导出至少改善 1.5x；
- 1080p60 重工程 steady drop 接近 0，p95 latency 在帧预算内；
- GUI 主线程不再因字幕 paint 出现可感知阻塞；
- N3/Python 关键帧差异达到已评审阈值；
- NVIDIA、AMD、Intel 至少完成 smoke；
- WARP/CPU fallback、device lost、sidecar restart 可用。

若 GPU render 很快但 readback 后端到端收益不足 1.5x，应停止功能迁移，转而先验证 G6 共享纹理；
不要靠继续堆 feature 掩盖架构瓶颈。

---

## 7. 测试策略

### 7.1 单元测试

- IR 字段与 capability negotiation；
- RGBA/BGRA、stride、premultiplied alpha；
- glyph fallback、bearing、advance、letter spacing；
- wipe segment、ruby timing；
- cache key/invalidation；
- generation/cancel/stale frame；
- adapter failure/WARP/device lost 映射。

### 7.2 图像回归

三套 oracle 分开使用：

- N3 对照帧：验证 N3 兼容目标；
- Python QPainter frame：验证产品现有语义没有意外改变；
- GPU 自身 golden：验证驱动/代码升级没有大范围漂移。

GPU 与 CPU 不要求逐像素完全一致。报告至少包含：

- changed pixel ratio；
- max/mean channel delta；
- alpha edge 差异；
- bounds/基线/走字边界偏差；
- glow extent 和总 alpha；
- 人工检查图（diff heatmap）。

### 7.3 稳定性

- 30 分钟连续播放；
- 高频 seek、暂停/恢复、单帧步进；
- resize/DPR/显示器切换；
- style/font/image fill 热更新；
- 工程切换；
- renderer kill/restart；
- GPU device removed/reset；
- 应用关闭时 sidecar、shared memory、Qt 顶层窗口销毁顺序。

---

## 8. 构建、打包与平台边界

### 8.1 Windows

- MSVC x64 + CMake/Ninja；
- Windows SDK / D3D11 / D2D1 / DWrite；
- DirectX 系统组件不随包重复分发；
- PyInstaller onedir 收集 sidecar exe；
- build script 检查 sidecar 能启动、报告 backend caps、渲染一帧；
- CI 至少跑 WARP smoke，真实 GPU 性能需专用 runner/本机矩阵。

### 8.2 macOS

首期不实现 GPU backend，继续 Python QPainter。Render IR 和 capability 不能假设 Direct2D 必然存在。
若以后需要跨平台 GPU，单独比较 Skia Ganesh/Graphite、Metal 或 Qt RHI；不要让 Direct2D 首期被跨平台抽象拖住。

### 8.3 产品设置

实验期不增加普通用户可见开关；使用开发环境变量或隐藏设置。达到 G3 后再设计中文 UI：

- 自动（GPU 失败回退 CPU）；
- CPU（兼容）；
- GPU 实验/正式；
- 显示实际 adapter/backend；
- GPU 字幕渲染与硬件视频编码分别设置。

---

## 9. 风险与禁区

### 9.1 高风险点

- Direct2D premultiplied alpha 与 ffmpeg/Qt straight alpha 混用导致黑边；
- 字体 fallback 和字体版本导致几何差异；
- 多 worker 共用 Direct2D/D3D 资源的线程安全和内部锁争用；
- GPU readback 强制同步，吞掉光栅化收益；
- 每帧从 Python 发送完整工程数据；
- cache key 缺字段造成跨 style/DPR 复用；
- device lost 后资源和 shared handle 未完全重建；
- Qt/sidecar/shared memory 销毁顺序导致退出崩溃；
- 预览 GPU、导出 CPU 时的观感差异；
- 把硬件编码速度误算为 GPU 字幕渲染收益；
- 预览调度积压失控（已在 CPU native 路径实际发生并导致 2～3fps，见 §2.5；G2 硬性要求
  就是为堵住它设立的）。

### 9.2 禁区

- 不直接复制 N3 反编译源码；
- 不修改 SUG submodule 源码；
- 不在没有 benchmark 的情况下默认开启 GPU；
- 不删除 CPU renderer/fallback；
- 不把 Direct2D 代码写回 Python `painter.py`；
- 不在 G0 同时重写视频播放器、音频时钟和导出器；
- 不恢复现有 CPU native 产品开关来冒充 GPU 进度；
- 不用全帧 WIC BMP 回读作为最终方案；
- 不用一个“GPU”设置同时控制字幕绘制和 NVENC/QSV/AMF。

---

## 10. 下一会话接手步骤

当用户明确要求“开始 GPU 后端/G0”时，新会话按以下顺序执行：

1. `git submodule status`；
2. `git status --short`，保护用户未提交改动；
3. 完整阅读本文；
4. 阅读 `AGENTS.md` §9；
5. 阅读 `字幕渲染核心C++化方案.md` 的 §3～§5、§7、§9；
6. 检查 `native/subtitle_renderer/CMakeLists.txt`、`src/main.cpp`、`native_protocol.py`、
   `native_backend.py`、native tests；
7. 先固定 G0 benchmark 输入和 JSON 事件，不写产品 UI；
8. 把 `RenderBackend`/Direct2D 代码拆到新文件；
9. 实现 adapter/caps + transparent texture + staging readback smoke；
10. 运行 native smoke、相关 pytest 和 1000-frame 稳定性测试；
11. 在本文 §11 更新日期、提交、测试结果、已知差异和下一刀。

如果用户只说“继续 GPU 工作”但没有指定阶段，先复核最新性能数据；G0～G5 已完成并默认使用 G5，G6 已停止且不得继续，后续只考虑 G5/Painter 与导出管线优化。

---

## 11. 进度日志

### 2026-07-19：逆向与方案持久化完成

已完成：

- 确认 N3 10.74.80.0 使用 D3D11 + Direct2D + DirectWrite + Media Foundation + XAudio2；
- 还原 N3 音频主时钟、视频调度线程、1～8 worker ring、DXGI shared bitmap 与 SwapChain 预览；
- 确认 N3 预览无 GPU->CPU 回读，导出有 WIC BMP 全帧回读；
- 用本机日志确认 RTX 3070 Ti adapter 和 8 个映像线程；
- 决定 Windows C++ Direct2D sidecar 路线；
- 决定复用现有协议/ring/fallback，但新建独立 GPU backend；
- 确定先 G0～G2 探针，达标后再完整迁移；
- 尚未修改任何产品代码。

下一步：**G0 环境与最小 GPU 探针**。

### 2026-07-19（补充）：CPU 预览崩溃根因复盘与探针清理

已完成：

- 复盘 CPU native 预览"播放只有 2～3fps"的历史问题，定位为预览调度策略层的积压失控
  （死亡螺旋），底层协议/shared memory/进程管理原语确认健康；完整结论见 §2.5；
- 据此为 G2 新增 6 条调度硬性要求，并把"真实 GUI + 真实视频播放下测量"写入 G2 验收；
- 确认「native 比 Python 卡」不构成 native 渲染能力不行的证据，GPU 路线的预期起点上调；
- 清理已完结的 C0 探针：删除 `native/subtitle_renderer_probe/`、
  `scripts/run_native_qpainter_probe.ps1` 与本机 `build/native-probe/` 产物
  （C0 结论仍保留在 `字幕渲染核心C++化方案.md`）；`.vscode/settings.json` 的 CMake
  源目录改指 `native/subtitle_renderer`；
- 保留 `native/subtitle_renderer/`、Python 协议/客户端/导出 adapter 与全部 native 测试，
  作为 G0 复用地基；QPainter CPU 渲染核心的移除并入 G0 骨架拆分执行。

### 2026-07-19（第三批）：G2 调度硬性要求在 CPU 后端上落地验证

把 §2.5 的调度修复直接实现在现有 CPU sidecar 预览上，作为 G2 调度器的预演。改动：

- `preview_async.py`：waiting 积压不再回灌新 range（要求 1）；range 帧数钳制到
  ring 槽数（要求 3）；新增自适应前瞻 `_effective_lookahead`，range 耗时超过前瞻窗口
  时对半收缩、恢复后逐步回涨（要求 6）；过期 waiting 按帧桶清除；晚于最新请求的帧
  不再进缓存；`NativePreviewFrameCache.take` 去掉冗余拷贝（要求 5 部分）；
- shm ring 常驻（要求 4）：`shm_key` 改为与 renderer 同生命周期，native 侧
  `ensureSharedFrameRing` 对同 key/槽数/尺寸直接复用，不再逐 range create+memset；
- `native_preview_enabled()` 从硬关闭恢复为 `KROK_SUBTITLE_NATIVE_RENDER=1` env
  opt-in（默认仍关闭，产品 UI 不暴露）；`resolve_native_renderer_path` 恢复
  显式参数 > env > 打包 > 构建树的发现顺序；sidecar 启动时自动把匹配 PyQt6 版本的
  aqt Qt bin 注入子进程 PATH。

实测（Dark spiral journey 真实 LRC，1080p60，8s，offscreen compare）：

- 修复前（仅破螺旋，未复用 ring）：native `36.75fps`、`steady_drop=178`、p95 `33.4ms`；
- 修复后：native `58fps`、`steady_drop=8`、p95 `5.9ms`（Python 同场景 `59fps`、p95 `2.6ms`）；
- 抽样像素 diff 全 0；10s seek/resize/style churn 压测 `fail=0`、cancel 正常。

同批发现与处理：

- 恢复 exe 发现后，14 个 Python-vs-native parity 测试（27 个用例）暴露已知漂移：
  native 冻结于 6 月底，未跟进 7 月 Python painter 布局改动（如 N3 字体像素修复），
  bounded diff mean 16~18 超容差 10。已加 `_NATIVE_PARITY_DIVERGED` 条件 skip
  （`KROK_SUBTITLE_NATIVE_PARITY_STRICT=1` 强制运行）；parity 恢复不单独立项，
  并入 GPU G1+ 的对照体系；
- MSVC + Ninja + Qt 6.11.0 构建链复验通过（G0 第 0 刀等效完成）；
  `run_native_renderer_smoke.ps1` 的 `cmd /c` 段在部分环境报 vswhere 不可用，
  分步执行等效命令可绕过，后续可修脚本。

遗留（不阻塞体验，GPU G2 时一并处理）：per-range `std::thread` 重建导致
`thread_local` layout cache 冷启动；Python 消费路径仍有 2~3 次全帧拷贝；
渲染语义为 6 月版本，预览观感与当前 Python painter 有轻微差异。

### 2026-07-19（第四批）：native 预览接入显示分辨率渲染（DPR）

用户实测 4K 工程 native 预览仅 20+fps。根因：Python 预览路径按
`DPR × 场景缩放` 渲染显示分辨率（4K 工程在普通窗口实际只渲 ~1150px 宽），
而 native 接线忽略 DPR、渲染真 4K 并回读 33MB/帧——像素量差 6~11 倍。
这与 N3 的预览质量档位（§1.7，低档 4K 只渲 540p）是同一个问题。

改动：Render IR `screen` 新增 `dpr` 字段（0 视为未设置，钳制 [0.01, 4]）；
native `RenderConfig` 增加 `dpr` 与物理尺寸换算，布局仍在逻辑坐标系、
`QPainter::scale` 缩放光栅化画布；shm ring 按物理尺寸分配；Python 侧
把已存的 `_device_pixel_ratio` 传入 configure，交付 QImage 设置
`setDevicePixelRatio`；fallback 渲染同样按显示分辨率。
`compare_preview_backends.py` 新增 `--dpr` 参数。

实测（Dark spiral journey，utopia+glow，3840×2160，8s，offscreen）：

- `--dpr 0.3`（模拟 4K 工程在普通窗口）：native `58.62fps`、`steady_drop=3`、
  p95 `0.07ms`；Python `59fps`、p95 `2.15ms`；
- `--dpr 1.0` 对照（全 4K 渲染）：native `57.5fps` 但 p95 `30ms`（帧预算边缘）；
- 单帧回读体量从 33MB 降到 ~3MB。

该 `dpr` 协议字段即 G2 预览质量档位的地基；GPU backend 直接沿用。

### 2026-07-19（第五批）：G0 Direct2D 最小 GPU 探针完成

实现：

- 新建 `backends/render_backend.h` 与 `backends/direct2d/`，把 GPU backend 与历史
  `main.cpp` 内的 CPU QPainter 渲染语义隔离；
- 按高性能偏好枚举 DXGI adapter，创建 BGRA-capable D3D11 FL 11.1/11.0 device；
  支持显式 WARP，硬件与 WARP 均启用 `ID3D11Multithread` 保护；
- 创建 multithreaded Direct2D factory/device/context 与 DirectWrite factory；
- 在透明 `B8G8R8A8_UNorm` premultiplied GPU texture 上绘制半透明矩形和固定 glyph；
- staging texture `CopyResource + Map` 回读，转换为 straight-alpha RGBA8888 后写入
  现有 `QSharedMemory` ring；
- 新增 `backend_info` / `render_probe` 协议、Python client、adapter/feature level/
  render/readback 诊断；更新 native smoke，并新增 `scripts/probe_gpu_renderer.py`；
- 未修改产品预览/导出选择逻辑，Python QPainter 仍是唯一正式路径。

本机验收（RTX 3070 Ti Laptop GPU，Windows 11，Qt 6.11.0）：

- hardware：NVIDIA RTX 3070 Ti，D3D FL 11.1；WARP：Microsoft Basic Render Driver，FL 11.1；
- 两条路径的透明像素为 `(0,0,0,0)`；输入 `(51,102,204,128)` 回读为
  `(52,102,203,128)`，误差在 premultiply/unpremultiply 允许的 1 LSB 内；
- 256×144、1000 帧：hardware `1792.74fps`，render/readback mean
  `0.0720/0.1717ms`，warmup 后工作集增长 `0.45MiB`；
- 256×144、1000 帧：WARP `1720.65fps`，render/readback mean
  `0.0570/0.1869ms`，工作集增长 `0.53MiB`；
- 相关回归：`167 passed, 27 skipped`；27 项为已有 CPU native/Python parity 漂移，
  与本次 G0 无关且仍由 G1 对照体系接手；
- `run_native_renderer_smoke.ps1 -RequireHardware` 通过，异常响应/进程退出诊断沿用并通过
  native protocol/transport 测试。

下一步：**G1 横排字幕核心**。先建立 DirectWrite 字体/fallback 与 glyph geometry cache，
再迁移 solid before/after、stroke/stroke2 和逐字 wipe；不提前接产品 UI。

### 2026-07-19（第六批）：G1 第一刀——DirectWrite 轮廓、走字与 N3 glow

实现（仍未接产品 UI）：

- Render IR 经 `gpu_configure` 物化为独立 `RenderScene`，configure 阶段用 DirectWrite
  shaping/fallback 生成并缓存 Direct2D glyph geometry 与逐字 hit-test 范围；
- `gpu_render_frame` 按现有 shared-memory ring 返回 straight RGBA，Python client 新增
  `configure_gpu()` / `render_gpu_frame()`；
- 横排 solid before/after、stroke、严格受 `UseEdge2` 控制的 stroke2、逐字 wipe、
  墨水边界 left/center/right 与 top/center/bottom 已出帧；
- 重新用用户指定的 N3 10.74.80.0 程序集反编译复核
  `SubtitleAction.DrawOneLineDecorBlurMulti()`：发光源只画装饰轮廓，宽度为
  `EdgeSize + DecorSize`，启用二重描边时为 `EdgeSize + EdgeSize2 + DecorSize`；
  Direct2D `GaussianBlur.StandardDeviation` 使用
  `R - floor(i * R / (BlurLevel + 1))`，默认 `Balanced`，各 pass SourceOver；GPU backend
  已按该层序和公式实现；
- G1 GPU 专项 `10 passed`；native CPU/protocol/GPU 联合回归 `43 passed, 27 skipped`。
  hardware/WARP 的差异仅集中于少量抗锯齿边缘，整帧 premultiplied 平均通道误差
  约 `0.038/255`。

本批尚不宣告 G1 完成。首个 GPU↔Python Painter 对照暴露了需要继续收敛的真实差异：
GPU 当前 whole-line DirectWrite shaping 尚未复刻 Painter/N3 的逐字符 outline bounds、
side bearing、advance、`CharGeometryLeftOffset` 和首描边计入步进规则；默认双行布局的 lane
基线也尚未进入 GPU scene。因此下一刀先改为逐字符 glyph geometry/layout cache，并加入
GPU↔Painter 非空帧、边界、wipe 与 premultiplied 像素 bounded-diff 门禁，再继续 ruby/角色。

随后同日完成上述第二刀：

- configure 改为逐 `TimingChar` 建立 DirectWrite layout/fallback，归一不同字体的 baseline，
  按 glyph outline bounds、advance、左右 bearing、`AllowBiting`、`SpaceWidth`、首描边宽度和
  `CharGeometryLeftOffset` 生成 positioned geometry；letter spacing 只加在 timing char 之间；
- 缓存每字符实际墨水 wipe 范围与每行 DirectWrite ascent/descent；单行和默认多 lane 的
  top/center/bottom 基线均按 Painter 行盒公式计算；
- 加入真实系统字体（Meiryo + Times New Roman）的 GPU↔Painter 自动对照：默认双 lane
  solid 帧 alpha 边界各边误差 ≤2px、整帧平均通道差约 `0.56/255`；N3 中档 glow
  alpha 边界误差 ≤2px、平均通道差约 `0.90/255`；
- G1 GPU 专项现为 `12 passed`，native CPU/protocol/GPU 联合回归
  `45 passed, 27 skipped`。

G1 剩余门槛：固定 N3 工程关键帧（不能只用合成文字）、1080p60 连续 10 秒协议稳态、
render/readback CSV，以及 geometry/layout cache 的 hit/miss/bytes 诊断。未完成这些门槛前
仍不接产品预览开关。

同日第三刀补齐工程诊断与稳态探针：

- 相同 RenderScene 重复 configure 直接命中常驻 geometry/layout cache；
  `gpu_configured` 返回 hit/miss、line/char/geometry 数与保守 bytes 估算，专项测试固定
  首次 miss、第二次 hit；
- 新增 `scripts/benchmark_gpu_renderer.py`，可选择 hardware/WARP、solid/glow，逐帧导出
  render/readback/roundtrip/checksum CSV；
- 1920×1080、60fps 时间轴、连续 600 帧均无协议错误：hardware solid `56.52fps`
  （render/readback mean `1.14/6.05ms`，roundtrip p95 `20.77ms`），hardware 中档 glow
  `55.42fps`（`2.01/6.05ms`，roundtrip p95 `21.85ms`），WARP solid `60.50fps`
  （`0.73/5.52ms`，roundtrip p95 `17.68ms`）。本阶段同步 request + 全 1080p readback +
  Python slot copy 已明确是端到端瓶颈；GPU render 本身仍在帧预算内，G2 必须用 latest-wins/
  look-ahead 与显示分辨率/strip readback，不能把这组同步吞吐误当最终预览性能；
- 专项现为 `13 passed`，native CPU/protocol/GPU 联合回归 `46 passed, 27 skipped`。

本机还确认 `Dark spiral journey/出力/off_vocal.mkv` 是可用的 N3 真实 1080p60 参考输出，
并在 15.2s 抽帧对照了走字位置。该帧同时含标题、signal、ruby、渐变和角色样式，属于 G3
组合场景，不能冒充 G1 的纯 solid 固定参考。因此 **G1 尚余且只余“隔离后的 N3 固定关键帧”
这一项视觉门禁**；在生成/固化该 fixture 前保持 G1 未完成、产品开关硬关闭。

### 2026-07-19（第七批）：G1 收口——N3 原生 glyph metrics 与双 oracle 门禁

- 去掉逐字符 `IDWriteTextLayout` 反推 bearing 的近似路径，改为与 N3 相同的
  `IDWriteFontFace.GetGlyphIndices()` → `GetGlyphRunOutline()` →
  `GetDesignGlyphMetrics()`；宽度与 `CharGeometryLeftOffset` 使用 N3 的整数公式，
  path sink 固定 round line join；
- 缺字 fallback 同步 N3 策略：先复用已经成功的 fallback face，再尝试
  `Microsoft JhengHei Bold`，最后以 Bold 扫描系统字体集合；与 N3 一致，只以首个
  UTF-16 glyph index 判断有效性，fallback outline 仍用原请求 face 查询 design metrics；
- 保留“双 oracle”边界：绝对 lane/基线/位置由现有 Painter 门禁负责，glyph 几何、描边和
  glow 由 N3 实帧负责。这样不会为了贴 N3 的独立布局锚点而破坏现有 Painter 产品输出；
- 新增 `scripts/compare_gpu_n3_reference.py`：从 N3 输出帧减去原视频恢复字幕 mask，GPU
  重建工程标题首行，并在有限平移搜索后比较字形 mask；平移只消除布局锚点差异，不参与宽度；
- `Dark spiral journey` 5.0s、1080p60 实帧门禁结果：阈值 24，N3 bbox
  `(48,45)-(1019,104)`，GPU bbox `(43,57)-(1016,104)`，宽度差 `2px`，最佳平移
  `(4,-12)`，mask IoU `0.782841`（门槛 `0.72`），通过；此前 TextLayout 近似路径的
  标题右边界约停在 `x=981`，精确 metrics 已消除累计宽度漂移；
- 精确 DirectWrite bearing 下，Painter solid 整帧平均通道差约 `0.86/255`，glow 约
  `1.24/255`，alpha 边界仍各边 `≤2px`；GPU 专项 `13 passed`。

至此 G1 的功能、Painter bounded diff、hardware/WARP、600 帧稳态性能/诊断和真实 N3
固定关键帧门禁全部完成，**G1 宣告完成**。下一阶段进入 G2；正式产品 GPU 开关仍保持硬关闭。

### 2026-07-19（第八批）：G2 第一刀——有界 latest-wins GPU 预览 worker

- 新增 `GpuAsyncSubtitleRenderer`，仅显式设置 `KROK_SUBTITLE_GPU_PREVIEW=1` 时由
  `PreviewGraphicsView` 选用；该开关独立于历史 CPU native 实验开关，产品设置中不暴露，
  默认仍走 `AsyncSubtitleRenderer`/Painter；
- 调度器固定为“1 个同步在途 + 1 个 pending”，pending 新请求直接覆盖旧请求，不存在 range、
  waiting 字典或无界队列；单测在首帧阻塞期间连续投入 100 个时间戳，实际只渲染首帧和
  最后一帧，`max_pending=1`；
- 播放时只允许一个下一帧 speculative look-ahead，占用同一个 pending 槽；完成后进入两帧
  上限的时间桶缓存，不会自行无限向前渲染；cache hit 可直接交付并继续维持单帧前瞻；
- state/style/viewport generation 变化使旧结果失效；暂停要求时间戳精确一致，播放态只接收
  120ms 有界迟到帧。GPU 创建、configure、逐帧渲染或 shared-memory 读取异常后，worker
  永久降级到同线程 Painter fallback，不把异常带入 GUI；
- `SharedFrameRingReader.read_qimage()` 直接把锁定的 shared-memory RGBA 行复制进最终
  `QImage`，预览消费由 `shm → bytes → QImage.copy()` 两次全帧拷贝降为一次；
- renderer/Direct2D device 和 shared reader 跨普通帧常驻。resize 因 QSharedMemory 命名段
  不能原地改大小而轮换 shm generation key，但不重启 sidecar；真实 WARP worker smoke 已连续
  输出 `640×360` 和 resize 后 `320×180` 两帧，configure 两次、failure/fallback 均为 0，
  stop 后线程正常退出；
- G2/transport + GPU 专项 `83 passed`；未受改动的 native export/protocol 独立回归
  `33 passed, 27 skipped`。若把两组用例反常地放在同一 pytest 进程且 transport 先运行，
  Qt offscreen 字体库初始化顺序会令历史 CPU-native Utopia bounded-diff 用例把 Arial 回退成
  Meiryo；按正常文件顺序/独立进程均通过，本批没有放宽这些历史阈值。

G2 尚未完成：下一刀补 render/readback/ready latency 与 pending replacement 诊断导出，随后跑
真实 GUI + 视频的连续播放、seek、resize/style churn、kill/restart/fallback 和慢帧自愈门禁。

### 2026-07-19（第九批）：G2 第二刀——60fps 稳态传输、自愈与真实组件烟测

- `GpuAsyncSubtitleRenderer` 新增 render/readback/roundtrip/ready-latency 的 mean/p95/max 有界滑动窗口，
  以及 request、pending replacement、stale、fallback、restart 计数；新增
  `scripts/benchmark_gpu_preview_scheduler.py`，可用真实 60Hz 驱动导出 CSV/JSON，并覆盖
  seek burst、resize 和 style churn。
- renderer 异常后立即交付 Painter 回退帧，1 秒有界冷却后自动重建 sidecar/device 并
  configure 当前 generation。实机强制终止子进程后，第二帧走 Painter，第三帧换新 PID
  恢复 GPU：`failures=1`、`restarts=1`、`fallback=1`。
- Direct2D frame target texture/bitmap/staging texture 改为跨帧常驻，仅在物理尺寸变化时重建。
  回读不再逐像素 unpremultiply 为 straight RGBA，而是保留 Direct2D 原生
  `bgra8888_premultiplied`，通过 shared-memory format id 2 交付
  `QImage.Format_ARGB32_Premultiplied`；Painter/CPU 旧协议仍保留 format id 1。
- 产品预览请求关闭每帧 8MiB 全量 checksum，专项 probe/benchmark 默认仍保留 checksum。
  1080p solid 稳态 600/600 帧交付，`render p95=2.82ms`、`readback p95=7.45ms`、
  `roundtrip p95=13.11ms`；中档 glow 为 599/600，`render p95=4.89ms`、
  `roundtrip p95=15.53ms`。
- churn 探针：播放 180/180，500 次 seek burst 只交付最新帧，resize 20/20、style 20/20，
  `requests=721`、`pending_replaced=499`、`max_pending=1`、`failures=0`，
  `roundtrip p95=12.84ms`。premultiplied BGRA 改造后重跑 N3 真实帧，mask IoU 仍为
  `0.782841`、宽度差 `2px`，未以性能换取字形偏差。
- 真实组件链路已用 `PreviewGraphicsView` + Dark Spiral N3 导入样式/LRC + 真实
  1920×1080@60fps MP4 驱动 240 帧：Qt Multimedia `LoadedMedia/NoError`，GPU 交付
  241 帧，`failures=0`、`fallback=0`、`max_pending=1`；稳态 `render p95=2.88ms`、
  `readback p95=5.05ms`、`roundtrip p95=15.69ms`。烟测的 Qt 异常退出还暴露并修复了
  `QSharedMemory` wrapper 先于 worker finally 销毁时的幂等 close 边角。
- 本批回归：GPU/transport `86 passed`；native export/protocol `33 passed, 27 skipped`。
- 人工慢帧门禁已加入同一 scheduler benchmark 与单测：在 1080p 链路内注入一次
  200ms worker 阻塞，阻塞期间继续以 60Hz 提交时间戳；结果旧帧淘汰 2 张、
  `pending_replaced=11`、`max_pending=1`，阻塞解除后 123.08ms 交付最新帧，低于
  250ms 恢复门槛，无 renderer failure/fallback。

G2 仍保持“进行中”：还需完成 30 分钟真实可见 GUI 连续播放，以及
NVIDIA/AMD/Intel 硬件矩阵。产品开关继续默认关闭，Painter 仍是 oracle 和回退。

### 2026-07-19（第十批）：G2 本机收口——30 分钟真实可见 GUI

- 新增 `scripts/stress_gpu_preview_gui.py`，使用真实 `PreviewGraphicsView`、Qt Multimedia、
  N3 导入样式/LRC 和 MP4，可见窗口内按实时视频时钟驱动，每 30 秒输出
  queue/failure/latency/RSS，并在结束时导出 JSON。timer-gap 诊断改为 4096 项固定窗口，
  避免验收工具自身因长时间运行增长内存。
- RTX 3070 Ti 本机可见窗口连续运行 `1800.006s`，Dark Spiral 真实
  1920×1080@60fps 视频每 120 秒回绕：`requests=107945`、`ready=106712`，
  平均交付约 `59.28fps`；`pending_replaced=1228`、`stale=101`、`max_pending=1`。
- 最后 4096 帧窗口：`render p95=4.44ms`、`readback p95=4.49ms`、
  `roundtrip p95=16.27ms`、`ready latency p95=33.14ms`；全程
  `renderer_failures=0`、`renderer_restarts=0`、`fallback_frames=0`，Qt Multimedia
  `LoadedMedia/NoError`。sidecar 全程保持同一 PID，工作集约 89–94MiB 往返波动，
  无单调泄漏或重启迹象。

G2 的本机实现、NVIDIA 硬件、WARP、Painter fallback、kill/restart、200ms 慢帧与
30 分钟真实 GUI 门禁已全部完成。AMD/Intel 矩阵属外部硬件验证，不阻塞进入 G3；
产品 GPU 开关仍默认关闭，Painter 仍为 oracle 与 fallback。

### 2026-07-19（第十一批）：G3 第一刀——ruby 布局、走字与独立发光

- Render IR 为 ruby 增加原始 `reading_parts`，保留连续时间戳形成的空 part；native
  侧据此复刻 Painter 的逐假名区间，空 part 期间 wipe 保持平台，不再把停顿错误均分进
  相邻假名。
- GPU scene 在 configure 阶段把 ruby 映射到正文目标字，分别缓存日文/英数字体的 N3
  原生 glyph geometry、墨水边界、advance 与计时区间；支持
  `auto/center/equal_space` 排列、`ruby_interval_px`、独立字号/字重/间距与拉丁字体。
- ruby 基线按正文 N3 box ascent、ruby box descent 与 `ruby_gap_px` 计算；lane 顶部范围
  把 ruby 纳入，避免居中/顶端布局时正文正确而 ruby 越界。
- sharp 层已覆盖 ruby before/after fill、stroke、严格受开关约束的 stroke2 与独立 wipe；
  glow 层把 before/after 轮廓先分别裁切再按各自半径模糊，支持三档 concentration 和
  N3 多 pass 顺序，不借用正文进度；不同前后半径有独立自动门禁。
- 合成与真实工程的 Painter oracle 均已进入自动门禁：合成 Meiryo 帧四边偏差不超过
  5px；`Dark spiral journey` 24.9s 的真实歌词/ruby 分段帧在 1920×1080 下缓存 5 组
  ruby，四边最大偏差 7px。真实工程原 UD 字体并非测试机系统字体，因此门禁只替换为双方
  均可解析的 Meiryo，保留真实文本、时间、字号、描边和发光。
- RTX 3070 Ti 上对上述真实帧循环 600 帧：render mean/p95 `3.77/4.39ms`，readback
  `3.95/4.76ms`，roundtrip `9.79/10.90ms`，同步吞吐 `102.14fps`；ruby 双发光源
  没有突破 1080p60 帧预算。
- GPU/transport 回归 `93 passed`；native protocol/export/benchmark 单独回归
  `56 passed, 27 skipped`。产品 GPU 开关继续默认关闭，Painter 继续作为正式输出、
  oracle 与异常 fallback。

G3 下一刀：角色/歌手覆盖与行内混合字体、字号、配色；随后依次处理
gradient/split/image fill、标题/多字幕源与 strip/bands readback。

### 2026-07-19（第十二批）：G3 歌手级整行样式覆盖

- `RenderScene` 增加与有效歌词行一一对应的 `lineStyles`；GPU configure 不再假定所有行
  共用一套字体资源，而是按行解析 DirectWrite 正文/拉丁/ruby 字体并缓存完整 N3 glyph
  geometry。未设置覆盖时仍回落全局样式。
- 直接复用 native/Painter 既有的 `resolvedStyleForLine()` 合并结果；歌手方案现在可以覆盖
  正文与 ruby 的字体、字号、字重、间距、before/after 配色、stroke/stroke2、装饰与 glow，
  不在 GPU 侧另造一套合并规则。
- render 阶段从当前有效行取得样式，brush、描边宽度、ruby 独立 glow 和布局参数均随行切换；
  scene cache key 已包含逐行样式，歌手方案变化会产生可观察的 cache miss，不会错误复用旧
  geometry。
- 新门禁同时验证字号/描边使可见几何变大、歌手 before 绿色实际出现在 GPU 像素中、缓存
  正确失效，并以 Painter 约束宽高与中心位置。DirectWrite N3 outline origin 与 Qt Meiryo
  font engine 的纵向原点存在约 10px 固有差异，测试保留 N3 glyph 几何并把 Painter 垂直中心
  漂移限制在 14px；这与 G1 确立的“Painter 管布局、N3 管字形”双 oracle 边界一致。
- 硬件/WARP build smoke 通过；GPU/transport `94 passed`，native
  protocol/export/benchmark `56 passed, 27 skipped`。产品 GPU 开关仍默认关闭。

下一刀继续把同一套 resolved style 从“每行”下沉到“逐字 run”，完成角色标签、行内混合
字体/字号/配色及对应 ruby anchor 规则。

### 2026-07-19（第十三批）：G3 逐字角色 run 与混合样式

- `TextChar` 增加角色样式索引，scene 按“歌手 + 角色标签”去重缓存 `charStyles`；逐字配置
  直接消费既有 `resolvedStyleForCharacter()` 结果，因此角色方案与 Painter 使用同一套继承顺序。
- DirectWrite configure 逐字选择日文/拉丁字体、字号、字重、描边宽度、咬字、空格宽度与字间距；
  行 ascent/descent、visual pad 和 ruby 正文锚点宽度取混合 run 的真实最大值，不再按全局字体估算。
- sharp 层逐字绘制各自 before/after fill、stroke、严格遵守开关的 stroke2，并继续共享整行逐字
  wipe；角色切换不会拆散时间轴或重建前缀字符串。
- glow 按“角色样式 + before/after”分组创建轮廓源，先在 wipe 边界裁切，再按各自半径与三档
  concentration 执行 N3 multi-pass；同角色的多个字共用一组全帧 effect source，避免退化为
  每字两张纹理。缓存诊断新增 `cached_styles` 并把行/角色样式计入 bytes 估算。
- 自动门禁覆盖同一行 Meiryo 92px/6px 描边与 Times New Roman 50px/3px 描边混排、两套
  before/after 配色、Painter 宽高边界，以及角色独立绿色/蓝色 glow。
- RTX 3070 Ti 的 1920×1080、18 字交替双角色、两套重 glow、600 帧压力结果：render
  mean/p95 `2.11/2.49ms`，readback p95 `4.75ms`，roundtrip p95 `8.98ms`，同步吞吐
  `122.46fps`，仍在 60fps 预算内。
- 硬件/WARP build smoke 通过；GPU/transport `96 passed`，native
  protocol/export/benchmark `56 passed, 27 skipped`。

当前角色切片尚未宣告完整：ruby 的角色专属外观与 `affects_ruby_anchor` 选择规则、渐变/split/
image fill 仍待下一刀；产品 GPU 开关继续默认关闭，Painter 继续作为 oracle/fallback。

### 2026-07-19（第十四批）：G3 角色 ruby 与锚点参与规则

- `TextStyle`/resolved style 补齐 `affects_ruby_anchor`。DirectWrite 只让非空白且明确参与的
  混合 run 抬高整行共享 ruby 基线；若全员退出，则与 Painter 一样只回退到实际 ruby 目标字，
  避免无关装饰字符把注音顶出画面。
- `TextRuby` 增加角色样式索引，选择规则严格复用 Painter 语义：按 ruby 的真实目标索引顺序，
  采用第一个带角色标签字符的 resolved style。角色 ruby 的 before/after 配色、stroke/stroke2、
  decoration 和独立 glow 半径/浓度均不再泄漏全局 ruby 样式。
- 保留现有 Painter 的兼容边界：角色方案参与 ruby 排版测量和绘制外观，但最终字形与共享基线
  仍使用行级 ruby font/gap。GPU 因此把角色测量尺寸与行级 DirectWrite outline 分离，未擅自改变
  CPU oracle 已有输出。
- ruby glow 按“角色样式 + before/after”分组，分别在自身 wipe 边界裁切并执行 N3 multi-pass；
  sharp 层也逐 ruby 创建匹配的 fill/stroke brush。角色主配色在没有显式角色 ruby 配色时会覆盖
  全局 ruby 配色，与 Painter 的默认继承规则一致。
- 新门禁覆盖高装饰字符 opt-in/opt-out 的 ruby 位移方向和幅度、角色 ruby 的字体测量/颜色/描边
  Painter 边界，以及角色专属洋红 glow；硬件/WARP build smoke 通过。GPU/transport
  `99 passed`，native protocol/export/benchmark `56 passed, 27 skipped`。

角色/ruby 基础切片至此收口。G3 下一刀进入 gradient/split/image fill；产品 GPU 开关继续默认
关闭，Painter 永久保留为 oracle/fallback。

### 2026-07-19（第十五批）：G3 Direct2D 渐变与硬色带填充

- GPU `TextStyle` 不再只携带退化后的单色，完整传递正文与 ruby 的 before/after
  text/stroke/stroke2/decor `PaintFill`；保留浮点 stop 位置，角色/歌手 resolved style 继续使用同一套
  IR，不在 Direct2D 侧另建项目字段。
- horizontal/vertical gradient 使用共享 N3 fill rect 创建 Direct2D linear-gradient brush；正文 fill rect
  按首字符整数 descent、整行最大 `FontSize + EdgeSize` 和首字符 edge/edge2 inset 计算，ruby 则按
  目标宽度与 Painter 的独立 ruby fill rect 计算，所有文字、描边和 glow 源共享同一坐标系。
- `split_vertical` 按 Painter/N3 的 MilleFeuille 语义展开为同位置的前后颜色 stop，形成无过渡硬边；
  extend mode 使用 wrap 而不是 clamp，因此字形超出 fill rect 时继续循环色带。三段测试的前三个可见
  换色扫描线与 Painter 相差不超过 `1px`。
- 自动门禁另以红/绿/蓝三段 vertical gradient 比较 GPU 与 Painter 的方向、中点和归一化取样，
  关键通道差限制在 `42/255`；硬件/WARP build smoke 通过。GPU/transport `101 passed`，native
  protocol/export/benchmark `56 passed, 27 skipped`。
- RTX 3070 Ti 上 1920×1080、18 字、五 stop gradient 连续 600 帧：render mean/p95
  `1.15/1.48ms`，readback p95 `5.76ms`，同步 roundtrip p95 `17.19ms`，含 Python QImage 交付的
  总吞吐 `64.45fps`；render 核心仍显著低于 60fps 帧预算，正式预览继续由 G2 latest-wins 调度吸收
  个别回读长尾。

G3 下一刀补 image fill 的 Direct2D bitmap brush、全局画布锚点、wrap/scale 与透明纹理 body
protection；产品 GPU 开关继续默认关闭。

### 2026-07-19（第十六批）：G3 WIC/Direct2D 图片填充

- image fill 在 configure 阶段经 WIC 解码为 `32bppPBGRA`，立即复制到独立 Direct2D bitmap，
  不保留 decoder/frame 对源文件的句柄；同一 scene 内按路径、修改时间和文件大小去重，图片覆盖后会
  触发 cache miss、重新解码并立即换色，不需要重启 sidecar。
- bitmap brush 使用双轴 wrap 与 linear interpolation，`image_scale_pct` 按 Painter/N3 的方向直接
  放大纹理；brush transform 抵消当前歌词行的 `dx/dy`，所以纹理固定锚定渲染目标原点，不会在
  before/after wipe、角色 run 或 ruby 间重新起相位。
- 图片填充无条件按 alpha-capable 处理。configure 预生成“主描边 widened geometry 减去字身”的
  外侧轮廓，sharp 层只在字身外绘制 primary stroke；同一规则覆盖正文、行内角色与 ruby，避免
  半透明纹理把内部描边混成脏色。缓存诊断计入图片像素与额外保护 geometry。
- 自动门禁覆盖 8×8 四色纹理的 100%/200% wrap/scale、before/after 帧逐字节同相位、与 Painter
  重叠区域的像素差，另覆盖半透明白图的红色主描边 body protection，以及同路径文件热更新。
  硬件/WARP build smoke 通过；GPU/transport `104 passed`，native protocol/export/benchmark
  `56 passed, 27 skipped`。
- RTX 3070 Ti 上 1920×1080、18 字、175% image fill、4px 描边连续 600 帧：render mean/p95
  `0.53/0.68ms`，readback p95 `7.19ms`，同步 roundtrip p95 `18.11ms`，含 QImage 交付的总吞吐
  `62.44fps`。图片只在 configure 解码，逐帧没有 WIC 或磁盘 I/O；长尾仍来自全帧 readback/copy。

gradient/split/image fill 基础切片至此收口。G3 下一刀进入标题、多字幕源及 Painter 已支持的常用
行特效覆盖；产品 GPU 开关继续默认关闭。

### 2026-07-19（第十七批）：G3 Direct2D 阴影剪影

- 补齐此前仅存在于 resolved style、但 GPU 没有消费的 `shadow_offset_x/y`；角色方案可独立覆盖，
  ruby 另支持 `ruby_shadow_offset_x/y`，未设置时与 Painter 一样继承正文偏移。
- shadow 不是平移一份裸字 fill，而是先按 `stroke + enabled stroke2` 外缘绘制整字剪影，再填充字身，
  对齐 N3 `DrawOneLineDecorShadow` 与 Painter `_paint_shadow_silhouette`。shadow brush 完整支持本阶段的
  solid/gradient/split/image fill；图片阴影仍固定画布原点，不因偏移重新起纹理相位。
- before shadow 先绘制完整源，after shadow 再按正文/ruby wipe 边界覆盖。常用横排路径的颜色分界
  保持在未偏移的 wipe x，而阴影几何本身按 offset 平移；after clip 的纵向范围随阴影移动，避免底部
  残留 before 色。逐字角色 run 使用各自偏移、颜色与描边外缘。
- 自动门禁覆盖角色正文绿色阴影、角色 ruby 洋红阴影的 Painter 中心方向，以及 18px/8px 偏移下
  before 绿/after 蓝 shadow 的左右边界；两种颜色边界与 Painter 相差不超过 `5px`。硬件/WARP
  build smoke 通过；GPU/transport `106 passed`，native protocol/export/benchmark
  `56 passed, 27 skipped`。

G3 的 solid/gradient/split/image、stroke/stroke2、shadow/glow、ruby 与角色常用基础层已齐。下一刀
进入标题与多字幕源横排路径；产品 GPU 开关继续默认关闭。

### 2026-07-19（第十八批）：G3 标题与多字幕源横排合成

- Render IR 显式保留 `track` 与 `extra_tracks` 的源边界；native scene 为每行携带源号与源内行号，
  lane 从每个源的第 0 行独立计算，ruby 也按源隔离，避免相同时间段的副轨注音误绑定主轨正文。
- Direct2D 每帧按“源 + lane”选择活动行，并按 Painter 的主轨歌词 → 标题 → 各副轨顺序执行
  SourceOver 合成；不再把全部来源摊平后只画第一条活动行。主/副轨各自的 `@Offset` 与全局
  `timing_offset_ms` 已同时进入正文、走字和 ruby 时间轴。
- 标题的方案/布局引用、`{title}`/`{artist}` 模板清理、显示窗口和逐字符角色方案直接复用
  Painter 解析函数生成 renderer-ready snapshot；Direct2D 继续复用正文的精确 glyph geometry、
  stroke/stroke2、shadow/glow 与 solid/gradient/split/image brush，不维护第二套简化标题绘制器。
- 常用横排标题覆盖多行、角色混合字体/字号/配色、九宫格 anchor、offset、head/tail/whole/
  head-tail 窗口以及淡入淡出；透明度直接施加到所有 sharp/decor brush，标题只跟主轨绘制一次。
- 自动门禁覆盖多字幕源红/绿叠绘与 Painter 几何、每源独立 offset、标题元数据窗口、0/50/100%
  淡入、窗口外透明、左上锚点，以及多行角色红/绿样式。硬件/WARP build smoke 通过；GPU/transport
  `110 passed`，native protocol 独立回归 `28 passed, 27 skipped`。历史 CPU-native Utopia 像素
  用例仍须按既有约定与 GPU 文件分进程运行，避免 Qt offscreen 字体初始化顺序改变 Arial 回退。

G3 下一刀进入 strip/bands readback、4K60 性能与组合 corpus 门禁；产品 GPU 开关继续默认关闭，
Painter 永久保留为 oracle 与任何不支持路径的整帧 fallback。

### 2026-07-19（第十九批）：G3 packed bands 回读与 4K60 门禁

- Direct2D render 阶段按活动行、ruby、标题和各角色的 stroke/glow/shadow 外扩计算保守纵向区间，
  相交区间合并；readback 不再固定 `CopyResource` 全帧，而是用多个
  `CopySubresourceRegion` 把 band 紧密排入常驻 staging texture 顶部后只 Map/复制有效行。
- shared-memory 增加 `bgra8888_premultiplied_bands` 格式和 `top/height/packed_top` 元数据。
  Python `read_qimage()` 先清透明目标，再把 packed 行直接复制到最终 QImage；没有活动内容时 payload
  为 0 字节。`read_frame()` 诊断路径也会展开为传统全帧 slot，旧调用方不需要理解 band 格式。
- 自动门禁同时显示顶部标题与底部 glow 歌词，确认生成两个不相邻 band、payload 小于全帧 70%，
  展开后与同一帧全量 `CopyResource` 输出逐字节相等；另覆盖空帧 0-byte payload。产品 GPU 预览已默认
  对 sidecar 请求 bands，实验开关本身仍默认关闭。
- RTX 3070 Ti、3840×2160、18 字中档 glow、600 帧、最终 QImage 消费：bands 平均只回读画面高度
  `7.04%`，render mean/p95 `2.34/2.81ms`，readback `3.62/6.15ms`，roundtrip
  `14.30/16.83ms`，同步吞吐 `69.87fps`；同场景全帧回读为 readback `22.64/34.35ms`、
  roundtrip `64.89/76.85ms`、`15.41fps`。bands 将端到端吞吐提升 `4.54x`，4K60 common path
  达标。1080p 同场景为 `159.27fps`、roundtrip p95 `7.79ms`。
- Dark Spiral 真实 `PreviewGraphicsView` + MP4 离屏 10 秒烟测交付 572 帧，GPU/sidecar failure、
  restart、Painter fallback 均为 0，`max_pending=1`，readback p95 `4.12ms`；组合场景 render p95
  `10.11ms`，继续在 60fps GPU 帧预算内。
- 硬件/WARP build smoke 通过；GPU/transport `111 passed`，native protocol/export/benchmark 独立
  回归 `58 passed, 27 skipped`。

G3 还剩 TACTIC/A stain 组合 corpus 的 raw overlay diff 与 cache/GPU memory 稳态门禁；完成前仍不把
GPU 开关暴露给普通用户。

### 2026-07-19（第二十批）：Painter 解析排版与能力门禁

- GPU 不再自行用相邻行时间近似分页。Python 在构建 Render IR 时直接调用 Painter 的
  `compute_display_lines()` / `display_windows_for_style()`，把每行最终 `lane`、显示起止时间以及
  智能居中结果固化进场景；Direct2D 只消费解析后的排版快照。手工显示窗口也继续遵守 Painter
  “不能截断实际演唱区间”的既有约束。
- 双行非对称布局、每 lane 左/右/居中和水平边距均由 Painter 的最终 schedule 驱动。新增红/绿双行
  自动门禁，分别扫描 GPU 与 Painter 的颜色边界，四个水平边缘偏差均不超过 `8px`；显示覆盖窗口
  另覆盖边界前、区间内、演唱结束后和 tail 外四个时间点。
- 新增集中式 GPU capability gate。竖排、RTL、逐行布局、signal、行进入/退出动画、viewport
  变换、guide symbols 与共享 timing span 等尚未迁移的语义，统一在创建 sidecar 之前整场回退
  Painter；不允许一部分 GPU、一部分 CPU 的混合合成，也不把“能力未实现”计作 renderer failure。
  统计新增 `capability_fallbacks`，便于后续逐项迁移和产品诊断。
- 硬件/WARP build smoke 通过；GPU/transport 回归 `114 passed`，native
  protocol/export/benchmark 独立回归 `60 passed, 27 skipped`。GPU 产品开关继续默认关闭，
  Painter 仍是布局、时序、兼容性与整帧 fallback 的唯一 oracle。

G3 下一刀继续做组合 corpus 的 raw overlay diff，以及重复 configure/render 下 cache 和 GPU/进程
内存稳态门禁；能力门禁中的高级项目留到 G4 逐项消除。

### 2026-07-19（第二十一批）：G3 corpus 与显存稳态收口

- 新增 `scripts/compare_gpu_painter_corpus.py`，固定三类 1080p raw subtitle overlay corpus：
  普通双行 + ruby + glow、TACTIC-like 三档 glow + 蓝白走字 + `UseEdge2=false` + 7px 字距，
  以及角色混合字号/渐变/图片填充/ruby/标题/多字幕源的 G3 重组合。每个时间点保存 Painter/GPU
  两张透明层，并输出无平移修正的 alpha IoU、四边 bbox 偏差、union 像素 premultiplied channel
  MAE/p95；Painter 与 GPU 同时为空的帧也作为有效等价结果处理。
- 本机存在的 `Dark spiral journey/1.n3proj` 自动加入真实 N3 公共路径切片，保留真实歌词、ruby、
  时间和效果，只把未安装字体归一到双方都可解析的 Meiryo，并隔离 G4 能力。12 个抽帧全部通过：
  普通样本 alpha IoU `0.9413+`、最大边缘差 `5px`；TACTIC-like 为 `0.8888+` / `8px`；
  G3 重组合为 `0.8981+` / `11px`；Dark Spiral 两个有效帧为 `0.9546+` / `4px`。
  本机确实没有 TACTIC 与 A stain 原工程，结果 JSON 明确列为缺失样本，合成 TACTIC-like 不冒充原工程。
- Direct2D backend 通过 `IDXGIAdapter3::QueryVideoMemoryInfo()` 暴露 local/non-local usage 与 budget；
  新增独立 `gpu_diagnostics` 命令，读取 cache/显存时不进入逐帧热路径。基准同步记录 sidecar RSS、
  warmup/end cache bytes、显存用量以及相同场景重复 configure 的 hit 增量。
- RTX 3070 Ti、3840×2160、600 帧、中档 glow、packed bands、100 次相同 configure：100 次全部
  cache hit，cache 始终 `12,936B`；local video memory 始终 `88,408,064B`，增长 0；sidecar RSS
  `121,229,312 → 122,093,568B`，仅波动 `864,256B`；non-local 增长 `692,224B`。端到端
  `73.32fps`，render/readback/roundtrip p95 分别 `2.85/5.24/16.16ms`，4K60 继续达标。
- hardware/WARP corpus 与 build smoke 均通过；GPU/transport 回归 `115 passed`，native
  protocol/export/benchmark 独立回归 `61 passed, 27 skipped`。

至此 G3 在本机可得 corpus、Painter raw overlay、cache/显存稳态和 4K60 common path 的门禁均已完成，
**G3 宣告完成**。TACTIC/A stain 原工程复测以及 AMD/Intel 仍是外部样本/硬件矩阵项；产品 GPU 开关
继续默认关闭。下一阶段进入 G4，并始终按 capability 逐项迁移，未实现语义整场回退 Painter。

### 2026-07-19（第二十二批）：G4 基础行动画

- Render IR 为每行固化 `style_with_line_animation()` 合并后的 entry/exit 类型与时长，因此全局动画和
  逐行 override 继续遵守 Painter 的继承顺序；显示起止点直接使用上一批由 Painter 解析的 display
  schedule，不在 Direct2D 侧重新判断分页窗口。
- Direct2D 实现与 `engine/animator.py` 同式的 `fade`、`slide_in`、`slide_out` 和双向 `rise`：入场用
  quadratic ease-out，退场用 quadratic ease-in；slide 距离为 `max(fontSize×0.9, 36)`，rise 距离为
  `max(fontSize×0.35, 18)`，双 lane 的水平移动方向也与 Painter 一致。最终 opacity/translation 统一
  作用到正文、ruby、stroke/stroke2、shadow/glow、角色 brush 和 packed-band 纵向范围。
- capability gate 只为上述基础动画放行。`char_fade`、`spin_flip`、`utopia` 以及包含这些类型的逐行
  override 仍整场回退 Painter；不存在半行 GPU、半行 CPU 的混画。
- 自动门禁分别覆盖 fade、slide 和 rise 的入/退场：半程 alpha 相对完整帧的比例与 Painter 偏差不超过
  `0.06`，水平/垂直中心位移偏差不超过 `3px`，窗口终点双方都严格为空。hardware/WARP build smoke
  通过；GPU/transport 回归 `118 passed`，native protocol/export/benchmark 独立回归
  `62 passed, 27 skipped`。

G4 下一刀迁移逐字符 `char_fade`；随后再处理带 per-glyph transform 的 `spin_flip`，两者不合并验收。

### 2026-07-19（第二十三批）：G4 逐字符 char-fade

- Direct2D 按 Painter/N3 固定常数实现逐字透明度：总错峰窗口 `350ms`，单字淡入/淡出 `250ms`，
  `delayStep = 350 // (charCount - 1)`；入场从左至右，退场按 Painter 的字符 end 公式逐个移除，
  且退场上下文仍从 `max(lineEnd, displayEnd - 600ms)` 才开始。全局与逐行 `char_fade` 均通过
  resolved animation IR，未设置时长的路径继续不启动动画。
- opacity 不是只乘正文 fill：stroke/stroke2、shadow、before/after glow source、行内角色 brush 都按
  glyph 独立透明度生成；ruby 整组使用 Painter 相同的首个目标正文索引，ruby fill/stroke/shadow/glow
  同步淡入淡出。完全透明的逐字帧直接跳过绘制和 band 回读。
- 自动门禁使用 4 字正文 + 双字 ruby + 双 glow，在入场两点、完整帧、退场两点和终点比较 Painter：
  各抽帧相对完整帧 alpha 比例偏差不超过 `0.09`；ruby 洋红像素在相同时间点出现/消失；终点双方
  均为空。`spin_flip`/`utopia` 仍由 capability gate 整场回退。
- 基准新增 `--animation`。RTX 3070 Ti、1920×1080、18 字、中档 glow、packed bands、600 帧
  char-fade：`146.56fps`，render/readback/roundtrip p95 `2.54/2.31/7.84ms`；local 显存增长 0，
  sidecar RSS 波动 `585,728B`，明显低于 60fps 帧预算。
- hardware/WARP build smoke 通过；GPU/transport 回归 `119 passed`，native
  protocol/export/benchmark 独立回归 `62 passed, 27 skipped`。

G4 下一刀进入 `spin_flip` 的 per-glyph scale/skew transform；该变换必须同时覆盖正文所有视觉层与
ruby 组，并继续使用 Painter 的 char-fade 时间轴。

### 2026-07-19（第二十四批）：G4 逐字符 spin-flip

- Direct2D 复用 Painter `char_fade` 的 `350ms` 总错峰、`250ms` 单字窗口与退场次序；每个正文 glyph 按自身 advance 框中心、ruby 按整组中心执行 `scale(opacity)` 与纵向 shear，入场/退场方向分别为负/正。矩阵系数直接对照 Qt `QTransform.translate → shear → scale → translate` 的结果，避免 Direct2D/Qt 乘法顺序歧义。
- Painter 的实际语义是先烘焙字形视觉栈、再做残差仿射。GPU 因此不能对已变换轮廓继续使用固定宽度 `DrawGeometry` 或固定半径 blur：stroke/stroke2 在 configure 阶段预扩成填充轮廓，再逐帧仿射；before/after glow 按正文逐字、ruby 逐组先生成未变换高斯层，再整体仿射；shadow 的描边轮廓和 offset 向量同样随矩阵变换。正文/ruby fill、保护描边、角色样式与 packed-band 纵向范围全部使用同一变换结果。
- capability gate 已为全局与逐行 `spin_flip` 放行；`utopia` 仍整场回退 Painter。自动门禁覆盖四字正文、双字 ruby、正文/ruby glow、stroke、shadow 与入场/退场共六个时间点：过渡帧相对完整帧的 alpha 比例与 Painter 偏差不超过 `0.12`，glow 边界不超过 `18px`，shadow 边界不超过 `8px`，恒等帧不超过 `8px`，终点双方均严格为空。
- RTX 3070 Ti、1920×1080、60fps、18 字、中档 glow、packed bands、600 帧 `spin_flip`：`138.22fps`，render/readback/roundtrip p95 分别为 `4.82/2.31/10.57ms`；local 显存增长 0，sidecar RSS 波动 `491,520B`。硬件/WARP build smoke 通过；GPU/transport 回归 `120 passed`，native protocol/export/benchmark 独立回归 `62 passed, 27 skipped`。

G4 下一刀进入 `utopia`。它同时包含逐字入场、演唱中 over-scale、ruby 分组与退场位移/旋转，仍按 capability 逐项迁移；产品 GPU 开关继续默认关闭，Painter 永久保留为 oracle 与 fallback。

### 2026-07-19（第二十五批）：G4 Utopia 正文

- Utopia 不按普通 entry/exit 短窗口处理：只要任一端选择 Utopia，整段显示窗口都统一走逐字仿射路径，消除静态/动态路径切换色闪。Direct2D 已对齐 Painter 固定状态机：`700ms` 入场总窗、`200ms` 字间错峰、`400ms` 放大、`100ms` 从 `1.3×` 回落，演唱中前 25% 且最多 `100ms` 的 `1.15×` over-scale，以及 `750ms` 退场。
- 退场起点按 Painter `_utopia_following_done_time()`：当前字等待后一个有效字的完成时间，末字再叠加 `max(lineTail - 750ms, 0)`；轨迹使用画面高度 `/15` 的振幅、横纵正弦位移、`-180°` 旋转、余弦 X 翻转与同步收缩。矩阵严格复刻 `_character_transform()` 的 scale-origin 分支，缩放原点为字符 advance 左下角，旋转中心为字符框中心。
- Utopia 走字按变换后的 ink bounds 加半个主描边计算水平 wipe；退场字强制为完整 after 色。正文 fill、stroke/stroke2、shadow/glow、角色样式与 band 范围共用同一逐字矩阵，其中 glow 仍遵守“先烘焙再仿射”的 Painter 顺序。
- capability 先只放行无 ruby 的 Utopia；存在 ruby 时返回明确的 `utopia_ruby_group` 并整场回退，等待下一批 group 语义完成。自动门禁覆盖入场、恒等、演唱 over-scale、分批退场和全空终点，正文 + glow 的 alpha 轨迹相对 Painter 偏差不超过 `0.14`、四边偏差不超过 `14px`。
- RTX 3070 Ti、1920×1080、60fps、18 字、中档 glow、packed bands、600 帧 Utopia：`92.92fps`，render/readback/roundtrip p95 `5.33/3.84/11.64ms`；local 显存增长 0，sidecar RSS 波动 `1,032,192B`。GPU/transport 回归 `121 passed`；native protocol/export/benchmark 独立回归 `62 passed, 27 skipped`。

下一批补齐 Utopia ruby/group/transformed glow；完成前带 ruby 的项目不会进入 GPU。

### 2026-07-19（第二十六批）：G4 Utopia ruby/group/transformed glow

- native scene 为每个 ruby 保留正文目标首尾索引，并为每个 reading unit 缓存独立 advance、枢轴、描边轮廓和时间段。多字 ruby 目标内的所有正文字符共享 Painter 的 group following-done 时刻；演唱中正文仍逐字 over-scale，不错误地把整组当成一个 glyph。
- ruby 入场使用目标首字的错峰索引，全部 reading unit 同步出现；演唱中各 unit 使用自己的起止时间做 `1.15×` over-scale 与 transformed wipe；退场按目标末字的 following-done 同步启动，但每个 unit 保持自己的左下缩放原点和字心旋转枢轴。这与当前 Painter `_paint_ruby_text_units_with_transition()` 的实际绘制语义一致。
- ruby fill、stroke/stroke2、透明填充保护、shadow offset、before/after glow 与 packed-band 范围全部消费 unit 级矩阵。glow 按 unit 先在上正坐标生成再仿射，shadow offset 先通过矩阵线性部分变换；after 色裁切使用变换后 ink bounds，而退场强制完整 after 色。
- `utopia_ruby_group` capability 回退已移除，全局与逐行 Utopia 现在都可覆盖带 ruby 的横排场景。自动门禁使用四字正文、双字目标、双 reading unit、正文/ruby 双 glow，覆盖入场、两段 ruby 演唱、共享退场与终点；alpha 轨迹偏差不超过 `0.15`、边界偏差不超过 `16px`。另以正文/ruby shadow 验证变换后偏移，边界偏差不超过 `12px`。
- 基准新增 `--ruby`。RTX 3070 Ti、1920×1080、60fps、18 字 + 4 reading units、中档正文/ruby glow、packed bands、600 帧 Utopia：`80.64fps`，render/readback/roundtrip p95 `9.71/3.95/15.49ms`；local 显存增长 0，sidecar RSS 波动 `1,413,120B`。GPU/transport 回归 `122 passed`；native protocol/export/benchmark 独立回归 `62 passed, 27 skipped`。

Utopia 横排主路径至此收口。G4 下一项进入 Sayatoo signal；产品 GPU 开关继续默认关闭，Painter 继续作为 oracle 与全部未迁移 feature 的整场 fallback。

### 2026-07-19（第二十七批）：G4 Sayatoo 横排 volume signal

- Render IR 构建显示窗口时复用 Painter `_display_style_for_signal_window()`，因此 `signals.duration + waiting - time_offset` 的完整引导时段会提前保留；Direct2D 不再只按普通歌词 lead 截掉倒计时前段。`lit.*` 与 `volume.*` 参数进入 resolved style，歌手方案仍按 Painter 的继承顺序解析。
- Direct2D 对齐 Painter `_volume_signal_geometry()`：柱数、尺寸、柱宽/间隔、描边外扩、`volume_ratio` 透视高度和三档纵向对齐均使用同一公式。信号与歌词的 offset-free 联合盒参与 left/center/right 布局，`volume_offset_x/y` 只移动信号本体，不反向拖动歌词。
- 时间状态覆盖闪烁阶段、最终填充阶段以及演唱期间保持最后一柱；`flash_times`、`flash_duration_ratio`、`transition_ratio`、waiting/time offset、整体透明度、before/overlay 填充与描边全部进入 GPU 绘制。信号纵向范围同时并入 packed-band readback。
- capability gate 只为横排 `lit_style=volume` 放行，circle/square/rounded 和竖排仍整场回退 Painter。自动门槛覆盖 8 个倒计时/填充/演唱时刻：四边界相对 Painter 最大偏差 `12px`；剔除 DirectWrite/Qt 字体光栅差异后，独立 signal 层 alpha 偏差低于 `5%`，闪烁灭灯时刻与蓝色覆盖推进一致。
- 基准新增 `--signals`。RTX 3070 Ti、1920×1080、60fps、packed bands、600 帧 volume signal：`220.28fps`，render/readback/roundtrip p95 `1.84/1.62/5.63ms`；local 显存增长 0，sidecar RSS 波动 `823,296B`。hardware/WARP build smoke 通过；GPU/transport `123 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。

下一批补齐横排 circle/square/rounded 的阴影、soften、高光与 fade/slide 熄灭过渡；完成前非 volume signal 继续由 capability gate 回退 Painter。

### 2026-07-19（第二十八批）：G4 Sayatoo 横排 shape signals

- Direct2D 补齐 circle/square/rounded 三种普通灯形，严格复制 Painter 的 `count/size/tracking/offset` 几何、文字与 signal 的 offset-free 联合布局，以及“初始全亮、从右向左逐个熄灭”的索引/phase 状态机。倒计时结束后灯形消失，但同一显示窗口内仍保留联合布局，避免歌词在开唱瞬间横向跳动。
- 普通灯形完整覆盖 `lit_fill/stroke`、stroke width、soften 外圈、35% 黑色投影、整体 opacity 和 active 灯的白色高光；square 使用直角矩形，rounded 使用 22% 半径，circle 使用椭圆，与 Painter `_draw_lit_shape_raw()` 一致。
- active 灯的 `fade` 按 transition ratio 熄灭；`slide` 同时对齐 opacity、角度和距离向量。移动、投影、soften、主体和高光共享 active opacity，packed-band 纵向范围包含 slide 与投影外扩。
- capability gate 已为全部四种横排 signal 放行；竖排仍由 `vertical` 能力门槛整场回退 Painter。自动门槛参数化覆盖 3 种形状 × 2 种过渡 × 9 个时刻：边界相对 Painter 最大偏差 `13px`，剔除文字层并归一化后的 signal alpha 轨迹偏差不超过 `0.08`，开唱时双方蓝色 signal 像素均严格为 0。
- 基准新增 `--signal-style`。RTX 3070 Ti、1920×1080、60fps、packed bands、600 帧 rounded signal：`220.29fps`，render/readback/roundtrip p95 `1.85/0.87/5.23ms`；local 显存增长 0，sidecar RSS 波动 `450,560B`。

横排 Sayatoo signal 至此收口。下一批进入 G4 竖排文本/信号布局；在竖排能力整体对齐 Painter 前，`vertical` 继续保持明确 fallback。

### 2026-07-19（第二十九批）：G4 竖排正文、走字与双列

- RenderScene/TextStyle 新增 vertical 布局标记。configure 阶段把既有 N3/DirectWrite glyph outline 转换为逐格竖排 geometry：字格高度取字体 ascent+descent，列宽按全角字号；汉字与拉丁字符直立，长音符/括号等 UTR#50 简化集合绕字格中心旋转 90°，句读点和小假名使用与 Painter 相同的右上偏移。
- 竖排坐标完全对齐 Painter：lane 0 位于最右列，后续 lane 按 `cellWidth + lineGap` 向左；`line_y_margin_px` 同时作为右边距和 top/bottom 纵向边距，top/center/bottom 三种锚点按整列高度解析。竖排不绘制 Sayatoo signal，与 Painter 的现有兼容边界一致。
- 走字从横向 ink wipe 切换为逐字格自上而下的纵向扫描；before/after fill、stroke/stroke2 与 shadow 共用纵向 clip，shadow 的 after clip 额外抵消 Y 偏移。packed-band 使用转换后 glyph、描边和阴影范围。
- capability 按组合逐项放行：无 ruby、无 glow、无逐字角色样式、无行/标题动画的竖排正文现可进 GPU；`vertical_ruby`、`vertical_glow`、`vertical_title`、`vertical_animation`、`vertical_inline_style` 仍整场回退 Painter。这样不会因正文已实现而误放行半成品组合。
- 自动门槛使用“縦/A/ー/。”覆盖直立、旋转、角标、双描边、阴影及 6 个走字时刻，top/center/bottom 的四边界相对 Painter 最大偏差 `3px`，alpha 总量偏差低于 `1.5%`；双列右→左布局边界偏差不超过 `4px`。hardware/WARP build smoke 通过；GPU/transport `133 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。
- 基准新增 `--vertical`。RTX 3070 Ti、1920×1080、60fps、6 字竖排、packed bands、600 帧：`71.73fps`，render/readback/roundtrip p95 `1.60/5.73/15.97ms`；local 显存增长 0。当前竖列覆盖画面高度较大，平均 readback ratio 为 `77.96%`，后续 G6 可进一步减少读回成本。

下一批补齐竖排 ruby 与 glow；完成后再移除对应 capability fallback，并补真实 N3 竖排组合 corpus。

### 2026-07-19（第三十批）：G4 竖排 glow

- 竖排正文复用已经验收的 Direct2D 全帧 glow source 与 N3 多 pass 高斯模糊，只把 before/after 源的走字裁剪切换为自上而下的纵向 clip；blur 仍在裁剪后的合成源上执行，避免对模糊结果硬切产生清晰横线。
- glow 的 stroke/stroke2 轮廓宽度、半径、concentration 和 before/after 颜色继续复用横排 resolved style；转换后的竖排 glyph bounds 与 blur 三倍半径一并进入 packed-band 范围。
- `vertical_glow` capability fallback 已移除。自动门槛覆盖直立字、旋转长音符、两档颜色、双描边、中档 glow 与 5 个纵向走字时刻：四边界相对 Painter 最大偏差 `12px`，相对完整帧的 alpha 轨迹偏差不超过 `0.09`。GPU/transport `134 passed`，native protocol/export/benchmark `63 passed, 27 skipped`；竖排 ruby 仍明确回退。

下一批进入竖排 ruby 的右侧列布局、reading-unit 时间轴和纵向 after wipe。

### 2026-07-19（第三十一批）：G4 竖排 ruby

- Direct2D 在 configure 阶段把 ruby reading unit 转成正文右侧的纵排字格：目标范围沿用正文
  首尾字符格，reading unit 在目标纵向跨度内等分；直立/旋转集合、句读点和小假名偏移继续复用
  正文的 UTR#50 简化规则。ruby 字格宽取全角 advance，右侧预留宽度则严格按
  `QFontMetrics.height() + ruby_gap_px` 对齐；native 从 DirectWrite face 的 ascent + descent
  计算并取整，因此 Meiryo 28px 会得到 Painter 相同的 42px，而不是误用 28px 字号。
- reading unit 时间轴完整保留空 part。空 part 不创建 geometry，但仍占自己的纵向字格与 wipe
  时间段，后续 unit 不会提前填入空槽；before/after fill、stroke/stroke2 与 shadow 都按自上而下
  的 ruby scan edge 裁切，shadow after clip 抵消 Y 偏移。
- ruby glow 继续使用独立的 before/after 半径、浓度与多 pass source，只把 source clip 切换为
  纵向；模糊在裁切后的源上执行。`vertical_ruby` capability fallback 已移除，竖排动画、标题和
  行内角色样式仍保持明确整场 Painter fallback。
- 自动门槛覆盖双字目标、三 reading unit（含中间空 part）与 6 个时刻：四边界相对 Painter
  最大偏差 `10px`，归一化 after 色轨迹偏差不超过 `0.05`，空 part 两端走字像素严格不变。
  shadow 边界偏差不超过 `2px`、alpha 轨迹偏差不超过 `0.02`；glow 边界偏差不超过 `8px`、
  alpha 轨迹偏差不超过 `0.03`。
- benchmark 的 `--vertical --ruby` 场景已改为真正匹配竖排正文的 ruby，不再空测。RTX 3070 Ti、
  1920×1080、60fps、中档正文/ruby glow、packed bands、600 帧：render p95 `3.42ms`，
  readback/roundtrip p95 `8.89/21.46ms`，同步吞吐 `56.26fps`，local 显存增长 0。平均 band
  覆盖率 `85.46%`，同步端到端长尾来自全高竖列的 readback/QImage copy，进一步收益留给 G6
  共享纹理；GPU 绘制核心仍明显低于 60fps 帧预算。
- hardware benchmark 与 WARP Painter 门槛均通过；GPU/transport 独立回归 `137 passed`，native
  protocol/export/benchmark 独立回归 `63 passed, 27 skipped`。

竖排正文、glow 与 ruby 基础组合至此收口。G4 下一刀继续处理 RTL、viewport transform、
`per_row`、竖排标题/行内角色和竖排行动画；产品 GPU 开关继续默认关闭，Painter 永久保留为
oracle 与 fallback。

### 2026-07-19（第三十二批）：G4 RTL 正文布局与走字

- GPU `TextStyle` 新增 RTL 方向标记。configure 仍按逻辑顺序生成 DirectWrite glyph，随后把每个
  glyph 独立平移到反向字符槽：首字符位于最右，后续字符依次向左；只移动 outline、描边轮廓和
  pivot，不对 glyph 本身做水平镜像。总宽、字距、left/center/right 锚点和 fill rect 因此继续与
  Painter `_char_left_positions()` 共用同一语义。
- 主字 wipe 从行右缘开始，按逻辑字符时间自右向左推进；before/after fill、stroke/stroke2、shadow
  和 glow source 都切换为 RTL clip。gradient/split/image fill 仍使用整行固定 fill rect，不随字符
  或走字重新起相位。
- capability 只放行非竖排、无 ruby、无 signal、无行内角色、无行动画的 RTL 正文；其余组合分别
  返回 `rtl_vertical`、`rtl_ruby`、`rtl_signal`、`rtl_inline_style`、`rtl_animation`，继续整场
  Painter fallback，避免正文完成后误放行尚未反转 visual units 的 ruby。
- 自动门槛使用不同宽度的 `W/i/M/.` 和 9 个走字时刻，分别覆盖 shadow/glow：四边界相对 Painter
  最大偏差 `10px`，归一化 after 色推进偏差不超过 `0.08`。WARP Painter 门槛与硬件 benchmark
  均通过。
- benchmark 新增 `--rtl`。RTX 3070 Ti、1920×1080、60fps、中档 glow、packed bands、600 帧：
  `156.29fps`，render/readback/roundtrip p95 `2.58/2.39/7.91ms`，平均 band 覆盖率 `14.07%`，
  local 显存增长 0。GPU/transport 独立回归 `139 passed`，native protocol/export/benchmark
  独立回归 `63 passed, 27 skipped`。

下一批继续 RTL ruby：visual reading units 反向排列但保持各自逻辑时间，小假名反序与浊点组合需
严格复用 Painter `_ruby_wipe_geometry()` / `_ruby_utopia_visual_units()` 的实际语义；完成前
`rtl_ruby` 不移除。

### 2026-07-19（第三十三批）：G4 RTL ruby

- ruby 先保留逻辑 reading unit 与各自时间段，再单独计算反向 visual slot：最后一个可见 unit
  放在最左，首个逻辑 unit 放在最右；geometry 本身不镜像。Painter
  `_ruby_utopia_visual_units()` 已把组合浊点/半浊点与前字合为一个 unit，IR/native 继续消费该边界，
  小假名则保持独立 unit 后参与整体反序。
- RTL 正文目标字符槽已经反向，因此 ruby target box 改为首尾目标槽的 min/max，避免直接使用
  `first.layoutLeft → last.layoutRight` 得到负宽。center/equal-space、ruby interval、fill rect 和全局
  gradient 相位继续复用横排实现。
- ruby wipe 按逻辑时间从右向左推进；空 reading part 保留时间平台。sharp fill/stroke/stroke2、shadow
  after clip 与 before/after glow source 全部切换为 RTL 裁剪，shadow 前沿额外抵消 X 偏移。
  `rtl_ruby` capability fallback 已移除；RTL + animation/signal/inline-style/vertical 仍保持独立回退。
- 自动门槛覆盖小假名、组合浊点、空 part 与 6 个时间点：整体边界相对 Painter 最大偏差 `5px`，
  after 色走字前沿偏差不超过 `10px`，归一化颜色面积轨迹偏差不超过 `0.12`，空 part 两端严格
  不推进。shadow/glow 边界分别不超过 `6/7px`，归一化 alpha 轨迹偏差不超过 `0.03`。
- RTX 3070 Ti、1920×1080、60fps、RTL + ruby + 中档正文/ruby glow、packed bands、600 帧：
  `107.84fps`，render/readback/roundtrip p95 `3.44/4.13/11.36ms`，平均 band 覆盖率 `20.00%`，
  local 显存增长 0。GPU/transport 独立回归 `142 passed`，native protocol/export/benchmark 独立
  回归 `63 passed, 27 skipped`。

RTL 正文与 ruby 基础路径至此收口。下一批按 G4 顺序处理 viewport transform、跨行 scope 与全部
标题路径；RTL 的 signal、行内角色和动画继续保持 capability fallback，待对应组合单独验收。

### 2026-07-19（第三十四批）：G4 viewport transform

- Render IR 中既有的 `viewport_scale_pct`、`viewport_rotation_deg`、`viewport_offset_x/y` 与九宫格 `viewport_align` 已进入 native scene。Direct2D 按 Painter `_apply_viewport_transform()` 的顺序围绕逻辑画布锚点执行平移、等比缩放和旋转；高 DPI 下只把逻辑位移换算为物理像素，比例与角度保持不变。
- sharp 正文/ruby、stroke/stroke2、shadow 与 Sayatoo signal 直接在同一最终矩阵下绘制。glow 则先在未变换坐标生成与 Painter 相同的裁切后高斯源，再把模糊结果整体变换，因此缩放会同时改变发光的可见半径，旋转也不会把 after wipe 硬切成画布轴向边缘。
- Painter 的标题契约经过像素对照重新确认：歌词与 signal 随 viewport 变化，但标题在 `painter.restore()` 后以屏幕坐标单独绘制。GPU 用 `staticOverlay` 明确绕过 viewport，避免标题跟随旋转或缩放；这不是近似处理，而是现有 Painter 输出的严格兼容边界。
- packed bands 不再因 viewport 非默认值直接退化为整帧。每条非标题区间用 viewport 矩阵变换覆盖全宽条带的四角，生成保守的纵向回读范围；标题保留原区间。旋转场景仍会扩大 band，但不会裁掉变换后的像素。
- 自动门槛覆盖 4 组单独 offset/scale/rotation/组合锚点，以及横排、竖排、RTL ruby、shadow/glow、标题与 volume signal：8 组 GPU/Painter 对照全部通过；`viewport_transform` capability fallback 已移除。GPU 独立回归 `79 passed`，transport `71 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。
- benchmark 新增 `--viewport`。RTX 3070 Ti、1920×1080、60fps、正文中档 glow、115% 缩放、12° 旋转、平移、packed bands、600 帧：`74.31fps`，render/readback/roundtrip p95 `2.98/5.63/15.32ms`，平均 band 覆盖率 `58.89%`，local 显存增长 0。该常用变换路径仍高于 60fps 门槛；产品 GPU 开关继续默认关闭，Painter 永久保留为 oracle/fallback。

G4 下一批继续处理 `per_row`/逐行布局覆盖、竖排标题/行内角色/动画以及 RTL signal/行内角色/动画的独立组合门槛。

### 2026-07-19（第三十五批）：G4 `per_row` 与逐行布局覆盖

- Python 在构建 Render IR 时对每行调用 Painter `_style_for_line()`，把布局卡、歌手方案和行动画按现有继承顺序解析，再随行固化几何快照。GPU 不再只看到全局布局，也不在 C++ 侧重复解释 `layout_index`；越界布局自然由 Painter 回退默认布局。
- 随行快照覆盖纵向锚点/边距/行距、行数与逐 lane 对齐、`per_row` 两行独立 X/Y 偏移，以及布局卡可覆盖的字间距、`allow_biting`、ruby interval/alignment/gap。角色 run 在套用角色外观后重新施加布局字符字段，与 Painter“布局字段最终覆盖”的顺序一致；char-style cache key 也加入布局字符签名，避免同一角色跨布局误复用 geometry。
- Direct2D 新增独立 `layoutOffsetX/Y`，在文字、stroke/stroke2、shadow/glow、ruby 与 signal 联合盒完成锚定后统一平移。`center_override` 与 Painter 一样优先于行级 X 偏移，Y 偏移仍保留；额外布局的 `smart_horizontal` 也在 Python 计算 center override 时使用该行的有效样式，而不是错误读取全局样式。
- 自动门槛覆盖红/绿双行的 left/right 锚点与正负 X/Y 偏移、`per_row` + volume signal 联合布局，以及额外布局卡的 top/right/margin/letter-spacing/ruby interval/gap。三组 GPU/Painter 对照通过，`per_row_layout` 与 `line_layout_override` capability fallback 已移除。GPU 独立回归 `82 passed`，transport `71 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。
- benchmark 新增 `--per-row`。RTX 3070 Ti、1920×1080、60fps、双行独立锚点/偏移、中档 glow、packed bands、600 帧：`91.09fps`，render/readback/roundtrip p95 `4.93/4.55/13.64ms`，平均 band 覆盖率 `28.33%`，local 显存增长 0。

G4 下一批处理当前剩余的组合门槛：竖排标题/行内角色/动画，以及 RTL signal/行内角色/动画；随后进入 guide/shared-span 等剩余 Painter 语义和 G5 集成。

### 2026-07-19（第三十六批）：G4 竖排/RTL 组合门槛收口

- 竖排标题继续走独立 `staticOverlay` 屏幕坐标路径，开启 `vertical` 不会改变标题位置或像素；GPU 以同场景 vertical 开/关输出逐字节相等作为门槛。DirectWrite 与 Qt 的长拉丁标题宽度差仍控制在既有字体引擎容差内。
- 对齐 Painter 当前兼容边界：竖排正文忽略行内角色标签，统一使用有效行样式；竖排的 `char_fade`、`spin_flip`、`utopia` 也按 Painter 视为无效并静态显示整列。fade/slide/rise 整行动画仍正常执行。GPU 不在 CPU oracle 尚未实现这些组合时擅自输出另一套视觉语义。
- RTL 的行内混合字号此前使用文字+signal 联合盒平移正文，而 Painter 对带角色 run 的正文独立锚定、只让 signal 使用联合盒。Direct2D 现分别计算 `dx` 与 `signalDx`；灭灯帧仍保留联合盒，避免 flash phase 期间歌词横跳。普通 volume/shape signal 与非 RTL 路径保持原行为。
- 组合门槛覆盖：竖排标题、竖排角色兼容边界、RTL+角色+volume signal、RTL+vertical，以及 vertical/RTL × fade/slide/rise、vertical/RTL × char-fade/spin/Utopia。共 16 组新增 GPU/Painter 对照通过，所有 `vertical_*`/`rtl_*` capability fallback 已移除；当前只剩真实未迁移的 guide symbol、shared timing span 和未知动画名。
- GPU 独立回归 `98 passed`，transport `71 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。RTX 3070 Ti、1920×1080、60fps、RTL + Utopia + 中档 glow、packed bands、600 帧：`81.98fps`，render/readback/roundtrip p95 `7.57/4.31/14.62ms`，平均 band 覆盖率 `14.05%`，local 显存增长 0。

G4 下一批进入 guide symbol 与 shared timing span；两者完成后审计 capability gate，并转入 G5 产品集成与 G6 稳态/故障恢复验收。

### 2026-07-19（第三十七批）：G4 shared timing span

- Render IR 不再让 native 根据原始打轴点自行猜测共享时间段。Python 对每行复用 Painter 的有效行样式、字体分流、N3 字宽规则、角色 run 与矢量字形宽度，调用同一 `compute_char_intervals()` 固化最终逐字区间；竖排继续沿用 Painter 的固定字格原始区间。C++ 只消费 `resolved_intervals`，因此共享源片段的宽度加权、暂停释放与行尾边界保持 CPU oracle 的现有语义。
- Direct2D 竖排扫光同时修正了空白字格：空格没有 outline geometry，但 Painter 仍会让它占固定高度并推进扫描边缘；native 现在无论 geometry 是否为空都会保存字格 top/bottom，不再在空格时间段把 after clip 回退到列顶。
- 自动门槛用宽度差显著的 `W / 空格 / M` 覆盖横排、RTL 与竖排，直接比较 before/after 蓝色推进比例以及最终边界。横排与 RTL 验证宽度加权区间，竖排验证固定字格时间；三组均通过，`shared_timing_span` capability fallback 已移除。
- 独立回归：GPU `101 passed`，transport `71 passed`，native protocol/export/benchmark `63 passed, 27 skipped`。当前 capability gate 只剩 guide symbol 与未知行动画名；产品 GPU 开关继续默认关闭，Painter 永久保留为 oracle 与 fallback。

G4 下一批迁移 guide symbol（前缀插入、前缀替换、行内替换、角色着色与矢量路径），随后完成 capability gate 审计并进入 G5。

### 2026-07-19（第三十八批）：G4 guide symbol 与 capability 收口

- Python 在构建 Render IR 时直接复用 Painter `_line_with_guide_symbol()`：额外前缀按 `duration × count` 生成虚拟打轴字符，安全匹配的 `replacement_prefix` 原位替换，行内映射保留源字符时间和角色标签。IR 中每个虚拟字符携带已校验的 M/L/C/Q/Z 路径、units-per-em、advance width 与最终角色标签；native 不再解释项目级 guide 配置。
- Direct2D 新增原生矢量字形 geometry：路径按有效字号缩放到基线坐标，advance 严格使用 Painter `_vector_glyph_width()` 的同源参数，随后与普通字形共用 fill、stroke/stroke2、shadow/glow、角色配色、逐字动画和 karaoke wipe。竖排不套 UTR#50 文本旋转，而是像 Painter 一样按实际矢量 bounds 居中字格；RTL 只反转字格顺序，不镜像路径。
- 对齐了 Painter 的源行锚定兼容语义：普通行由 Sayatoo row layout 先测量未替换歌词，再从该 text origin 插入 guide；角色 run 与竖排则按最终渲染 geometry 自行布局。Python 预解析 `guide_anchor_bounds`，Direct2D 在普通横排/RTL 及 signal 联合布局时消费该锚框，避免额外前缀被错误地整体重新居中。
- 自动门槛覆盖前缀插入、前缀替换、行内替换 × 横排/RTL/竖排共 9 组，验证最终路径边界、逐字 before/after 色进度与角色方案颜色；GPU/Painter 对照全部通过。`guide_symbol` capability fallback 已移除。
- capability 审计后只剩未知全局/逐行动画名会整场回退 Painter，这是面向未来字段的安全闸，不是当前产品功能缺口。独立回归：GPU `110 passed`，transport `71 passed`，native protocol/export/benchmark `64 passed, 27 skipped`。Painter 继续永久作为 oracle/fallback，产品 GPU 开关仍默认关闭。

G4 功能迁移至此收口。下一阶段进入 G5 产品集成：审计预览/导出开关、配置持久化、sidecar 打包与用户可见回退诊断，再进入 G6 稳态和故障恢复门槛。

### 2026-07-19（第三十九批）：G5 产品开关、GPU 导出与 Windows 打包

- 输出页新增彼此独立的“GPU 字幕预览”和“GPU 字幕导出”实验开关，仅在 Windows 显示并默认关闭；二者保存为本机输出偏好，不写入工程撤销历史。GPU 字幕导出不改动 `encoder_mode`，视频仍由用户当前选择的 libx264/NVENC/QSV/AMF 等 ffmpeg 编码器输出。
- 预览开关可在运行时安全替换 `AsyncSubtitleRenderer` / `GpuAsyncSubtitleRenderer`，切换前停止旧 worker，保留播放状态并刷新当前时间。能力门槛或 sidecar 异常时仍交付 Painter 帧；首次回退会向主窗口发送中文诊断，异常路径保持一秒有界冷却和自动重启。
- 导出新增常驻 sidecar 的逐帧 Direct2D 路径：每帧使用 packed-band staging readback，经共享内存展开成透明图像并转换为 ffmpeg 要求的 straight RGBA。取消沿用现有 `ExportCancelled`；sidecar/共享内存失败会终止 ffmpeg、删除半成品，并从第 0 帧用 Painter 完整重启，避免输出混合两种渲染器。
- Windows 构建脚本现在先编译并 WARP 冒烟 `krok_subtitle_renderer.exe`，再把 sidecar 放到主程序 exe 同级。frozen 启动会从 `_internal/PyQt6/Qt6/bin` 发现 Qt DLL，并为 sidecar 注入对应 PATH/QT_PLUGIN_PATH；成包后通过隐藏的 `--package-gpu-smoke` 实测 WARP、GPU configure、条带共享内存读回和非透明字幕像素。
- 本机完整 `scripts/build_windows.bat` 通过：包内容、multiprocessing spawn、成包 Direct2D/WARP smoke、全量 zip 与增量 app/runtime 资产均成功。另以 320×180、60fps、15 帧纯色背景实测 GPU→共享内存→ffmpeg/libx264，生成有效 MP4；GPU 独立回归 `110 passed`，transport `72 passed`，native protocol `34 passed, 27 skipped`，native export/renderer `54 passed`，loaders `39 passed`。

G5 产品接入与 Windows 分发链路至此具备可验收形态，开关仍默认关闭，Painter 永久作为 oracle/fallback。下一阶段进入 G6：长时预览/导出、设备丢失、sidecar kill/restart、取消与重复切换、显存/RSS 增长和 60/120fps 性能门槛。

### 2026-07-19（第四十批）：稳定性/故障门禁与 G6 决策

- 修正独立 GPU benchmark 在构建 Painter 对齐字体度量前未创建 `QApplication` 的问题；命令行基准现在能在没有 pytest/主窗口的环境中稳定运行。预览调度基准新增 `--kill-sidecar`，会杀掉真实运行中的 sidecar，验证 Painter 当前帧回退、一秒冷却和重新 configure 后的 GPU 恢复。
- 30 秒 1080p60 实时预览压力测试覆盖 1800 帧播放、500 次突发 seek、20 次 resize、20 次 style churn 和一次 200ms 慢帧：播放交付 `1800/1800`，`max_pending=1`，无 renderer failure/fallback；慢帧释放后 `118.861ms` 交付最新帧。render/readback/roundtrip p95 为 `5.42/6.45/13.46ms`，GPU 路径保持最新优先且没有进入慢稳态。
- 真实 kill sidecar 后，预览先交付 Painter fallback 帧，再在下一次重试中重建进程/device/cache；`renderer_failures/restarts/fallback_frames=1/1/1`，GPU 恢复请求耗时 `162.731ms`。连续 12 次 GPU↔Painter 切换后没有遗留 `subtitle-preview-gpu-render` 线程。真实 WARP 导出在第 10 帧取消，抛出 `ExportCancelled` 且半成品已删除。
- 性能审计明确了当前边界。常用 1080p ruby+glow+packed bands 为 `110.46fps`，render/readback/roundtrip p95 `3.36/4.57/11.21ms`；4K60 同场景为 `55.59fps`，其中 GPU render p95 仅 `3.73ms`，但 readback/roundtrip p95 达 `7.89/20.84ms`。重组合（ruby+glow+Utopia+signal+双行+viewport）1080p 仅 `35.25fps`，render/readback/roundtrip p95 `16.65/4.59/31.15ms`。三场景显存增长均为 0，sidecar RSS 仅有约 0.8～1.6MB 波动。
- 结论：G0～G5 的功能、产品回退和分发链路已完成，但 1080p120、4K60 和重组合尚未达到默认启用门槛。瓶颈已主要落在 staging readback、IPC 和 QImage，而非 Direct2D 字形绘制；因此不再继续以迁移更多绘制功能掩盖瓶颈。GPU 保持实验/默认关闭，Painter 永久保留。若继续追求这些档位，应单独启动 G6 的 D3D 共享纹理/原生 HWND 预览设计与真实视频时钟验收，不能在本批中冒险塞入产品路径。

稳定性自动回归目前为 transport `73 passed`；G6 共享纹理属于新的高风险架构项目，不因 G0～G5 完成而自动授权或默认开启。

### 2026-07-19（第四十一批）：G6 DirectComposition 原生预览首批

- sidecar 新增 `NativePreviewSurface`：在 Qt viewport 的父 HWND 下创建无输入、透明的 child HWND，使用
  premultiplied `CreateSwapChainForComposition` + DirectComposition visual 呈现。Direct2D frame target 与
  swap-chain back buffer 在同一 D3D11 device 内 `CopyResource`，`gpu_present_frame` 不创建 staging texture、
  shared-memory slot 或 QImage；`gpu_preview_close` 显式销毁 visual、swap chain 和 child HWND。
- Python `GpuAsyncSubtitleRenderer` 新增 G6 模式，仍维持单在途 + 单 pending latest-wins。Qt GUI 线程只解析
  viewport 父 HWND、scene 裁切后的物理几何与 DPR，worker 继续消费统一播放器/音频时钟给出的 `t_ms`。
  native 帧通过 `frame_presented` 计数，Painter fallback 仍通过原有 `frame_ready(QImage)` 交付；当时的
  实验入口为 `KROK_SUBTITLE_GPU_NATIVE_PREVIEW=1`。该入口现已硬关闭，本段仅保留历史记录。
- 自动门槛新增：协议能力、父子 HWND/尺寸/销毁、零 readback、无 shared-memory reader/QImage、resize、seek、
  style churn、GPU↔Painter teardown。`scripts/benchmark_gpu_preview_scheduler.py --native-preview` 可输出
  `render/present/readback/roundtrip`，并复用 `--kill-sidecar` 与慢帧自愈注入。
- Windows onedir 完整构建通过；成包 `--package-gpu-smoke` 现同时实测 G5 packed-band 回读与 G6 WARP
  DirectComposition child HWND 创建/呈现/关闭，而不只检查 capability。Direct2D GPU + transport 回归为
  `201 passed, 1 skipped`，真实 windows-platform G6 冒烟另为 `1 passed`。
- WARP 故障门禁（640×360、60fps）覆盖 60 帧播放、20 次 seek、2 次 resize、2 次 style churn、sidecar kill
  和 100ms 慢帧：播放 `60/60`，`max_pending=1`，kill 后 Painter 回退并在 `72.389ms` 重建 GPU；慢帧释放后
  `119.042ms` 交付最新帧；render/present p95 `1.80/0.25ms`，readback 全程 `0`。
- RTX 3070 Ti 常用单行 glow 原生预览门槛：4K60 连续 300 帧交付 `300/300`，render/present/roundtrip p95
  `4.43/0.10/5.01ms`；1080p120 连续 600 帧交付 `600/600`，p95 `4.19/0.10/4.73ms`；两组均无 stale、
  failure 或 fallback。相比 G5 的 4K60 roundtrip p95 `20.84ms`，G6 已确认移除 staging/readback/QImage
  瓶颈，但复杂 Utopia/signal/viewport 组合仍需继续优化 Direct2D render core。

G6 首批架构与本机性能门槛已落地，仍不得默认开启。下一批继续做真实视频 30 分钟播放、多显示器/DPR/
最小化恢复、真实 device removed/reset，以及 AMD/Intel 矩阵；Painter 永久保留为 oracle/fallback。

### 2026-07-20（第四十二批）：utopia 委托裁剪修复与 G7 性能专项立项

- 修复 463389d 引入的 utopia 回归：`utopiaCharWipe` 在"本字唱完、下一字仍在唱"的委托窗口把裁剪
  bounds 也取自下一字几何，导致刚唱完的字正文/描边被整体裁掉、只剩发光剪影（用户可见为粉色 blob）。
  修法：裁剪矩形恒用本字自身包围盒，仅 wipe edge 沿委托字形区间推进；委托目标几何缺失时回退本字
  unclamped 行为。shadow/正文/描边三条调用路径同时修复，ruby 路径无委托不受影响。
- 新增回归用例 `test_gpu_utopia_keeps_finished_char_body_while_next_char_wipes`：双字 track 取
  t=1500ms 落在委托窗口，统计首字区域实心 after 像素并对照 Painter oracle。红绿验证：旧逻辑下
  GPU 实心像素为 0（精确复现 blob），修复后通过。GPU backend + transport 回归 `202 passed, 1 skipped`。
- 顺带发现独立 parity 缺口：utopia 唱字期间 GPU 与 Painter 字形缩放差约 1.2x（Painter 更大），在现有
  14px 包围盒容差下未被门禁捕获，待单独对齐。
- 完成 4K render-core 性能审计与 N3 发光架构逆向（`SubtitleAction.DrawOneLineDecorBlurMulti`、
  `Utopia.CreateUtopiaTransform`、`VideoPlayer` 常驻 work bitmap），确认 utopia×发光 20fps 的根因是
  per-char 全画布发光层，而非 Direct2D 矢量绘制能力；普通走字 render 4K 已达标。结论与目标持久化为
  §5 G7 专项（发光行级化、scratch 常驻化、导出流水线化、预览质量档位）。

### 2026-07-20（第四十三批）：G7 第 1～3 项落地——发光行级化、scratch 常驻化与导出流水线

- **发光行级化（f8dea29）**：首刀"全量烘焙变换 + 行级统一模糊"虽是 N3 语义，但在 Painter oracle
  18px 容差下失败——Painter 对 spin/utopia 的语义是"模糊上正字形、再对模糊结果施加变换"，缩放动画
  期发光外扩会差出约 3σ（实测 spin 入场右边界差 36px、utopia 入场四边差 13～19px）。最终架构改为
  按"本帧动画矩阵是否恒等"分流：恒等字符共享行级 glow source（普通行合并 before/after 单源 + 相位
  排序；行内混排与 ruby 按样式分组，utopia 行沿用 `wipePhaseAt` 逐字符相位与 `utopiaCharWipe`/
  `utopiaRubyUnitWipe` 动画裁剪），仅变换中的字符/ruby 单元保留旧的逐字符"模糊后变换"层。稳态唱字
  帧从每帧 30～60 次全画布模糊降到每行一组 sigma，动画突发窗口（入出场 ≤750ms）自动回落逐字符层
  保证视觉语义。
- **scratch/effect 常驻化（f8dea29）**：glow scratch 位图与 GaussianBlur effect 池挂 impl_（上限 8，
  每行合成 flush 后回卷，突发超额条目随帧释放），configure 尺寸变化/设备重建时清池；scratch Clear
  用 aliased 裁剪限定到内容矩形，合成 `DrawImage` 传 targetOffset+imageRectangle 只求值行/字符邻域，
  借 Direct2D effect 图按需求值把单次模糊成本与画布尺寸解耦（这一步同时把仅发光场景 p95 从 6.5ms
  压到 2.7ms）。
- **4K RTX 3070 Ti bands 基准（对照 §5 G7 立项表）**：utopia+发光+ruby 20.5fps/23.87ms →
  **60.3fps/2.74ms（p95 5.82ms）**；utopia+发光 23.6→78.9fps；仅发光 54.8→81.9fps（p95 2.68ms）；
  仅 utopia 57.7→100.3fps；普通走字+ruby+发光 54.0→71.8fps。显存稳态增长 0～33MB（至多一张突发
  scratch），无分配洪流。GPU backend oracle 套件 126 passed, 1 skipped；transport+export 84 passed。
- **导出流水线化（98fa4da）**：`gpu_render_frame` 新增 `slot_count`（clamp 1～4），按
  `frame_index % slot_count` 写共享 ring 并在响应元数据回报真实槽位；Python 客户端拆分
  `begin_render_gpu_frame`/`finish_render_gpu_frame`，`iter_gpu_rgba_frames` 双槽一帧深度流水
  （先收第 N 帧响应、立即发第 N+1 帧请求、再展开第 N 帧），sidecar GPU 渲染/回读与 Python band
  展开/RGBA 转换重叠。新增真实 sidecar 测试钉住"流水线输出与串行单槽逐字节一致"。4K
  utopia+发光+ruby 180 帧导出 34.5→53.0fps（1.54x，达 ≥1.5x 门槛）。
- 顺带发现（与 G7 无关、未修）：gpu_backend 与 native_protocol 两个测试文件在同一 pytest 进程中
  先后运行时，protocol 的 utopia 像素对照测试（CPU sidecar vs Painter）会因进程内 QApplication
  平台插件被先创建的 GPU 测试固定而超差；两文件各自单独运行全绿，已登记为独立修复任务。
- G7 第 4 项预览质量档位仍待产品侧确认交互后排期；GPU 产品开关与 G6 原生预览默认状态不变。

### 2026-07-20（第四十四批）：真实 N3 工程 Utopia 恒等描边快路径

- 用用户工程 `25 m.n3proj`（1920×1080@60、47 行/537 字/109 组 ruby、全局 24px 中档 glow）复现了
  隔离基准遗漏的退化：20～25 秒密集段无动画 render mean `5.20ms`，强制全局 Utopia 后升到
  `26.71ms`，300/300 帧超过 16.67ms；把 glow 从 24px 降至 10px 仍为 `26.50ms`，完全关闭 glow
  仍为 `25.60ms`，证明本轮根因不是 G7-1/2 的发光路径。
- 根因是正文与 ruby 描边选择预扩轮廓时使用了“本行是否启用 Utopia/spin”的全局判断。Utopia 稳态
  实际通常只有当前唱字/注音单元的矩阵非恒等，但整行所有已稳定字符仍每帧 `FillGeometry` 复杂的
  widened stroke path；长行因此承担几十次不必要的预扩轮廓填充。现在正文、ruby 和 shadow silhouette
  均按**该字本帧是否真实 transformed**分流：只有非恒等矩阵使用预扩轮廓，恒等字恢复 Direct2D
  `DrawGeometry` 描边。全局无角色行同时复用已经创建的六个 line-level brush，避免逐字重复创建 COM brush。
- 修复后同一 300 帧密集段 Utopia+24px glow render mean `11.46ms`（`2.33x`），超过 16.67ms 的帧由
  `300/300` 降到 `53/300`；关闭 glow 为 `9.29ms`。真实 GUI 反复播放 23 字最密集行时 render mean/p95
  `9.08/15.78ms`，核心重新进入 60fps 预算。离屏端到端 FPS 仍受 4K 视频解码、JSON/共享内存/QImage
  与 Qt 主线程调度影响且复跑波动较大，因此继续按 §6.2 拆分指标，不用单一 FPS 代替 render 结论。
- 排除了“把 glow scratch 常驻上限从 8 提到 64”的临时假设：同场景 render mean `18.67→22.06ms`，
  进程 RSS 增长约 `428→562MiB`，既变慢又增压，未保留该改动。`stress_gpu_preview_gui.py` 新增
  `--animation` 覆盖项，后续可在不改用户工程的情况下做 `project/none/.../utopia` 同场景 A/B。

### 2026-07-20（第四十五批）：默认 G5，停止 G6

- 经用户最终决定，Windows 交互会话默认使用稳定的 G5 shared-memory/QImage GPU 预览，并默认启用
  GPU 字幕导出；两项旧配置分别执行一次版本迁移，之后仍尊重用户手动关闭。
- G6 DirectComposition 不再继续，产品判定函数硬返回关闭；即使遗留环境中存在
  `KROK_SUBTITLE_GPU_NATIVE_PREVIEW=1` 也不会进入 G6。
- Painter 永久保留为无 GPU、能力检查失败和运行异常时的 fallback；离屏测试环境默认不开启 GPU。

### 2026-07-20（第四十六批）：Utopia 与对侧逐字过渡组合修复

- 修复 Utopia 与另一侧“逐文字渐隐”或“旋转翻转”组合时，后者在 Windows 默认 G5 路径完全失效的问题。
  根因是 Painter 与 Direct2D 都把“任一侧配置了 Utopia”当成整段可见期的全局优先级，Direct2D 还据此
  跳过了逐字透明度与旋转矩阵，导致组合帧与未配置对侧特效的参考帧完全一致。
- 现在按本帧活动窗口决定上下文：若另一侧的逐字渐入/渐出正在执行，则该逐字过渡临时优先；窗口结束后
  恢复 Utopia 稳态路径，保留其唱中弹跳、擦除和避免静态/逐字路径切换色闪的既有语义。Utopia 作为渐入
  或渐出两种方向均采用同一规则。
- 新增 4 组真实 Direct2D 帧回归，覆盖 Utopia 入/出场 × 逐文字渐隐/旋转翻转，并与 Painter 的相对 alpha
  变化对照；同时补齐 Painter 上下文的双向组合测试。GPU 文件除一项既有 N3 ruby 字体边界容差用例外，
  其余 `135 passed, 1 skipped`；Painter 文件 `303 passed`。

### 2026-07-20（第四十七批）：N3 ruby→正文切分条件校准

- 用代码内最小回归 `メロディー / melody` 复核 N3 10.74.80.0
  `DrawDataGenerator.SetOneLineWipe()`：ruby 组内只要存在正文显式 begin/end 边界，N3 就保留正文
  自己的逐字时钟；仅组内无边界时才调用 `RubyTimesToKanjiTimes()` 按注音可视字符位置映射正文。
- `TimingChar` / Render IR 新增 `explicit_start`、`explicit_end`，LRC 与 SUG 直读保留时间戳来源；
  Painter 与 Direct2D 的两种 ruby 正文模式使用同一显式边界优先规则。仅在正文组内没有独立边界时，
  `checkpoint_segments` 才按 ruby 时间段数均分，`reading_units` 才按 ruby 可视字符数映射。
- 回归钉住 `d` 起点对应正文 `デ` 起点（2/5）和 `y` 起点对应 `ィ` 起点（3/5），并保留“正文
  无内部边界时按注音字符映射”的既有覆盖；真实 WARP GPU 帧与 Painter 比例差门禁通过。

### 2026-07-21（第四十八批）：导出全量 realization 屏障与有界多 worker

- GPU 导出不再复用预览的 8192 项 realization 容量：第一帧前按 256 MiB 默认预算把容量扩到 65536，等待所有主字幕/副字幕的 fill、protected stroke、stroke、stroke2 任务完成，并以 5% 粒度报告准备进度；等待过程可取消，超大工程仍受 8192～262144 动态上限约束。真实 4K 工程共 2117 项任务在 `1.82s` 内完成，首帧诊断为 realization miss 0。
- sidecar 硬件导出新增 2/4 worker 独立 Direct2D context 与有界共享环。完成事件可乱序返回，但 Python 仅保留一个 ring window，按 `frame_index` 严格顺序交付 ffmpeg；释放在途 credit 后才回写响应，避免消费者立即复用槽位时误报 queue full。产品硬上限为 4，WARP 固定为 1。
- 真实 `25 m.n3proj` 4K60、30 秒背景解码 + overlay + NVENC 端到端结果：1/2/4 worker 分别为 `82.46/80.45/83.88s`（`21.83/22.37/21.46fps`）；ffmpeg 管道累计等待 `24.66/17.50/15.27s`，sidecar 峰值 RSS `208/330/548 MiB`，DXGI 本地显存占用 `177/354/709 MiB`。2 worker 仅快约 2.5%，但连续两轮都优于 1；4 worker 因重复 realization、额外 context 和下游瓶颈反而变慢，因此硬件默认 2、保留环境变量选择 1～4，不照搬 N3 的 8 worker。
- 顺序/资源门禁覆盖乱序完成、ring window 上限、取消和诊断回收。1/2/4 输出均为 1800 帧；独立 context 首次命中新内容存在 Direct2D 栅格缓存的微小非位级差异（同一 4K 帧 19 像素、最大通道差 2），会改变有损编码 GOP 哈希，但不是丢帧或错序。Python 导出测试 `61 passed`，真实 native worker/流水线/realization 定向测试 `3 passed`。
