# GPU 渲染导出性能优化执行计划书

> 状态：执行中；阶段 1 的 R2 收益门槛未通过；按用户要求补做的阶段 3 共享 Device/realization 探针也未通过端到端收益门槛，产品默认保持关闭；N3 隔离重编译与同条件分段导出计时已完成
> 创建日期：2026-07-21
> 目标平台：Windows GPU 字幕导出后端
> 参考计划：[`GPU预览宽描边性能优化执行计划书.md`](GPU预览宽描边性能优化执行计划书.md)
> 上游设计：[`字幕渲染-GPU后端逆向与实施计划.md`](字幕渲染-GPU后端逆向与实施计划.md)

## 0. 计划目的

当前 GPU 预览管线已经达到可用性能，下一阶段不再继续扩大预览侧优化范围，而是单独优化 GPU 导出管线。

本计划以 NicoKaraMaker3（下称 N3）10.74.80.0 的已逆向导出架构为参照，但不把“结构相似”当作成功。所有改造都必须先证明当前瓶颈，再按阶段落地，并用端到端导出吞吐、画质、稳定性和回退能力决定是否保留。

最终希望得到一条真正面向离线导出的管线：

1. 一个导出会话只准备一次静态场景、字形和可复用资源；
2. 多个输出 worker 共享同一套不可变场景资源，各自持有独立的绘制上下文和帧资源；
3. 帧在 native 侧完成渲染、读回、排序和输出，不再逐帧经过 `QImage -> RGBA -> bytes -> Python`；
4. 动态特效继续使用实时 geometry 路径，静态字符才使用高精度 realization；
5. 预览、CPU QPainter、取消、错误处理和 ffmpeg 编码能力不因优化而退化。

## 1. 北极星与不可突破的边界

### 1.1 北极星指标

在相同项目、相同输出分辨率、帧率、背景、编码器和编码参数下：

- 4K60 重特效项目的端到端 GPU 导出速度，至少比阶段 0 基线提高 **50%**；或在能取得 N3 同条件数据时，与 N3 的差距缩小到 **15% 以内**；
- 1080p60 普通项目、4K60 普通项目不得比阶段 0 慢超过 **5%**；
- 输出帧数、帧顺序、时长和音画同步与阶段 0 GPU 导出基线一致；
- 纯传输、调度和资源共享优化不得改变同一 Direct2D 场景的输出像素；确实改变 Direct2D 栅格化路径的阶段，只能与阶段 0 Direct2D 基线做明确的同引擎 A/B；
- GPU 失败后仍能回退到 CPU 导出，取消和错误传播仍然可靠。

以上是项目级完成标准。任何单项微基准变快，都不能代替端到端结果。

### 1.2 明确不做

本计划不包含：

- 重启或继续 G6 DirectComposition；
- 改写已经合格的 GPU 预览呈现路径；
- 删除 CPU QPainter oracle 或 fallback；
- 修改字幕排版、走字、ruby、Utopia 或其他特效语义；
- 为速度把 realization 容差从 `0.25f` 放宽到 `3.0f`；
- 修改 SUG submodule；
- 增加 Windows 以外的 GPU 导出后端；
- 增加 MP4、60/120fps 之外的新产品能力；
- 在尚未证明 ffmpeg 合成是瓶颈前，自建完整的视频解码、音频同步和色彩管理管线；
- 为追求 N3 的外形而照搬其类结构或反编译代码。

### 1.3 防做偏总规则

1. **先测量，后改架构。** 阶段 0 没有解释至少 90% 的墙钟时间前，不进入共享 Device 或 native 合成改造。
2. **同条件比较。** 分辨率、fps、项目、背景、编码器、preset、码率/CRF、硬件、预览开关必须记录并固定。
3. **每阶段只有一个主要变量。** 传输、共享资源、worker 数量、背景合成不能混在同一个性能结论里。
4. **每阶段有独立回退开关。** 新旧实现至少保留到全局回归结束，不能边做边删除基线。
5. **预览路径冻结。** 导出改造不得默认改动预览调度；预览固定基准下降超过 5% 即阻断。
6. **单 worker 先过门。** 未证明单 worker 的导出专用管线正确且更快前，不用增加 worker 掩盖串行开销。
7. **不把编码器等待算成字幕渲染问题。** 如果同条件下 70% 以上时间都阻塞在编码器，先停止 native 架构改造。
8. **不把合成假定成瓶颈。** 只有 ffmpeg overlay/filter 占端到端时间达到 20% 时，才允许进入阶段 4。
9. **不假设 8 worker 最快。** 必须实测 1/2/3/4/8，按吞吐、尾延迟和显存共同选择。
10. **realization 要么正确共享，要么关闭。** 不再保留“每个 backend 独立以 `0.25f` 准备整套 realization”的产品路径。
11. **真实项目优先。** 合成场景用于定位，最终门槛必须由真实 `.yurika`/`.n3proj` 项目通过。
12. **达不到最小收益就停止。** 每阶段都设保留门槛；未通过时回滚该阶段，不继续围绕失败设计追加复杂度。

### 1.4 三种基准不得混用

| 基准 | 用途 | 允许得出的结论 |
|---|---|---|
| 阶段 0 Direct2D 导出 | 本计划的画质与行为回归基线 | 优化前后的同引擎输出是否改变 |
| N3 同条件导出 | 端到端性能和架构参照 | 当前导出吞吐与 N3 的差距；不能据此要求像素相同 |
| CPU QPainter | 功能语义参考与故障回退 | 歌词内容、时间、走字方向、图层有无、动画阶段和大体布局是否正确；不能作为 Direct2D 像素基线 |

DirectWrite/Direct2D 与 Qt/QPainter 使用不同的字形 hinting、抗锯齿覆盖、描边连接、模糊核和 alpha 合成实现。即使两者语义都正确，也不应期待像素级一致。因此：

- 北极星指标不包含 GPU/CPU 像素对齐；
- transport、pipe、worker 调度等不应改变画面的阶段，要求 Direct2D 优化前后逐像素一致；
- realization 或合成方式确实可能改变 Direct2D 栅格结果时，只与阶段 0 Direct2D 输出做同引擎 A/B，并为该阶段单独制定边界、MAE 和可见差异门槛；
- CPU 只负责发现缺字、漏层、错误时序、错误走字、严重裁切或完全不同的布局语义，不用跨引擎像素 diff 决定性能优化是否通过。

## 2. 已确认的事实与仍需验证的假设

### 2.1 N3 已逆向确认的导出结构

逆向证据位于 `.reverse/n3_decomp/`，执行前必须重新核对以下文件对应版本：

- `NicoKaraMaker3.Models.MoviePlayers/VideoPlayer.cs`
- `NicoKaraMaker3.Models.MoviePlayers/VideoWriter.cs`
- `NicoKaraMaker3.Models.Media/DirectXCommon.cs`
- `NicoKaraMaker3.Models.AddOns.SubtitleActions/Utopia.cs`

已确认：

1. 全程序从同一个 Direct2D Device 创建绘制上下文；
2. 导出 worker 各自持有长期存活的 `DeviceContext`、输出 target 和 work bitmap；
3. 字幕布局与字形在正式输出前统一准备；
4. 每个稳定字符只生成一套共享的 Filled / Stroked / Stroked2 realization，容差为 `0.25f`；
5. Utopia 等动态变换帧临时使用动态 geometry，未变换的稳定字符仍回落到共享静态 GeometryPack；
6. 输出 worker 通过有界环形结构并行处理帧，并按顺序向 ffmpeg 输入写入；
7. N3 的预览和导出共享字幕绘制核心，但输出 sink 不同：预览呈现，导出读回并编码。它们不是两套完全无关的 renderer。

### 2.2 当前工程已确认的结构

当前 GPU 导出快速路径涉及：

- `krok_helper/subtitle_render/engine/renderer.py`
- `krok_helper/subtitle_render/engine/native_export.py`
- `krok_helper/subtitle_render/native_backend.py`
- `native/subtitle_renderer/src/main.cpp`
- `native/subtitle_renderer/src/backends/direct2d/d2d_backend.cpp`
- `native/subtitle_renderer/src/backends/render_backend.h`

当前每帧大致经过：

```text
Python 请求帧
  -> sidecar JSON 命令
  -> Direct2D 绘制
  -> GPU staging readback
  -> native 内存复制
  -> shared-memory ring
  -> Python 等待 JSON event
  -> shared memory 复制/按 band 展开成整帧 QImage
  -> QImage 转 RGBA8888
  -> QImage 转 bytes
  -> Python 写 ffmpeg stdin
  -> ffmpeg 合成背景、字幕和音频
  -> 编码器
```

已知问题：

- band readback 降低了 GPU 读回字节，但 Python 侧仍会创建、清空和展开整帧 `QImage`；
- 最终 rawvideo 管道仍按整帧 RGBA 传输，band 不能减少 ffmpeg 输入字节；
- Python 逐帧参与格式转换、对象创建和 `stdin.write`；
- 多 worker 当前是多个完整 `Direct2DGpuBackend`，静态场景准备成本会随 worker 数量重复；
- 当前 realization 已因 `0.25f` 下重复准备过慢而在导出配置中关闭；
- 既有 G7 数据显示，4K Utopia + glow + ruby 的 native 绘制均值已约为 2.74ms，而完整导出仍明显更慢，说明不能继续只盯着绘制核心。

### 2.3 尚未证明、禁止先入为主的事项

- Python/QImage 桥接在各类项目中究竟占多少；
- ffmpeg overlay、缩放、预览 JPEG 支路和编码器各占多少；
- 共享一个 D3D11/D2D Device 后，读回时 immediate context 的最佳并发方式；
- native 直接写 ffmpeg 后能否获得足够大的端到端收益；
- 视频背景是否值得进入 native 最终帧合成；
- 当前工程与 N3 在完全相同编码条件下的真实速度差距。

这些事项只能由阶段 0 和各阶段 A/B 数据决定。

## 3. 固定基准矩阵

### 3.1 固定项目

所有阶段至少覆盖以下项目：

| 编号 | 项目 | 目的 |
|---|---|---|
| R1 | `C:\Users\18007\Downloads\芽吹の唄 - 大原ゆい子.yurika` | 真实 ruby、渐变和常规歌词项目 |
| R2 | 固定的 Dark Spiral / Utopia 重特效项目 | 动态 geometry、glow、ruby 压力 |
| S1 | 普通主文字 + ruby，无 glow | 基础吞吐与桥接成本 |
| S2 | Utopia + glow + ruby | 当前核心重特效基准 |
| S3 | 14px 描边 + 7px 二重描边 + glow | 宽描边与 realization 基准 |
| S4 | 图片/渐变填充 + 多角色 + 混合字体 | 资源域与复杂样式回归 |

R2 的文件路径在阶段 0 第一次运行时写入基准结果元数据；不得在后续阶段临时换项目。

### 3.2 固定输出条件

必须覆盖：

- 1920x1080 @ 60fps；
- 3840x2160 @ 60fps；
- 1920x1080 @ 120fps，在 60fps 全部通过后执行；
- 纯色背景、静态图背景、视频背景；
- 关闭导出预览和开启导出预览两组；
- 无编码/null sink、固定 x264 软件编码、一个固定硬件编码器三组。

每个性能点：

- 预热 1 次；
- 正式运行至少 3 次；
- 记录 mean、p50、p95、最小值、最大值和变异系数；
- 变异系数超过 5% 时不得下结论，先排查温度、后台负载、编码器动态频率和磁盘。

### 3.3 固定画质帧

R1、R2 各选择至少 8 个固定时间点：

- 句子出现前；
- 走字 0%、25%、50%、75%、100%；
- Utopia 进入、峰值、退出；
- ruby 与主文字同时渐变；
- 宽描边 + glow 最重的一帧。

每个时间点保留阶段 0 Direct2D 基线和新路径截图，并记录同引擎像素 diff。另保留 CPU QPainter 截图只做语义人工审计，不把它纳入像素差阈值。

## 4. 统一性能账本

阶段 0 必须把每帧和每次导出拆成以下字段。后续阶段不能另造一套口径：

| 字段 | 含义 |
|---|---|
| `prepare_layout_ms` | 项目快照、排版和静态场景准备 |
| `prepare_geometry_ms` | geometry 构建 |
| `prepare_realization_ms` | realization 构建 |
| `native_render_ms` | native 绘制命令时间 |
| `gpu_wait_ms` | GPU 完成等待 |
| `readback_copy_ms` | GPU target 到 staging/CPU 的读回 |
| `native_pack_ms` | premultiplied BGRA 到输出像素格式的转换/打包 |
| `shm_copy_ms` | native 到 shared-memory slot 的复制 |
| `protocol_wait_ms` | JSON/event 往返和调度等待 |
| `python_expand_ms` | band 展开/整帧 QImage 填充 |
| `python_convert_ms` | QImage 格式转换 |
| `python_bytes_ms` | `QImage -> bytes` 复制 |
| `stdin_block_ms` | 写 ffmpeg stdin 的阻塞时间 |
| `ffmpeg_filter_ms` | overlay/scale/fps 等过滤链成本 |
| `encoder_ms` | 编码器消耗；无法直接采样时用隔离实验推导 |
| `total_wall_ms` | 从导出开始到 ffmpeg 正常退出 |
| `peak_rss_mb` | 主进程、sidecar、ffmpeg 峰值内存 |
| `peak_vram_mb` | GPU 峰值显存 |
| `bytes_per_frame` | 每帧跨进程和写入管道的字节数 |
| `copies_per_frame` | 可确认的完整帧 CPU 复制次数 |

隔离实验至少包含：

1. native render + readback，不启动 ffmpeg；
2. 固定测试帧反复通过 Python/QImage/pipe，不执行字幕布局；
3. ffmpeg 只接固定 rawvideo，不执行字幕 renderer；
4. ffmpeg 背景处理但无字幕 overlay；
5. ffmpeg 背景 + 固定字幕层 overlay；
6. 完整端到端导出。

各阶段的时间归因必须解释至少 90% 的 `total_wall_ms`。不足部分作为 `unaccounted_ms` 明确列出，不能并入“native 慢”。

## 5. 阶段 0：诊断与基线冻结

> 预计：1～2 个工作日
> 性质：只增加诊断，不改变产品行为

### 5.1 目标

回答三个问题：

1. 当前端到端时间主要花在 renderer、桥接、ffmpeg filter 还是编码器；
2. N3 同条件下的差距是否足以支持后续架构投入；
3. 最先值得删除的是哪一次复制、哪一次跨进程往返或哪段串行等待。

### 5.2 修改文件

- 新增 `scripts/benchmark_gpu_export_pipeline.py`；
- 扩展 `krok_helper/subtitle_render/engine/native_export.py` 的可选诊断统计；
- 扩展 `krok_helper/subtitle_render/engine/renderer.py` 的导出阶段统计；
- 必要时扩展 native 诊断事件，但不得改变帧协议和默认路径；
- 新增/补充对应的定向测试文件。

### 5.3 可执行步骤

1. ✅ 为一次导出生成稳定的 `export_run_id`；
2. ✅ 把第 4 节字段写入一个 JSON 汇总和逐帧 CSV；
3. ✅ 诊断默认关闭，只通过环境变量或 benchmark 参数开启；
4. 把项目 hash、Git commit、GPU、驱动、CPU、内存、编码器、ffmpeg 命令、分辨率、fps、worker 数、背景类型、导出预览状态写入元数据；
5. 跑完第 3 节矩阵，并将结果保存在 `build/gpu-export-stage0/`；
6. 在 N3 中使用相同源、输出规格和编码设置执行 R1/R2；若无法完全一致，逐项写明差异，不得直接宣称倍率；
7. ✅ 生成阶段 0 决策表，不做默认路径架构改动。

### 5.4 阶段门槛

- 开启诊断后的端到端开销不超过 2%；
- 三次运行变异系数不超过 5%；
- 性能账本解释至少 90% 的墙钟时间；
- 固定画质帧与阶段 0 Direct2D 输出完全一致；
- 能单独量出 Python/QImage 桥接、stdin 阻塞、filter 和 encoder 的量级。

### 5.5 强制决策

| 阶段 0 结果 | 后续动作 |
|---|---|
| `stdin_block + encoder` >= 70% | 暂停 native 重构；先评估编码器配置/吞吐 |
| Python/QImage/协议 >= 25% | 进入阶段 1 |
| native render >= 25% | 回到绘制热点诊断；不得用多 worker 掩盖 |
| ffmpeg filter >= 20% | 允许在阶段 3 后评估阶段 4 |
| 同条件下比 N3 慢 < 15% | 不执行完整对齐；只做低风险复制消除 |
| 无法稳定复现 | 停止；先修复 benchmark 和环境 |

### 5.6 当前执行结论（2026-07-21）

| 场景 | 阶段 0 结果 | 决策 |
|---|---|---|
| R1 重特效短区间 | `native_render` 约 124.42 ms/帧，其中 `EndDraw`/驱动等待约 123.98 ms/帧（约 99.6%） | 绘制热点占主导，不用增加 worker 掩盖 |
| 4K 普通项目 | packed 固定裁剪端到端约快 34.3% | 保留阶段 1 实验路径用于 A/B |
| R1 重特效 11 帧 | packed 内部写帧约快 0.6%，短测端到端反而退化 | 未达到 R2 至少 15% 门槛；默认关闭，并停止进入阶段 2/3 |
| 《メフィスト》1080p60 / 35 秒重负载段 | Direct2D 每帧约 10.88 次 `EndDraw`；合成约 7.39 ms、主字发光源约 4.70 ms、ruby 发光源约 1.20 ms | `EndDraw` 主要在执行真实模糊/合成工作，不把减少函数调用次数误判成优化 |
| 《メフィスト》2 秒 pipe A/B | 合并提交边界慢约 6.6%；整帧 packed 慢约 10.1% | 两项均不进入默认路径；不以画质或复杂度换取负收益 |
| 《メフィスト》2 秒 pipe / 原 QImage 路径 | 2 worker 69.08 fps，3 worker 75.36 fps，提升约 9.1%；1/2/3/4 worker 连续 60 帧逐字节一致 | 硬件 GPU 导出默认改为 3 worker；WARP 保持单 worker，可用环境变量回退 |
| 《メフィスト》5 秒视频背景 + x264 `veryfast` | 2 worker 57.40 fps，3 worker 62.20 fps，短端到端提升约 8.4% | 收益低于阶段 1 的 15% 晋级门槛，但属于低风险默认调优；阶段 2/3 仍暂停 |
| 发光 scratch 复用 | 旧路径在画面右侧最多 21 px 会受同一 worker 上一帧内容影响 | 改为安装局部 clip 前清空整个 scratch target，消除边缘残影和 worker 历史依赖 |
| 《メフィスト》描边 15、发光保留、无 Utopia，5 秒 pipe | 共享 Device + 一套共享 realization：`EndDraw` 14.75 → 11.54 ms/帧（-21.7%），但 `native_render` 15.24 → 25.18 ms/帧（+65.2%），稳定吞吐 100.71 → 77.73 fps（-22.8%），另需 30.92 秒 realization 预热 | 共享 Device 的跨 worker 提交争用大于 realization 收益；默认关闭 |
| 《メフィスト》描边 15、发光保留、Utopia 入场/出场，5 秒 pipe | 共享 Device + 一套共享 realization：`EndDraw` 67.53 → 48.13 ms/帧（-28.7%），但 `native_render` 68.39 → 130.06 ms/帧（+90.2%），稳定吞吐 35.62 → 20.44 fps（-42.6%），另需 30.73 秒预热 | Utopia 动态 geometry 仍正确回退，但共享队列争用更严重；默认关闭 |
| 《メフィスト》29～34 秒真实视频背景 + x264 `veryfast` MP4 | 无 Utopia：4.74 → 34.32 秒（63.31 → 8.74 fps）；Utopia：9.32 → 44.97 秒（32.20 → 6.67 fps）；四个成品均为 1920×1080、60fps、300 帧、5.000 秒 | 包含约 31 秒预热后的真实用户等待时间大幅退化，最终性能门槛失败 |
| 同项目独立 Device + 每 worker realization 隔离对照 | 无 Utopia：`EndDraw` -28.6%、稳定吞吐 +14.7%；Utopia：`EndDraw` -9.4%、稳定吞吐 +6.9%；但三套 realization 预热分别需 35.92/36.66 秒 | 证明 realization 本身有效、失败点主要是共享 Device；预热无法在本项目时长内稳定回本，不进入默认路径 |
| 共享 realization 画质门禁 | 合成用例可见区域平均通道差 0.05～0.53/255、P99 ≤ 19、轮廓 IoU=1.0；《メフィスト》1920×1080 无/有 Utopia 实帧均通过 MAE ≤ 3、轮廓 IoU ≥ 0.98 | 未减少描边、发光或 `0.25f` 精度；画质门禁通过，但性能门禁失败 |
| realization 预热分阶段计时 | 描边 15 的共享单套预热 29.69 秒，其中 `CreateStrokedGeometryRealization` 29.62 秒（99.8%）；context 0.012 ms、等待 14.54 ms、fill 54.84 ms、发布 1.38 ms | 慢点已经精确落在 D2D 宽描边 realization 创建，不是 Python 轮询、线程调度、锁或设备冷启动 |
| 《メフィスト》原项目描边与强制 15px 对照 | N3 项目 598 个主字符实际均为 10px 描边；同一实现预热由强制 15px 的 29.69 秒降至 10px 的 11.86 秒（-60.0%） | 先前“N3 预热很短”并非与强制 15px 压测完全同负载；宽度对创建成本呈明显非线性，但不能为性能改小用户描边 |
| realization 替代准备策略 | 同步准备 30.23 秒；先热设备再准备 29.90 秒；预扩边后做 filled realization 28.43 秒 | 三项均未达到收益门槛，相关实验实现已移除，不进入默认路径 |
| realization 后台摊薄，3 worker、描边 15、5 秒纯 native | realization 关闭时无/Utopia 均约 221 fps；后台准备后分别为 43.6/50.5 fps（-80.3%/-77.2%），仅完成 586/4629、583/4629 个任务 | 创建过程争抢同一 GPU/Direct2D 提交域，不能用后台化隐藏；实验实现已移除 |
| 共享 Device readback 事务互斥 | 5 秒稳定吞吐 65.09 → 64.40 fps（-1.1%），锁等待仅 0.00068 ms/帧 | readback 排队不是共享 Device 退化主因；保留原有并发策略，实验互斥已移除 |
| N3 隔离重编译，描边 15、发光保留、29～34 秒、x264 `veryfast` | 无 Utopia：realization 28.78 秒，分段输出 8.78 秒（34.19 fps）；Utopia：realization 29.07 秒，分段输出 9.87 秒（30.39 fps） | N3 的强制 15px realization 与当前实现同样慢；此前“预热明显更快”的观察不能外推到 15px 同负载 |
| 同条件当前默认工作台 | 无 Utopia：5.06 秒（59.24 fps）；Utopia：10.21 秒（29.39 fps） | 普通路径工作台明显快于 N3；Utopia 稳态吞吐与 N3 接近，但工作台相对自身普通路径退化约 50%，这是下一项真正值得优化的差异 |
| N3 提交点与读回拆分 | 外层 `EndDraw` 均值仅 0.72/0.65 ms；每帧 29 次发光内部 `EndDraw`，单次均值 0.69/0.70 ms；WIC BMP 读回均值 36.52/42.78 ms；stdin 写入约 2.42/2.43 ms | N3 不是靠消灭 `EndDraw` 或零拷贝取胜；其慢工作分散在重复 blur 提交、字幕绘制和 WIC 读回，并由 8 worker 重叠 |
| Utopia 相对普通路径的退化 | N3 分段输出 8.78 → 9.87 秒（+12.5%）；工作台 5.06 → 10.21 秒（+101.6%），工作台 `frame_layers EndDraw` 均值 11.66 → 63.33 ms | 重点改为工作台动态 Utopia 描边/图层提交；N3 对动态变换 geometry 直接 `DrawGeometry`，当前工作台对动态描边使用预扩边 geometry 再 `FillGeometry`，应先做保持视觉语义的窄 A/B，不能继续优化 realization |
| N3 式动态描边，3 轮 pipe 中位数 | Utopia：动态 geometry 11.33 → 5.62 个/诊断帧，`frame_layers EndDraw` 51.84 → 5.28 ms，稳定吞吐 41.53 → 124.66 fps；旋转翻转：1.96 → 0.99 个、15.54 → 5.00 ms、85.78 → 126.44 fps；逐字垂落：2.12 → 0.99 个、15.16 → 5.06 ms、86.03 → 124.83 fps | 三种真实几何变换统一改为“只变换基础字形 + 对变换后字形直接 `DrawGeometry`”；逐字淡入淡出只有透明度变化，继续复用静态 geometry/realization |
| 同项目真实视频背景 + x264 `veryfast` MP4 | Utopia：9.60 → 4.36 秒（31.26 → 68.87 fps）；旋转翻转：5.40 → 4.35 秒（55.54 → 68.97 fps）；逐字垂落：5.29 → 4.35 秒（56.75 → 68.91 fps） | 六个成品均为 1920×1080、60fps、300 帧、5.000 秒；动态特效已不再把最终导出压到 60fps 以下 |
| 动态描边画质门禁 | 三种特效逐帧最低整帧 SSIM 为 0.9854～0.9951，最差帧人工抽检保留描边、二重描边、ruby 与发光；透明图片填充兼容分支可见通道均差 ≤ 1/255、P99 ≤ 16、轮廓 IoU ≥ 0.995；CPU 逐字垂落 + glow 定向语义对照通过 | 不删除视觉层、不减小用户描边/发光；透明渐变、图片填充等需要防止描边透入字腔时，继续使用 outside-only 保护轮廓 |

N3 10.74.80.0 的逆向结果确认其采用共享 Device、长期 worker、一次性场景准备和严格有序输出，但最终帧仍经过 WIC BMP、CPU 行翻转和 ffmpeg stdin，不是零拷贝路径。当前阶段只吸收其会话生命周期与调度思想，不复制 WIC 传输链。

### 5.7 N3 隔离计时补充步骤（2026-07-22）

1. ✅ 在临时目录重编译 N3 10.74.80.0，并验证替换后的 DLL 可由原版 apphost 与资源正常启动；
2. ✅ 只修改临时副本，保留原安装、原 DLL 和原 `.n3proj` 不变；
3. ✅ 增加命令行直达导出入口和 realization、逐帧绘制、内外层 `EndDraw`、WIC 读回、stdin、ffmpeg 分阶段计时；
4. ✅ 固定《メフィスト》29～34 秒、1920×1080、60fps、描边 15、保留发光、x264 `veryfast`/CRF 18/无音频，分别执行无 Utopia 与 Utopia；
5. ✅ 用当前默认工作台执行同项目、同区间、同编码参数的无 Utopia/Utopia 对照；
6. ✅ 用 ffprobe 验证四个成品均为 60fps、300 帧、5.000 秒，并抽帧确认描边与发光未被删减；
7. ✅ 将 N3 的 steady-state 差异定位到动态 Utopia 绘制策略，而不是外层 `EndDraw`、ffmpeg stdin 或 realization 预热优势。

### 5.8 N3 式动态描边执行步骤（2026-07-22）

1. ✅ 将 Utopia、旋转翻转、逐字垂落统一归类为逐字符几何变换；逐字淡入淡出保持透明度快路；
2. ✅ 动态帧只创建变换后的基础字形，不再为一重/二重描边创建变换后的预扩边 geometry；
3. ✅ 主文字、ruby 与阴影统一对变换后的基础字形调用 `DrawGeometry`，保持 N3 的动态描边语义；
4. ✅ 修正逐字垂落此前遗漏的 ruby 与 glow 动态分类，使发光随字符一起剪切变换；
5. ✅ 保留透明渐变、图片填充和透明实色的 outside-only 字腔保护路径，不用性能换画质；
6. ✅ 用 `KROK_GPU_DYNAMIC_DIRECT_STROKE=0` 保留旧路径一键回退，并完成新旧路径专项测试；
7. ✅ 完成《メフィスト》29～34 秒、描边 15、保留发光的无特效/Utopia/旋转翻转/逐字垂落 pipe A/B；
8. ✅ 完成三种动态特效真实视频背景 MP4 A/B，并用 ffprobe 验证 60fps、300 帧和 5.000 秒；
9. ✅ 完成最差差异帧抽检、透明图片填充保护门禁及 Direct2D GPU 后端完整回归。

### 5.9 回退

设置 `KROK_GPU_DYNAMIC_DIRECT_STROKE=0` 即可回到“变换预扩边 geometry + `FillGeometry`”旧行为；新路径默认开启。其他阶段 0 诊断开关仍可独立删除/关闭，不改变帧协议。

## 6. 阶段 1：建立导出专用会话与零 QImage 快速路径

> 预计：3～5 个工作日
> 前置：阶段 0 证明 Python/QImage/逐帧协议是有效瓶颈

### 6.1 目标

先消除当前最明确、风险最低的浪费：导出帧不再为了交给 ffmpeg 而构造整帧 `QImage`，也不再执行 `QImage -> RGBA8888 -> bytes`。

### 6.2 设计约束

- 预览继续使用现有 `QImage` 读帧路径；
- 导出新增独立 reader，不改 `SharedFrameRingReader.read_qimage()` 的预览语义；
- sidecar 在 native 侧把 premultiplied BGRA 转成 ffmpeg 需要的 straight RGBA；
- shared-memory slot 的所有权保持到 `stdin.write(memoryview)` 完成，之后才允许 producer 复用；
- 优先把字幕输入改成一个**会话期固定的裁剪矩形**：横排通常保留完整宽度，只裁掉永远透明的上下区域，并让 ffmpeg 按固定偏移 overlay；
- 固定裁剪矩形必须覆盖整个导出期间所有布局、描边、glow、ruby 和已知动画位移。无法保守证明边界的竖排、RTL 或全屏特效必须回退到整帧；
- rawvideo 的宽、高和偏移必须在会话开始时固定，不能每帧改变；
- 如果 ffmpeg 仍要求整帧 RGBA，则必须明确记录 band 展开转移到 native 后是否真的减少复制，而不是只换语言实现。

### 6.3 修改文件

- `krok_helper/subtitle_render/engine/native_export.py`
- `krok_helper/subtitle_render/native_backend.py`
- `native/subtitle_renderer/src/main.cpp`
- `native/subtitle_renderer/src/backends/render_backend.h`
- `native/subtitle_renderer/src/backends/direct2d/d2d_backend.cpp`
- `tests/test_subtitle_render_native_export.py`
- `tests/test_subtitle_render_gpu_backend.py`

### 6.4 可执行步骤

1. ✅ 增加导出会话握手，固定输出宽高、stride、像素格式、slot 数和 band 范围；
2. ✅ 先实现整帧 `packed_rgba`，用它单独验证格式转换与 slot 生命周期；
3. ✅ 再计算会话期固定裁剪矩形，并扩展 ffmpeg 字幕输入尺寸和 overlay 偏移；
4. ✅ 对不能证明动画包围盒的项目自动使用整帧，禁止裁断换性能；
5. ✅ 在 sidecar 中直接执行 BGRA premultiplied -> RGBA straight 转换；
6. ✅ 新增只返回 slot 元数据和 `memoryview` 的 Python reader；
7. ✅ 让 `_write_frames_gpu()` 直接把 bytes-like view 写入 ffmpeg stdin；
8. 在写完成或失败后显式 ack slot，保证 producer 不覆盖未消费帧；
9. ✅ 保留原 QImage 路径，增加 `KROK_SUBTITLE_GPU_EXPORT_PACKED=0/1` A/B 开关；
10. ✅ 对透明边缘、glow、半透明图片填充和裁剪矩形边缘做逐像素验证；
11. ✅ 记录每帧完整帧分配次数、复制次数和实际 pipe bytes，确认不是把 `bytes()` 隐藏进 helper。

### 6.5 阶段门槛

- ✅ 导出快路径中 `python_expand_ms`、`python_convert_ms`、`python_bytes_ms` 归零；
- ✅ Python 侧不再创建每帧整图 `QImage`；
- ✅ 桥接耗时比阶段 0 下降至少 50%；
- 4K60 R1/R2 端到端至少提高 15%；
- ✅ 与阶段 0 Direct2D 固定画质帧逐像素一致，尤其 alpha 边缘不得发黑或变亮；
- 取消、ffmpeg 提前退出、slot 超时均不死锁；
- 未达到 15% 端到端收益时保留代码与否需重新评审，禁止直接进入共享 Device 大改。

### 6.6 回退

`KROK_SUBTITLE_GPU_EXPORT_PACKED=0` 恢复阶段 0 QImage 路径。默认值在全局门槛通过前保持旧路径。

## 7. 阶段 2：native 直接输出与批量协议

> 预计：3～6 个工作日
> 前置：阶段 1 通过；性能账本仍显示逐帧 Python 调度或写管道是瓶颈

### 7.1 目标

把 N3 的“worker 输出环直接进入编码输入”思想落到当前架构：Python 负责创建 ffmpeg、传入导出快照、显示进度和处理错误，但不再逐帧搬运像素。

### 7.2 首选结构

```text
Python
  ├─ 创建 ffmpeg 与输出管道
  ├─ 启动一次 gpu_export_open/run
  ├─ 接收稀疏进度、取消和最终状态
  └─ 处理产品级错误与 fallback

sidecar
  ├─ 长生命周期导出会话
  ├─ worker 渲染/读回/排序
  └─ 直接写 ffmpeg rawvideo 输入句柄或 Windows named pipe
```

第一版不得让 sidecar 自己拼装或启动 ffmpeg 命令，以免复制现有编码、音频、错误处理和打包逻辑。

### 7.3 可执行步骤

1. 做一个最小 Windows 句柄/命名管道探针，验证 Python 创建、sidecar 写入、ffmpeg 消费、取消关闭和错误码传播；
2. 选择“继承写句柄”或“命名管道”之一，记录选择依据；
3. 定义 `gpu_export_open`、`gpu_export_run`、`gpu_export_cancel`、`gpu_export_close`；
4. 将逐帧 JSON 命令改为一次会话命令；
5. 将每帧完成事件改为每 N 帧/每 100ms 一次进度事件；
6. 在 sidecar 内实现有界 output ring 和严格顺序输出；
7. ffmpeg 写阻塞时只阻塞输出线程，不占用所有渲染 worker；
8. 发生首个错误后停止生产新帧、唤醒所有等待者、关闭写端并返回首因；
9. 保留旧 Python 写入路径，增加 `KROK_SUBTITLE_GPU_EXPORT_DIRECT_PIPE=0/1`；
10. 增加 frame index、PTS、写入字节数和最终帧数一致性断言。

### 7.4 阶段门槛

- 产品快路径不再逐帧发送 JSON render 命令；
- Python 不再逐帧接触字幕像素；
- 所有输出帧严格有序、无重复、无缺失；
- 用户取消到 worker/ffmpeg 全部停止不超过 2 秒；
- ffmpeg 错误、broken pipe、sidecar 崩溃能显示原始首因；
- 相对阶段 1，协议与 Python 调度时间下降至少 80%；
- 相对阶段 0，4K60 R2 端到端累计提升至少 30%；
- 不满足累计 30% 时停止，重新检查编码器/ffmpeg filter，不进入阶段 3。

### 7.5 回退

`KROK_SUBTITLE_GPU_EXPORT_DIRECT_PIPE=0` 使用阶段 1 的 Python bytes-like 写入；阶段 1 还可继续回退到阶段 0。

## 8. 阶段 3：N3 式共享 Device、场景和 realization

> 预计：5～9 个工作日
> 前置：阶段 2 达标，且 worker 重复准备/资源复制仍是显著成本

### 8.1 目标结构

```text
GpuExportSession
  ├─ D3D11 / D2D Device                         全会话唯一
  ├─ DWrite factory                             全会话共享
  ├─ ImmutableScene / DrawLineInfo              全会话一套
  ├─ GeometryPack                               每字符一套
  ├─ Filled / Stroked / Stroked2 realization   每稳定字符一套，0.25f
  └─ ExportWorker[1..N]
       ├─ D2D DeviceContext                     worker 独占
       ├─ target texture/bitmap                 worker 独占
       ├─ work bitmap                           worker 独占
       ├─ staging/readback slots                worker 独占
       ├─ brush/effect/scratch                  worker 独占或按资源域确认
       └─ output ring slot                      worker/帧独占
```

N3 使用进程级共享 Device；本阶段有意先把共享边界限定在一个 `GpuExportSession` 内，不强迫已经稳定的预览后端共享同一 Device。对导出 worker 而言仍满足“一个 Device、多 DeviceContext、共享字形资源”的核心结构；是否进一步与预览共享必须另有数据和独立计划，不能夹带进本次改造。

### 8.2 资源归属表必须先完成

在改生产代码前，为现有 `Direct2DGpuBackend::Impl` 的每一个成员标注：

- session immutable；
- session mutable + mutex；
- worker private；
- frame local；
- 不可跨 DeviceContext 共享。

没有完成资源归属表，不得把现有 `D2DDevice` 简单改成 `shared_ptr`。

### 8.3 必做探针

先写独立 native probe，只验证：

1. ✅ 一个 D2D Device 创建两个 DeviceContext；
2. ✅ context A 创建的 geometry/realization 能否由 context B 正确绘制；
3. ✅ 两个 context 并行画到各自 target；
4. ✅ D3D11 immediate context 下 Copy/Map 的串行、互斥和集中 readback queue 策略对照；
5. device removed、resize、销毁顺序；
6. 10000 帧无 `WRONG_RESOURCE_DOMAIN`、access violation、花帧或死锁。

探针失败时不得在产品代码中继续“边改边试”。需记录具体失败点，保留阶段 2 架构。

### 8.4 可执行步骤

1. 把 scene snapshot、排版和静态 geometry 从 backend 实例提升到 `GpuExportSession`；
2. ✅ 建立单一 hardware Device；WARP 作为另一种会话模式，不能在同一会话混用；
3. ✅ 从该 Device 为每个 worker 创建独立 DeviceContext；
4. ✅ 只为每个 worker 创建 target/work/staging/scratch；
5. ✅ 在开始输出前同步执行一次 geometry 和 realization 准备；
6. ✅ realization 使用 `0.25f`，不得因准备慢而临时放宽；
7. ✅ 静态字符共享 realization；
8. ✅ Utopia/缩放/旋转/位移中的动态字符走实时 geometry，回到稳定状态后继续使用共享 realization；
9. 图片和渐变填充按资源域分别验证，不能假定 bitmap/effect 可任意跨 context；
10. ✅ 为 readback 选择探针证明最优且稳定的策略；
11. configure 只构建一套 scene/cache，不再对 N 个完整 backend 重复配置；
12. ✅ 保留“阶段 2 多独立 backend”开关做 A/B；
13. 导出结束按 worker -> shared cache -> contexts -> device 的明确顺序释放。

### 8.5 realization 资格规则

以下全部满足才允许使用 realization：

- geometry 在当前帧没有非平移动态变换；
- fill/stroke/stroke2 所依赖的几何不变；
- 资源属于当前 session Device；
- realization 已成功构建；
- 画质测试覆盖对应描边宽度、缩放和 glow。

不满足时使用 `DrawGeometry`。禁止为了命中率把动态 geometry 错当静态缓存。

### 8.6 阶段门槛

- 1/2/4 worker 的静态 geometry 和 realization 计数相同，只存在一套；
- configure/preparation 时间不再近似随 worker 数线性增长；
- `0.25f` 路径与阶段 0 Direct2D 固定帧完成同引擎 A/B，不能出现此前“多边形字形”问题；
- 10000 帧压力测试无资源域错误、device removed、崩溃和死锁；
- 相对阶段 2，R2 端到端至少再提高 10%，或准备阶段至少提高 2 倍且长导出不退化；
- 相对阶段 0，4K60 R2 累计提升至少 40%；
- 若只降低准备时间但让稳定导出慢超过 5%，默认不启用 realization；
- 若共享 Device 无法稳定通过探针，停止该阶段，不降低质量、不复制每 worker realization 冒充对齐。

### 8.7 回退

保留 session 级 `shared_resources=false`，恢复阶段 2 的独立 backend；保留 `realization_enabled=false` 作为正确性回退。

### 8.8 共享资源探针执行结论（2026-07-22）

已实现并验证导出池内单一 D3D11/D2D Device、worker 独立 DeviceContext/target/scratch/staging，以及由 master worker 以 `0.25f` 预热后供 follower worker 复用的 geometry realization。探针没有出现 `WRONG_RESOURCE_DOMAIN`、device removed、花帧或崩溃，真实《メフィスト》无/有 Utopia 像素门禁均通过。

但性能门槛明确失败：共享 Device 把三个原本独立的提交/读回队列收敛到同一资源域后，`EndDraw` 内部时间虽然下降，等待转移到了其他 Direct2D/D3D 提交点，导致 `native_render` 与稳定吞吐显著退化。该路径只保留为显式 A/B 探针，`KROK_SUBTITLE_GPU_EXPORT_SHARED_RESOURCES` 和 `KROK_SUBTITLE_GPU_EXPORT_REALIZATION` 默认均为 `0`，不进入产品默认导出。

独立 Device + 每 worker realization 的隔离对照证明 realization 能降低静态宽描边的真实绘制成本，但约 36 秒的一次性预热无法在《メフィスト》当前导出时长内可靠回本，Utopia 场景收益更低。细分计时进一步证明，描边 15 时 99.8% 的共享预热时间直接耗在 `CreateStrokedGeometryRealization`；同步执行、设备预热、预扩边转 filled realization 和导出中后台摊薄均未改善端到端性能。N3 原项目实际使用 10px 描边，而本轮压力基准强制为 15px；10px 对照把预热降到 11.86 秒，但这不构成改小用户描边的理由。

共享 Device 的 readback 事务互斥也已完成对照，锁等待近乎为零且吞吐下降 1.1%，所以维持原有策略。因此本阶段到此停止，不继续用更多 worker 或降低画质掩盖架构负收益；仅 10,000 帧长压力门禁按用户允许的放宽条件保持未勾选。

## 9. 阶段 4：最终帧 native 合成（条件阶段）

> 预计：4～8 个工作日
> 只有阶段 0/2 数据证明 ffmpeg filter >= 20% 才允许执行

### 9.1 为什么是条件阶段

N3 worker 输出的是包含背景和字幕的最终帧，而当前工程让 ffmpeg 负责背景、缩放和字幕 overlay。对齐这一点可能减少一次整帧合成，但也可能引入视频解码、色彩空间、旋转、像素比例、音画同步和硬件解码的巨大新风险。

因此必须从最小探针开始，不能直接重写背景管线。

### 9.2 分级探针

1. **4A：纯色背景。** 在 native target 中清屏后直接画字幕；
2. **4B：静态图背景。** 会话开始时上传一次图片 bitmap；
3. **4C：视频背景。** 仅当 4A/4B 已证明至少 15% 增益，且视频背景 filter 仍为瓶颈时评估；
4. 4C 优先让现有 ffmpeg 解码后通过独立输入面交给 sidecar，不先自建解码器；
5. 音频继续由现有 ffmpeg 路径处理，禁止在此阶段重写音频管线。

### 9.3 必须验证

- 背景缩放、裁剪、letterbox 和旋转；
- SDR 色彩、limited/full range、alpha 和像素格式；
- 静态图缓存生命周期；
- 视频首帧、尾帧、变帧率源、seek 和音画同步；
- 导出预览支路；
- 软件和硬件编码器；
- CPU fallback 仍使用原 ffmpeg 合成。

### 9.4 阶段门槛

- 4A/4B 相对阶段 3 端到端至少提高 15%，否则停止整个阶段；
- 视频背景不得因 native 合成出现可见色差或音画偏移；
- 4C 增加的解码/传输开销后仍有至少 10% 端到端收益；
- 任一背景类型需要维护两套复杂滤镜语义且收益不足时，保留 ffmpeg overlay，不追求形式上的 N3 对齐。

### 9.5 回退

按背景类型回退到 ffmpeg overlay。不得用一个全局开关强迫尚未验证的视频背景进入 native 合成。

## 10. 阶段 5：worker 数量与输出调度定型

> 预计：2～3 个工作日
> 前置：单 worker 的最终管线通过画质、稳定性和阶段收益门槛

### 10.1 目标

在已经消除主要串行浪费后，再决定多少 worker 能填满 GPU/readback/encoder，而不是先按 N3 上限固定为 8。

### 10.2 可执行步骤

1. 实测 worker `1/2/3/4/8`；
2. 每个 worker 使用独立 target、work bitmap、staging 和 output slot；
3. output ring 容量分别测试 `worker_count`、`2 * worker_count` 和固定上限；
4. 渲染完成可乱序，写入 ffmpeg 必须按 frame index 严格排序；
5. 导出禁止 latest-wins、跳帧或覆盖未消费 slot；
6. 分别测试 renderer-bound、readback-bound、filter-bound、encoder-bound 场景；
7. hardware 与 WARP 分开选择，WARP 默认只测试 1 worker；
8. 记录 GPU 利用率、CPU 利用率、显存、RSS、队列深度、worker idle、writer blocked；
9. 默认 worker 只从通过全部项目的候选中选择；
10. 保留环境变量允许诊断覆盖，但产品默认不得动态无限增加。

### 10.3 阶段门槛

- 默认 worker 相对单 worker 至少提高 15%；
- 相对前一个 worker 数，吞吐提高不足 5% 而显存提高超过 20% 时，不选更高值；
- 10 分钟导出无丢帧、重复帧、乱序、队列无限增长和内存爬升；
- 编码器饱和时 worker 能自然背压，不出现 busy loop；
- 4K60 默认配置峰值显存和 RSS 处于阶段 0 确定的预算内；
- 不因 N3 支持 8 worker 而强行选择 8。

## 11. 阶段 6：产品化、故障矩阵与默认开启

> 预计：3～5 个工作日

### 11.1 故障矩阵

必须逐项验证：

- 用户正常取消；
- ffmpeg 启动失败；
- ffmpeg 中途退出/broken pipe；
- 输出目录无权限；
- 磁盘空间不足；
- sidecar 启动失败或中途崩溃；
- GPU device removed/reset；
- shared-memory/pipe 超时；
- worker 内部异常；
- 应用退出时仍在导出；
- 硬件后端失败后 WARP/CPU 回退；
- 60fps 与 120fps 的末帧、时长和音画同步；
- 开启导出预览时 GUI 不阻塞。

### 11.2 回退原则

- GPU 在第 0 帧前失败：自动切到 CPU；
- GPU 已经写出部分帧后失败：终止本次 ffmpeg，清理不完整输出，再从第 0 帧用 CPU 重启；
- 禁止把 GPU 和 CPU 的半段输出拼接成一个文件；
- 错误信息保留第一原因，清理错误不能覆盖原错误；
- 用户取消不得触发自动 CPU 重跑。

### 11.3 长时间与硬件覆盖

- R1/R2 各执行一次完整导出；
- 至少执行一次 30 分钟 4K60 压力导出；
- NVIDIA、AMD、Intel 各至少一台机器；
- 无合适 GPU 时验证 WARP 和 CPU fallback；
- 打包后的 onedir 应用执行一次真实导出，不只测试源码环境。

### 11.4 默认开启条件

只有以下全部满足，才把新管线设为默认：

- 第 1.1 节北极星指标通过；
- 阶段 0～3 的 A/B 回退仍可用；
- 阶段 4 若未达标已保持关闭；
- 定向测试、画质帧、完整真实项目和 30 分钟压力测试通过；
- 预览基准下降不超过 5%；
- CPU fallback 和取消通过；
- 打包应用通过；
- 性能结果能由统一账本解释，不存在只在单一机器上的偶然收益。

## 12. 全局测试与验收矩阵

### 12.1 每阶段最小定向测试

执行时只运行与改动相关的测试，不要求每个小阶段跑全仓库：

```powershell
C:\Python314\python.exe -m pytest `
  tests/test_subtitle_render_native_export.py `
  tests/test_subtitle_render_gpu_backend.py `
  tests/test_subtitle_render_renderer.py -q
```

如果实际测试文件名不同，先用 `rg --files tests` 定位现有对应文件，不为凑命令重复新建测试模块。

### 12.2 正确性

- frame index、PTS、frame count；
- 60/120fps 时长；
- ruby、横向渐变、走字；
- Utopia 进入/退出和动态变换；
- glow、描边、二重描边；
- 图片/渐变填充；
- 竖排、RTL、多角色；
- 标题；
- 视频、图片、纯色背景；
- alpha 和 premultiplied/straight 转换；
- 阶段 0 Direct2D / 新 Direct2D 路径固定帧 A/B；
- CPU QPainter 语义审计，不做跨引擎像素门禁。

### 12.3 性能

- 准备时间；
- 首帧时间；
- 稳态 fps；
- p95 帧延迟；
- 端到端墙钟时间；
- Python/native/ffmpeg CPU 占用；
- GPU 利用率；
- peak RSS/VRAM；
- 跨进程字节/帧；
- 完整帧复制次数/帧；
- writer blocked 和 worker idle 比例。

### 12.4 不可接受的“成功”

以下结果一律不算完成：

- renderer benchmark 变快但最终 MP4 不变快；
- 只在纯色背景或合成短句上变快；
- 用 `3.0f` realization 容差换来的速度；
- 通过增加 worker 换来吞吐，但准备时间、显存或崩溃率显著增加；
- 预览变慢或被迫改用新导出协议；
- 取消后仍有 sidecar/ffmpeg 残留；
- N3 条件不同却直接报告“已对齐 N3”；
- 把 ffmpeg 编码器饱和误判为 GPU renderer 慢。

## 13. 时间成本、风险与阶段停止点

| 阶段 | 预计时间 | 主要风险 | 可以交付后停止吗 |
|---|---:|---|---|
| 0 诊断与基线 | 1～2 天 | 计时口径不完整 | 可以；得到可信瓶颈结论 |
| 1 零 QImage | 3～5 天 | alpha/slot 生命周期 | 可以；低风险复制优化 |
| 2 native 直接输出 | 3～6 天 | pipe、取消、错误传播 | 可以；形成导出专用 sink |
| 3 共享 Device/资源 | 5～9 天 | 资源域、并发读回、销毁顺序 | 可以；完成 N3 核心结构对齐 |
| 4 native 最终帧合成 | 4～8 天 | 色彩、视频解码、音画同步 | 条件阶段，可完全不做 |
| 5 worker 定型 | 2～3 天 | 显存和背压 | 不宜跳过 |
| 6 产品化 | 3～5 天 | 长时稳定性和 fallback | 必须完成后才默认开启 |

核心路线（不含条件阶段 4）预计 **17～30 个工作日**。包含视频背景 native 合成时预计 **21～38 个工作日**。阶段 0 可能证明收益不足，此时应在 1～2 天后主动停止，而不是为了完成计划继续重构。

错误率按变更面估计：

- 阶段 1：低～中，集中在 alpha 和共享内存生命周期；
- 阶段 2：中～高，集中在跨进程管道、取消和错误传播；
- 阶段 3：高，集中在 D2D 资源域、D3D11 immediate context、并发读回和销毁；
- 阶段 4：极高，涉及视频和色彩链，是最容易做偏的部分；
- 阶段 5/6：中，主要是队列背压和长时故障。

## 14. 建议提交切分

每个提交只包含一个可 A/B、可回退的阶段，不跨阶段混合：

1. `perf: add gpu export pipeline diagnostics`
2. `perf: add packed gpu export frame transport`
3. `perf: add native gpu export output pipe`
4. `refactor: introduce shared gpu export session resources`
5. `perf: share export geometry realizations across workers`
6. `perf: tune gpu export worker scheduling`
7. 条件提交：`perf: composite static backgrounds in gpu export`
8. 条件提交：`perf: add video background ingress for gpu export`
9. `fix: harden gpu export cancellation and fallback`
10. `test: add gpu export quality and endurance coverage`

任何阶段未过门槛，对应提交不进入默认路径。阶段 4 的提交不得与阶段 3 或阶段 5 混在一起。

## 15. 最终交付清单

- [ ] 阶段 0 原始 CSV/JSON 与环境元数据完整；
- [x] N3 同条件或差异明确的对照数据；
- [ ] 性能账本解释至少 90% 墙钟时间；
- [ ] 导出快路径无逐帧 `QImage`、`bytes()` 和 JSON render 命令；
- [ ] native 输出 ring 严格有序、可背压、可取消；
- [x] 单 Device、多 DeviceContext 探针通过；
- [x] 一套 scene/geometry/realization 被所有 worker 共享；
- [x] realization 容差保持 `0.25f`，Utopia、旋转翻转、逐字垂落动态 geometry 路径通过；
- [ ] 1/2/3/4/8 worker 已实测并选定默认值；
- [ ] ffmpeg/native 背景合成由数据决定，没有越过条件门槛；
- [ ] R1/R2 阶段 0 Direct2D / 新 Direct2D 固定画质帧通过同引擎 A/B；
- [ ] CPU QPainter 语义审计通过，且未被用作跨引擎像素门禁；
- [ ] 60/120fps 帧数、时长和音画同步通过；
- [ ] 取消、broken pipe、device removed、磁盘错误和进程退出通过；
- [ ] CPU fallback 从第 0 帧重启并清理不完整文件；
- [ ] 30 分钟压力导出无泄漏、死锁和残留进程；
- [ ] 预览性能下降不超过 5%；
- [ ] 4K60 R2 达到 1.5x 或与 N3 同条件差距小于 15%；
- [ ] 打包应用真实导出通过；
- [x] 新路径通过总开关可立即回退。

---

本计划的核心不是“把代码改得像 N3”，而是按 N3 已验证的导出思想，逐步消除当前工程中可测量的导出瓶颈。任何没有被阶段数据证明的对齐项，都不得进入下一阶段。
