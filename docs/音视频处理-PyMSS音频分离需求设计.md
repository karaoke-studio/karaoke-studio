# 音视频处理与 PyMSS 音频分离 — 需求与设计

> 状态：P0 功能已实现并进入发布前验收；产品默认使用真实 PyMSS 后端，Mock 仅用于测试
>
> 校准日期：2026-08-07
> 适用仓库：Karaoke Studio（卡拉OK工作台）

## 0. 文档目的

本文定义工作流第 2 步由“波形对齐”升级为“音视频处理”后的产品需求、PyMSS
集成边界、安装与修复流程、服务状态机、三类固定音频分离任务，以及首版验收标准。

本文同时记录产品需求、已冻结工程决策和当前实现证据。未完成的外部发布动作或尚未覆盖
的硬件实测会明确列出，不以 Mock、意图或窄范围单测冒充完成。

## 1. 背景与技术选型

当前工作流第 2 步只包含波形对齐。目标是在不破坏现有波形对齐能力的前提下，把第
2 步扩展为统一的“音视频处理”入口，并新增面向卡拉 OK 制作的音频分离功能。

旧 MSST-WebUI 官方仓库已明确提示项目即将停止维护，并推荐使用其后继 PyMSS：

- MSST-WebUI：<https://github.com/SUC-DriverOld/MSST-WebUI>
- PyMSS：<https://github.com/pymss-project/pymss>
- PyMSS Server CLI：<https://github.com/pymss-project/pymss/blob/main/docs/server/cli.md>
- PyMSS Server API：<https://github.com/pymss-project/pymss/blob/main/docs/server/api.md>

因此本功能采用以下口径：

1. 产品对用户展示为“音频分离（PyMSS）”。
2. 不嵌入、不调用旧 MSST-WebUI 的 Gradio 页面。
3. 推理底座使用固定并经过验证的 PyMSS 版本。
4. PyMSS 在独立进程和独立 Python 运行环境中运行，不直接塞进工作台的 PyInstaller
   `_internal/`。
5. GUI 只依赖工作台内部的后端适配层，不直接依赖具体 HTTP 字段或 PyMSS 模块对象，
   以便未来替换通信方式而不重写界面。
6. Karaoke Studio 主安装包显式排除 `torch`、`pymss`、`pymss_core` 和 `pip`；托管
   PyMSS 底座、设备对应的 PyTorch wheel 与模型都在用户确认后下载到独立目录。
7. 托管底座自带嵌入式 Python 和私有 pip，因此用户电脑不需要预装 Python 或 pip；
   私有 pip 只用于把经过固定大小与 SHA-256 校验的官方 PyTorch wheel 安装进托管目录。

截至本文校准日期，PyMSS 最新正式 Release 为 `v2.0.18`，项目元数据仍标记为
Alpha。首版开发必须固定确切版本，不能运行时无约束升级到最新版。

## 2. 产品目标与非目标

### 2.1 产品目标

- 将工作流第 2 步名称改为“音视频处理”。
- 在第 2 步内部提供“波形对齐”和“音频分离”两个主 Tab。
- 完整保留现有波形对齐界面、快捷键、素材状态和导出能力。
- 支持工作台自动下载、校验、启动和监控托管 PyMSS。
- 支持用户选择电脑上已有的兼容 PyMSS 环境或服务。
- 支持老 MSST-WebUI 用户只安装 PyMSS 托管 Runtime，并原地复用已经下载的兼容模型，
  不要求复制或重新下载大体积权重。
- 面向普通用户只提供“分离人声”“分离伴奏”“提取和声伴奏”三类任务，不暴露完整模型
  列表和底层推理参数。
- 模型和参数由工作台的版本化预设管理，并支持按需下载。
- PyMSS 文件或模型被删除、损坏、移动后，能够识别并引导修复。

### 2.2 首版非目标

- 不复刻 MSST-WebUI 的完整界面。
- 不提供合奏模式、训练、验证、音频转 MIDI 等高级功能；旧 MSST 模型导入仅用于三类
  固定任务的兼容模型映射。
- 不提供**手工填写**底层推理参数。设置里可以为每个任务改用其他 catalog 模型并选择
  输出轨（见 §8.5），但输出轨一律从该模型自己声明的名字里**选择**，不允许手输；
  chunk / overlap / batch 等推理参数仍固定沿用模型配置自带值。
- 不在工作台主进程内加载 PyTorch 或模型权重。
- 不把全部 PyMSS 模型随工作台安装包发布。
- 不承诺任意歌曲都存在唯一“最佳”模型；默认预设只代表工作台验证过的推荐组合。
- 不复用旧 MSST-WebUI 的 Python 环境、Gradio 服务和运行参数；只读复用其模型权重、
  配置及模型映射文件。

## 3. 页面与导航结构

### 3.1 顶部工作流

第 2 步显示调整为：

| 字段 | 新值 |
|---|---|
| 标题 | 音视频处理 |
| 描述 | 波形对齐与音频分离 |
| 内部模块 ID | 首版优先保留现有 ID，避免无必要的状态迁移 |

### 3.2 第 2 步内部布局

第 2 步页面顶部使用工作台公共的药丸分段 `WorkspaceSwitcher + QStackedWidget` 主导航：

1. **波形对齐**：挂载现有 `align_page` 内容。
2. **音频分离**：挂载新的 PyMSS 页面。

切换内部 Tab 不应清空波形对齐素材、停止无关任务或改变顶部工作流步骤。离开整个第 2
步时，现有波形预览停止行为继续保持；PyMSS 服务是否继续运行由服务生命周期规则决定。

### 3.3 音频分离页

服务就绪后，页面至少包含：

- 服务状态卡：安装位置、PyMSS 版本、运行状态、当前设备、当前模型。
- 音频输入卡：选择或拖入待处理音频。
- 输出设置：输出目录与输出格式；模型和高级推理参数不在主页面暴露。
- 三个任务入口：分离人声、分离伴奏、提取和声伴奏。
- 当前任务卡：阶段、耗时、粗粒度进度、取消/停止入口、错误信息。
- 结果卡：输出文件列表、试听、打开文件、打开目录。
- 设置入口：安装位置、Runtime 来源、服务地址、下载源、设备诊断和修复安装。

### 3.4 页面层级与主工作区

音频分离 Tab 采用状态驱动的一页式工作区，不另开独立主窗口。页面自上而下分为：

1. **状态与操作条**：始终可见，展示“未配置 / 未启动 / 启动中 / 已就绪 / 处理中 /
   需要修复”等归一化状态；右侧只放当前最重要的主操作，如“开始配置”“启动服务”或
   “修复安装”，详细诊断收进二级入口。
2. **素材与输出区**：左右双卡布局，左侧选择或拖入音频，右侧选择输出目录与格式。
   两张卡都保留最近一次有效选择；服务状态变化不得清空内容。
3. **任务选择区**：三张等宽任务卡，分别为“分离人声”“分离伴奏”“提取和声伴奏”。每张
   卡展示用户可理解的输出说明、预计输出文件和当前可用性，不显示底层模型名称及参数。
4. **当前任务区**：只在存在下载、加载或推理任务时展开，展示真实阶段、已知进度、耗时、
   当前文件和取消/停止按钮。
5. **结果区**：只在存在成功结果时展开，以文件行为单位提供试听、打开文件、打开目录和
   复制路径；新任务开始后保留上一批结果，直到用户主动清除或关闭本次会话。

任务卡的可用性必须直接说明原因，例如“需要启动服务”“需要下载 1.48 GB 模型”或
“所选外部模型路径失效”，不能仅置灰。主操作按钮遵循单一主操作原则：同一状态下只突出
一个建议动作，其他操作使用次要按钮或菜单。

### 3.5 首次配置与迁移向导 UI

`UNCONFIGURED` 状态不显示空白的任务工作区，而显示欢迎说明和三个入口：

| 入口 | 适用用户 | 后续步骤 |
|---|---|---|
| 安装 PyMSS 和推荐模型 | 首次使用分离功能 | 选择安装目录；先安装底座，模型仍按任务下载 |
| 仅安装 PyMSS，复用 MSST 模型 | 已经使用 MSST-WebUI | 选择安装目录 → 选择 MSST 根目录 → 扫描、验证并映射模型 |
| 使用已有 PyMSS | 已有兼容环境或服务 | 选择可执行环境或填写服务地址并做能力检测 |

向导在当前 Tab 内以步骤页展示，至少包含顶部步骤指示、返回、取消和当前步骤主按钮；取消
后回到首次配置页，不留下被识别为完整安装的半成品。大型下载开始前必须展示下载体积、
解压占用、最终目录、可用空间和“模型不会随底座全部下载”的说明。

复用 MSST 模型的扫描结果页按任务分成“人声、伴奏、和声”三行，每行只允许选择已验证
或可以完成加载测试的模型。默认自动推荐最佳匹配，用户可以从兼容候选下拉框更换；模型
架构、权重路径、配置路径、摘要和失败原因放在“查看详情”折叠区。三个任务并非都必须
一次配置完成，未映射的任务以后按需下载推荐模型即可。

### 3.6 运行状态的页面表现

| 状态组 | 页面表现 | 保留内容 |
|---|---|---|
| 未配置 | 显示三入口首次配置页 | 无 |
| 安装/扫描中 | 显示步骤、进度、当前文件、下载量和取消 | 已确认的路径与选项 |
| 已安装未启动 | 显示主工作区，顶部突出“启动服务” | 输入、输出和历史结果 |
| 服务就绪 | 三类任务可按依赖状态操作 | 全部工作区状态 |
| 模型缺失 | 对应任务卡显示体积和“下载并继续” | 其他任务仍可使用 |
| 外部模型失效 | 对应任务卡显示“重新定位 / 改用推荐模型” | 不影响其他有效模型 |
| 处理中 | 当前任务区展开；锁定会冲突的配置操作 | 输入、输出和历史结果 |
| 安装损坏/服务失败 | 顶部错误状态卡展示摘要与修复主操作 | 不清空用户工作内容 |

### 3.7 窗口适配与交互约束

- 沿用现有 Fluent 风格、间距、圆角、颜色和控件，不在第 2 步内建立第二套视觉体系。
- 标准宽度下素材与输出卡左右排列、三张任务卡横排；可用宽度不足时依次改为单列，页面
  使用统一纵向滚动，不允许出现相互嵌套的横向滚动条。
- 进度不能只用颜色表达；所有状态同时提供中文文本和图标。键盘焦点顺序按页面从上到下，
  拖放区域必须同时保留可点击的文件选择入口。
- 运行时日志、底层模型信息和硬件诊断使用侧边设置页或对话框承载，不挤占日常工作区。
- 波形对齐和音频分离两个 Tab 各自保存页面状态；应用重启后恢复最后使用的内部 Tab，但
  不自动恢复未完成任务。

## 4. 首次使用与安装目录

### 4.1 首次进入

第一次进入“音频分离”Tab，且没有有效配置时，展示三个入口：

1. **安装 PyMSS 和推荐模型**
2. **仅安装 PyMSS，复用 MSST 模型**
3. **使用已有 PyMSS**

不得在用户尚未确认目录、预计下载量和硬件兼容性前自动开始大型下载。

### 4.2 自动安装的目录选择

点击“自动安装 PyMSS”后，必须先显示安装位置选择页：

| 选项 | 规则 |
|---|---|
| 软件根目录 | 当前默认选项；路径为 `<软件根目录>\pymss\` |
| 其他目录 | 用户通过目录选择器指定；最终仍建议在所选目录下创建独立的 `pymss` 子目录 |

目录确认页必须显示：

- 最终绝对路径；
- 当前可用磁盘空间；
- 托管 Runtime 预计下载量和解压后占用量；
- 模型按需下载说明，不得让用户误以为首次安装会下载全部模型；
- 非 NVIDIA Windows 设备可能只能使用 CPU、速度较慢的提示。

即使暂时默认允许安装到软件根目录，也必须在开始下载前执行以下检查：

- 目标目录或其最近存在的父目录可写；
- 能创建、写入、重命名和删除临时探测文件；
- 剩余空间满足 Runtime 下载、解压临时副本和安全余量；
- 路径不位于文件而非目录上；
- 路径不是工作台 `_internal/` 或更新器临时目录。

软件根目录不可写或空间不足时，不得静默改到其他位置；应说明原因并引导用户选择其他
目录。

### 4.3 推荐目录布局

无论选择软件根目录还是其他目录，托管安装内部统一采用以下结构：

```text
pymss/
├── runtime/                   # 独立 Python、PyMSS、PyTorch、server 依赖
├── models/                    # 按预设下载的模型与配置
├── manifests/
│   ├── runtime-manifest.json  # 工作台托管 Runtime 清单
│   ├── model-state.json       # 已验证的托管模型状态缓存
│   └── external-models.json   # 只保存外部模型引用、映射与验证结果
├── staging/                   # 下载与解压暂存；安装成功后清理
└── logs/                      # 服务启动、模型加载和推理日志
```

工作台自动更新器只管理 `Karaoke Studio.exe`、`Updater.exe`、
`krok_subtitle_renderer.exe` 和 `_internal/`。即使 `pymss/` 位于软件根目录，也不得把它
加入工作台 app/runtime part 的更新 targets，工作台升级必须保留该目录。

### 4.4 使用已有 PyMSS

“使用已有 PyMSS”至少支持以下两种方式：

- 选择已有 PyMSS 的 Python/可执行程序环境，由工作台负责启动服务；
- 填写已经运行的 PyMSS Server 地址，由工作台只连接、不管理其进程。

不能只凭目录名或文件名判断可用，必须验证：

- 能获得 PyMSS 版本；
- 支持 `serve` 命令；
- 启动后 `/health` 响应符合兼容协议；
- 必需的模型管理和音频分离端点存在；
- 版本在工作台声明的兼容范围内。

外部环境不使用工作台托管文件清单，工作台不得擅自修复、覆盖或删除用户的外部环境。
发现异常时只提供“重新检测”“重新选择”和“改用自动安装”。

### 4.5 仅安装 PyMSS 底座并复用现有 MSST 模型

该模式仍使用工作台下载和维护的 PyMSS 托管 Runtime，但不强制下载推荐模型。用户选择一个
已有的 MSST-WebUI 根目录后，工作台以只读方式扫描：

- 官方模型映射：`data_backup/msst_model_map.json`、`data_backup/vr_model_map.json`；
- 第三方模型映射：`config_unofficial/unofficial_msst_model.json`、
  `config_unofficial/unofficial_vr_model.json`；
- 模型权重、对应 YAML 配置及映射中记录的 stem 信息。

工作台不得简单地把 PyMSS `model_dir` 指向 MSST `pretrain`。两者的 catalog 名称和目录
结构可能不同，应通过工作台维护的外部模型清单，以绝对 `model_path`、`config_path` 和
内部别名逐个注册。PyMSS 支持自定义模型路径和用户模型注册；旧 MSST 配置中的部分推理
字段可自动转换，但兼容结论仍必须以真实加载测试为准：

- PyMSS 中文说明：<https://github.com/pymss-project/pymss/blob/main/README_CN.md>
- PyMSS 模型注册：<https://github.com/pymss-project/pymss/blob/main/pymss/model_registry.py>
- MSST 模型与配置映射：<https://github.com/SUC-DriverOld/MSST-WebUI/blob/main/docs/inference.md>

扫描结果分为：

| 分类 | 判定 | 用户操作 |
|---|---|---|
| 已验证兼容 | 权重、配置、架构和输出 stem 匹配，加载冒烟成功 | 可映射到对应任务 |
| 等待验证 | 已识别架构和配置，但尚未在当前设备加载 | 执行加载测试 |
| 配置缺失 | 有权重，但缺少执行所需配置或映射信息 | 精确匹配 catalog 后只下载小型配置，或改选模型 |
| 暂不支持 | PyMSS 当前加载器不支持或输出语义不符合任务 | 不允许用于固定任务 |
| 文件已变化 | 路径、大小、修改时间或摘要与导入记录不符 | 重新扫描并验证 |

外部模型默认原地使用：不复制、不移动、不重命名、不修改 MSST 文件，也不把其写进托管
模型清单。首次导入对候选权重做大小与摘要校验；后续启动优先比较路径、大小和修改时间，
只在变化后重算大文件摘要，避免每次启动读取数 GB 权重。

用户删除或移动旧 MSST 目录后，工作台应将对应任务标记为“外部模型不可用”，提供：

- 重新选择 MSST 根目录并批量重新定位；
- 为单个任务选择另一个已识别的兼容模型；
- 下载工作台推荐模型；
- 可选地将模型复制到 PyMSS 托管模型目录。

“复制到托管目录”不是默认行为，执行前必须显示新增磁盘占用并再次确认。移除外部模型
配置只删除 `external-models.json` 中的引用，不删除用户的 MSST 权重和配置。

## 5. 安装完整性、删除检测与修复

### 5.1 设计原则

用户可能手动删除整个 `pymss/`、删除部分运行库、只删除模型、移动目录，或在下载中途
强制退出。上述情况必须被视为正常可恢复状态，不能表现为无响应或直接抛出底层 Python
错误。

Runtime 与模型分开校验、分开修复：

- Runtime 损坏只修复 Runtime；
- 模型缺失只补下缺失模型；
- 一个预设模型损坏不得触发所有模型重新下载；
- 外部 MSST 模型失效只更新引用状态，不擅自下载替代品或修改原目录；
- 配置路径不存在时优先允许在原路径修复，也允许用户重新定位。

### 5.2 托管 Runtime 清单

自动安装完成后必须写入 `runtime-manifest.json`。至少记录：

```json
{
  "schema": 1,
  "managed": true,
  "runtime_version": "1",
  "pymss_version": "2.0.18",
  "python_version": "3.12",
  "variant": "windows-cpu-or-windows-cu128",
  "complete": true,
  "files": [
    {
      "path": "runtime/python.exe",
      "size": 0,
      "sha256": "..."
    }
  ]
}
```

`complete=true` 只能在全部文件下载、解压和校验成功后原子写入。下载不得直接覆盖一个
仍然有效的 Runtime；应先落到 `staging/`，校验成功后再切换。

发布底座使用 CPython 3.12.10 embeddable、PyMSS 2.0.18、pymss-core 0.1.6，并包含
PyMSS Server 所需的私有依赖与 pip，但**不包含 PyTorch**。客户端按设备单独获取
PyTorch 2.7.1：CPU wheel 为 215,985,616 bytes，CUDA 12.8 wheel 为
3,273,024,349 bytes；文件名、URL、大小和 SHA-256 均固定在客户端契约中。安装成功后，
PyTorch 文件会与底座文件一起写入完整性清单。

### 5.3 校验时机

| 时机 | 校验级别 |
|---|---|
| 工作台启动 | 轻量检查：配置路径、清单、完成标记、入口文件 |
| 首次进入音频分离 Tab | 轻量检查：版本、文件存在性和大小；检查所需模型的存在状态 |
| 正常启动服务前 | 轻量 Runtime 检查 + 可执行探测 + 版本兼容性 |
| 提交任务前 | 当前任务预设依赖的模型、配置和辅助文件检查 |
| 托管服务启动、模型加载或分离失败后 | 后台完整 Runtime 清单与 SHA-256 校验；校验通过时保留原始错误，确认损坏时进入修复状态 |
| 用户手动“重新检测” | 后台完整 Runtime 清单与 SHA-256 校验 |
| 下载/修复/升级完成后 | 大小、SHA-256、版本和启动冒烟检查 |

普通进入页面和正常启动不得反复读取数 GB 文件计算哈希。若用户在程序运行期间删除或
篡改文件，轻量检查可发现的缺失、大小变化立即进入损坏状态；只有实际启动、模型加载或
分离失败时才自动执行完整校验。完整校验确认损坏后提示用户修复，不静默下载数 GB；若
校验通过，则保留真实运行错误，避免把模型、设备或服务错误误报成 Runtime 损坏。

### 5.4 损坏分类

| 分类 | 示例 | 用户侧状态 |
|---|---|---|
| 配置缺失 | 从未设置目录 | 未配置 |
| 安装目录缺失 | 用户删除整个 `pymss/` | 安装缺失，需要修复 |
| Runtime 不完整 | `python.exe`、PyMSS 包或 DLL 缺失 | 安装损坏，需要修复 |
| 版本不兼容 | 用户覆盖为未验证版本 | 版本不兼容 |
| 模型缺失 | 任务所需权重被删除 | 模型未安装，可按需下载 |
| 模型损坏 | 大小或哈希不符 | 模型损坏，需要重新下载 |
| 外部模型缺失 | 已引用的 MSST 权重或配置被删除/移动 | 外部模型不可用，需要重新定位 |
| 外部模型变化 | 大小、修改时间或摘要与导入记录不符 | 需要重新扫描和加载验证 |
| 外部模型不兼容 | PyMSS 无对应加载器或输出 stem 不符合任务 | 改选模型或下载推荐模型 |
| 路径已移动 | 原路径消失但用户知道新位置 | 需要重新定位 |
| 外部服务离线 | 用户提供的服务地址不可达 | 外部服务未连接 |

### 5.5 修复操作

托管安装处于缺失或损坏状态时，页面提供：

- **修复安装**：在当前保存路径补齐缺失/损坏文件；
- **重新选择位置**：选择现有目录或新目录；
- **重新完整安装**：重新下载 Runtime，但默认保留校验通过的模型；
- **移除配置**：只清除工作台中的路径和状态，不直接删除用户文件。

修复开始前若托管服务仍在运行，必须先停止服务。修复完成后执行服务启动冒烟，成功后
回到“已安装，服务未启动”。修复失败保留原有可用文件和日志，不得留下
`complete=true` 的假成功清单。

模型缺失时不强制用户修复整个 Runtime。用户点击任务后，可在确认预计下载量后只下载该
任务需要的模型。

## 6. PyMSS 服务状态机

### 6.1 状态定义

| 状态 | 含义 | 主要操作 |
|---|---|---|
| `UNCONFIGURED` | 没有安装或外部服务配置 | 自动安装、使用已有 PyMSS |
| `LOCATION_REQUIRED` | 已选择自动安装，但尚未确认目录 | 选择根目录或其他目录 |
| `RUNTIME_DOWNLOADING` | 正在下载托管 Runtime | 查看进度、取消 |
| `RUNTIME_VERIFYING` | 正在校验 Runtime 文件完整性 | 等待，不允许启动服务 |
| `INSTALL_MISSING` | 保存的托管目录已不存在 | 修复、重新定位、移除配置 |
| `INSTALL_DAMAGED` | 托管 Runtime 文件不完整或损坏 | 修复、重新完整安装 |
| `VERSION_INCOMPATIBLE` | 托管 PyMSS 版本不在当前工作台兼容范围 | 确认下载量后原地更新 Runtime |
| `EXTERNAL_VERSION_INCOMPATIBLE` | 用户选择的外部 PyMSS 版本不兼容 | 用户自行升级后重新检测，或重新选择环境 |
| `INSTALLED_STOPPED` | Runtime 有效，服务未启动 | 启动服务、检查更新、修复 |
| `SERVICE_STARTING` | 进程已启动，等待健康检查 | 显示日志、取消启动 |
| `SERVICE_READY` | `/health` 正常，可管理模型 | 选择输入、开始任务 |
| `MODEL_REQUIRED` | 当前任务所需模型缺失 | 显示体积并按需下载 |
| `MODEL_DOWNLOADING` | 正在下载/校验模型 | 显示进度、取消 |
| `MODEL_LOADING` | 服务正在加载或切换模型 | 显示模型与设备信息 |
| `EXTERNAL_MODEL_READY` | 当前任务映射的 MSST 模型已验证可用 | 开始任务、重新映射 |
| `EXTERNAL_MODEL_MISSING` | 外部模型权重或配置路径失效 | 重新定位、改选、下载推荐模型 |
| `EXTERNAL_MODEL_CHANGED` | 外部模型文件与导入记录不一致 | 重新扫描并加载验证 |
| `EXTERNAL_MODEL_UNSUPPORTED` | 模型架构或 stem 语义不符合固定任务 | 改选模型、下载推荐模型 |
| `PROCESSING` | 正在执行分离 | 显示阶段、停止 |
| `SERVICE_STOPPING` | 正在停止托管服务 | 等待退出或强制结束 |
| `EXTERNAL_OFFLINE` | 外部服务地址不可达 | 重连、修改地址、改用托管安装 |
| `ERROR` | 不能归入上述状态的失败 | 中文错误、诊断、重试 |

### 6.2 关键状态迁移

1. `UNCONFIGURED → LOCATION_REQUIRED → RUNTIME_DOWNLOADING → RUNTIME_VERIFYING → INSTALLED_STOPPED`
2. `INSTALLED_STOPPED → SERVICE_STARTING → SERVICE_READY`
3. `SERVICE_READY → MODEL_REQUIRED → MODEL_DOWNLOADING → MODEL_LOADING → PROCESSING`
4. `PROCESSING → SERVICE_READY`（成功、可恢复失败或停止后重启成功）
5. 任意托管就绪状态发现目录消失：`→ INSTALL_MISSING`
6. 任意托管就绪状态发现文件/哈希异常：`→ INSTALL_DAMAGED`
7. `INSTALL_MISSING / INSTALL_DAMAGED → RUNTIME_DOWNLOADING`（修复）
8. 外部服务健康检查失败：`SERVICE_READY → EXTERNAL_OFFLINE`
9. 外部 MSST 模型导入成功：`SERVICE_READY → EXTERNAL_MODEL_READY`
10. 外部模型路径或指纹变化：`EXTERNAL_MODEL_READY → EXTERNAL_MODEL_MISSING /
    EXTERNAL_MODEL_CHANGED → EXTERNAL_MODEL_READY`（重新定位和验证成功）
11. 工作台升级后托管版本过旧：`VERSION_INCOMPATIBLE → RUNTIME_DOWNLOADING →
    RUNTIME_VERIFYING → INSTALLED_STOPPED`；保留 `models/`、外部映射、缓存和日志。
12. 外部环境版本过旧：`EXTERNAL_VERSION_INCOMPATIBLE`；工作台不得修改该环境，只能在
    用户自行升级后重新检测，或改选其他环境。

不得只监听进程是否存在。托管服务状态至少同时使用：

- 工作台持有的进程状态；
- `GET /health`；
- `model_loaded`、`model_loading`、当前模型和设备字段；
- 连续失败次数与超时。

## 7. 服务启动与生命周期

### 7.1 托管服务

工作台启动托管服务时：

- 只监听 `127.0.0.1`；
- 选择未被占用的本地端口；
- 使用本次进程随机生成的 API key；
- 设置独立模型目录；
- 继承工作台明确配置的代理策略，不意外继承未知代理；
- 先以空载模式启动，使 HTTP 尽快可用，再通过模型加载 API 按任务切换模型；
- 捕获标准输出和错误输出到模块日志，用户看到的是归一化中文状态。

用户点击“启动服务”后由工作台完成进程启动，不要求用户打开终端。首版默认不在工作台
启动时自动启动 PyMSS；未来可增加“进入音频分离页时自动启动”设置。

### 7.2 外部服务

- 工作台不拥有外部服务进程，不应在退出或取消任务时杀死它。
- 外部服务的地址和鉴权信息由用户提供。
- `/health` 可达不代表协议一定兼容，还必须执行能力和版本探测。
- 外部服务离线后持续低频重试，但不得频繁弹窗。

### 7.3 退出行为

- 工作台启动的托管服务应在工作台正常退出前停止并等待释放模型/GPU。
- 工作台更新前必须停止托管服务，但不得删除 Runtime 和模型。
- 有任务运行时关闭工作台，应提示等待、停止任务或取消关闭。
- 外部服务默认保持运行。

## 8. 三类固定任务与版本化预设

### 8.1 用户功能语义

| 功能 | 输入 | 默认输出 | 内部语义 |
|---|---|---|---|
| 分离人声 | 原曲音频 | 人声 | 使用人声提取预设，保存 `vocals` |
| 分离伴奏 | 原曲音频 | 伴奏 | 使用伴奏提取预设，保存 `other` |
| 提取和声伴奏 | 原曲音频 | 和声伴奏 | 单阶段 Karaoke 链路，保存残余轨 `other` |

人声与伴奏共用同一个双输出模型 `inst_v1e`：装一次，两个任务都可用，只是保存的 stem
不同（`vocals` / `other`）。

和声伴奏任务由 Karaoke 模型直接处理原曲，保存残余轨 `other` —— 该轨是**去掉主唱、
保留伴奏与和声**的音频，不是纯和声。因此任务名、输出文件名和界面文案一律使用“和声
伴奏”，不得表述为“和声”，避免把带和声的伴奏误标为纯和声（这条约束的原意保留：语义
必须与产物一致）。首版不提供“纯和声”和“主唱”两轨输出；若将来需要，应另立两阶段预设
（先取完整人声，再由 Karaoke 模型拆分），而不是复用本预设的产物。

多阶段预设会缓存中间阶段的完整结果 ZIP（当前三个预设均为单阶段，暂未使用该路径）。
缓存键包含输入内容 SHA-256、预设 ID/版本、
PyMSS 版本、模型名、外部模型权重/配置摘要、stem、推理参数和输出格式；复用前还会再次
校验缓存 ZIP 的大小与 SHA-256。输入、预设、模型或缓存内容任一变化都会自动失效。

### 8.2 预设而非 GUI 参数

主页面不展示模型类型、checkpoint、config、chunk、overlap、TTA、batch size 等参数。
工作台维护版本化预设，至少包含：

- 预设 ID 与版本；
- 任务类型；
- 一个或多个处理步骤；
- 每一步的模型引用（catalog name 或受管的外部模型别名）、输入来源、输出 stem；
- 固定推理参数；
- 输出格式和命名规则；
- 模型文件大小、哈希和来源；
- 最低/建议显存；
- 支持的 PyMSS 版本范围；
- 中间文件保留与缓存规则。

预设变化后不能错误复用旧缓存。缓存键至少包含输入文件指纹、预设 ID、预设版本、模型
版本和关键参数签名。

### 8.3 首版冻结预设

首版固定使用 PyMSS 2.0.18 catalog 中已支持的三个模型，不向普通用户显示模型名或底层
参数：

| 任务/阶段 | 冻结模型 | catalog 大小 | 输出语义 |
|---|---|---:|---|
| 分离人声 | `inst_v1e` | 913,102,724 bytes | `vocals → 人声` |
| 分离伴奏 | `inst_v1e` | 913,102,724 bytes | `other → 伴奏` |
| 提取和声伴奏 | `model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956` | 913,096,801 bytes | `other → 和声伴奏` |

人声与伴奏共用 `inst_v1e`，实际只需下载两个模型，合计约 1.70 GB。

参考：

- MSST 音频分离技巧：<https://r1kc63iz15l.feishu.cn/wiki/Zc8nwya67iGdR8k8mVnce09Sn1g>
- PyMSS model catalog：<https://github.com/pymss-project/pymss/blob/main/pymss/resources/model_catalog.json>

模型权重不随工作台或 PyMSS 底座再分发，而是由 PyMSS Server 按任务从用户选择的上游源
下载并校验。PyMSS catalog 没有提供统一的模型授权字段，因此不得在工作台 Release 中
内置这些权重；后续替换默认模型仍须同步做音质回归与来源/授权审计。

### 8.4 模型按需下载

- 安装托管 Runtime 时不默认下载全部模型。
- 用户第一次执行某任务时，列出该任务缺少的模型、总下载量和预计磁盘占用。
- 若该任务已经映射且验证过外部 MSST 模型，不再提示下载工作台推荐权重。
- 用户确认后再下载；下载成功后自动继续原任务。
- 模型下载支持续传、取消、逐源重试、大小与哈希校验。
- 下载中断产生的临时文件不得被识别为可用模型。
- 用户删除模型后，下次进入页面或提交对应任务时进入 `MODEL_REQUIRED`，只补该模型。

### 8.5 用户自选模型与输出轨

主页面仍然只有三个固定任务、不暴露任何底层参数；**设置对话框**的「模型与输出轨」页
允许为每个任务改用 catalog 中的其他受支持模型。

**输出轨必须是选出来的，不能是填出来的。** 同一个概念在不同模型里的名字并不统一，
实测四个模型就有三种命名：

| 模型 | 真实输出轨 |
|---|---|
| `inst_v1e` / `inst_v1e_plus` | `other` / `vocals` |
| `model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956` | `karaoke` / `other` |
| `mel_band_roformer_karaoke_becruily`、`bs_roformer_karaoke_anvuew` | `Vocals` / `Instrumental`（首字母大写） |

因此界面必须给出该模型真实声明的轨名供选择。数据来源有严格要求：

- **权威来源**是模型 YAML 的 `training.instruments`（PyMSS 自身也读这里，其
  `model_card` 的 `instruments_source` 字段写明了这一点）。该文件仅约 1 KB，
  未下载权重也能单独取到：catalog 详情里 `role="config"` 的条目带 `remote_url`；
  已下载的模型直接读 `model_dir` 下的本地副本。
- **不得使用** catalog 的 `target_stem` 字段。它与实际不符——`inst_v1e` 标的是
  `vocals/instrumental`，实际却是 `other` 与 `vocals`。
- 配置解析失败时**不猜**：界面提示「无法读出输出轨，请换一个模型」，并禁用确定按钮。
  工作台没有 PyYAML 依赖（它只存在于 pymss 运行时中），因此 `stems.py` 只做针对
  `training.instruments` 的定向解析，任何不合预期的形状一律判为失败。

覆盖记录写在 `settings.pymss["task_model_overrides"]`，形如
`{"vocal": {"model": ..., "stem": ..., "size_bytes": ...}}`；只有模型与输出轨都齐全
才视为有效，缺一半按未覆盖处理。覆盖会同时影响任务卡的按需下载提示与实际推理步骤；
缓存键本就包含模型名与 stem，换模型后不会错误复用旧产物。

优先级：外部 MSST 映射 > 用户覆盖 > 推荐预设。

### 8.6 手动放置模型的自动导入

用户可能从别处拷贝已有权重。只要按工作台下载时的目录结构摆进
`<安装目录>/models/`，就应当被直接认成「已下载」，不需要再去设置里选一遍。

判定依据是托管 Runtime 自带的 `model_catalog.json`（`runtime/Lib/site-packages/
pymss/resources/`），它给出每个模型的权重相对路径、配置相对路径与确切字节数：

- **完全离线**：服务没启动时也能判断，避免明明有权重、任务卡却显示「需下载」。
- **大小必须逐字节吻合**：拷贝一半或下载中断的文件不得被认成可用模型（§8.4）。
- **权重与配置都要在**：缺配置的模型无法加载，不算完整。
- 名字按调用方的写法返回：catalog 正式名带 `.ckpt` 后缀而预设用不带后缀的别名，
  只回正式名会对不上，功能等于失效。
- 外部服务/外部环境的目录不归工作台管理，不做此扫描。

**安装目录不再嵌套**：向导原先无条件把所选目录拼上 `pymss` 子目录，用户若直接选中
已有的 `…\pymss`，会得到 `…\pymss\pymss`，模型放在外层就永远扫不到。现在所选目录
本身叫 `pymss`、或已含 `manifests/runtime-manifest.json` 时，直接使用该目录。

## 9. 输入、输出与任务体验

### 9.1 输入

P0 接受工作台现有常用音频格式：`wav`、`flac`、`mp3`、`m4a`、`aac`、`ape`、
`alac`。从视频文件提取音轨可作为 P1，不阻塞首版。

首版最大时长冻结为 600 秒。输入统一由工作台现有 FFmpeg/ffprobe 路径探测并转换为
44.1 kHz、双声道、float32 PCM，再交给 PyMSS Server。

提交前检查：

- 文件存在且可读；
- 能探测时长、采样率和声道；
- 时长不超过产品当前限制；
- 输出目录可写且剩余空间足够；
- 服务、模型和设备状态满足任务要求。

### 9.2 输出

- 默认保存无损 `wav`，用户可切换为 `flac`。
- 文件名使用中文可理解后缀，例如 `_人声`、`_伴奏`、`_主唱`、`_和声`。
- 临时文件先写入任务暂存目录，全部成功后再移动到最终输出目录。
- 失败或取消后清理不完整输出，但保留对后续任务确有价值且已校验完成的缓存。
- 结果卡提供试听、打开文件、打开目录和复制路径。

### 9.3 进度与取消

PyMSS 当前官方 `/v1/audio/separations` 是同步请求，没有任务查询、逐块进度或取消端点，
且 HTTP 输入需要由客户端解码、重采样并传输完整 PCM。工作台启动本机 PyMSS 时通过
轻量桥接入口接入底层 `progress_callback`，把已处理音频秒数与总秒数写入本地状态文件，
因此托管服务和工作台启动的本机外部环境可以显示真实百分比；用户提供的远程服务地址
仍只能显示不确定进度。任何模式均不得按耗时臆造百分比，并应显示真实可知的阶段：

1. 准备音频；
2. 下载模型；
3. 加载模型；
4. 分离处理中；
5. 编码/接收输出；
6. 保存结果。

托管服务的 P0 停止策略允许终止并重启工作台拥有的 PyMSS 进程，以确保 GPU 推理真正
停止。外部服务无法保证远端推理停止，界面必须明确提示“已停止等待结果，外部服务可能
仍在处理”。

正式产品适配层应保留改用工作台自有任务 sidecar 或文件路径 IPC 的空间，以解决：

- 远程服务的精确进度；
- 可靠取消；
- 长音频整段 PCM 带来的内存峰值；
- 输出 ZIP 在内存中生成的问题。

## 10. 设置持久化

建议在 `AppSettings` 中增加独立 `audio_processing` 或 `pymss` dict namespace。概念字段
如下，最终字段名由实现阶段测试约束：

```json
{
  "pymss": {
    "mode": "managed",
    "install_dir": "D:/.../pymss",
    "runtime_variant": "windows-cpu | windows-cu128",
    "expected_pymss_version": "2.0.18",
    "external_executable": "",
    "external_server_url": "",
    "model_dir": "D:/.../pymss/models",
    "external_model_registry": "D:/.../pymss/manifests/external-models.json",
    "legacy_msst_root": "D:/.../MSST-WebUI",
    "task_model_bindings": {
      "vocal": "managed-or-external-alias",
      "instrumental": "managed-or-external-alias",
      "harmony": "managed-or-external-alias"
    },
    "download_source": "modelscope | huggingface | hf-mirror",
    "output_dir": "",
    "output_format": "wav",
    "last_internal_tab": "alignment"
  }
}
```

API key 不以明文长期写入普通设置；托管服务每次启动生成临时 key。外部服务需要长期凭据
时，应使用系统安全存储或明确提示其保存方式。

## 11. 错误处理矩阵

| 场景 | 用户侧行为 | 恢复策略 |
|---|---|---|
| 软件根目录不可写 | 安装前说明原因 | 选择其他目录 |
| 磁盘空间不足 | 显示需要与可用空间 | 清理空间或换目录 |
| 下载中断 | 保持未完成状态 | 续传或重试，不写成功清单 |
| 用户删除整个托管目录 | 显示“安装缺失” | 原路径修复或重新定位 |
| 用户删除部分 Runtime | 显示“安装损坏” | 仅补齐缺失/损坏文件 |
| 用户只删除模型 | 对应任务显示模型未安装 | 只下载该模型 |
| 模型哈希不符 | 禁止加载 | 删除损坏副本并重新下载 |
| 旧 MSST 根目录被移动 | 已映射任务显示外部模型不可用 | 选择新根目录并批量重新定位 |
| 外部模型配置缺失 | 不允许直接开始任务 | 精确匹配后只下载配置，或改选/下载推荐模型 |
| 外部模型内容发生变化 | 暂停使用该映射 | 重新扫描、计算摘要并做加载测试 |
| 外部模型加载不兼容 | 显示归一化原因，不暴露堆栈为主提示 | 改选兼容模型或下载推荐模型 |
| 端口被占用 | 不连接未知进程 | 换用空闲端口后启动 |
| 服务进程存在但 `/health` 失败 | 显示启动失败与日志 | 停止进程并重试 |
| 外部服务离线 | 页面保持可操作的离线状态 | 重连、修改地址或改用托管安装 |
| 模型加载爆显存 | 中文说明当前设备与模型 | 降级到兼容预设或 CPU，具体策略待实测 |
| 用户取消托管推理 | 停止任务 | 终止并重启托管服务 |
| 工作台更新 | 先停止托管服务 | 保留根目录下的 `pymss/` 与模型 |

## 12. 安全与授权

- 托管服务默认只绑定 loopback，不允许默认监听 `0.0.0.0`。
- 工作台连接已有服务前必须确认目标确实是兼容 PyMSS，不能只探测端口开放。
- 自动下载 Runtime 和模型必须验证大小与 SHA-256；下载源和最终 URL写入日志。
- PyMSS 本体采用 MIT License，与当前 GPL-3.0 工作台兼容。
- 模型权重可能有独立授权；进入默认预设前必须逐个确认来源、许可、再分发和商业使用
  条件。
- 未取得明确再分发授权的模型不得打入工作台 Release，应从原始来源由用户按需下载。

## 13. 实施状态

### P0-A：技术验证

- 已固定 CPython 3.12.10、PyMSS 2.0.18、pymss-core 0.1.6、PyTorch 2.7.1。
- 已用真实 CPU 托管 Runtime 启动 server，并通过 `/health`、API v1 能力与进程停止冒烟。
- 已按真实 PyMSS 2.0.18 catalog/API 固定模型名、下载、加载与同步 PCM/ZIP 请求契约。
- CUDA 12.8 变体与三类完整大模型的音质/显存/长音频实测属于发布候选验收，不能由单元
  测试代替；不影响 CPU 与外部 PyMSS 路径的代码完成度。

### P0-B：安装与服务管理

- 已完成根目录/其他目录选择、写入/重命名预检、空间预检和危险目录拒绝。
- 已完成底座分片下载、官方 PyTorch wheel 断点续传、SHA-256 校验、暂存、原子切换、
  旧 Runtime 回滚、完整清单与修复。
- 已完成仅装底座、只读扫描 MSST、逐任务映射、模型/配置双指纹、首次真实加载验证、
  移动后重定位和只移除引用。
- 已完成随机本地端口、临时 API key、健康监控、日志、停止、取消重启和退出清理。
- 已覆盖整目录缺失、关键文件缺失/同大小篡改、模型删除、外部权重/配置变化与半成品。

### P0-C：产品界面与三类任务

- 已完成第 2 步改名、双主 Tab、Tab 持久化且保留既有波形对齐实例。
- 已完成音频拖放、输出设置、三固定任务、六阶段进度、托管/外部取消和结果操作。
- 已完成三入口配置向导、修复/重映射路径、状态驱动页面和响应式单列布局。
- 已完成模型级按需下载、多来源回退、删除复检与带完整指纹的中间结果缓存。
- 已完成中文状态、错误、诊断与日志入口；生产页面默认真实后端，Mock 只用于测试。

### P1：产品化加强

- 工作台自有任务 sidecar 或文件路径 IPC。
- 更准确的进度和不重启服务的可靠取消。
- 硬件探测后的标准/兼容预设自动选择。
- 音频质量回归样本和端到端 CI 冒烟。
- 视频文件音轨提取入口。

## 14. P0 验收标准

### 14.1 页面与原功能

- 顶部第 2 步显示“音视频处理”。
- 内部两个主 Tab 可切换，波形对齐现有功能和状态不回归。
- 音频分离页在不同安装/服务状态下显示正确入口，不暴露底层模型参数。
- 首次配置页明确提供“安装推荐模型”“仅安装底座并复用 MSST 模型”“使用已有 PyMSS”
  三个入口，能够取消和返回且不留下假成功状态。
- 服务状态变化、安装损坏和模型重新定位不得清空已选择的输入、输出目录和历史结果。
- 标准宽度使用双列素材区与三任务横排；窄窗口自动变为单列且没有横向滚动条。

### 14.2 安装位置

- 首次自动安装默认选择 `<软件根目录>\pymss\`。
- 用户可选择其他目录，选择结果重启后仍保留。
- 根目录不可写或空间不足时能在下载前阻止并引导换目录。
- 工作台自动更新后，根目录下的 `pymss/`、模型和日志仍然存在。

### 14.3 删除检测与修复

- 删除整个 `pymss/` 后，重新进入页面能显示“安装缺失”，并可在原路径修复。
- 删除一个 Runtime 关键文件后，能显示“安装损坏”，修复后服务可启动。
- 下载中途强制退出后，不会把半成品识别为有效安装。
- 删除一个模型后，Runtime 仍保持有效；只在对应任务上提示并补下该模型。
- 模型哈希被篡改后禁止加载，并可重新下载恢复。
- 只安装 PyMSS 底座后，可以在不复制大模型的情况下扫描旧 MSST 目录，将兼容模型分别
  映射给人声、伴奏和和声任务。
- 移动或删除旧 MSST 目录后，对应任务显示外部模型失效；重新选择新目录后可以恢复映射。
- 移除外部模型引用或 PyMSS 配置不得删除旧 MSST 目录中的任何用户文件。
- 外部 PyMSS 环境异常时不擅自修改用户文件。

### 14.4 服务

- 工作台能启动和停止托管服务，并通过 `/health` 正确识别启动、就绪、模型加载和失败。
- 服务只监听本机，端口冲突时不会连接未知服务。
- 退出工作台和进入自动更新前能停止托管服务并释放 GPU。
- 外部服务离线后页面不崩溃，恢复上线后可重新连接。

### 14.5 分离任务

- 三种入口分别产出语义正确的人声、伴奏、主唱与和声文件。
- “提取和声伴奏”产出去掉主唱、保留和声的伴奏轨，任务名与文件名如实反映该语义，
  不得表述为纯和声。
- 缺失模型能在用户确认体积后按需下载并继续任务。
- 成功结果可试听和打开目录；失败或取消不留下被误认为成功的文件。
- 同一输入、同一预设版本下可安全复用已完成的中间人声。

## 15. 已冻结决策与发布门槛

1. **版本**：CPython 3.12.10、PyMSS 2.0.18、pymss-core 0.1.6、PyTorch 2.7.1。
2. **设备变体**：Windows CPU 与 CUDA 12.8；仅在 NVIDIA 驱动不低于 570.65 时自动选择
   CUDA，否则使用 CPU。两者共用无 torch 底座，按设备下载对应官方 wheel。
3. **预设**：采用 §8.3 的两个模型（人声/伴奏共用 `inst_v1e`）和单阶段和声伴奏链路；
   首版不提供手动模型参数。
4. **输出**：默认 WAV，可选 FLAC；由 PyMSS Server 编码，不在工作台二次有损转码。
5. **输入上限**：600 秒；转换前后分别检查临时目录和输出目录空间。
6. **取消**：托管推理通过终止并重启工作台拥有的服务实现可靠停止；外部服务只停止等待，
   UI 明确提示远端可能继续。
7. **模型发布**：允许用户经 PyMSS 上游源按需下载并校验；模型不进入工作台或底座 Release。
   上游 catalog 无统一许可字段，因此任何随包再分发仍禁止。
8. **Runtime 资产**：独立公开资产仓库 `karaoke-studio/karaoke-studio-runtime` 使用 GitHub
   Release tag `pymss-runtime-v2.0.18-r1`；schema 1，分片与整包均用 SHA-256。客户端还固定
   官方 torch wheel 的 URL、文件名、大小和 SHA-256。资产仓库不是 PyMSS fork，不存放模型、
   torch 或工作台源码；自动发布使用主仓库 secret `PYMSS_RUNTIME_RELEASE_TOKEN`，避免 Runtime
   Release 污染主程序仓库的 `/releases/latest`。
9. **旧 MSST 范围**：读取官方/非官方 MSST 与 VR 映射，支持 PyMSS 2.0.18 已有加载器；
   导入时校验权重和配置摘要，首次任务执行做最长 900 秒真实加载验证。不兼容即停用映射。
10. **复用方式**：P0 只原地只读引用，不复制权重；用户可以随时解除引用或改用推荐模型。

### 15.1 后续版本升级规则

托管安装不是一次性版本。工作台以 `integration.py` 中的集中版本契约同时约束客户端、
底座构建器和 Release 地址：

- PyMSS 或 pymss-core 升级时，修改对应固定版本；
- 嵌入式 Python、底座依赖或文件内容变化时，递增 `PYMSS_RUNTIME_VERSION`；
- PyTorch 变化时，同时更新固定版本、CPU/CUDA wheel 的 URL、文件名、体积与 SHA-256，
  并递增 Runtime 修订；
- 先运行 PyMSS runtime workflow 并发布两种设备清单，再发布引用该契约的工作台版本。
  workflow 从构建出的清单生成 `pymss-runtime-v<PyMSS>-r<runtime>` 标签，不维护第二份
  手写版本号。同一标签的资产不可覆盖：内容一致时跳过，不一致时发布任务失败并要求递增
  `PYMSS_RUNTIME_VERSION`，防止已发布客户端在版本号不变时取得不同底座。

用户升级工作台后，客户端会比较已安装清单中的 PyMSS 版本、Runtime 修订和 Python ABI。
任一不匹配均进入“版本不兼容”状态，显示待下载量并等待用户确认；确认后只原子替换
`runtime/` 和 Runtime 清单，保留模型、MSST 映射、中间缓存与日志，失败则回滚旧 Runtime。
用户指定的外部 PyMSS 环境不由工作台写入或升级，只提示用户自行升级后重新检测，或改选
其他环境。升级不静默开始，也不把新版底座、torch 或模型加入工作台安装包。

安装、修复和升级在替换 Runtime 前会先停止工作台拥有的服务；新目录切换后，在旧目录备份
仍保留时启动一次临时服务并检查健康、API 版本和必需端点。文件复检或服务冒烟任一失败，
都会恢复旧 `runtime/` 与旧清单。用户取消已有安装的修复/升级时，也恢复原安装对应状态，
不会误回到首次配置状态。

## 16. UI 设计与历史实施记录（2026-08-07）

本节保留 UI 框架阶段的设计依据与演进记录，便于追溯视觉和交互决策。其中提到的
“Mock 驱动”“P0-A 待启动”均为历史状态；当前生产入口和验收状态以 §13、§15、§17 为准。

### 16.1 UI 阶段当时的已确认决策（历史）

1. **当时范围**：先用 `MockSeparationBackend` 预览 21 个服务状态并冻结
   `SeparationBackend` 接口；该阶段随后已经由真实后端替换，产品不再走 Mock。
2. **状态区形态**：合并 §3.3 服务状态卡与 §3.4 状态与操作条为**一条顶部状态条**：
   图标 + 归一化中文状态 + 单一主操作；安装位置/版本/设备/当前模型收进「详细信息」
   折叠区。
3. **设置入口**：独立设置对话框，仿全局设置（Pivot 分页：安装与 Runtime / 服务与
   下载 / 诊断与日志 / 修复与重置），不占工作区。
4. **代码位置**：新子包 `krok_helper/audio_processing/`；`gui_qt.py` 只改约 10 行；
   工作流模块 ID 保留 `waveform_align`（§3.1）。

### 16.2 已确认的对 §3 的 UI 细化（实施时以此为准）

1. 任务卡右上角加状态徽标（就绪 / 需下载 x.xx GB / 外部模型 / 不可用原因），卡片
   本体与卡内主按钮同为该卡主操作（如「下载并继续」）；不可用原因必须以中文文本
   直接显示，不仅置灰。
2. 当前任务区用六阶段步骤条：准备音频 → 下载模型 → 加载模型 → 分离处理中 →
   编码/接收输出 → 保存结果；当前阶段高亮，下载阶段显示真实字节进度条，本机分离阶段
   显示底层回调提供的已处理音频时长、总时长和百分比，其余阶段用不确定进度条 + 已用
   时间；严格不伪造百分比（§9.3）。多阶段任务的子阶段作为
   该任务的子阶段展示。
3. 结果区**按任务分组**（组头 = 任务名 + 完成时间 + 移除按钮），组内每文件一行：
   试听 / 打开文件 / 打开目录 / 复制路径；新任务不清空旧结果，直到用户主动清除。
4. 向导取消钩子：取消回到欢迎页时调用 `backend.cleanup_incomplete()`，由后端负责
   清理半成品。
5. 试听用 QMediaPlayer（QtMultimedia 已随字幕渲染模块打进安装包），行内
   试听/停止切换，同一时间只播一个；import 失败时回退为系统方式打开。

### 16.3 文件布局与已完成进度

```
krok_helper/audio_processing/
├── __init__.py                  ✅ 包入口（导出 AudioProcessingPage / AudioSeparationPage）
├── responsive.py                ✅ 宽度自适应网格（双卡→单列、三任务卡→纵排）
├── page.py                      ✅ AudioProcessingPage 容器（Pivot + QStackedWidget，
│                                   持久化 pymss.last_internal_tab）
└── separation/
    ├── __init__.py              ✅
    ├── states.py                ✅ 22 个 ServiceState + StateLevel + STATE_META
    │                              （中文文案/主操作）+ TaskType/TaskSpec/TASK_STAGES
    │                              + TaskDependency + format_size/format_elapsed
    ├── backend.py               ✅ SeparationBackend 接口（信号：snapshotChanged /
    │                              taskProgressChanged / resultReady / logAppended）
    │                              + MockSeparationBackend（simulate_delays=False 时
    │                              全同步，供测试）；FLOW_FULL / FLOW_REUSE_MSST /
    │                              FLOW_EXISTING / FLOW_UPGRADE 四个向导流程常量
    ├── real_backend.py          ✅ 产品真实后端（安装、服务、模型、任务与恢复）
    ├── runtime.py / service.py  ✅ 托管 Runtime 与服务进程生命周期
    ├── client.py / audio_io.py  ✅ PyMSS API 与 FFmpeg/输出安全
    ├── msst.py / cache.py       ✅ 旧 MSST 复用与中间结果完整性缓存
    ├── widgets.py               ✅ 自带 CardWidget（QFrame[cardWidget=true]，沿用
    │                              theme_workbench 全局 QSS，不反向依赖 gui_qt）
    │                              + StatusActionBar / AudioInputCard /
    │                              OutputSettingsCard / TaskCard / CurrentTaskPanel /
    │                              ResultsPanel
    ├── wizard.py                ✅ WelcomeView（三入口）+ WizardView（步骤指示 +
    │                              返回/取消/主按钮）；步骤页：InstallLocationStep /
    │                              ConfirmStep / ProgressStep / MsstMappingStep /
    │                              ConnectStep / CapabilityStep / DoneStep
    ├── settings_dialog.py       ✅ SeparationSettingsDialog（Pivot 四页）
    └── page.py                  ✅ AudioSeparationPage（welcome/wizard/workspace
                                   三视图 + 状态机胶水）
```

接线与测试：`krok_helper/settings.py`（`pymss` namespace）、`krok_helper/gui_qt.py`
（第 2 步改名 + 容器页挂载）、`tests/test_audio_processing_ui.py`（23 项）均已完成。
公共分段切换控件见 `krok_helper/workspace_switcher.py`（§16.7）。

关键实现约定（已验证）：

- 卡片统一用自带 `CardWidget(QFrame)`（`setProperty("cardWidget", True)`），配色由
  `theme_workbench.py` 的全局 QSS `QFrame[cardWidget="true"]` 驱动，与 gui_qt 一致。
- qfluentwidgets 1.11.2 已验证可用 API：`Pivot.addItem(routeKey, text, onClick)`、
  `CardWidget.clicked`/`setClickEnabled`、`IndeterminateProgressBar`、`IconWidget`、
  `FIF.MICROPHONE/MUSIC/PEOPLE/CLOUD_DOWNLOAD/LIBRARY/LINK/CARE_*_SOLID` 等（注意没有
  `FIF.WARNING/HEALTH/REPAIR/OPEN/BACK`，别用）。
- 测试环境：`tests/conftest.py` 进程级钉住 offscreen QApplication，每个测试自动隔离
  设置目录（`KARAOKE_STUDIO_SETTINGS_DIR`）。

### 16.4 实施记录（全部完成）

> 下列 7 步已全部实施完毕，`pytest tests\test_audio_processing_ui.py` 18 项通过，
> offscreen 构造完整 `KrokHelperQtApp` 冒烟通过。保留原文供追溯实现意图。

1. ✅ **`separation/settings_dialog.py`**：`SeparationSettingsDialog(QDialog)`，Pivot +
   QStackedWidget 四页（参考 `gui_qt._open_global_settings_window` 的写法，设置页用
   FluentScrollArea + SettingCardGroup；本地复制 `build_settings_tab_page` 小helper，
   不 import gui_qt）：
   - 安装与 Runtime：安装位置（只读 + 更改…/打开目录）、运行方式、PyMSS 版本 + 重新检测；
   - 服务与下载：下载源 ComboBox（ModelScope / Hugging Face / HF Mirror）、经能力检测
     向导重新配置外部服务、输出目录显示与更改；
   - 诊断与日志：设备、版本、Runtime、模型和错误诊断，打开日志目录、只读日志预览
     （绑 `backend.logAppended`）；
   - 修复与重置：修复安装 / 重新选择位置 / 重新完整安装 / 移除配置（移除前确认，
     可复用 `krok_helper.qfluent_compat.ask_fluent_confirm`），动作调 backend 后关闭。
2. ✅ **`responsive.py`**：`ResponsiveGrid(QWidget)`：持有子件列表 + 最小列宽，
   `resizeEvent` 里按可用宽度计算列数并用 QGridLayout 重排；素材+输出双卡最小列宽约
   360 px，任务卡约 260 px。窄窗口一律退化为单列，禁止横向滚动条。
3. ✅ **`separation/page.py`**：`AudioSeparationPage(QWidget)`：
   - 构造签名仿 `VideoDownloadPage`：`(settings, save_settings, parent=None)`；
     `settings_ns = settings.pymss`（dict，就地读写），backend 可由参数注入（测试用），
     UI 阶段当时默认 `MockSeparationBackend(settings_ns)`；现默认
     `RealSeparationBackend(settings_ns)`，测试仍可显式注入 Mock；
   - 内部 QStackedWidget 三视图：WelcomeView / WizardView / WorkspaceView；
     状态→视图映射：UNCONFIGURED→welcome；LOCATION_REQUIRED / RUNTIME_DOWNLOADING /
     RUNTIME_VERIFYING→wizard；其余→workspace；
   - WorkspaceView：FluentScrollArea（NoFrame + enableTransparentBackground）纵向排布
     StatusActionBar、ResponsiveGrid（素材+输出）、ResponsiveGrid（三任务卡）、
     CurrentTaskPanel（仅 MODEL_DOWNLOADING/MODEL_LOADING/PROCESSING 显示）、
     ResultsPanel（有结果显示）；
   - 状态条主操作按 STATE_META 的 action key 分发到 backend（start_service /
     repair_install / cancel_task / …）；齿轮打开 SeparationSettingsDialog；
   - 素材/输出选择写入 `settings_ns["last_input"]` / `output_dir` / `output_format`
     并调 `save_settings()`；切换内部 Tab 不得清空任何内容（§3.2）。
4. ✅ **`page.py`（容器页）**：`AudioProcessingPage(alignment_page, separation_page,
   settings, save_settings, parent=None)`：Pivot 两项「波形对齐 / 音频分离」+
   QStackedWidget；恢复 `settings.pymss["last_internal_tab"]`（默认 alignment），
   切换即保存；暴露 `current_tab()` 供测试。
5. ✅ **接线（三处小改）**：
   - `krok_helper/settings.py`：`AppSettings` 增加 `pymss: dict = field(default_factory=dict)`
     （放在 `subtitle_render` 字段后）；`load_app_settings` 返回处增加
     `pymss=_safe_dict(payload.get("pymss"))`；`save_app_settings` 走 `asdict` 无需改。
   - `krok_helper/gui_qt.py` 第 1433 行 `WORKFLOW_STEPS`：标题改「音视频处理」，描述
     改「波形对齐与音频分离」，模块 ID 不变。
   - `krok_helper/gui_qt.py` `_build_ui`：`self.align_page` 全文件仅 3 处引用
     （构建约 3090 行 / `module_pages` 登记约 3154 行 / `page_stack.addWidget` 约
     3161 行）。在构建后插入：
     `self.audio_separation_page = AudioSeparationPage(self.settings, self._save_all_settings, parent=self.page_stack)`、
     `self.audio_processing_page = AudioProcessingPage(self.align_page, self.audio_separation_page, self.settings, self._save_all_settings, parent=self.page_stack)`；
     后两处引用改为 `self.audio_processing_page`。离开第 2 步停止波形预览的既有逻辑
     （约 3178 行）按模块 ID 判断，不受影响。
6. ✅ **测试 `tests/test_audio_processing_ui.py`**（offscreen，Mock 用
   `simulate_delays=False`）：
   - 容器页默认进「波形对齐」Tab，切到「音频分离」后 `last_internal_tab` 持久化，
     新实例恢复；
   - MockBackend 全链路：UNCONFIGURED → start_wizard(FLOW_FULL) → confirm_install_location
     → start_install → INSTALLED_STOPPED → start_service → SERVICE_READY →
     request_task(缺模型任务) → MODEL_REQUIRED → start_model_download → MODEL_DOWNLOADING
     → MODEL_LOADING → PROCESSING → SERVICE_READY + resultReady（核对文件后缀
     _人声/_伴奏/_主唱/_和声）；
   - 视图映射：UNCONFIGURED 显示 welcome；INSTALLED_STOPPED 显示 workspace 且主操作为
     「启动服务」；SERVICE_READY 时缺模型任务卡显示「需下载 x.xx GB」与「下载并继续」；
   - 向导取消：回到 welcome 且 backend 回到 UNCONFIGURED；
   - ResultsPanel：add_result/clear_results 分组计数正确；
   - ResponsiveGrid：宽度 1200 px 时任务卡一行三列，压到 500 px 时单列。
7. ✅ **冒烟**：`QT_QPA_PLATFORM=offscreen` 构造完整 `KrokHelperQtApp`，确认第 2 步
   挂载容器页、`pymss` namespace 落盘；再跑全量 `pytest tests\` 确认无回归。

### 16.5 待清理小项

- ✅ `separation/wizard.py`：已删除未使用的 `MockSeparationBackend` import。
- ✅ `separation/states.py`：已删除未使用的 `field` import。

### 16.6 P0-C UI 阶段收尾结论（2026-08-07）

UI 框架阶段到此完成。实施过程中相对 §16.4 原计划的两处修正：

1. **`MockSeparationBackend` 不再假设「有安装目录 ⇒ 模型齐全」**。原实现里
   `_refresh_dependencies()` 会在读到 `install_dir` 时把三个任务全标为已下载，与
   §8.4「安装托管 Runtime 时不默认下载全部模型」直接冲突，也让「需下载 x.xx GB」分支
   永远测不到。现改为把已下载模型集合持久化到 `settings.pymss["downloaded_models"]`：
   全新安装三个任务都显示需下载，下载过的模型在重建后端（模拟重启）后仍为就绪。
   真实后端替换时应保持同一语义——以模型文件的实际校验结果为准，不得由安装状态推断。
2. **横向滚动条策略显式化**。§3.7 要求任何宽度下都不出现横向滚动条，原先靠
   qfluentwidgets `ScrollArea` 的默认值满足；已在 `_build_workspace` 中显式
   `setHorizontalScrollBarPolicy(ScrollBarAlwaysOff)`，避免依赖库默认行为。

`tests/test_audio_processing_ui.py` 共 18 项，其中 `TestAcceptanceCriteria` 逐条覆盖
§14.1 的页面验收标准（第 2 步改名、三入口、状态变化不清空输入/输出/结果、模型按需
下载语义、双列↔单列退化、无横向滚动条）。

该历史阶段之后已经完成 P0-A/P0-B 与真实 `RealSeparationBackend`；当前不再以
`MockSeparationBackend` 作为产品路径。冻结结论见 §15。

### 16.7 视觉打磨（2026-08-07，第二轮）

首版 UI 框架跑通后按实机截图做的一轮视觉统一，结论如下。

**内部 Tab 改用药丸分段控件。** 原 `Pivot`（下划线文字）与工作台整体风格不统一。
字幕视频生成模块的「预览 / 导出」已有一套自绘分段控件（主色药丸 + 图标 + 滑移动画），
现将其从 `subtitle_render/frontend/workspace_switcher.py` 提升为公共组件
`krok_helper/workspace_switcher.py`，原路径改为兼容转发（`main_window.py` 与
`tests/test_subtitle_render_loaders.py` 的导入不变）。第 2 步的「波形对齐 / 音频分离」
直接复用，配 `FIF.ALIGNMENT` / `FIF.MIX_VOLUMES` 两个图标。

公共化时注意：`theme_workbench` 在 module-import 期实例化 SUG theme 单例，要求
`QApplication` 已存在，因此公共模块自带惰性 `palette()` + 浅色兜底，不在 import 期取色。

**向导与欢迎页重排。** 原实现把内容直接铺满窗口宽度，1800px 宽屏下每行过长、重心失衡，
且信息全是多行纯文本堆叠。现统一为：

1. 限宽 780px 居中列（`WIZARD_COLUMN_MAX_WIDTH`），欢迎页同款；
2. 步骤指示从「第 N 步 / 共 M 步」纯文本换成 `WizardStepper`（编号圆点 + 连接线 +
   步骤名，已完成显示对勾）；每个 `WizardStep` 新增 `step_label` / `hint` 两个类属性；
3. 内容装进卡片，卡片贴合内容高度而非拉满整页；
4. 裸 `RadioButton` 换成 `OptionCard`（整卡可点、选中态主色描边），
   多行文本换成 `InfoGrid`（标签→值），说明段换成 `HintBox`（浅底 + 图标）。

实现期踩到的三个坑，均已修复并留了注释：

- 裸 `QWidget` 不绘制样式表里的 `background`，`HintBox` 必须
  `setAttribute(WA_StyledBackground, True)`；
- `InfoGrid` 行数变化时若只隐藏旧行，`QGridLayout` 会残留行高导致错位，改为整体重建；
- 步骤在自己的构造函数里就会回调 `WizardView.refresh_footer()`，此时 `_steps` 尚未装配，
  需要空列表保护。

**顺带修掉一个真实缺陷**：`MockSeparationBackend._delay()` 用了
`QTimer.singleShot(msec, context, slot)` —— 这个重载在 PyQt6 里不存在。因为全部测试都跑
`simulate_delays=False` 的同步分支，一直没暴露；而实机默认走延时分支，点「启动服务」
必然抛 `TypeError`。已改为挂在后端下的一次性 `QTimer`，并补了
`test_delayed_transitions_do_not_raise` 守住这条路径。

> 教训：Mock 后端的「测试用同步分支」和「实机用延时分支」是两条真实代码路径，
> 只测同步分支等于没测实机行为。真实后端替换时同样要保证两条路径都有覆盖。

### 16.8 文案精简（2026-08-07，第三轮）

排版定下来后做了一轮文案审计，发现同一句话在一条流程里反复出现。

**确立的原则：一句说明只在用户需要它做「当下这个决定」的地方出现一次。**

审计出三组重复 + 一类结构性冗余：

| 重复内容 | 原出现次数 | 保留位置 |
|---|---:|---|
| 模型按需下载 | 5 | 只留安装位置页说明块（那里有磁盘数字，需要解释为什么只占 2.5 GB） |
| 不复制 / 不移动权重 | 3 | 只留 MSST 映射步骤的 `hint`（这步的核心承诺） |
| 不修改外部环境 | 3 | 只留连接步骤的说明块 |

结构性冗余：多个步骤的 `hint` 在复述 `title`（「选择安装位置」+「PyMSS Runtime 会安装到
这里…」），或复述页面上已逐条显示的内容（「能力检测」+「确认版本、必需端点与健康检查…」，
而下方本来就逐项列出检查结果）。这类 `hint` 一律删空 —— `WizardStep.hint` 保持可选，
只在标题确实说不清这步要做什么决定时才写。

其余改动是把长句压成短句：入口卡只回答「这条路适合谁」，不解释机制；任务卡描述去掉
与标题重复的部分（「从原曲中提取人声，得到去伴奏的人声轨」→「得到去掉伴奏的人声轨」）。

> 后续新增文案时先自查：这句话在本流程里是不是已经说过？用户在这一屏要做的决定，
> 少了这句会不会做错？两个答案都是否，就不要写。

## 17. 当前实现、体积边界与验收证据

### 17.1 产品实现路径

- `AudioProcessingPage` 复用原波形对齐页面实例，并增加“音频分离”主 Tab；最后 Tab 落盘。
- `AudioSeparationPage` 默认构造 `RealSeparationBackend`；Mock 只允许由测试显式注入。
- 托管安装使用 `ManagedRuntimeInstaller`：目录预检 → 分片底座 → 官方 torch wheel 续传 →
  完整校验 → staging 原子切换 → 失败恢复旧 runtime → 原子写 `complete=true` 清单。
- 服务以 `127.0.0.1` 随机端口和每次启动随机 API key 运行，托管 Python 设置
  `PYTHONNOUSERSITE=1`；外部环境不被工作台修改，外部服务凭据只保存在本次进程内存。
- 首次进入音频分离页和正常启动服务只异步执行轻量清单检查；托管服务启动、模型加载或
  分离实际失败后才执行完整清单哈希。安装、修复、升级完成及用户手动“重新检测”也执行
  完整校验。只有确认 Runtime 损坏时才提示修复，不静默重新下载。Runtime、推荐模型和
  外部 MSST 权重分别恢复，不相互触发整包重装。
- 大型底座下载确认页会先检测当前 NVIDIA 驱动兼容性，固定本次 CPU 或 CUDA 12.8
  方案并显示对应预计总下载量；检测失败时不能开始下载。安装、模型下载日志记录清单、
  分片、官方 torch wheel、模型源和 catalog 文件地址。
- 输出先写任务临时目录，再全部复制为 `.part`，最后统一发布；任何失败会撤销本批已发布
  文件。托管任务取消会重启服务；外部任务取消只停止客户端等待，未返回的外部 HTTP
  请求使用可脱离守护线程，不阻止工作台退出。

### 17.2 软件本体体积边界

PyMSS 集成源码约 0.3 MiB，只导入标准库及工作台已有的 PyQt6、qfluentwidgets、requests
和 FFmpeg 封装。主程序 Windows/macOS PyInstaller 命令显式排除 `torch`、`pymss`、
`pymss_core`、`pip`，打包后扫描发现任一对应目录就令构建失败；runtime part profile 已
升级，禁止复用可能误含这些组件的旧 Runtime 包。

下列内容均不进入 Karaoke Studio 本体 Release：

- PyMSS 托管底座及其第二套嵌入式 Python；
- 私有 pip、PyTorch、CUDA DLL；
- 推荐模型、外部 MSST 模型、缓存和服务日志。

它们只会在用户确认目录和下载量后进入所选 `pymss/` 目录。底座构建器也有反向门禁，
若底座意外包含 `torch`、`functorch` 或 `torchgen` 会拒绝产出。

### 17.3 发布前验收状态

| 项目 | 当前证据 | 状态 |
|---|---|---|
| Python 编译 | `compileall` 覆盖音频处理包与 Runtime 构建器 | 通过 |
| 状态机/UI/Runtime/MSST/API/缓存 | `tests/test_audio_processing_*.py` 与 `tests/test_build_pymss_runtime.py` | 87 项定向测试通过 |
| 主窗口嵌入 | 离屏构造主窗口，切换两个内部 Tab，确认生产入口为真实后端并安全关闭 | 通过 |
| 真实 CPU 底座 | 本地构建无 torch 底座，使用私有 pip 安装官方 CPU torch，并完整校验 | 通过 |
| 真实 PyMSS Server | PyMSS 2.0.18 CLI 启动与 `/health` 冒烟 | 通过 |
| 固定模型 catalog | 真实服务确认三个模型存在、受支持、体积匹配且各文件有 HTTPS 下载地址 | 通过（未下载权重） |
| 本地 Release 资产 | CPU/CUDA 两套清单与分片完成 SHA-256、版本、同源底座和无 torch 校验 | 通过并已上传 |
| CUDA 与三套大模型音质/显存 | 需要发布候选机下载约 4.1 GB 模型并执行样本矩阵 | 发布候选验收 |
| 远端托管 Runtime 资产 | 独立仓库 `karaoke-studio/karaoke-studio-runtime` 的 `pymss-runtime-v2.0.18-r1` | 已发布；CPU/CUDA 清单与分卷公网回读通过 |

本轮按用户要求只执行 PyMSS 定向测试、一个共享字幕控件回归、主窗口离屏冒烟和真实 CPU
服务冒烟，没有运行仓库全量测试。

远端资产发布属于独立资产仓库的 Release 操作，不进入主程序 Release 列表。首次资产已经发布，
正式用户的 CPU/CUDA 清单和底座分卷 URL 均可匿名访问；后续版本必须先发布不可变 Runtime
资产，再发布引用该版本契约的工作台更新。

### 16.9 卡片高度稳定（2026-08-07）

用户反馈：素材/输出两张卡与三张任务卡会随状态变化忽高忽低。查出三个独立成因：

1. **同一行卡片不等高** —— `ResponsiveGrid` 用 `AlignTop` 加子件。带对齐标志的子件只按
   自身 sizeHint 取高，不填满单元格；去掉对齐标志后同行子件自然等高。
2. **任务卡随状态跳动** —— `TaskCard._reason` 在 `setVisible(True/False)` 之间切换，
   占位忽有忽无。改为常驻（仅切换文本），并预留两行高度
   （`fontMetrics().lineSpacing() * 2`），容得下当前全部归一化原因文案。
3. **卡片被拉伸后内容散开** —— `OutputSettingsCard` 补尾部 `addStretch(1)` 让内容保持
   顶对齐；`_DropZoneFrame` 设为垂直 `Expanding`，由拖放区吸收多余高度，而不是把标题
   和提示撑散。

验证方式：直接对 `TaskCard.set_dependency` 施加 7 种依赖状态（服务未启动 / 缺模型 /
就绪 / 外部模型可用 / 外部模型失效 / 任务进行中 / 需先处理错误），逐一比对徽标与原因
文案确实不同，再测高度 —— 三卡恒为同一高度且跨状态不变。

> 踩坑记录：第一版验证是通过后端 `_set_state` + `_downloaded_models` 间接驱动的，但页面
> 现在默认用 `RealSeparationBackend`，它不读 `_downloaded_models`（那是 Mock 专有字段），
> 所以七种「状态」其实一次都没切换，五行测量数字相同是**假通过**。测 UI 状态时应当直接
> 驱动组件入口（`set_dependency`），而不是依赖某个后端实现的内部字段；测试里也要断言
> 「各场景确实不同」，否则稳定性断言可能在无变化的情况下空转通过。

对应回归测试：`tests/test_audio_processing_ui.py::TestCardHeightStability`（三项）。
