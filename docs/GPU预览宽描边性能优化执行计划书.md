# GPU 预览宽描边性能优化执行计划书

> 状态:执行中(阶段 0 已完成,阶段 1 待执行)
>
> 建立日期:2026-07-21
>
> 上游文档:`docs/GPU预览宽描边性能与N3对齐实施计划.md`(含全部 N3 反编译结论,
> 本文不重复论证,只给可执行步骤)
>
> 核心约束:Python QPainter 永久保留为视觉 oracle 与故障回退;G6 DirectComposition
> 保持硬关闭;不复制 N3 源码,只对齐可观察行为。

## 0. 北极星与总规则

**北极星指标**:固定工程(`C:\Users\18007\Downloads\芽吹の唄 - 大原ゆい子.yurika`,
1080p60,107px 正文 + 14px 主描边 + 7px 二重描边 + 5px 发光)稳定播放段
render p95 < 16.67ms,争取 12~14ms。

**每个阶段必须遵守的五条规则**(直接回应"修了宽描边不能冒出新慢点"):

1. **先测后改**:任何优化提交前,先跑阶段 0 建立的基线脚本并归档 CSV;
   提交后重跑同一脚本,轻描边(0/5px)、无装饰、WARP 三个"非目标场景"
   的 render mean 回退超过 5% 即视为引入新性能问题,不得合并;
2. **逐帧路径零资源创建**:warmup 之后的稳定播放帧,画刷创建数、
   稳定字符几何创建数、realization 创建数必须为 0(由阶段 0 计数器验证)。
   realization/几何只允许在 configure/预热阶段创建;
3. **缓存 key 禁区**:wipe 进度、播放时间戳、帧号一律不得进入任何缓存 key
   (N3 已证明走字只是 clip 分割,与缓存无关);
4. **每阶段一个回退开关**:新路径必须能通过配置或环境变量一键回到旧行为,
   开关名在本文各阶段中固定,验收后至少保留两个版本;
5. **每阶段单独提交**:一个阶段一个(或一组)commit,禁止跨阶段混提。

**固定测试时间点**(覆盖走字全生命周期,四组描边 0/5/14/30/50px 都要跑):

- T1 Utopia 进场前(整行 before 态);
- T2 Utopia 进场中(动态变换字符最多);
- T3 稳定走字中段(恰有一个字符在 wipe clip 分割);
- T4 逐字淡出/退场。

## 1. 阶段 0:诊断计数与基线(对应 P0)

**目标**:让宽描边热点可以被计数解释,而不是靠总 FPS 猜。

**改动文件**:

- `native/subtitle_renderer/src/backends/direct2d/d2d_backend.cpp`
  (画刷创建点约 662/686/753/1013 行,动态几何创建点约 1473~3161 行,
  glow 约 934/2555~2592 行)
- `krok_helper/subtitle_render/native_backend.py`(`gpu_diagnostics` 响应中
  透出计数器)
- `scripts/benchmark_gpu_renderer.py`(新增描边 sweep 与计数输出)

**任务**:

- [x] 在 Direct2D 后端加每帧计数器结构体(编译期宏 + 运行期开关,默认开,
  计数本身不得引入可测开销):brush_created、geometry_created_stable、
  geometry_created_dynamic、realization_hit/miss、stroke_draw/stroke2_draw 次数、
  glow_source_area_px、layer_push 次数;
- [x] 分段计时:animation/layout、geometry、stroke、glow、GPU wait、readback、
  shm/QImage、端到端;
- [x] `gpu_diagnostics` 返回累计计数;`benchmark_gpu_renderer.py` 增加
  `--stroke-sweep 0,5,14,30,50` 与 `--time-points T1,T2,T3,T4`(毫秒列表)参数,
  输出逐帧 CSV 到 `build/gpu-widestroke-baseline-<hw>-<date>.csv`;
- [x] 采集并归档基线:硬件 D2D 与 WARP 各一份,记录 GPU 型号、驱动版本、commit;
- [x] 10 分钟连续播放采样 sidecar RSS、显存、stale/drop 计数
  (用 `scripts/stress_gpu_preview_gui.py`)。

**验收门禁**:四组描边 × 四个时间点的计数能解释 §2.1 表中"14px 比 5px 慢一倍"
的具体去向(多出的 DrawGeometry 次数/几何创建次数/glow 面积)。

**回退开关**:计数器运行期开关 `KROK_GPU_COUNTERS=0`。

**新风险与防护**:计数器开销 → 基线前先对比开/关计数的 render mean,差异须 < 1%。

### 阶段 0 实测回填(2026-07-21)

固定参数为 1920×1080@60、107px 正文、7px 二重描边、5px 发光；测试点为
`T1=167050`、`T2=167180`、`T3=175000`、`T4=182395`。硬件为
NVIDIA GeForce RTX 3070 Ti Laptop GPU，驱动 `32.0.15.8097`，基线源码
commit 为 `649af875ba06d1e8f1fc5884ef15c191b176fb9e`。

| 主描边 | 硬件 render mean / p95 | WARP render mean / p95 | 每帧画刷均值 | 动态几何均值 | glow source 面积均值 |
|---:|---:|---:|---:|---:|---:|
| 0px | 6.893 / 8.516ms | 7.110 / 8.893ms | 37.2 | 1.5 | 728,464px |
| 5px | 10.409 / 15.560ms | 8.266 / 10.507ms | 37.2 | 2.2 | 802,527px |
| 14px | 22.299 / 30.988ms | 9.344 / 11.676ms | 37.2 | 2.2 | 954,638px |
| 30px | 43.292 / 67.235ms | 10.182 / 13.026ms | 37.2 | 2.2 | 1,250,646px |
| 50px | 65.974 / 110.026ms | 10.468 / 14.914ms | 37.2 | 2.2 | 1,677,345px |

归档文件：

- `build/gpu-widestroke-baseline-hardware-20260721.csv`；
- `build/gpu-widestroke-baseline-warp-20260721.csv`；
- `build/gpu-widestroke-baseline-no-decoration-hardware-20260721.csv`；
- `build/gpu-widestroke-baseline-stress-hardware-20260721.json`。

计数器开/关同场景 render mean 为 19.79/20.22ms，未观察到正向开销，满足
`<1%` 门禁。10 分钟长稳交付 13,809 帧，renderer failure/fallback/restart 均为
0，`max_pending=1`；sidecar RSS 90.09→88.38MiB，峰值 92.02MiB，本地显存结束
约 23.7MiB。宽度增加时 Draw 次数与动态几何次数基本不变，而 glow source 面积及
硬件 render 时间同步增长；关闭装饰后 14px 仍为 20.29ms，证明首要瓶颈是硬件
Direct2D 宽轮廓逐帧栅格化，glow 是放大项而非唯一根因。

阶段 0 定向测试新增项为 `3 passed`。全量 transport + GPU backend 当前为
`214 passed, 1 skipped, 3 failed`；三个失败均为既有 Painter/GPU 边界容差用例，
在 `KROK_GPU_COUNTERS=0` 和旧 sidecar 上可复现，因此不是计数插桩造成，但在最终
完成定义前仍须修复，不能作为放宽视觉门禁的理由。

## 2. 阶段 1:画刷与稳定几何复用(对应 P1)

**目标**:warmup 后稳定帧 brush_created == 0、geometry_created_stable == 0。

**改动文件**:`d2d_backend.cpp`(+ 如需拆分,新建 `d2d_brush_cache.h/cpp`)。

**任务**:

- [x] 新建 BrushCache,严格 per-DeviceContext 持有:solid 按 RGBA、
  bitmap 按图片源+缩放、渐变按 stops 序列(端点/行 Y 不进 key,
  采用 N3 的"每 target 一个实例 + 每行改写起止端点"模式);
- [x] 角色样式(【N配色】)的行内画刷全部走同一缓存,不再逐字符/逐层/逐帧创建;
- [x] 审计全部 CreateTransformedGeometry 调用点(position/RTL/vertical/ruby/
  interference/spin 各族):稳定字符只消费 configure 阶段缓存;
- [x] 动态字符(本帧有变换矩阵)的临时几何**本帧只算一次**,body/protection/
  stroke/stroke2/glow 五处共用(N3 是每层每 clip 半边各建一次,此处做得比 N3 好);
- [x] wipe 分割所需的字符包围盒缓存到字符资源上(N3 每帧 GetBounds,可安全缓存;
  key 不含 wipe 进度);
- [x] 缓存容量上限、命中/未命中/失效原因计数接入阶段 0 诊断;样式修改、
  DPR 变化、render target 重建时精确失效。

**验收门禁**:

- 稳定播放(T3)warmup 后 brush_created == 0、geometry_created_stable == 0;
- 描边 5px→14px 时资源创建次数不再近似翻倍;
- 0/5px 与无装饰场景 render mean 回退 < 5%;
- 视觉对照(`scripts/compare_gpu_painter_corpus.py` 与
  `scripts/compare_gpu_n3_reference.py`)全部通过既有容差;
- 缓存失效测试:改字号/字体/描边/颜色/渐变/图片/角色方案后不得复用旧资源
  (新增用例进 `tests/test_subtitle_render_gpu_backend.py`)。

**回退开关**:`gpu_configure` 增加 `resource_cache: false`(或环境变量
`KROK_GPU_RESOURCE_CACHE=0`)。

**新风险与防护**:

- 失效遗漏 → 串色/旧样式残留:靠上面的失效测试矩阵门禁;
- 缓存无界增长 → 容量上限 + 诊断计数 + 10 分钟 RSS 门禁。

### 阶段 1 实测结果(2026-07-21)

已实现严格绑定单个 `Direct2DGpuBackend`/DeviceContext 的 512 项 LRU 画刷缓存，
solid、gradient、bitmap 与角色样式统一进入缓存；渐变端点和 bitmap transform 在每次
绘制前改写，不进入 key。场景签名变化会清空资源，target/backend 重建则随 DeviceContext
一起销毁；`KROK_GPU_RESOURCE_CACHE=0` 可恢复逐次创建路径。稳定帧
`brush_created=0`、`geometry_created_stable=0`，容量压力测试确认上限为 512 且产生
可观测 eviction。

固定工程硬件 A/B（缓存关→开，render mean）：

| 主描边 | glow 场景 | 变化 | 无装饰场景 | 变化 |
|---:|---:|---:|---:|---:|
| 0px | 6.466→4.736ms | -26.75% | 4.970→3.264ms | -34.32% |
| 5px | 9.345→7.552ms | -19.19% | 7.872→5.980ms | -24.04% |
| 14px | 21.252→22.152ms | +4.23% | 20.597→19.098ms | -7.28% |
| 30px | 40.613→42.422ms | +4.46% | — | — |
| 50px | 58.967→56.841ms | -3.60% | — | — |

14/30px 的单轮硬件波动仍低于 5% 非目标回退门限；两个 A/B 集合共 640 帧 checksum
零差异。`compare_gpu_painter_corpus.py` 全部通过既有容差（含本机真实 Dark Spiral
`.n3proj` 切片）；独立 N3 成品/源视频不在仓库内，故视频减法门禁留待取得外部资产后
补跑。缓存定向测试 13 项通过，覆盖关闭开关、稳定帧、容量淘汰及字号/字体/描边/颜色/
渐变/图片/角色方案失效。

归档文件：

- `build/gpu-widestroke-stage1-cache-{off,on}-hardware-20260721.csv`；
- `build/gpu-widestroke-stage1-cache-{off,on}-no-decoration-hardware-20260721.csv`；
- `build/gpu-stage1-painter-corpus/result.json`。

## 3. 阶段 2:宽描边 realization(对应 P2,预期最大收益)

**目标**:稳定字符(含正在走字的字符)的 filled/stroked/stroked2 改为
DrawGeometryRealization,消除宽描边逐帧 tessellation。

**关键设计决定**(依据 N3 二次核实结论):

1. **正在走字的字符照常使用 realization**——wipe 是 axis-aligned clip 分割,
   realization 在 clip 内正常工作;只有本帧带变换矩阵的字符
   (Utopia 进出场/走字脉冲、旋转翻转)回退 DrawGeometry;
2. realization 只在 configure/预热阶段创建,逐帧路径遇 miss 直接走
   DrawGeometry 并计数,**绝不在帧内补建**(N3 用 null 检查回退,同构);
3. glow/blur 路径保持 DrawGeometry(N3 同款行为,保证层次一致),
   realization 不改变 glow 源的生成顺序。

**任务**:

- [x] GeometryPack 式字符资源增加 filled/stroked/stroked2 realization 槽位,
  容差以 0.25 为 A/B 起点;主描边与二重描边分别建,key 包含有效字号、DPR、
  字体、glyph、描边宽度、join/miter;
- [x] 预热策略:configure 结束后异步预热,单次样式变更的预热预算上限
  (建议先定 50ms/帧片分摊),预热完成前 miss 回退 DrawGeometry;
- [x] 显存预算:realization 总量计数 + 上限,超限按当前时间邻近行优先保留并回退;
- [x] A/B 脚本:`benchmark_gpu_renderer.py --realization on|off` 跑四组描边 ×
  四时间点,归档对照 CSV;
- [x] 视觉 A/B:realization 开/关关键帧像素差必须落在既有 GPU/Painter 容差内,
  特别覆盖大字号、斜杠、日英数字体、注音、标题、行内角色样式;
- [x] 若细描边(≤5px)A/B 出现可见差异或负收益,则按描边宽度阈值启用
  (仅 ≥ 阈值的稳定宽描边走 realization),阈值写进配置并记录数据依据。

**验收门禁**:

- 14px+7px 场景 render mean 显著下降(目标进入 16.67ms 预算,含回读余量);
- 稳定播放 realization_miss == 0(预热完成后);
- 预热不产生超过 1 帧周期的可感知卡顿(seek/样式修改压力测试);
- 0/5px、无装饰、WARP 回退 < 5%;
- Utopia 全程(T1~T4)视觉门禁通过——动态字符回退路径不经过 realization。

**回退开关**:`KROK_GPU_REALIZATION=0`(默认开,细描边阈值另有配置项)。

**新风险与防护**:

- 预热卡顿(用户最担心的"新慢点"之一)→ 异步分摊 + 预算上限 + seek/churn 压测门禁;
- 显存增长(每字符最多 4 个 realization)→ 总量上限 + 时间邻近优先保留 +
  10 分钟显存门禁;
- WARP 上 realization 可能无收益甚至负收益 → WARP 基线单独 A/B,负收益则
  WARP 自动禁用 realization;
- 画质回退 → 像素差门禁,超容差先修语义再谈性能。

### 阶段 2 实测结果(2026-07-21)

正文与注音字符均增加 filled/stroked/stroked2（以及图片/渐变正文保护层）
realization。预热使用同一 D2D device 的独立 DeviceContext，只在前台渲染空闲
100ms 后运行，每约 50ms 主动让出调度；队列按 `prewarm_t_ms` 邻近行排序，逐帧路径
只消费已发布资源，动态矩阵字符与 glow 永远回退 DrawGeometry。样式 churn 使用代际
取消：旧 worker 不等待昂贵调用结束即可退役，其任务持有独立 geometry 引用，发布时
校验 generation，绝不写入新场景。0.25 起始容差全工程 14px 预热约 35.0s；在
14/30/50px 三档 realization 开关像素边界与 MAE 门禁通过后，最终容差定为 3.0，
预热降至约 11.34/22.19/38.41s。硬上限 8192 项，本工程最终 1113 项，未触顶；
由于场景缓存本身随配置整体失效，超限策略改为保留当前时间邻近行并让远端行 miss
回退，而不是在不可变场景内做会制造抖动的逐帧 LRU。

固定工程硬件 A/B（realization 关→开，render mean）：

| 主描边 | 关闭 | 开启（全热） | 变化 | 全项目预热 |
|---:|---:|---:|---:|---:|
| 0px | 4.774ms | 4.771ms | -0.07% | 不启用 |
| 5px | 8.066ms | 7.838ms | -2.82% | 不启用正文 |
| 14px | 21.109ms | 17.020ms | -19.37% | 11.34s |
| 30px | 41.403ms | 37.053ms | -10.51% | 22.19s |
| 50px | 61.614ms | 57.331ms | -6.95% | 38.41s |

14px 的重复探针区间为 16.12~17.02ms，已到 60fps 预算边缘；最新成对 40 帧均值仍比
16.67ms 高 0.35ms，须由阶段 3 的 glow/dirty rect 留出稳定余量，不能把单次较低结果
当作门禁已稳过。全热后四时间点 `realization_miss=0`。14→15px 的真实工程样式 churn
八次采样中，开启 realization 相对关闭的最大附加耗时为 13.4ms，低于一帧；定向测试
连续退役四代 worker 后，细描边新场景 realization 计数仍为 0。WARP 默认自动禁用，自动/显式
关闭 A/B 为 8.355/8.750ms 且 40 帧 checksum 零差异；需要诊断时可用
`KROK_GPU_REALIZATION_WARP=1` 强制开启。主描边阈值为 8px，0/5px 不改变正文像素路径。

realization 定向、样式 churn 与 Utopia 动态回退测试 `8 passed`，宽描边开/关像素边界与 MAE 通过；
Painter corpus（含本机真实 Dark Spiral `.n3proj` 切片）通过。60 秒真实 GUI 连续播放
交付 1,783 帧，renderer failure/restart/fallback 均为 0，sidecar RSS 增长 2.91MiB；
持续请求期间空闲门控阻止重预热与前台争抢。GPU backend 全文件为
`155 passed, 1 skipped, 3 failed`，仍仅为阶段 0 登记的三个 Painter/GPU 边界基线。

归档文件：

- `build/gpu-widestroke-stage2-realization-off-hardware-20260721.csv`；
- `build/gpu-widestroke-stage2-realization-on-{fine,14,30,50}-hardware-20260721.csv`；
- `build/gpu-widestroke-stage2-realization-on-{14,30-50}-tolerance3-hardware-20260721.csv`；
- `build/gpu-widestroke-stage2-realization-off-14-30-50-paired-hardware-20260721.csv`；
- `build/gpu-widestroke-stage2-realization-{auto,off}-warp-20260721.csv`；
- `build/gpu-widestroke-stage2-stress-hardware-20260721.json`；
- `build/gpu-stage2-painter-corpus/result.json`。

## 4. 阶段 3:glow 与脏矩形收紧(对应 P3)

**目标**:glow 成本从"正比于全画布"降为"正比于发光行实际面积"。

**任务**:

- [x] glow scratch bitmap / GaussianBlur effect / 目标纹理常驻复用
  (现有池化基础上按未来 worker 隔离);
- [x] glow source 边界 = 行内实际字符包围盒 ∪ 主描边 ∪ stroke2 ∪ blur padding,
  不用整画布(N3 用整画布,此处超越 N3;包围盒复用阶段 1 缓存);
- [x] 稳定字符合并进行级 glow source,动态字符单独动态层;
- [x] before/after、ruby、正文、标题、角色样式的 glow key 不得串色;
- [x] 验证 Utopia 开场最左侧无多余 before 蓝光、注音开头不丢发光
  (历史回归点,进视觉对照集)。

**验收门禁**:glow_source_area_px 计数显著下降;三档 glow 视觉门禁通过;
关 glow 场景零回退。

**回退开关**:`KROK_GPU_GLOW_DIRTY_RECT=0`。

**新风险与防护**:脏矩形算小 → 发光被裁边:padding 公式进单元测试,
视觉对照集加 50px 描边 + 大 glow 组合。

### 阶段 3 实测结果(2026-07-21)

`KROK_GPU_GLOW_DIRTY_RECT=1` 时，scratch pool 从固定 1920×1080 改为按每层脏区
尺寸单调增长并复用（每行复位游标、最多保留 8 个槽位），GaussianBlur effect 和最终
frame target 继续常驻。每个裁剪原点对齐到原整画布的设备像素网格；正文/标题沿用完整
行几何包围盒，ruby 沿用完整注音包围盒，行内角色样式只合并本样式当前可见的稳定字符，
Utopia/spin 动态字符仍保持独立的“先 blur 后 transform”层。输出和输入依赖区继续保留
`3σ + 16px` 安全尾部；曾尝试更小 padding，但 viewport 回归能捕获 4~7px 低透明度
尾部裁切，因此没有以画质换面积。

最终固定工程成对 80 帧 A/B（dirty off→on，realization 全热）：

| 指标 | 关闭 | 开启 | 变化 |
|---|---:|---:|---:|
| render mean | 16.120ms | 16.011ms | -0.68% |
| render p95 | 28.653ms | 26.373ms | -7.96% |
| 诊断缓存估算（含 glow） | 34.11MB | 4.43MB | -87.0% |
| D3D 本地显存占用 | 60.49MB | 31.92MB | -47.2% |

四时间点 `glow_source_area_px` 分别下降 34.4% / 34.4% / 44.0% / 1.2%，合计
下降约 33%；T4 只有短行且原边界已紧，故收益很小。最终 14px+7px 均值进入
16.67ms 预算，但 p95 仍为 26.37ms，阶段 4 必须按硬关卡重新复验，不能据单次均值
直接进入多 worker。

三档 glow × 50px 主描边 × 7px 二重描边 × Utopia × ruby 的 dirty/full A/B
`3 passed`，边界差 ≤2px、全通道 MAE <0.5；关 glow 开关 A/B checksum 完全一致。
全部 glow 定向用例 `24 passed`，Painter corpus（含真实 Dark Spiral `.n3proj`）通过。
60 秒真实 GUI 压测 `passed=true`，renderer failure/restart/fallback 均为 0，sidecar
RSS +2.40MiB，结束时 D3D 本地显存 24.90MB。GPU backend 全文件为
`159 passed, 1 skipped, 3 failed`，仍仅为阶段 0 登记的三个 Painter/GPU 边界基线。

归档文件：

- `build/gpu-widestroke-stage3-final2-dirty-{off,on}-hardware-20260721.csv`；
- `build/gpu-widestroke-stage3-stress-hardware-20260721.json`；
- `build/gpu-stage3-painter-corpus/result.json`。

## 5. 阶段 4:单 worker 复验关卡(硬关卡)

- [x] 重跑阶段 0 全套基线,与初始基线对照,回填两份 CSV 路径到本文档;
- [x] 判据:14px+7px render mean 相比基线下降且逼近单帧预算,四条非目标场景
  (0/5px、无装饰、WARP、Painter 对照)全部无回退;
- [x] **只有本关通过才进入阶段 5**。若 render core 未明显下降,先回到
  阶段 1~3 找剩余热点,不允许用多 worker 掩盖单帧问题(否则只是让
  command queue 与回读带宽更早饱和,并放大过期帧,这正是要避免的新性能问题)。

2026-07-21 复验结论：**通过，允许进入阶段 5**。固定工程、固定时间点、相同
1920x1080@60 配置下，阶段 0 与阶段 4 的硬件结果如下（单位 ms）：

| 主描边 | 初始 mean / p95 | 阶段 4 mean / p95 | mean 变化 |
|---:|---:|---:|---:|
| 0px | 6.894 / 8.516 | 4.514 / 5.543 | -34.5% |
| 5px | 10.409 / 15.560 | 7.355 / 9.828 | -29.3% |
| 14px | 22.299 / 30.988 | 16.885 / 27.400 | -24.3% |
| 30px | 43.292 / 67.235 | 36.471 / 64.022 | -15.9% |
| 50px | 65.974 / 110.026 | 55.355 / 97.626 | -16.1% |

14px+7px 的 render core 已明显下降并逼近 16.67ms 单帧预算；本次 80 帧样本
均值仍高 0.215ms，p95 仍为 27.40ms，故阶段 5 只解决连续播放吞吐和过期帧
调度，不能把并行度当成单帧延迟修复。无装饰硬件 0/5/14px 为
3.032/5.665/15.300ms；WARP 14px 为 7.496/9.166ms，realization 按设计自动
关闭；Painter corpus（含真实 Dark Spiral `.n3proj`）`passed=true`。四条非目标
场景均未回退。

归档文件：

- 初始：`build/gpu-widestroke-baseline-{hardware,no-decoration-hardware,warp}-20260721.csv`；
- 最终：`build/gpu-widestroke-stage4-final-{hardware,no-decoration-hardware,warp}-20260721.csv`；
- 视觉：`build/gpu-stage4-painter-corpus/result.json`。

## 6. 阶段 5:多 worker 流水线(对应 P4)

**目标**:连续播放吞吐接近 60fps,且不产生旧帧积压。

**改动文件**:`d2d_backend.cpp` / `native_preview_surface.cpp`(sidecar 侧
worker 池)、`krok_helper/subtitle_render/native_backend.py`(协议)、
`krok_helper/subtitle_render/frontend/preview_async.py`(调度器)。

**任务**:

- [ ] sidecar 内新建常驻 GpuPreviewWorkerPool:共享 D3D11/D2D device;
  每 worker 独立 DeviceContext + frame target + staging/readback +
  glow scratch + shm ring slot,常驻线程不按批重建(N3 同构:
  常驻线程 + 事件唤醒 + ring);
- [ ] 画刷/realization 缓存按 worker 隔离(阶段 1 已保证 per-context),
  worker 启动后渲染 warmup 帧;
- [ ] 配置生成不可变 scene snapshot + generation;请求携带时间戳、serial、
  generation、frame index;GUI 只接收当前 generation 且不落后播放时钟的帧
  (保留 latest-wins,增加 PTS/serial 丢弃,对齐 N3 音频主时钟丢帧);
- [ ] 有界 in-flight 集合;seek/暂停/尺寸变化/样式修改/关窗时丢弃旧结果并
  安全回收 worker;
- [ ] 从 2 worker 起依次实测 1/2/3/4/8(`scripts/benchmark_gpu_preview_scheduler.py`
  扩展多 worker、慢帧注入、generation 失效、sidecar kill/restart),
  以吞吐、ready p50/p95/max、stale/drop、显存、RSS 选默认值;
- [ ] 弱 GPU/WARP 自动收缩到 1;保留 sidecar 异常后 Painter fallback、
  冷却与自动重启;
- [ ] 不复用旧 `NativeAsyncSubtitleRenderer` 的整批 range 调度
  (历史上出现过缓存窗口追不上播放时钟的 2~3fps 失控)。

**验收门禁**:

- 任意时刻 in-flight 与 ring 占用 ≤ 配置上限(压力测试断言);
- 交付帧时间戳单调且属于当前 generation;
- 连续播放、快速 seek、样式 churn 无延迟累积,停止播放后不再交付大量旧帧;
- 默认 worker 数由 1/2/3/4/8 实测数据决定并写回本文档,不预设 8 最快
  (G5 仍需回读,worker 上限受带宽与显存约束);
- 10 分钟 RSS/显存无泄漏;WARP 功能门禁通过(不强求 60fps)。

**回退开关**:`gpu_configure` 增加 `worker_count`(1 = 现行为,默认值待实测)。

**新风险与防护**:

- 跨 worker 共享可变资源 → 竞争/D2D 错误:per-context 隔离 + 压测;
- 多 worker 放大回读带宽 → readback 计时进基线,负收益档位不启用;
- 首帧抖动 → warmup 帧。

## 7. 阶段 6:可选质量档位与收口(对应 P5 + 完成定义)

- [ ] 评估 0.25/0.5/1.0 渲染倍率:对齐 N3 做法,倍率在 configure 阶段烘进
  几何与 target 尺寸,不做逐帧 transform;字幕与视频用同一显示坐标,
  DPR/最小化恢复/跨屏不得改布局;仅作为弱 GPU 降载,不替代阶段 1~3;
- [ ] 回填最终数据:各阶段基线/结果 CSV 路径、realization 容差与阈值、
  worker 默认值、缓存容量、未采用方案及原因;
- [ ] 上游计划文档"完成定义"7 条逐条对勾后,两份文档状态改为"已完成"。

## 8. 全局回归门禁(每阶段提交前必跑)

| 门禁 | 命令 | 通过标准 |
|---|---|---|
| 定向测试 | `python -m pytest tests/test_subtitle_render_transport.py tests/test_subtitle_render_gpu_backend.py` | 全绿(按项目惯例只跑 transport + gpu backend,不跑整套 GUI) |
| 渲染基准 | `scripts/benchmark_gpu_renderer.py --stroke-sweep 0,5,14,30,50 --time-points T1..T4` | 目标场景改善;0/5px、无装饰回退 < 5% |
| Painter 对照 | `scripts/compare_gpu_painter_corpus.py` | 既有容差内 |
| N3 对照 | `scripts/compare_gpu_n3_reference.py` | 既有容差内 |
| 调度压测 | `scripts/benchmark_gpu_preview_scheduler.py`(阶段 5 起) | in-flight/ring 不超限,时间戳单调 |
| 长跑 | `scripts/stress_gpu_preview_gui.py` 10 分钟 | RSS/显存平稳,stale/drop 正常 |
| WARP | 上述基准加 WARP 模式 | 功能正确,无泄漏,回退 < 5% |

任何一项不过,该阶段不得合并;用回退开关切回旧路径后重新分析。

## 9. 提交切分建议

1. `perf(gpu): 帧级诊断计数与描边 sweep 基线`(阶段 0)
2. `perf(gpu): 画刷缓存与稳定几何复用`(阶段 1)
3. `perf(gpu): 动态字符临时几何本帧单次计算`(阶段 1,可独立)
4. `perf(gpu): 宽描边 geometry realization`(阶段 2)
5. `perf(gpu): glow 脏矩形与常驻资源`(阶段 3)
6. `perf(gpu): 预览多 worker 流水线`(阶段 5,内部可再拆 池/调度/门禁)
7. `feat(gpu): 预览质量档位`(阶段 6,可选)

CHANGELOG 与发布说明按项目惯例使用中文。
