# GPU 预览宽描边性能与 N3 对齐实施计划

> 状态：已完成(2026-07-21；P0～P4 验收通过，P5 质量档位按既定边界独立排期；完整数据见配套执行文档)
>
> 建立日期：2026-07-21
>
> 适用范围：Windows Direct2D GPU 字幕预览（G5 shared-memory/QImage）及其共用的渲染核心
>
> 基线原则：Python QPainter 永久保留为视觉 oracle 与故障回退；G6 DirectComposition 继续硬关闭

## 1. 目标

解决 GPU 预览中“描边越宽，帧率越低”的问题，并在 clean-room 边界内对齐
NicoKaraMaker3 10.74.80.0（下称 N3）的资源复用和预览流水线设计。

本专项需要同时完成两件事：

1. 降低单帧 Direct2D 渲染成本，重点覆盖正文、注音、标题、行内角色样式、二重描边、发光和
   Utopia/旋转翻转组合；
2. 在单帧成本可控后，把正式 GPU 预览从单 in-flight 调度扩展为有界的多
   `ID2D1DeviceContext` 流水线，提高连续播放吞吐，但不制造旧帧积压。

## 2. 当前基线与根因

### 2.1 固定复现工程

首要基准使用用户提供的真实工程，不要求先测 4K：

```text
C:\Users\18007\Downloads\芽吹の唄 - 大原ゆい子.yurika
```

工程基线为 1920×1080、60fps；主要角色样式约为 107px 正文、14px 主描边、7px
二重描边、5px 发光，并包含 Utopia 进场和逐字淡出。测试时间点应覆盖 Utopia 进场前、进场中、
稳定走字和退场。

在 RTX 3070 Ti Laptop GPU、G5 packed-band 回读下测得的代表性数据如下。数据用于确认趋势，
后续提交必须重新测量并记录硬件、驱动和版本：

| 主描边 + 二重描边 | Direct2D render mean | readback mean |
|---|---:|---:|
| 5px + 7px | 约 15.3ms | 约 2.0ms |
| 14px + 7px | 约 29.5ms | 约 2.0ms |
| 30px + 7px | 约 45.9ms | 约 2.2ms |
| 50px + 7px | 约 62.7ms | 约 2.7ms |

结论：这次下降首先发生在 Direct2D render core，回读不是主要变量；关闭二重描边能明显改善，
Utopia 和发光会进一步放大成本。

### 2.2 当前正式 GPU 预览并非 8 worker

当前产品选择 `GpuAsyncSubtitleRenderer`：

- 一个名为 `subtitle-preview-gpu-render` 的 Python 后台线程；
- 恰好一个同步 sidecar 请求正在执行；
- 最多一个可被新时间戳替换的 pending 请求；
- sidecar 的 `Direct2DGpuBackend` 持有一个 `D2DDevice` 及其单一 DeviceContext；
- 每帧经过 Direct2D 渲染、staging readback、共享内存、QImage，再由 Qt 合成。

项目中已有的多 worker `render_range` 是 C++ QPainter 批量路径；导出界面的“自动（最多 8
进程）”也是导出设置。二者都不是当前 Direct2D GPU 实时预览的 worker 池，不能直接复用或只改一个
线程数常量。

### 2.3 当前渲染热点

需要用计时和计数器逐项确认以下热点，不以推测代替测量：

1. 行内角色样式路径会在逐字符、逐图层、逐帧创建画刷；
2. Utopia/旋转翻转的动态字符会为正文、保护层、描边和二重描边分别创建临时
   `ID2D1TransformedGeometry`；
3. 宽主描边和二重描边仍走每帧 `DrawGeometry`，当前没有
   `ID2D1GeometryRealization`；
4. 发光源宽度包含主描边、二重描边和发光半径，描边越宽，参与模糊与合成的区域越大；
5. 稳定字符和正在动画的字符虽然已有一定分组，但几何、画刷和 glow source 的复用边界仍不完整。

## 3. 已核实的 N3 做法

以下条目来自 N3 10.74.80.0 的反编译观察，只记录行为和架构，不复制其源码。

### 3.1 字体、几何与画刷缓存

1. `DrawDataGenerator` 以字体信息为键缓存 DirectWrite font face；
2. 每个字符保存原始 `CharGeometry`，并长期持有完成位置/缩放后的
   `GeometryPack.Source`；
3. 画刷按 `ID2D1RenderTarget` 缓存在并发字典中，而不是每字符、每层、每帧重建；
4. `GeometryPack` 对正文保护层参数采用惰性缓存；
5. 导出开始前，N3 为稳定几何建立 filled、stroked、stroked2
   `ID2D1GeometryRealization`，容差为 0.25，并用 `DrawGeometryRealization` 绘制；
6. N3 预览没有启用上述 realization，发光也始终使用几何绘制。因此我们可以借鉴缓存层次，
   但是否在预览使用 realization 必须以画质和性能实测决定，不能把“N3 对齐”误解成逐行照搬。

### 3.2 Utopia 的稳定/动态拆分

1. 不在动画窗口内的稳定字符复用长期持有的 positioned geometry；
2. 正在执行 Utopia 的字符才创建临时变换几何；
3. 正文、描边、二重描边和发光分别使用相同的稳定/动态判定；
4. 发光使用常驻 work bitmap，按整行和发光档位处理，不建立整行成品帧缓存。

### 3.3 预览 worker 池

N3 默认启用多线程，worker 数为：

```text
min(Environment.ProcessorCount / 2, 8)
```

它不是 8 个进程或 8 块 GPU，而是：

- 一个共享的 D3D11/D2D device；
- 每个 worker 独立的 `ID2D1DeviceContext1`；
- 每个 worker 独立的 GPU target、compatible work bitmap、事件和 frame ring slot；
- 常驻 OS 线程，不按每批帧反复创建；
- 音频作为主时钟，落后帧可丢弃；
- 字幕与视频在 GPU target 合成后直接送 swap chain，不做 GPU→CPU 回读。

多 worker 的主要价值是让解码、CPU 命令准备、GPU 提交和呈现形成流水线，并非让一个 30ms 的
字幕帧自动变成 `30 / 8` ms。

### 3.4 走字(wipe)的缓存与绘制机制(2026-07-21 二次核实)

这是本次专项最关键的新结论:**走字进度完全不参与任何缓存失效**。

1. 走字时间表在 generate 阶段一次算好:每个字符持有毫秒时间数组与 0~1 归一化
   位置数组两个并行序列,含 ruby→亲字时间折算和相邻字符咬合时的 wipe 终点修正
   (终点位置被夹到下一字符左缘),此后播放期只读;
2. 每帧对正在走字的字符只做三件事:线性查表求当前 wipe X;
   axis-aligned clip(wipeX→画布右缘)画 before 色;
   axis-aligned clip(画布左缘→wipeX)画 after 色。
   两次绘制消费的是**同一份长期持有的几何/realization**,画刷来自同一缓存;
3. 走字前/走字后的字符每帧各画一次(before 或 after 刷),无 clip;
   同一行内绘制顺序为:before 字符从右到左、after 字符从左到右、走字字符最后;
   层顺序为二重描边→描边→本体,ruby 整行先于亲文字;
4. **正在走字的字符在导出路径照常使用 DrawGeometryRealization**——
   realization 在 axis-aligned clip 内正常工作。只有本帧带变换矩阵的字符
   (Utopia 进场/退场/走字缩放脉冲)才回退 DrawGeometry;
5. wipe X 每帧通过 GetBounds() 从几何取包围盒再插值(边界含主描边一半的横向外扩),
   N3 未缓存包围盒,这是可安全缓存的小优化点。

对我们的直接含义:

- 任何缓存 key(几何、realization、画刷、glow source)都**不得包含 wipe 进度或
  播放时间戳**,否则缓存必然退化为逐帧重建,制造新的性能问题;
- "稳定/动态"字符的判定标准是"本帧是否有逐帧变换矩阵",而不是"是否正在走字"。
  播放中任意时刻几乎总有字符在走字,若把走字字符降级为 DrawGeometry,
  宽描边优化在连续播放中将持续失效,收益被显著蚕食。

### 3.5 其他已核实细节

1. Utopia 的走字缩放脉冲(约 1.15 倍、限时)意味着 Utopia 行中正在走字的字符
   每帧要创建临时变换几何;由于本体/描边/二重描边三层 × before/after 两个 clip
   半边各自独立创建,单字符每帧最多 6 次临时几何创建加销毁,发光时更多。
   N3 在此处也是暴力实现,没有"本帧算一次、三层共用"——这是可以优于 N3 的点,
   与 P1 中"动态字符集合计算一次共用"的条目一致;
2. 渐变画刷采用"每 render target 一个实例 + 每行绘制前改写起止端点"的模式:
   N3 在每行每绘制阶段(一行一帧最多 8 次)遍历所有字体的所有画刷并改写线性
   渐变端点。含义:(a) 渐变画刷缓存的 key 不含行 Y 坐标,画刷数量与行数无关;
   (b) 这种就地改写模式要求画刷缓存严格 per-DeviceContext,多 worker 共享同一
   画刷实例会产生跨线程改写竞争;
3. N3 预览的降载缩放是在 freeze 阶段直接把 previewScale 乘进
   transformed geometry(并同步缩小 target bitmap),不是逐帧 transform;
   DrawGeometry 的描边宽度绘制时也乘 Scale。预览里 14px 描边在 0.5 倍率下
   实际是 7px 描边画在四分之一面积画布上——这是 N3 预览流畅的第一杠杆(对应 P5);
4. 导出前的 realization 预热是一次性同步过程,在专用 device context 上创建,
   约占其导出进度条的 10%;每字符 filled + stroked(启用二重描边时再加 stroked2)。
   创建失败或未创建的字符通过 null 检查自动回退 DrawGeometry,
   即 realization 是纯加速层,不承担正确性;
5. 发光(blur)为 BlurLevel+1 次"全画布 work bitmap 重绘 + 全画布高斯模糊"叠加,
   半径逐次递减;blur 路径永远走 DrawGeometry(宽度加 DecorSize),
   从不使用 realization。发光成本正比于 (BlurLevel+1) × 全画布面积,
   N3 没有做脏矩形——P3 的脏矩形收紧同样是超越 N3 的点;
6. 样式修改时 N3 的做法等价于 generation swap 的原始形态:等全部 worker 空转、
   全量重建绘制数据、释放旧数据;渲染期间新旧两代数据不混用。

## 4. 差异矩阵

| 项目 | 当前 Karaoke Studio | N3 | 本专项动作 |
|---|---|---|---|
| font face | 已按样式缓存 | 按字体信息缓存 | 保持并补缓存命中诊断 |
| 字符原始/定位几何 | configure 阶段已有缓存 | 长期持有 | 审计 key 与失效，不重复创建稳定几何 |
| 画刷 | 部分逐字符/逐层/逐帧创建 | 按 render target 缓存 | 建立每 context/target 的画刷缓存 |
| GeometryRealization | 未使用 | 导出使用，预览不用 | 对宽描边预览和导出分别 A/B |
| Utopia | 动态层存在重复几何工作 | 仅动画字符临时变换 | 统一稳定/动态掩码并复用稳定层 |
| 走字(wipe) | 需审计是否有按 wipe 进度重建的资源 | clip 分割复用同一几何,零缓存失效 | 审计并禁止 wipe 进度/时间戳进入任何缓存 key |
| glow work bitmap/effect | 已有池，但宽描边仍扩大成本 | 每 worker 常驻 | 改为每 worker 常驻并收紧脏矩形 |
| GPU 预览 worker | 1 in-flight + 1 pending | 1～8 个 context | 新建有界多 context 流水线 |
| 帧交付 | staging + shm + QImage | GPU swap chain | G5 内优化流水；G6 不重新开放 |
| 过期帧策略 | latest-wins | 音频时钟丢帧 | 保留 latest-wins，增加 PTS/serial 丢弃 |

## 5. 实施分项

### P0：固定基准与诊断

- [x] 给 Direct2D 后端补齐每帧计数：画刷创建、transformed geometry 创建、
  realization hit/miss、稳定/动态字符数、stroke/stroke2 draw 次数、glow source 面积；
- [x] 在 benchmark 输出中分离 animation/layout、geometry、stroke、glow、GPU wait、readback、
  shared-memory/QImage 和端到端延迟；
- [x] 固定真实工程的四组描边宽度和至少四个动画时间点；
- [x] 保存改动前基线，避免只用总 FPS 判断效果；
- [x] 检查 10 分钟连续播放的 sidecar RSS、显存增长和 stale/drop 计数。

### P1：对齐 N3 的资源缓存层次

- [x] 新增 `BrushCache`，按 DeviceContext/target、填充类型、颜色/渐变/图片参数建立 key；
  缓存实例严格 per-DeviceContext(N3 的渐变端点就地改写模式决定了画刷不可跨
  context 共享,这是 P4 多 worker 的前置约束);渐变按 N3 模式采用
  "每 target 一个实例 + 每行改写端点",端点(行 Y)不进入 key；
- [x] solid、linear gradient、MilleFeuille、bitmap brush 都必须复用，角色样式修改或
  render target 重建时精确失效；
- [x] 审计字符 base、positioned、body-protection、stroke、stroke2 geometry 的所有创建点；
- [x] 稳定字符只消费 configure 阶段缓存，逐帧路径不得重新创建等价 geometry；
- [x] 把 Utopia/旋转翻转的“本帧动态字符集合”计算一次，正文、描边、stroke2、shadow、glow 共用；
- [x] 为缓存加入容量、命中/未命中和失效原因诊断，不允许无界增长。

### P2：宽描边 realization 与绘制路径

- [x] 为稳定正文建立 filled/stroked/stroked2 realization 原型，容差先以 N3 的 0.25 为
  A/B 基准；
- [x] 分别测试普通走字、Utopia、旋转翻转、逐字淡出和行内角色样式；
- [x] 稳定/动态的判定标准为"本帧是否有逐帧变换矩阵"(Utopia 进场/退场/走字脉冲、
  旋转翻转),而不是"是否正在走字":正在走字但无变换的字符照常使用 realization
  (clip 内 DrawGeometryRealization 正常工作,已在 N3 导出路径核实);
  仅本帧带变换矩阵的动态字符回退 `DrawGeometry`；
- [x] realization 只允许在 configure/预热阶段创建,逐帧渲染路径禁止创建 realization,
  以免样式修改或 seek 后出现新的预热卡顿；预热需有每次样式变更的时间预算与
  异步分摊策略；
- [x] 任何缓存 key 不得包含 wipe 进度或播放时间戳；
- [x] 主描边和二重描边分别建 key，key 必须包含有效字号、DPR、字体、glyph、描边宽度、
  join/miter 等所有影响像素的参数；
- [x] 验证大字号、斜杠、日英数字体、注音和标题，避免重现字号/字体路由问题；
- [x] 如果预览 realization 在细描边或动画缩放时产生可见差异，只在达到阈值的稳定宽描边启用，
  不为统一代码路径牺牲画质；
- [x] glow 保持从正确的描边后源生成，不能因为 realization 改变 N3/Painter 层顺序。

### P3：glow 与脏区继续收紧

- [x] 每个未来 worker 独立持有并复用 glow scratch bitmap、effect 和目标纹理；
- [x] glow source 的边界使用实际字符/行包围盒加主描边、stroke2、blur padding，不使用整画布；
- [x] 稳定字符合并到行级 glow source，只有正在变换的字符使用动态层；
- [x] before/after、ruby、正文、标题和角色样式的 glow cache key 必须互不串色；
- [x] 特别验证 Utopia 开场最左侧不会产生不应出现的 before 蓝光，注音开头不丢发光。

### P4：正式 GPU 预览多 worker 流水线

- [x] sidecar 新建常驻 `GpuPreviewWorkerPool`；共享 D3D11/D2D device 试作未通过
  配置期稳定性门禁，硬件池采用默认 2 worker、每 worker 独立 device/context；
  8px 以上宽描边按实测自动选择 WARP 并收缩为 1 worker；
- [x] 画刷缓存与 realization 缓存按 worker(per-DeviceContext)隔离,禁止跨 worker
  共享可变 D2D 资源;worker 启动后先渲染 warmup 帧填充各自缓存,避免首帧抖动；
- [x] 每个 worker 独立持有 DeviceContext、frame target、staging/readback texture、glow scratch、
  effect 和共享内存 ring slot；
- [x] 配置生成不可变 scene snapshot；样式或尺寸变化通过 generation 切换，旧 generation
  结果不得呈现；
- [x] 调度器维护有界 in-flight 集合，不允许普通队列无限累积；
- [x] 请求携带时间戳、serial、generation 和 frame index，GUI 只接收当前 generation 且没有
  落后最新播放时钟的帧；
- [x] seek、暂停、尺寸变化、样式修改和关闭窗口时，允许丢弃旧结果并安全回收 worker；
- [x] 从 2 worker 开始，依次实测 1/2/3/4/8；以吞吐、ready latency、stale/drop、显存和
  sidecar RSS 决定默认值，不预设“8 一定最快”；
- [x] G5 仍需回读，默认 worker 上限应由带宽和显存实测决定；弱 GPU/WARP 自动收缩到 1；
- [x] 保留 sidecar 异常后的 Painter fallback、冷却和自动重启；
- [x] 不复用旧 `NativeAsyncSubtitleRenderer` 的整批 range 调度，避免再次出现缓存窗口追不上
  播放时钟的 2～3fps 失控。

### P5：可选的 N3 预览质量档位

- [x] 评估 0.25 / 0.5 / 1.0 三档渲染倍率，结论为保持 1.0，三档 UI/协议因需
  同步视频与字幕坐标、DPR 和跨屏恢复而独立排期；
- [x] 2026-07-22 在预览标题栏最小化按钮左侧接入中性样式的“预览质量”控件，提供
  流畅（1/4）/均衡（1/2）/清晰（1/1）三档；字幕仍使用
  工程逻辑坐标，物理栅格倍率取 `min(显示倍率, 档位倍率)`，清晰档保持原显示分辨率行为；
- [x] 视频继续由 `QGraphicsVideoItem` 按同一 scene/view 映射呈现，切档只重配昂贵的字幕透明层，
  DPR、最小化恢复和跨屏移动继续走现有渲染目标刷新，不改变视频/字幕布局；
- [x] 档位作为本机偏好保存、不写入工程、不影响导出，只用于弱 GPU 降载，不能替代 P1～P4；
- [x] 未重新开放 G6 DirectComposition，G5 generation/cancel/fallback 边界保持不变。

## 6. 实施顺序

严格按以下顺序推进，每一阶段单独提交并保留可回退开关：

1. P0：先得到可重复、可解释的真实工程数据；
2. P1：完成画刷和稳定几何复用；
3. P2：对宽描边 realization 做视觉/性能 A/B；
4. P3：收紧 glow source 和每 worker 常驻资源；
5. 单 worker 重新验收，只有 render core 已明显下降才进入 P4；
6. P4 从 2 worker 起逐档测量，选出默认值和弱 GPU 回退策略；
7. P5 作为独立产品功能评估，不阻塞本专项收口。

原因：多 worker 只能改善流水线吞吐，不能消除单帧内部的重复几何和宽描边开销。如果先上 8
worker，可能只是让同一 GPU command queue、staging readback 和内存带宽更早饱和，并增加过期帧。

## 7. 正确性与性能验收

### 7.1 视觉门禁

- Painter 是产品语义 oracle；N3 是字形、描边层次和特效兼容目标；
- 普通路径与分色路径都要覆盖正文、注音和标题的日文/英数字体、字号和斜杠；
- 覆盖主描边 0/5/14/30/50px、stroke2 开关、三档 glow 和无装饰；
- 覆盖 Utopia + 逐字淡出、Utopia + 旋转翻转及反向搭配；
- 验证 before/after 颜色、渐变、图片填充和行内角色配色不串缓存；
- realization 开/关的关键帧像素差必须落在既有 GPU/Painter 容差内；若超出，先修语义再谈性能。

### 7.2 性能门禁

以固定 1080p60 工程为主，不要求本专项先跑 4K：

- 14px + 7px 的稳定播放目标为 render p95 小于 16.67ms，争取留出回读余量至 12～14ms；
- 描边从 5px 增至 14px 时，不应再出现近似翻倍的重复资源创建次数；
- cache warmup 后，稳定帧画刷创建数和稳定 geometry 创建数应接近 0；
- 多 worker 后连续播放应接近 60fps，`max_pending`/in-flight 永远受配置上限约束；
- 记录 1/2/3/4/8 worker 的 render throughput、ready p50/p95/max、readback、stale/drop、
  sidecar RSS 和本地显存，默认值必须来自数据；
- 连续播放、快速 seek 和样式 churn 下不得出现明显延迟累积或播放停止后继续交付大量旧帧；
- 硬件 Direct2D 与 WARP 都需通过功能门禁；若同机实测 WARP 在宽描边下明显更快，
  产品可按描边阈值自动选择，但必须保留显式回退、稳定样式 churn 且不泄漏。

### 7.3 建议自动化

- 扩展 `scripts/benchmark_gpu_renderer.py`：真实工程、描边 sweep、特效时间点和缓存计数；
- 扩展 `scripts/benchmark_gpu_preview_scheduler.py`：多 worker、播放时钟、seek、慢帧注入、
  generation 失效和 sidecar kill/restart；
- GPU/Painter 对照测试至少覆盖正文、ruby、title、inline style、stroke2、glow、Utopia；
- 新增缓存失效测试：修改字号、字体、DPR、描边、颜色、渐变、图片和角色方案后不得复用旧资源；
- 新增调度压力测试：任意时刻 in-flight 和 ring 占用不超过配置值，交付帧时间戳单调且属于当前
  generation。

## 8. 非目标与边界

- 不删除或弱化 CPU QPainter 路径；
- 不把 8 worker 实现成 8 个 sidecar 进程；
- 不恢复 G6 DirectComposition 产品入口；
- 不使用整段视频或整行最终成品位图缓存规避真实渲染问题；
- 不复制 N3 反编译源码，只记录可观察行为并独立实现；
- 不用单一总 FPS 或合成小样替代真实 `.yurika` 工程验收；
- 不把导出多进程数、C++ QPainter `render_range` 线程数和 GPU 预览 DeviceContext 数混为一谈。

## 9. 完成定义

只有以下条件全部满足，本专项才算完成：

- [x] 固定工程的宽描边热点可由诊断计数和分段计时解释；
- [x] 画刷、稳定几何、保护层和 glow 工作资源按设计复用且能够精确失效；
- [x] 14px + 7px 真实工程达到 1080p60 单帧预算或有明确、可复现的剩余硬件瓶颈；
- [x] 多 worker 池经过 1/2/3/4/8 对照，默认值由实际数据决定；
- [x] latest-wins、generation、ring slot、seek/resize/teardown 和故障回退均有自动测试；
- [x] 普通/分色、正文/ruby/title、日英数字体、宽描边、glow 和 Utopia 组合通过 Painter/N3
   视觉门禁；
- [x] 文档回填最终性能数据、采用的 worker 默认值、缓存容量和未采用方案的原因。

最终结论摘要：固定工程 14px+7px 单 worker 硬件 render mean 从 22.299ms 降至
16.885ms（-24.3%），p95 27.400ms，剩余尾延迟可由 GPU wait/glow 与真实动态字符
稳定复现；1/2/3/4/8 的硬件对照确认 2 worker 最优，但真实长跑只有约 18～32fps，
因此没有用短合成样例冒充完成。阶段 0/4 同机数据同时证明 WARP 在 8px 以上宽描边
显著更快，产品最终按 8px 阈值自动选择 WARP（renderer 生命周期内单向选择，避免
样式滑块反复重建设备），实际 worker 收缩为 1。固定工程 15 秒真实视频交付率 97.9%，
render mean/p95 4.23/6.02ms；10 分钟最终门禁交付 35,707/36,000 帧（99.19%），
render mean/p95 4.20/5.93ms，failure/restart/fallback=0，sidecar RSS 仅增长 5.23MiB；
硬件细描边路径、双 worker 池和所有显式回退仍保留。最终 transport + GPU backend
`245 passed, 1 skipped`；协议合并回归 `283 passed, 28 skipped`；
Painter corpus 通过，真实 Dark Spiral 24,900ms 包围盒最大边差 3px，N3 成品视频
减法门禁 IoU 0.796927、宽度差 1px。所有阈值、缓存容量、CSV/JSON 路径和未采用方案
详见 `docs/GPU预览宽描边性能优化执行计划书.md`。
