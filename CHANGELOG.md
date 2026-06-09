# Changelog

本项目所有重大变更都记录在此。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

打轴模块（StrangeUtaGame）合并前的历史见
[`krok_helper/lyrics_timing/CHANGELOG.md`](./krok_helper/lyrics_timing/CHANGELOG.md)。

---

## [Unreleased]

---

## [3.0.6] — 2026-06-09

新增顶部 6 步工作流栏的紧凑模式开关：打轴时嫌工作流栏占太高的用户，点一下就能把它折成一条窄行，6 步全部仍可点击切换，状态会持久化到 `settings.json`。

### Added

- 工作流栏右上角齿轮图标左侧新增「折叠 / 展开」按钮：折叠后整条 bar 从 80px 高缩到 44px，每一步只剩编号 + 标题（隐藏副标题与下划线），6 步全部仍可点击切换；按钮图标会在 ↑ / ↓ 之间切换并带中文 tooltip。
- 新增持久化设置 `workflow_compact`（写入 `%APPDATA%\Karaoke Studio\settings.json`），默认 `false` 保持老用户视觉不变；用户点过折叠后下次启动自动保持紧凑态。

---

## [3.0.5] — 2026-06-09

修复 v3.0.x 的一次真实数据丢失事故——升级时打轴模块的设置 / 词典 / 演唱者 / 网络词典缓存可能被全部清空。

### Fixed

- 修复 `%APPDATA%\Karaoke Studio\settings.json` 在保存时不是原子写、进程被打断会把文件 truncate 后留下空 / 半截内容的问题。改为先写 `settings.json.tmp` 再用 `os.replace` 原子替换；从此进程被杀只会留下未完成的 `.tmp` 文件，本体一定是上一次的完整版本。
- 修复启动时若 `settings.json` 损坏会被默默回落到默认值、并导致打轴模块 4 个命名空间（主配置 / 词典 / 演唱者 / 网络词典）一并消失的问题。现在解析失败会把坏文件备份到 `settings.json.corrupt-<时间戳>`，并在主窗口打开后弹红框告知备份位置与恢复入口（全局设置 → 工具 → 打轴模块数据导入）。

### 给老用户

- 上一版升级时若已经丢失了打轴模块设置 / 词典 / 演唱者，请检查 `%APPDATA%\Karaoke Studio\` 下是否仍有 `settings.json.tmp` 或系统快照（OneDrive / 历史版本）可恢复；若没有，请重新从原 StrangeUtaGame 目录导入。本次修复主要确保不再复发。

---

## [3.0.4.1] — 2026-06-09

仅 submodule 同步，主程序代码无改动。更新 StrangeUtaGame 子模块到 `SUGv1.1.6`。

### Added

- 注音分析所选字符支持跨行选中：选区内每一行的字符会被分别打包提交给分析引擎，按行排队顺序执行，避免词典 / 索引异常。
- 跨行选中时，编辑 / F3 拆分 / 批量改读音操作会被拦截并通过 InfoBar 提示「请仅在单行内操作」；跨行删除 / 复制 / 演唱者批量处理可正常使用并合并为一次 undo。
- 跨行拖动选词：数据模型、渲染高亮、右键菜单、选区边缘自动滚动全部就位。
- 双击音量 / 速度滑块可快速回到默认值。
- 导出时若检测到未打轴的行，会同步在 InfoBar 显示哪些行未完成。
- 按行设置演唱者支持多选。

### Fixed

- 修复无音频载入时 TSM 引擎仍派发任务的问题。
- 修复 nicokara 标签设置长内容显示不完整。
- 修复添加句号时右移的空格未按半个汉字字宽生成。
- 修复英文单词后紧跟的 `,` 影响英文单词后空格句尾取消的逻辑。
- 优化分色标签助手 UI 细节。

---

## [3.0.4] — 2026-06-09

修复 v3.0.3「打轴模块数据导入」的关键回归（取代 v3.0.3，建议老用户在升级后**重新点一次**导入）。

### Fixed

- 修复从旧版 StrangeUtaGame standalone 导入设置时，打轴模块的界面字号 / 注音字号与间距 / 节奏点标记大小与间距 / 行间距系数等设置被静默丢弃、退回到工作台默认值的问题。原因是 v3.0.3 的导入函数按 SUG 内部一份**不完整**的字段清单递归过滤未知项，把这些「未在清单里声明、但 SUG 运行时仍正常读取」的合法设置也当成未知项剔除了。现在导入逻辑只过滤未知的顶层 namespace（`audio` / `ui` / `timing` 等），namespace 内部整体保留交给 SUG 自己处理。
- 已经在 v3.0.3 里执行过导入的用户，升级到 v3.0.4 后请在「全局设置 → 工具 → 打轴模块数据导入」**再点一次**同样的目录，丢失的字号 / 间距 / 系数等会被补回；词典 / 演唱者按原策略合并去重，不会因为重导出现重复条目。

---

## [3.0.3] — 2026-06-09

完善打轴模块的嵌入体验与旧版用户迁移路径，主程序代码改动；submodule 未动。

### Added

- 新增「打轴模块数据导入」入口（全局设置 → 工具）：可从旧版 StrangeUtaGame standalone 的安装目录一键导入设置、词典、演唱者和网络词典缓存；主配置中未知的设置项会被忽略、缺失项使用默认值；词典与演唱者按名称合并去重，工作台已有的同名条目优先保留。
- 歌词检索输入框现在支持把歌词文件拖入：自动从 LRC 的 `[ti:]` / `[ar:]` 标签提取歌名与歌手，否则回落到文件名，免去手动输入。

### Fixed

- 修复打轴模块在工作台模式下正常退出时，autosave / 临时文件未被清理的问题——之前会导致下次启动工作台时打轴模块误触发「上次异常退出，是否恢复」提示。

---

## [3.0.2.1] — 2026-06-08

仅 submodule 同步，主程序代码无改动。更新 StrangeUtaGame 子模块从 `SUGv1.1.1+7` 到 `SUGv1.1.5`，跨越 v1.1.3 / v1.1.4 / v1.1.5 三个上游版本。

### Added

- 分色标签设置助手优化；创建分色时可以从项目里已用过的颜色直接选择。
- LLM 注音协议改用项目 inline annotated 格式；prompt 新增「单字独立读音 vs 熟字訓」拆分判定规则。
- LLM 注音日志重构：每次调用拆分为 request / response / extracted 三个文件；仅启动时清理，退出后保留便于排查。

### Fixed

- 修复 ASS 导出 roundtrip。
- 修复 nicokara 导出：多入口下 `nicokara_tags` 未加载或被污染；导出时过滤空行与空格。
- 修复节奏点 marker：矢量填充修复字形残缺，间距可配置，高亮对齐。
- 修复 LLM 注音用户词典被「单字块」穿刺：连续单字块合并为同一 morpheme，并改用多字块 + 辞典 ‐/＝ 锚点。
- 修复 phase5 复合词补全：新增 `compound_group_id` 保护，覆盖全流程注音路径。
- 修复默认演唱者判定：不再仅凭「未命名」字段判断。
- 修复导唱符过滤：排除无法被 n3 识别的变体选择符。

---

## [3.0.2] — 2026-06-06

### Fixed

- 修复从 Firefox 导入 YouTube Cookie 后生成无效 Netscape cookie 文件的问题。
- 修复部分 YouTube 播放列表链接解析失败的问题。
- 修复歌词打轴模块嵌入工作台后，横向拖动窗口时可能误拖动内部内容宽度的问题。

### Changed

- 更新弹窗现在会展示 GitHub Release 发布说明，方便用户在更新前查看本次变更。
- Windows 打包产物现在使用工作台 logo 作为 `Karaoke Studio.exe` 图标。
- 更新 StrangeUtaGame 子模块，包含注音/用户词典稳定性修复与嵌入工作台相关调整。

---

## [3.0.1] — 2026-06-06

### Changed

- 仓库迁移至 `github.com/karaoke-studio/karaoke-studio`，由 Myosotis11037 与 Xuan-cc 共同维护
- 合并 `Myosotis11037/karaoke-helper` 与 `Xuan-cc/StrangeUtaGame` 为单一仓库；StrangeUtaGame 源码重定位至 `krok_helper/lyrics_timing/`，git history 完整保留
- 仓库整体采用 GPL-3.0 协议（合并前 krok-helper 无 LICENSE，StrangeUtaGame 为 GPL-3.0）
- 将 `krok_helper/lyrics_timing` 拆为 `karaoke-studio/StrangeUtaGame` submodule，保留 SUG 社交资产与独立历史
- 增加工作台自动更新的 Windows release zip / SHA-256 生成流程

### Added

- 增加本地 updater 集成测试，覆盖全量 zip 下载、解压和安装目录替换

### 下一步计划

下一个发布版本 `v3.1.0` 将包含**打轴模块与主程序工作流的 UI 集成**（当前模块代码已合入，但主窗口第 4 步仍为占位）。

---

## [3.0.0] — 2026-06（合并前）

`Myosotis11037/karaoke-helper` 作为独立项目发布的最后一个版本。

详细变更见该版本 tag `v3.0.0` 的 commit 历史。

StrangeUtaGame 同期版本为 `SUGv1.1.1`，相关历史见
[`krok_helper/lyrics_timing/CHANGELOG.md`](./krok_helper/lyrics_timing/CHANGELOG.md)。
