# AI 自动打轴——需求与实施计划

> 状态：P0 主路径已实现；SUG standalone 打包链路（安装→分离→对齐）已跑通，
> KS 打包嵌入链路待冒烟（详见 §15 实施进度）
>
> 校准日期：2026-08-15
>
> 适用仓库：Karaoke Studio 与 StrangeUtaGame（SUG）submodule

## 0. 文档目的

本文记录 SUG 歌词打轴模块接入 AI 自动时间轴对齐的完整产品决策、standalone / embedded
边界、模型与人声复用策略、缓存规则、取消与撤销语义、实施阶段和验收标准。

本功能允许同时修改 Karaoke Studio 主仓库和 StrangeUtaGame 独立仓库。开发者可在任意
位置建立 StrangeUtaGame 的独立工作树，先在该仓库完成 SUG 改动、测试与提交，再回到主仓库
更新 `krok_helper/lyrics_timing` 的 submodule 指针。本文不约定独立仓库的本机绝对路径。
不得直接把两边实现揉成只能在工作台运行的单一路径；SUG standalone 是正式产品路径，必须
与 embedded 同时通过验收。

参考项目：

- FA-Kara：<https://github.com/moriwx/FA-Kara>
- yohane：<https://github.com/Japan7/yohane>
- 日语卡拉 OK 微调模型：
  <https://huggingface.co/NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn>

## 1. 已冻结的产品决策

1. 在 SUG 打轴页工具栏增加一级入口“AI 打轴”。
2. standalone 与 embedded 使用同一个按钮、同一个完整弹窗和同一套核心逻辑。
3. 不使用分步向导。弹窗一次性展示素材、环境、模型、人声与执行状态。
4. 原始音频只取 SUG 当前加载的音频素材；SUG 未加载音频时不打开弹窗，并显示 Infobar。
5. 正式支持日语、中文、英语及其任意混排，不施加“一行一种语言”的限制。
6. **项目中已经标注的文字、Ruby、读音及 checkpoint 信息拥有绝对第一优先级。**
   只有标注缺失时，才调用 SUG 现有自动注音能力补足缺口。
7. 默认采用效果优先的日语卡拉 OK 微调模型，同时保留 MMS 基础模型为高级选项。
8. 模型与重型运行环境不随主程序安装包分发，在用户主动操作后下载到自选目录。
9. 模型下载不增加许可证确认弹窗；模型卡、设置页和关于信息需持续展示来源、署名、
   许可证及非商业限制。
10. AI 结果成功后直接覆盖全部时间戳，不提供“只补空白”模式；整个应用动作可一次撤销。
11. embedded 分离始终跟随工作台当前“分离人声”设置；standalone 使用自己的分离设置。
12. 一次任务内模型只加载一次；任务结束释放模型和显存。跨任务只复用磁盘文件，不要求
    模型常驻内存。
13. 人声与对齐结果均放入 SUG `.cache` 管理范围，默认各只保留最近 **2 个项目**。
14. 下载、环境安装、模型准备、分离和对齐的进度 UI 必须复用项目内已有组件、文案层级、
    进度条、错误状态与取消交互风格，不另造一套视觉语言。
15. 对齐或分离中途取消必须二次确认；确认后丢弃本次尚未应用的全部打轴数据。

## 2. 产品目标与非目标

### 2.1 产品目标

- 用户已经配置过环境时，加载音频和歌词后只需点击“AI 打轴”并执行一次自动对齐。
- 自动寻找可复用人声；只有确实没有可用产物时才执行分离。
- embedded 复用工作台已安装的 PyMSS Runtime、分离模型、当前设置与会话产物。
- standalone 能独立配置 Runtime、分离模型、对齐模型和目录并完成相同任务。
- 充分消费 SUG 工程现有标注，避免重新注音覆盖用户校对过的读音。
- 对缺失标注调用 SUG 既有自动注音能力，不复制另一套互相漂移的注音实现。
- 支持可靠进度、预计剩余时间、取消、错误恢复和一次撤销。
- 相同源音频、模型和参数组合不重复分离；模型不重复存储。

### 2.2 非目标

- 不承诺 AI 结果无需人工检查。
- 不把 FA-Kara 或 yohane 的 CLI 原样嵌入 SUG。
- 不在 Qt 主线程运行 PyTorch、Transformers 或长耗时音频处理。
- 不把模型塞进 PyInstaller 主包或 Git 仓库。
- 不自动覆盖用户已经标注的 Ruby、读音或文字。
- 不提供后台永久常驻模型模式。
- 不根据宽泛的 `vocal` / `vocals` 文件名模糊猜测其他歌曲的人声。
- 首期不实现云端推理服务。

## 3. 用户交互

### 3.1 一级入口与前置阻拦

打轴页工具栏增加“AI 打轴”按钮。

点击后先检查：

1. 当前存在 SUG Project；
2. Project 存在可对齐正文；
3. SUG 当前已加载可读取的音频素材。

没有音频时，显示中文 Infobar，提示先加载音频，不构造也不显示 AI 弹窗。项目或正文不满足
条件时采用相同的轻量阻拦方式。

### 3.2 完整弹窗

弹窗一次性包含以下区域：

- 原始音频：当前路径、时长、媒体身份和可读取状态；
- 人声素材：当前来源、缓存命中、同目录命中或需要重新分离；
- 分离环境：Runtime、设备、当前“分离人声”模型和模型状态；
- 对齐环境：worker Runtime、设备、版本和完整性；
- 对齐模型：模型名、位置、来源、许可证、大小和校验状态；
- 存储位置：Runtime、模型和 `.cache/ai_timing`；
- 高级选项：对齐模型、设备、尾音修正、音频倍速、下载源等；
- 执行区：“自动对齐”主按钮、进度、ETA、日志摘要与取消按钮。

统一状态为：已就绪、可复用、需要下载、需要分离、校验中、不可用、错误。

“自动对齐”按钮仅在所有前置条件满足时启用。点击后必须再次基于当前工程、音频和设置生成
不可变执行快照，不能只相信弹窗打开时的旧状态。

### 3.3 下载与浏览动作

弹窗提供：

- 下载或修复对齐 Runtime；
- 下载或修复对齐模型；
- 浏览已有 Runtime；
- 浏览已有模型目录；
- 更改模型位置；
- 更改缓存位置；
- 恢复推荐设置；
- 重新校验。

下载动作不再额外弹许可证确认框。模型卡片长期显示模型来源、许可证
`CC-BY-NC-SA-4.0` 和“仅限非商业使用”，并提供可点击链接。项目开源不改变模型自身的
非商业限制，相关措辞不得暗示模型随 GPL 获得商业使用许可。

### 3.4 进度 UI

下载和运行进度应优先复用以下现有设计：

- 工作台 PyMSS 安装/修复和模型下载的状态卡、进度条、日志与错误呈现；
- 工作台音频分离任务的阶段进度、已处理时长和取消交互；
- SUG 已有长任务弹窗、StateToolTip、Infobar 和非模态弹窗策略；
- 项目已有的统一 Fluent 按钮、间距、标题、正文和危险确认样式。

禁止为 AI 打轴另建一套颜色、进度控件或“控制台式”界面。若现有组件无法直接共享，应先
抽出项目级通用组件，再由原功能与 AI 打轴共同消费。

## 4. 标注优先的混合语言策略

### 4.1 核心原则

对齐输入不是从 Project 的可见歌词重新猜读音，而是先读取 SUG 已经持有的权威标注。

严格优先级如下：

1. Character/RubyPart 中用户已经确认或导入的细粒度读音；
2. Character 已有 Ruby 的完整读音及 checkpoint 切分；
3. Project 中其他既有、可明确归属到该 Character 的读音信息；
4. 对仍无读音的缺口调用 SUG 自动注音接口；
5. 对自动注音仍无法解析的缺口报错并阻止执行。

后一级只能填补前一级的空白，不得改写、规范化替换或重新切分已有标注。即使自动分析给出
“更标准”的结果，也必须保留项目原标注，因为它可能是用户针对唱法手工校正的读音。

### 4.2 抽取 SUG 注音接口

SUG 现有日语 Ruby、中文拼音、罗马字、数字和自动检查能力需要抽成不依赖 UI 的应用层接口：

```python
class PronunciationResolver:
    def resolve_project(
        self,
        project: Project,
        *,
        fill_missing: bool,
    ) -> PronunciationPlan: ...
```

接口分为两步：

1. `collect_existing_annotations(project)`：只读提取现有标注，不做推测和写回；
2. `fill_missing_annotations(plan)`：只针对缺口调用 SUG 自动注音能力。

UI 中的“注音分析”“中文拼音注音”“全部转为罗马字”等动作也应逐步改为消费相同底层服务，
避免 AI 打轴与人工按钮形成两套规则。

### 4.3 PronunciationPlan

每个对齐单元至少保存：

- 行索引、Character 索引与 checkpoint/RubyPart 索引；
- 原始文字与显示用 Ruby；
- 对齐用读音；
- 读音来源：`existing_part`、`existing_ruby`、`existing_character` 或 `generated`；
- 脚本/语言类别；
- 是否生成模型 token；
- token 到 SUG 对象的反向映射；
- 原标注摘要，用于应用前检测工程是否已变化。

标点、空格和装饰字符可以不产生模型 token，但必须保留结构映射，供时间边界修正和结果写回。

### 4.4 混合语言分段

在尊重已有标注的前提下，仅对缺失部分按字符或连续脚本片段分析：

- 日文汉字：调用 SUG 现有日语注音分析；
- 平假名、片假名：调用 SUG 现有日语罗马字能力；
- 中文汉字：调用 SUG 现有中文拼音能力；
- 拉丁字母：走英文规范化和发音处理；
- 数字：复用 SUG 已有数字读法；
- 标点与空白：一般不生成 token；
- 混合脚本：按上述规则组合，不按整行强行判定单一语言。

已有 RubyPart/checkpoint 的边界必须保留。自动补注音只为缺口建立临时映射，不得重新切分已有
RubyPart。

### 4.5 临时补注音与工程写回

自动补出的注音首先只存在于执行快照中：

- 执行失败或取消：全部丢弃；
- 对齐成功：与时间戳一起作为一次原子命令应用；
- 一次撤销：同时恢复原时间戳和原注音状态。

## 5. 对齐引擎

### 5.1 默认与备选模型

默认使用效果优先的：

`NextFire/mms-300m-ForcedAligner-karaoke-ja-Latn`

高级选项保留 torchaudio MMS_FA 基础模型。模型切换不会静默发生；默认模型失败时应提示具体
原因，让用户主动选择备选模型。

### 5.2 实现边界

吸收 FA-Kara 的歌词适配、非静音区映射、行首与尾音修正思路；吸收 yohane 的 forced
aligner 抽象、Wav2Vec2 加载和 token span 计算。不得把上游 CLI、固定输入输出文件名或整套
脚本直接嵌入产品。

SUG 内建立稳定接口：

```python
class ForcedAlignmentProvider:
    def validate_model(self, request): ...
    def load(self, request, progress, cancel): ...
    def align(self, request, progress, cancel): ...
    def unload(self): ...
```

模型输出先转换为独立 `AlignmentResult`，通过完整校验后才能接触当前 Project。

### 5.3 worker 进程

forced alignment 在独立 worker 进程执行，以满足：

- 不阻塞 Qt；
- standalone 与 embedded 行为一致；
- 取消时可协作停止并在必要时终止进程；
- 推理崩溃不拖垮工作台或 SUG；
- 任务结束能可靠释放模型和 CUDA 上下文。

worker 协议使用版本化消息，至少覆盖 `validate`、`download`、`load`、`align`、`cancel`、
`progress`、`result` 和 `error`。

## 6. 人声查找、分离与复用

### 6.1 查找顺序

1. 当前工程显式记录的人声；
2. embedded 宿主当前会话中与原音频身份匹配的人声；
3. `.cache/ai_timing/vocals` 中校验通过的人声；
4. 原音频同目录内严格匹配的 `原文件名_人声.<支持的扩展名>`；
5. 调用分离服务重新生成。

工作台现有“分离人声”的默认输出名称为 `原文件名_人声.wav`。同目录查找只接受严格的
`_人声` 后缀和受支持音频扩展名，不接受宽泛名称或相似度匹配。存在多个严格候选时优先无损
格式；仍无法唯一决定时在弹窗内要求用户选择。

### 6.2 embedded

工作台实现 `AiTimingHost` 能力，向 SUG 提供：

- 当前分离环境和模型状态；
- “分离人声”实际生效的模型、stem 和参数；
- 当前会话已产生的人声；
- 启动、监控和取消人声分离；
- SUG AI 缓存位置。

embedded 不再安装第二份 PyMSS，不复制分离模型，也不在 SUG 设置中保存另一套分离参数。
高级区域只读显示“跟随工作台设置”，并允许跳转到工作台现有分离设置。

### 6.3 standalone

standalone 在 SUG 设置中保存自己的分离 Runtime、模型、stem、设备和下载源。默认配置与
工作台“分离人声”任务保持一致，但两种运行模式各自持久化设置。

standalone 适配器与 embedded 宿主适配器消费同一版本化分离协议，避免出现两套任务语义。

### 6.4 分离产物缓存

工作台现有中间缓存只覆盖多阶段步骤，默认人声任务为单阶段，不能满足最终人声复用。需要新增
校验过的最终人声缓存。

缓存键至少包含：

- 原始媒体身份与实际音频内容摘要；
- Runtime、preset 和协议版本；
- 实际模型身份及权重摘要；
- stem、推理参数和输出格式版本。

只有 manifest、大小和摘要全部验证通过的完成项可被复用。

## 7. 目录、模型与缓存

### 7.1 目录职责

- Runtime：Python、PyTorch、Transformers 等运行环境；
- 对齐模型：Hugging Face 权重；
- 分离模型：PyMSS/MSST 权重；
- `.cache`：可再生人声、对齐结果、临时工作文件和日志。

模型不得放入自动清理的 `.cache`。模型下载必须使用显式目标目录，并控制 `HF_HOME`、
`HF_HUB_CACHE` 和 `TRANSFORMERS_CACHE`，防止应用模型目录和 Hugging Face 默认缓存各存一份。

### 7.2 AI 缓存结构

```text
.cache/
└── ai_timing/
    ├── vocals/
    │   └── <cache-key>/
    │       ├── manifest.json
    │       └── vocals.wav
    ├── alignment/
    │   └── <cache-key>/
    │       ├── manifest.json
    │       └── result.json
    ├── work/
    └── logs/
```

embedded 使用宿主注入的 SUG cache 根目录；standalone 使用 `app_dirs.cache_dir()`。两者的
相对目录和 manifest 规则完全一致。

### 7.3 自动清理

- `work/` 和未完成 `.part`：任务结束及下次启动时清理；
- 人声缓存：默认只保留最近使用的 2 个项目；
- 对齐结果缓存：默认只保留最近使用的 2 个项目；
- 正在运行或锁定的条目不参与清理；
- 模型和 Runtime 永不由 AI cache 清理器删除；
- 设置中允许手动清理并可调整上限，但默认值保持 2。

缓存清理必须严格限定在 `ai_timing` 根目录内，采用解析后的绝对路径检查，不得对未验证路径
执行递归删除。

## 8. 执行、进度、取消与应用

### 8.1 执行前检查

点击“自动对齐”后重新检查：

1. 工程与音频未切换；
2. 歌词、Ruby 和 checkpoint 修订号未变化；
3. 所有正文都能生成合法 token；
4. 人声可用或分离环境完整；
5. 对齐 Runtime 与模型完整；
6. 缓存与工作目录可写且空间足够；
7. 当前没有冲突的后台任务。

任一条件不满足均阻止执行并在对应状态卡提供可操作原因。

### 8.2 阶段进度

对齐任务按以下阶段报告：

1. 创建不可变工程快照；
2. 提取已有标注；
3. 仅为缺口执行自动注音；
4. 获取或生成可用人声；
5. 准备音频与非静音区；
6. 加载对齐模型；
7. forced alignment 推理；
8. token span 映射回 Character/checkpoint；
9. 行首、静音与尾音修正；
10. 完整性校验；
11. 原子应用并刷新界面。

人声分离阶段沿用现有按音频处理时长报告的进度。对齐阶段使用可确定的阶段权重并结合真实
推理进度。ETA 首次运行显示“正在估算”，获取足够样本后采用平滑速度估计，不显示剧烈跳动的
瞬时值。

### 8.3 取消

取消按钮显示二次确认：

> 确定取消 AI 打轴吗？当前尚未应用的结果将被丢弃。

确认后：

- 设置协作式取消标记；
- 通知 PyMSS 或 forced-alignment worker 停止；
- 必要时终止本任务拥有的 worker；
- 删除本次未完成工作文件；
- 丢弃临时注音、时间戳和 AlignmentResult；
- 不写入撤销栈，不改变当前 Project；
- 已完整校验并注册的模型与人声缓存可以保留；
- 未完整写入的缓存不可被后续任务识别。

### 8.4 应用与撤销

成功结果通过新的 `ApplyAiTimingCommand` 一次性应用：

- 覆盖全部现有时间戳；
- 写入本次为缺口生成且确有必要的注音；
- 维护 Ruby/checkpoint 派生数据；
- 刷新波形标签、歌词预览、完成度和脏状态。

一次 undo 必须完整恢复执行前的全部时间戳和注音状态。结果校验失败、工程修订号变化或用户
取消时不得部分应用。

执行完成后直接应用并显示完成提示，不再要求用户二次点击“应用”。

## 9. standalone / embedded 契约

SUG 定义可选宿主协议：

```python
class AiTimingHost(Protocol):
    def separation_status(self) -> SeparationStatus: ...
    def effective_vocal_model(self) -> ModelIdentity: ...
    def find_session_vocal(self, source: MediaIdentity) -> Path | None: ...
    def separate_vocal(self, request, callbacks) -> CancelHandle: ...
    def ai_cache_dir(self) -> Path: ...
```

- embedded：由工作台注入实现；
- standalone：协议为空，SUG 自动选择本地托管适配器；
- 对齐核心、弹窗、缓存、PronunciationPlan 和应用命令不区分模式；
- 只有环境/分离能力的来源通过适配器变化。

需同步更新 SUG `docs/EMBEDDING.md` 和 `tests/unit/test_embedded_contract.py`，确保 embedded
新增能力不会破坏 standalone 的窗口、设置、主题、语言、保存和退出行为。

## 10. 模型生命周期与去重

- 每次任务只实例化一次选定对齐模型；
- 同一次人声准备只加载一次实际分离模型；
- 任务结束释放模型引用和设备资源；
- 跨任务从同一磁盘目录重新加载；
- 下载完成后只从受控本地路径加载；
- 模型 manifest 记录模型 ID、revision、文件列表、大小和摘要；
- 路径规范化后不得主动复制出第二份相同权重；
- embedded 不复制工作台已有分离模型；
- standalone 浏览已有兼容目录时原地使用，不强制导入复制。

## 11. 实施阶段

### 阶段 A：领域契约与标注优先接口

1. 在独立 SUG 仓库定义 `PronunciationPlan`、token 映射和来源枚举。
2. 抽取“只读已有标注”接口。
3. 抽取“只补缺口”的自动注音接口。
4. 覆盖日中英混排、RubyPart、checkpoint、数字、标点和未支持字符测试。
5. 确保任何自动分析都不会覆盖已有项目标注。

完成门槛：纯领域测试证明现有标注优先级和反向映射稳定。

### 阶段 B：对齐请求、结果与写回

1. 定义版本化 `AlignmentRequest` / `AlignmentResult` schema。
2. 实现 token span 到 Character/checkpoint 的映射。
3. 实现时间单调性、覆盖率、边界和数量校验。
4. 实现 `ApplyAiTimingCommand`。
5. 验证全量覆盖和一次撤销。

完成门槛：使用固定 emission 的测试可无模型地完成稳定写回与撤销。

### 阶段 C：forced-alignment worker

1. 建立独立进程协议和取消机制。
2. 接入微调 Wav2Vec2 模型。
3. 接入 MMS_FA 备选模型。
4. 实现非静音区、原时间轴映射和尾音修正。
5. 实现模型卸载、崩溃隔离和中文错误转换。

完成门槛：CPU 小样本真实模型烟测通过，取消后无残留进程。

### 阶段 D：模型与 Runtime 管理

1. 建立受控模型目录和 manifest。
2. 接入下载、恢复、摘要校验、浏览已有目录和镜像源。
3. 控制 Hugging Face 缓存变量，验证无重复副本。
4. 实现 standalone 对齐 Runtime 设置。
5. 按项目现有 UI 风格完成状态、下载进度、错误和修复交互。

完成门槛：离线已有模型可运行，下载中断不会注册半成品。

### 阶段 E：人声发现与 AI cache

1. 实现媒体身份和严格 `_人声` 文件名匹配。
2. 实现人声、对齐结果 manifest 和原子写入。
3. 实现各保留最近 2 个项目的 LRU 清理。
4. 实现 standalone 人声准备适配器。
5. 扩展工作台现有分离后端，缓存单阶段最终人声。

完成门槛：相同源音频、模型和参数不重复分离；变化任一关键字段均正确失效。

### 阶段 F：完整弹窗与任务控制

1. 增加打轴页一级按钮和无音频 Infobar。
2. 建立完整弹窗及全部状态卡。
3. 复用现有下载、运行进度与错误 UI 风格。
4. 接入 ETA、取消二次确认和任务清理。
5. 完成成功后自动应用与提示。

完成门槛：UI 线程不卡顿，所有缺失条件都能在执行前阻拦并给出解决动作。

### 阶段 G：embedded 宿主接入

1. 在 SUG 增加 `AiTimingHost` 可选协议。
2. 工作台实现当前分离设置、会话人声、任务启动和缓存路径能力。
3. embedded 复用 PyMSS Runtime 和模型，不产生第二份配置或权重。
4. 接入工作台退出时后台任务检查与取消。
5. 更新嵌入契约和回归测试。

完成门槛：embedded 已有人声路径零分离完成；缺人声时仅调用一次现有分离任务。

### 阶段 H：集成、打包与 submodule 更新

1. 运行 SUG 完整测试和 standalone Qt offscreen 冒烟。
2. 在 SUG 当前分支提交改动。
3. 更新主仓库 submodule 指针。
4. 运行工作台完整测试和 embedded Qt offscreen 冒烟。
5. 更新 PyInstaller 收集、worker 启动器和 Runtime manifest；模型仍不进安装包。
6. 完成 Windows CPU、NVIDIA 和离线模型手工烟测。

完成门槛：两种运行模式、取消、撤销、缓存命中和真实打包应用均通过。

## 12. 测试矩阵

### 12.1 SUG 单元测试

- 项目已有 RubyPart 优先于所有自动分析；
- 已有整段 Ruby 优先，仅缺失 Character 自动补充；
- 日中英同一行混排；
- 数字、标点、空格和装饰符；
- tokenizer 不支持字符时执行前阻拦；
- token 到 Character/checkpoint 反向映射；
- 全量覆盖时间戳；
- 一次撤销恢复时间戳和注音；
- 取消、失败和过期结果不修改工程；
- standalone 设置持久化；
- embedded 不写 standalone 分离设置。

### 12.2 工作台单元测试

- `原文件名_人声.<ext>` 严格匹配与多候选行为；
- 会话人声优先级；
- 最终人声 cache hit/miss；
- 源音频、模型、stem、参数或版本变化导致失效；
- embedded 跟随当前“分离人声”配置；
- 同一模型不重复下载或复制；
- 人声与对齐缓存各只保留最近 2 个项目；
- 清理不会越出 `ai_timing` 根目录。

### 12.3 集成与真实烟测

- standalone：已有环境、模型和人声；
- standalone：缺环境、模型和人声；
- embedded：复用工作台会话人声；
- embedded：通过工作台新分离；
- 日中英混排且包含人工校正 Ruby；
- 分离中取消；
- 对齐中取消；
- 完成后一次撤销；
- 运行中切换工程或修改歌词；
- 无音频 Infobar；
- 离线已有模型；
- Windows CPU 与 NVIDIA CUDA；
- PyInstaller 打包后的 standalone 与 embedded。

普通 CI 使用伪 worker、固定 emission 和小型音频，不下载大型模型。真实模型测试作为手动或
定期 smoke，不以 Mock 结果代替真实模型验收。

## 13. 验收标准

必须同时满足：

- standalone 与 embedded 都能通过同一“AI 打轴”入口完成任务；
- 没有音频时不打开弹窗；
- 日中英混排可执行；
- 项目已有标注永远优先，自动注音只补缺口；
- 模型和 Runtime 不随安装包分发；
- 用户能下载、浏览和选择模型位置；
- embedded 复用工作台分离设置、Runtime、模型和会话人声；
- standalone 使用自己的可配置分离设置；
- 相同输入和配置不重复分离；
- 应用不主动制造重复模型副本；
- 一次任务内模型不重复加载；
- 人声与对齐结果默认各只保留最近 2 个项目；
- 下载和运行进度符合项目已有 UI 风格；
- 取消不留下部分时间戳或临时注音；
- 成功后自动覆盖全部时间戳；
- 一次撤销完整恢复；
- UI 不阻塞，错误均为中文且可操作；
- 两仓测试、Qt 冒烟和打包实测通过。

## 14. 当前实现起点

实施从阶段 A 开始。第一批代码只处理 PronunciationPlan、项目现有标注提取、缺口补注音和
反向映射，不先做 UI 或模型下载。只有领域契约通过混合语言与标注优先测试后，才进入 worker、
缓存和弹窗阶段。

## 15. 实施进度（2026-08-15 校准）

阶段 A–H 全部落地：转写（拼音表音 / e2k 英文 / 数字读音）、word_groups 比例切分、
尾音能量判据、MMS_FA 层进度、方案 B 共享 Runtime、发行包安装路线与 embedded 宿主接入
均已合入 SUG `main`。本轮为**打包（frozen）链路的集中修复**，SUG standalone 已从安装到
对齐全链路跑通；以下按问题记录，提交号均为 SUG 仓库（另注 KS 仓库者除外）。

### 15.1 打包版「安装 / 修复」报缺少 python.exe（58cde67）

- 根因：`install_from_release` 按「zip 条目相对 runtime 根、无前缀」校验，而
  karaoke-studio-runtime 真实发行包（pymss-runtime-v2.0.18-r1）的 zip 条目与清单
  `files[].path` **均带 `runtime/` 前缀**（与 KS `separation/runtime.py` 的 `_safe_member`
  契约一致，已对真实资产验证：6569 个条目全部 `runtime/…`）。
- 修复：校验 `payload/runtime/python.exe`、只搬 `payload/runtime` 子目录（顺带消灭
  `target/runtime/runtime` 双重前缀隐患，带杀软锁重试）；快路径判定从完整 probe 改为
  「torch 可导入」，半装环境免重下 3GB wheel；成功后清理 staging（约 3.2GB）；cu128
  路线拉清单前做峰值磁盘预检（约 9.5GB，完成后约 6GB）。

### 15.2 安装后「分离环境」行仍显示未安装、重复修复无效（f4ede90）

- 根因：`StandaloneVocalSeparator` 在弹窗构造时固化 `settings.runtime_python`（当时为
  空），prober 闭包始终探测空路径；重开程序才恢复。
- 修复：分离器改惰性读取解释器路径（str 或零参 callable），宿主/standalone 接线传活设置
  引用；embedded 回落分离同样受益（能拿到构造后才注入的托管 runtime）。

### 15.3 pip 警告漏进弹窗底部状态行 + 安装无日志（f4ede90）

- pip 的 Scripts 目录 PATH 警告（"Consider adding this directory to PATH…"）被逐行转发
  函数写进状态行：所有 pip 调用加 `--no-warn-script-location`。
- 新增安装日志 `ai_runtime\install.log`：会话头（时间/pid/frozen 标记）、限噪进度行
  （消息变化即记，否则最多 2 秒一条）、pip 完整逐行输出、失败原因（`log_line`）；
  安装失败弹窗附日志路径，成功提示同；shared 模式日志落在解释器上级目录；超 5MB 轮转。

### 15.4 对齐 worker 启动即崩：python313.dll conflicts（a42f95f）

- 根因：runpy 引导把 frozen 包根 `_internal` 用 `sys.path.insert(0, …)` 插到最前，
  PyInstaller 为宿主 Python（SUG 打包用 3.13）收集的 stdlib 扩展（`unicodedata.pyd` 等）
  遮蔽了嵌入式运行环境（3.12）自带版本 → `import unicodedata` 触发
  `ImportError: Module use of python313.dll conflicts with this version of Python`。
- 修复：包根改 `sys.path.append`（运行环境 stdlib 优先，包根兜底）；外部解释器环境不再
  设 `PYTHONPATH=_internal`（对非嵌入式 Python 同样会遮蔽 stdlib）。
- 已在真实机器复现 insert 崩溃 / 验证 append 通过（torch 2.7.1+cu128 CUDA 可用）。
- KS 现状：KS 用 Python 3.12 构建、托管 runtime 同为 3.12，天然不触发；append 修复使
  未来版本错位也免疫（随 submodule bump 生效）。

### 15.5 KS 打包嵌入 worker 缺源码（KS 仓库 1bb216b）

- 根因：KS 用 `--collect-submodules` 把 SUG 编进 PYZ（仅 frozen 应用自身可用），数据
  add-data 只落 `config/resource/bass`，`_internal\strange_uta_game` 是无 `__init__.py`
  的命名空间包 → runpy 引导 `ModuleNotFoundError: strange_uta_game.backend`（真实打包
  产物上复现；此前 KS 冒烟为源码运行，未暴露）。
- 修复：`build_windows.bat` / `build_macos.command` 在打包末尾把 submodule 源码树复制到
  `_internal\strange_uta_game` 同相对路径（剔除 `__pycache__`/`.pyc`），包校验清单加
  worker `client.py` 探针；frozen 应用自身 import 仍走 PYZ（FrozenImporter 优先）。
  已用提取出的真实复制步骤 + 干净解释器环境验证 worker 引导。

### 15.6 其他打包体验修复

- 打包版未装环境时运行环境行显示「未安装（点击下方「安装 / 修复」）」而非误导性的
  「当前解释器」（782ba6e）；frozen 磁盘预估按发行包口径（CUDA 完成后约 6GB）。
- 防黑框（`hidden_subprocess_kwargs`）与 frozen 空解释器守卫（防幽灵进程）此前已合入。

### 15.7 GPU 支持口径

- NVIDIA：检测到独显且驱动满足 cu128 下限时装 CUDA 版（对齐环境行显示 GPU/CUDA/torch
  版本）；驱动过旧自动落 CPU 版。
- AMD / Intel / 无独显：一律 CPU 版发行包（底座 153MB + torch cpu wheel 约 216MB，落盘
  约 2GB），对齐与分离全部 CPU 执行——功能完整、无报错，仅推理速度显著慢于 CUDA；
  设备下拉手动选 CUDA 会回退 CPU 并提示。torch 官方无 Windows AMD 加速轮子
  （ROCm 仅 Linux），DirectML 不在支持范围。
- Apple 芯片：源码运行可走 MPS；打包版 runtime 安装暂不支持 macOS（明确报错提示）。

### 15.8 当前待办

1. KS 打包冒烟：嵌入 AI 打轴执行链路（安装 / 修复 → 分离 → 对齐）。
2. 发版 4.2.6（流程 B，攒批 bump submodule + CHANGELOG + README）；期间 release.yml 已
   暂改为草稿发布（KS 仓库），本地冒烟通过后手动发布并移除 `draft: true`。
