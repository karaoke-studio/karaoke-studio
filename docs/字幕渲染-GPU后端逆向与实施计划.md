# 字幕渲染 GPU 后端：NicoKaraMaker3 逆向结论与实施计划

> 状态：设计完成，尚未开始实现  
> 最后更新：2026-07-19  
> 逆向基准：NicoKaraMaker3 10.74.80.0 x64  
> 产品基线：Python QPainter 仍是唯一正式字幕渲染路径；本文不改变当前产品开关

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
- 正式产品默认不开启 GPU；只有达到本文验收门槛并完成显卡矩阵验证后，才讨论默认开关。

### 0.2 当前没有做的事情

- 尚未创建 Direct2D backend 源码、CMake target 或产品设置项；
- 尚未修改 Python 预览/导出选择逻辑；
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
- native preview/export 开关在产品代码中硬返回 false。

### 2.2 已证明的 GIL 边界

PyQt QThread 池实测约 1.07x，关键 QPainter/PyQt 调用期间无法获得有效线程并行；导出使用
multiprocessing 可以绕开 GIL，但预览用多进程会增加调度、shared memory、seek generation 和销毁复杂度。

### 2.3 已有 native 资产

可复用：

- `krok_helper/subtitle_render/native_protocol.py`：Render IR v1；
- `krok_helper/subtitle_render/native_backend.py`：JSON-lines sidecar client；
- `krok_helper/subtitle_render/engine/native_export.py`：range render/shared-memory adapter；
- `native/subtitle_renderer/`：CMake、Qt C++ sidecar、共享内存 frame ring；
- native protocol/benchmark/export/transport 测试；
- generation、取消、乱序 frame ready 重排、超时和进程退出诊断。

不可直接复用为 GPU 实现：

- 当前 native 核心仍是 QImage + QPainter CPU raster；
- 其缓存 parity 不完整，且 vertical/title/signal 等没有达到当前 Python 功能全集；
- 2026-07-11 后产品策略已硬关闭 native 路径。

### 2.4 当前性能参考

`.bench/bench_render_20260717_162928.csv` 的 1080p `full` 微基准：

- paint mean：约 4.63ms；
- alloc/fill：约 1.22ms；
- copy：约 1.06ms；
- total mean：约 6.91ms，纯字幕理论约 144.7fps。

这不是端到端视频预览/导出速度。GPU 项目必须证明它改善的不是一个已经足够快的微基准，而是
真实重工程中的预览刷新、主线程响应、4K/120fps 或动态重特效瓶颈。

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

### G0：环境与最小 GPU 探针（3～5 天）

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

### G1：横排字幕核心（1～2 周）

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

### G2：实验预览接线（1～2 周）

覆盖：

- 复用 `NativeRendererProcess`/generation/cancel/ring；
- Python 只发送 timestamp；
- latest-wins + look-ahead；
- GPU 失败自动回退 Python `AsyncSubtitleRenderer`；
- 仅开发环境显式开关，产品默认硬关闭；
- seek、resize、style churn、工程切换、关闭窗口 teardown。

首期允许 GPU readback -> shared memory -> QImage。此阶段测量读回是否吞掉 GPU 收益。

验收：

- 1080p60 普通横排稳定 60fps；
- 重 glow 场景字幕 ready rate、p95 latency、steady drop 优于 Python；
- 连续播放 30 分钟、seek 500 次、resize/style churn 无崩溃；
- Python GUI 主线程响应不随字幕 paint 增长；
- renderer kill/restart/fallback 可恢复。

### G3：常用功能达到可用 MVP（2～4 周）

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

### G4：高级功能 parity（1～3 个月）

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

### G5：GPU 导出实验（2～4 周）

- GPU 只渲染透明 subtitle strip/bands；
- staging readback 后仍交现有 ffmpeg overlay/encoder；
- 不在第一版重写背景视频解码；
- 不把“GPU 字幕渲染”和“NVENC/QSV/AMF 编码”混为一个开关；
- 保留 60/120fps、取消、进度、半成品删除和 CPU fallback。

达到收益门槛后，再评估 D3D11 texture -> hardware encoder/filter 的 vendor-specific 零拷贝方案。

### G6：共享 GPU 纹理/原生预览（可选，2～4 周起）

目标是去掉 G2 的 GPU readback/QImage blit，接近 N3 的 SwapChain 路径。必须单独立项，因为它会涉及
PyQt/native HWND、Qt Multimedia、统一视频时钟和 teardown，不是 Direct2D 字幕 backend 的必要前置。

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
- 把硬件编码速度误算为 GPU 字幕渲染收益。

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

如果用户只说“继续 GPU 工作”但没有指定阶段，默认从尚未完成的最早阶段开始；当前是 **G0**。

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

