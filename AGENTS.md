# AGENTS.md

给 Claude Code / Codex / 其他 coding agent 看的项目入口。读完应该能在不打扰用户的前提下定位代码、跑测试、走完发版流程。

---

## 0. 用户持久化偏好（最高优先级）

以下规则适用于本仓库的所有新会话，除非用户在当前请求中主动、明确地要求例外：

1. **禁止创建新分支**：始终在当前分支（通常为 `main`）工作、提交和推送。不得因 agent、skill、插件或通用 Git 工作流的默认建议自行创建功能分支；只有用户明确要求“创建分支”时才允许创建。
2. **禁止新建文档**：不得自行新增计划、说明、报告、设计稿、Markdown 或其他文档文件。只有用户明确要求“新建文档”时才允许创建；现有文档仅在任务确有必要时原地更新。
3. **不要怀疑开发者的操作**（2026-10 用户明令）：用户报的缺陷一律当作可复现事实对待。自动化复现失败时，先扩大复现的时长与覆盖面——很多缺陷是**渐进性楔死**（正常运行几秒后才卡死，例如缓存填满后死键无法逐出），短时段探针（<10s）天然漏掉；再检查用户实际运行路径与探针的差异（真实应用负载、已运行实例不会热加载新代码等）。绝不在回复中出现「是否操作有误/是否用了旧版本」式的质疑表述。

---

## 1. 这是什么

**Lin-K Lyrics（凛K）**——面向卡拉 OK / B 站投稿制作的 Windows 桌面工具，Python + PyQt6 + PyInstaller，仓库主体是 `karaoke-studio/karaoke-studio`，由 [Myosotis11037](https://github.com/Myosotis11037)（原 karaoke-helper 作者）与 [Xuan-cc](https://github.com/Xuan-cc)（原 StrangeUtaGame 作者）于 2026-06 合并而成，2026-08 由 Karaoke Studio 更名为 Lin-K Lyrics（仓库名、发布资产名与环境变量前缀均保持不变）。详情见 [AUTHORS.md](AUTHORS.md) / [NOTICE](NOTICE)。

UI 的核心是 [`WORKFLOW_STEPS`](krok_helper/workflow_bar.py) 定义的 **6 步工作流**：

| 步骤 | 模块 | 实现位置 |
|---|---|---|
| 1 | 视频下载 | [`krok_helper/video_download/`](krok_helper/video_download/)（yt-dlp 封装） |
| 2 | 音视频处理（波形对齐 / 音频分离 / 音频合成） | [`krok_helper/audio_processing/`](krok_helper/audio_processing/)（容器页 + `separation/` 音频分离 + `merge/` 音频合成）、[`krok_helper/alignment/`](krok_helper/alignment/)（波形对齐页）+ [`audio_alignment.py`](krok_helper/audio_alignment.py)（对齐算法） |
| 3 | 歌词检索 | [`krok_helper/lyrics_search/`](krok_helper/lyrics_search/)（页面）+ [`lyrics.py`](krok_helper/lyrics.py)（检索与转换逻辑） |
| 4 | 歌词打轴 | [`krok_helper/lyrics_timing/`](krok_helper/lyrics_timing/) — **SUG submodule**（见 §4） |
| 5 | 字幕视频生成 | [`krok_helper/subtitle_render/`](krok_helper/subtitle_render/)（见 §9） |
| 6 | Hi-Res 混流 | [`krok_helper/hires/`](krok_helper/hires/)（页面）+ [`pipeline.py`](krok_helper/pipeline.py)（混流流水线） |

---

## 2. 仓库结构

六个步骤各自的子包见 §1 表格，这里只列其余主干；未列出的顶层模块多为启动、日志等支撑代码。

```
karaoke-studio/
├── app.py                          # 入口 → krok_helper.cli.main
├── krok_helper/
│   ├── __init__.py                 # 把 lyrics_timing/src 加到 sys.path，并安装 SUG 目录探针
│   ├── cli.py / config.py          # argparse + 启动 GUI / APP_VERSION 等常量
│   ├── gui_qt.py                   # 主窗口外壳 KrokHelperQtApp；各步页面在 §1 表格列出的子包里
│   ├── workflow.py / workflow_bar.py / workflow_host.py    # 工作流模块 ID / 顶部步条 / 宿主契约
│   ├── ui_kit.py / theme_workbench.py / qfluent_compat.py  # 共用控件 / 主题门面 / qfluentwidgets 兼容修补
│   ├── lyrics_timing/              # ⚠️ Git submodule（StrangeUtaGame）— 不要直接改源码
│   ├── global_settings/            # 全局设置对话框（设置序列化在 settings.py）
│   ├── updater/                    # 自动更新客户端（worker / installer / sources / settings，macOS 走 Sparkle）
│   ├── updater_app/                # 独立 Updater.exe（PyInstaller 单独打包）
│   └── assets/                     # 图标、logo、平台 SVG
├── native/subtitle_renderer/       # 步骤 5 的 C++ Direct2D 渲染 sidecar（CMake + Ninja）
├── scripts/                        # build_windows.bat / build_macos.command / build_parts.py / release.py，另有基准与探针脚本
├── tests/                          # pytest，对应主程序各模块
├── docs/                           # release-process.md、auto_update.md、README_DEV.md（CLI / 路径 / 环境变量 / 打包参考）及各专题设计文档
├── .github/workflows/release.yml   # tag v* 触发 → 打包 → 发布 GitHub Release
├── CHANGELOG.md / README.md / AUTHORS.md / LICENSE / NOTICE
└── AGENTS.md                       # 本文件
```

---

## 3. 跑起来

```powershell
# 带 PyQt6 的 Python 解释器
C:\Python314\python.exe app.py
```

CLI 选项见 [`krok_helper/cli.py`](krok_helper/cli.py)（`--video` / `--on-audio` / `--off-audio` / `--output-dir` / `--ffmpeg-dir` 等）。无参数则直接进 GUI。参数表、数据路径与环境变量见 [`docs/README_DEV.md`](docs/README_DEV.md)。

测试优先并行跑（`pytest-xdist`，已列入 `requirements-dev.txt`）：

```powershell
# 日常快循环：跳过最慢的 GPU 对照文件，约 9 分钟
C:\Python314\python.exe -m pytest tests -q -n 8 --ignore=tests/test_subtitle_render_gpu_backend.py
# 只验证 GPU/Painter 对照：约 4.5 分钟（串行约 28 分钟）
C:\Python314\python.exe -m pytest tests\test_subtitle_render_gpu_backend.py -q -n 6
# 全量：约 12 分钟（串行 40~60 分钟）
C:\Python314\python.exe -m pytest tests -q -n 8
```

- **开发时不要每次都跑全量**：整套约 6000 个测试，`tests\test_subtitle_render_gpu_backend.py` 最慢（每个用例都新起 sidecar + configure），`tests\test_subtitle_render_property_panel.py` 次之。日常开发只跑与改动相关的测试文件/用例（如 `pytest tests\test_subtitle_render_property_panel.py` 或单个 `::test_name`），全量回归留给发版前或跨模块大改动合并前。
- **并行的代价**：少数 GPU 用例（`test_gpu_inline_volume_uses_registered_geometry_transition` 的个别参数、`test_gpu_band_presets_advance_frames`）在并行负载下会偶发失败，排查这类用例时串行单跑确认。
- GUI 测试结束后的顶层窗口由根 conftest 的 `_reap_stray_toplevel_widgets` 按测试回收，不要再引入跨测试存活的无父窗口。

Qt 嵌入冒烟（无显示器环境；进程退出码不作回归信号，见 §8.12）：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Python314\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.lyrics_timing_page).__name__)"
```

---

## 4. Submodule（StrangeUtaGame）边界

`krok_helper/lyrics_timing/` 是独立仓库 [`karaoke-studio/StrangeUtaGame`](https://github.com/karaoke-studio/StrangeUtaGame) 的 submodule。

**规则**：

- **不要直接改 `krok_helper/lyrics_timing/src/strange_uta_game/` 里的代码**。要改先去 SUG 仓库提 PR，merge 后再 bump submodule。
- 嵌入主程序的入口是 [`krok_helper/gui_qt.py`](krok_helper/gui_qt.py) 里的 `self.lyrics_timing_page`（由 `LyricsTimingMainWindow.for_embedding(...)` 构造）。SUG 自己暴露 `MainWindow` class，宿主把它当一个 Qt widget 挂上去。
- 嵌入契约见 SUG 仓库的 `docs/EMBEDDING.md`（在 submodule 内）。
- [`krok_helper/__init__.py`](krok_helper/__init__.py) 会自动把 `lyrics_timing/src/` 加进 `sys.path`，所以 `import strange_uta_game` 在主程序里直接可用。

**新会话第一件事**：

```powershell
git submodule status
```

如果显示 `-<sha>`（前面有 `-`），说明 submodule 没初始化，跑 `git submodule update --init --recursive` 否则什么都跑不起来。

---

## 5. 自动更新机制

- [`krok_helper/updater/worker.py`](krok_helper/updater/worker.py)：在主程序里跑，查询 GitHub Releases API（全 403 时用 github.com 网页 302 跳转兜底），对比 `APP_VERSION`，跨版本聚合 changelog。
- [`krok_helper/updater_app/`](krok_helper/updater_app/)：独立 GUI `Updater.exe`（复用 SUG `updater_app` 的 PyQt6 界面与增量更新逻辑），主程序退出后由它显示进度、替换文件并重启，不弹控制台。由 [`build_updater.py`](krok_helper/updater_app/build_updater.py) 单独打包。
- **增量更新**：[`scripts/build_parts.py`](scripts/build_parts.py) 产出 `KaraokeStudio-windows.json`（manifest）+ `-app.zip` + `-runtime.zip`；Updater 按 manifest diff 只下变化的 part，失败自动回退全量 zip。依赖未变时 CI 复用上一版 runtime zip 原文件（`--require-runtime-reuse` 安全闸）。完整机制、配置与失败矩阵见 [`docs/auto_update.md`](docs/auto_update.md)；设计取舍见 [`docs/工作台更新器完善计划.md`](docs/工作台更新器完善计划.md)。
- 资产命名是硬编码的：`KaraokeStudio-windows.zip` / `KaraokeStudio-macos.zip`（见 `worker.current_asset_name()`）。**manifest 名 `KaraokeStudio-windows.json` 由存量客户端从 zip 名派生，全都不可改** —— 应用 2026-08 已更名为 Lin-K Lyrics，但这套 `KaraokeStudio-*` 资产名刻意保持原样。
- **macOS 走 Sparkle**：CI 额外产出 `KaraokeStudio-macos-appcast.xml` 与 `KaraokeStudio-macos-deltas.zip`，机制见 [`docs/auto_update.md`](docs/auto_update.md) §2.1。
- **旧名 EXE 已停发（自 4.4.0）**：包里主程序是 `Lin-K Lyrics.exe`；同内容的旧名副本 `Karaoke Studio.exe` 只随 4.3.x 及更早版本分发，自 4.4.0 起由版本闸 `build_parts.FIRST_LEGACY_FREE_VERSION = (4,4,0)` 自动停发（`SHIP_LEGACY_APP_EXE` 同时驱动 `APP_TARGETS`、`main()` 护栏与 `build_windows.bat` 的复制步骤）。以下配套**长期保留，不要当过期代码清理**：Updater 的 `_cleanup_legacy_main_exe`（新版本成功拉起后迁移旧名快捷方式、回收旧名副本）、`installer.find_app_exe_name()` 固定返回新名、`release.py` 自 4.4.0 起在 release body 头部自动加的老版本手动更新提示（`MANUAL_UPDATE_NOTICE`）、护栏 `tests/test_rename_release_invariants.py`。原理与失败矩阵见 [docs/auto_update.md §8.1](docs/auto_update.md)。

---

## 6. 发版

所有发版规则在 [`docs/release-process.md`](docs/release-process.md)，**必读**。要点：

- 工作台与 submodule **不会同时发版**，分两条流程：
  - **流程 A · 工作台更新**：SemVer 3 段 bump（`3.0.2 → 3.0.3` 等）。
  - **流程 B · Submodule 更新**：第 4 段递增（`3.0.2 → 3.0.2.1`）。
- **废弃 SUG 与 Lin-K 大版本联动**：同步 SUG 或 SUG 自身提升主/次版本，不自动要求 Lin-K 提升 MAJOR / MINOR。纯子模块同步仍递增第 4 段；Lin-K 自身的版本升级按实际改动性质决定。
- Tag 格式 `vX.Y.Z[.N]`，push tag 触发 [`.github/workflows/release.yml`](.github/workflows/release.yml) 自动打包+发 release。
- 发版准备统一运行 `python scripts/release.py prepare X.Y.Z[.N]`：它会同步 `APP_VERSION` 与 [`README.md`](README.md) 顶部「当前版本」，并在 `CHANGELOG.md` 插入当天的占位段。补全 CHANGELOG 后运行 `python scripts/release.py notes X.Y.Z[.N]` 生成中文 release body。
- **release body 由 CI 从 `CHANGELOG.md` 当前 tag 的版本段提取**，更新弹窗会原样展示，所以必须是中文；打 tag 前必须先跑上面的 notes 命令校验，避免更新弹窗展示错误内容。
- **写 CHANGELOG 时必须在版本段顶部添加反馈群提示**：`<span style="color:#d64545"><b>Lin-K/SUG 反馈交流群 QQ 1108437280</b></span>`（HTML span 写法原样保留，更新弹窗按富文本渲染）。
- **写 CHANGELOG 时必须核对实际提交作者**：用 `git log <上一个tag>..HEAD --format='%an %s'` 查看本版本各提交的作者，在对应条目后加上 `@作者名`，不要凭猜测署名。**署名映射：GitHub 用户 `Myosotis11037` 的所有提交统一标注 `@凛夜`；`Xuan-cc` 的提交标注 `@Xuan-cc`。** CHANGELOG 与 release body 均遵守此规则。
- **条目署名必须可点击跳转本人 GitHub 主页**：使用 Markdown 链接；凛夜统一写作 `[@凛夜](https://github.com/Myosotis11037)`，Xuan-cc 写作 `[@Xuan-cc](https://github.com/Xuan-cc)`，其他作者同样使用核实后的真实账号主页。CHANGELOG 与 release body 保持一致，主页链接不能代替版本末尾「本版贡献者」中的真实账号 `@mention`。
- **发版补改不得转移贡献归属**：agent 在发版检查中补改已有贡献时，不得仅凭自动继承的本机 Git 身份重新署名；须结合原始提交、`git blame`、实际改动范围和用户确认核实归属，用户明确纠正优先。独立的新修复也不能一律归给原代码作者。误署名案例（2026-10-12 · v4.3.7.5）记录在 `docs/release-process.md` §6。
- **保留 Release Contributors 账号关联**：版本段末尾（分隔线前）添加「本版贡献者」一行，按核实后的实际贡献归属去重列出真实 GitHub 账号；两位均有贡献时写 `本版贡献者：凛夜（@Myosotis11037）、@Xuan-cc`，仅一位有贡献时只列该位。条目中的 `@凛夜` 保持不变，真实账号提及用于让 GitHub 生成 Contributors 头像列表。CHANGELOG 与 release body 均须包含此行，发布后核对头像列表。详见 `docs/release-process.md` §6。

---

## 7. 代码约定

- **语言**：用户可见字符串（UI / CHANGELOG / release body / 弹窗）一律**中文**；commit message / code comment / docstring 用英文或中文都可以，跟现有文件保持一致。
- **commit 风格**：现有历史混用 `fix:` / `feat:` / `chore:` 前缀（Conventional Commits）和 `Release X.Y.Z` 形式。新功能/修复用前缀；发版 commit 用 `Release X.Y.Z`。
- **测试**：tests 目录下；命名 `test_<module>.py`。改一个模块时优先扩对应测试。
- **GUI 改动**：`gui_qt.py` 是主窗口外壳，各步页面在对应子包（多为 `page.py`，见 §2）；定位时 grep `class XxxCard` / `class XxxPage` 比硬翻快得多。

---

## 8. 已知坑（别再踩）

条目编号被其他文档和代码注释引用（如「坑 #5」「§8.9」「§8.11」），增删时不要重新编号。

1. **CI checkout 必须带 `submodules: recursive`**，否则 runner 上 `krok_helper/lyrics_timing/src/` 是空的，构建脚本会在「Checking bundled SUG source path」失败。
2. **构建脚本和 workflow 资产名要对齐**：`scripts\build_windows.bat` 产出 `KaraokeStudio-windows.zip` + `.sha256`，workflow 直接 upload；不要在 workflow 里再加一层 `Compress-Archive`，会覆盖出错误的 zip。
3. **macOS 构建**：build script 只输出 `.app`，workflow 用 `ditto -c -k --sequesterRsrc --keepParent` 打 zip。
4. **macOS 构建过程会临时改 `strange_uta_game/__version__.py` 的 `VARIANT` 为 `"mac"`**，脚本用 `trap restore_sug_version EXIT` 恢复（见 [`scripts/build_macos.command`](scripts/build_macos.command)）。如果中断了，手工还原 SUG。
5. **不得恢复 `generate_release_notes: true`**——release body 必须来自当前 tag 的 `CHANGELOG.md` 版本段（见 §6），否则更新弹窗会展示英文 compare 链接。
6. **不要 `--force` push 已经发布过 release 的 tag**——已经下载过的用户的客户端不会重拉。
7. **README 版本号容易漏改**——`APP_VERSION` 与 README 顶部「当前版本」必须同步；`release.py prepare` 会一并改，手改时别漏。
8. **Qt 相关版本全部固定 6.11.0，打包必须保留 MSVC 运行时刷新步骤**——PyQt6 binding、
   `PyQt6-Qt6` runtime 与 native SDK 必须同为 6.11.0（6.11.0 binding 配 6.11.1 runtime 虽符合
   wheel 依赖范围，但 `PyQt6.QtSvg` 会因程序入口不匹配而加载失败；sidecar 的 CMake 用
   `KROK_EXPECTED_QT_VERSION` 断言 native Qt 与 PyQt6 的 Qt 版本一致）。native smoke 脚本的
   `aqtinstall` 固定为 upstream commit `bbfb1f7c0590b9eb3fa91356e75bb64fb15d3643`（PyPI 上的
   3.3.0 读不了 Qt 6.11 仓库结构），不可改回无版本约束。`scripts/build_windows.bat` 的
   「Refreshing bundled MSVC runtime DLLs」步骤不能删：某些 PyQt6-Qt6 wheel 在 `Qt6\bin` 捆绑
   VS2019 时代的旧 `MSVCP140*.dll`/`VCRUNTIME140*.dll`，被同目录优先加载后，VS2022 编译的扩展
   （如 pedalboard_native）会在其中 0xc0000005 无声闪退（v4.1.0 事故）；该步骤用构建机
   System32 的运行时覆盖低于 14.38 的副本。改收集规则时同步 bump `scripts/build_parts.py` 的
   `RUNTIME_PROFILE`，否则 CI 会复用含旧运行时的 runtime zip。排查此类闪退：
   `Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'}` 看故障模块。

9. **受限/沙箱环境下的配置目录写探测卡死：根因已消除，别改回去**——旧版 SUG 的
   `app_dirs.program_dir()` 取 `sys.argv[0]` 所在目录做可写探测；`python -m pytest` 下它落在
   site-packages（ProgramData），沙箱对该目录的探测会**无限阻塞并持续烧 CPU**（无报错、
   无超时；`py-spy dump` 栈顶为 `tempfile._mkstemp_inner ← app_dirs._is_dir_writable`）。
   现在有两层防护：SUG 的 `program_dir()` 已改为按 `__file__` 推导源码树根，不再读
   `sys.argv[0]`；工作台在 `krok_helper/__init__.py` 里用 `directory_probe.is_dir_writable`
   （`os.open` 单次探测，拒绝访问立即失败）替换了 SUG 的探针。所以不再需要「把
   `run_pytest.py` 放到可写目录再启动 pytest」的旧规避办法。受限环境下若 `tmp_path` 报
   `PermissionError: pytest-of-<user>`，加 `--basetemp=%TEMP%/xxx` 即可；正常终端 / CI
   不受影响。

10. **`tests/conftest.py` 顶部的预导入不能删**——它先导入
    `krok_helper.audio_processing.separation.ai_timing_host` 把 Qt/qfluentwidgets 导入链
    预热好（带 `# noqa: F401`，看起来像无用导入）。去掉后 pytest 收集期 `PyQt6.QtSvg` 会报
    「DLL load failed: 找不到指定的程序」——同一解释器直接 `python -c "import krok_helper..."`
    正常，仅 pytest 的导入环境触发。有它在，`python -m pytest` 与 xdist worker 都可直接用，
    不需要临时启动脚本。

11. **改 sidecar 共享头文件后增量构建不可信，必须 `--clean-first`**——本机
    中文 VS 工具链下 MSVC 的 `/showIncludes` 输出是本地化的「注意: 包含文件:」，
    ninja 的头文件依赖解析失效；改了 `render_types.h` 这类被大量 TU 包含的
    头（例如给结构体加字段）后，增量构建只会重编少数几个 .cpp，新旧
    sizeof 不同的对象文件混链，运行期随机 0xC0000409 / 0xC0000005 崩溃
    （表现之一：configure 即崩，GPU 预览拉起的 sidecar 死亡，连带
    `preview_async` 报 `OSError 22`）。预防：凡动 `native/subtitle_renderer`
    的头文件，用
    `cmake --build build/native-renderer --config Release --clean-first`
    全量重建后再验证。

12. **offscreen 冒烟的进程退出码不是回归信号**——`QT_QPA_PLATFORM=offscreen` 下
    「整应用构造→关闭→解释器退出」脚本约半数在进程收尾段错误（exit 139，
    faulthandler 无 Python 帧）；真实 Windows 平台同脚本干净退出、pytest 全绿，
    用户实际运行不受影响。页面控件构成、构造时机、SUG 补丁顺序、字体预热线程、
    `theme.changed` 连接等均已排除，指向 offscreen 插件在解释器终结阶段的环境性
    缺陷。**验证整应用生命周期请用真实平台跑脚本**（offscreen 下构造期断言仍
    可信）。若未来在真实平台复现，再按 §9 的销毁顺序思路深挖。

13. **sidecar 的 d2d_fx / d2d_backend_internal / d2d_backend_render 禁止新增
    Qt 头文件**——Qt 头（未定义 `QT_NO_KEYWORDS` 时）会把 `emit` / `slots` /
    `signals` **关键字宏**带进编译单元，而这些文件里有既有的同名局部变量
    （如 d2d_fx.cpp 的 `const int emit = ...`），宏展开为空后声明变成
    `const int = ...`，在别人的老代码行上报 C2513，极具误导性（例如在
    d2d_fx.cpp 加 `#include <QImage>` 就会触发）。需要图像像素时传原始预乘
    BGRA 缓冲（参考 `scanlineImageCompositePixels` 的裸指针 API），Qt 侧解码
    放在 d2d_backend_configure.cpp（它本来就含 `<QImage>`）。

14. **sidecar 构建环境用错一次后，增量构建会复用坏 .obj 造成运行期崩溃**——
    本机构建树（`build/native-renderer`）配置的是 **Community 版 MSVC 14.51**
    （cl 绝对路径烙在 ninja 规则里），用 BuildTools 版 vcvars 起环境会导致
    STL 头找不到而部分 TU 编译失败；换回正确环境后**增量构建只重编失败的
    TU，之前用错误 STL 头编出的 .obj 被留用**，混链出的 exe 在
    `gpu_configure` 100% 崩 0xC0000005（曾被误判为源码缺陷）。
    预防：构建 sidecar 统一用 `build/full_clean_rebuild.bat`（Community
    vcvars + `--target clean` + 全量重建）；凡怀疑构建环境出过错，先
    `cmake --build build/native-renderer --target clean` 全清再编，不要相信
    增量产物。定位此类崩溃可用「录制 stdin 协议直接重放到 exe」的办法
    （绕开 pytest/Python 变量；`build/replay_check.py` 参考实现）。`build/`
    未入库，这两个脚本只在本机存在。

---

## 9. 字幕渲染模块

第 5 步「字幕视频生成」，目标对标 NicoKaraMaker3，按工作台流程 A 直接在 `main` 开发。代码在 `krok_helper/subtitle_render/`（`domain` / `engine` / `frontend` / `n3` / `native` / `project` / `serialization` / `settings` / `sources` 子包），C++ sidecar 在 `native/subtitle_renderer/`。字幕源支持 `.sug` / `.lrc` / `.n3proj`，工程文件为 `.yurika`。

### 接手前必读

1. [`docs/字幕渲染模块-需求设计.md`](docs/字幕渲染模块-需求设计.md)：总体状态、产品决策和剩余主线。
2. [`docs/N3项目导入兼容性与实施计划.md`](docs/N3项目导入兼容性与实施计划.md)：N3 字段矩阵、明确不做项、下一步 TDD 顺序。
3. [`docs/字幕布局-N3对齐改造计划.md`](docs/字幕布局-N3对齐改造计划.md)：布局 P1-P4 历史、字符排版参数现状与兼容边界。
4. [`docs/SUG与字幕渲染模块-Python走字差异.md`](docs/SUG与字幕渲染模块-Python走字差异.md)：SUG/LRC 数据保真边界。
5. 若任务涉及 GIL、GPU、Direct2D 或 native 预览，读
   [`docs/字幕渲染-GPU后端逆向与实施计划.md`](docs/字幕渲染-GPU后端逆向与实施计划.md)：
   N3 10.74.80.0 逆向证据、Windows C++ Direct2D 决策、G0-G6 实施与验收门槛。

文档 1 的「当前进度」是 2026-07-13 的快照（渲染后端口径已于 2026-11-10 复核更新）；其余条目与现状冲突时，以下面的「关键约束」和代码为准。

### 关键约束

- **引擎选型已定**：离屏栅格化 + ffmpeg rawvideo pipe，不改成 ASS/libass 主路径。
- **GPU（Direct2D）是主渲染口径**：Windows 交互会话默认使用 G5 shared-memory 预览并默认
  启用 GPU 字幕导出，改动以 GPU 表现为准。CPU QPainter 保留为 fallback（无 GPU / 非 Windows /
  GPU 初始化失败）与离线校验路径，**不再是判定正确性的 oracle**。G6 DirectComposition 已按用户
  决定停止且产品入口硬关闭，后续不得继续或重新开放。
- **两条后端必须给出同一布局结果**，出现分歧时按 `layout_semantics` 对应的语义判对错
  （`n3_1074` 以 N3 10.74 逆向结论为准，`legacy` 以旧工程既有画面为准），不要因为"GPU 是主口径"
  就直接把 GPU 的现状当成正确答案——行网格属于页级量，任何依赖行内容的实现都要先存疑。
- **背景缩放以预览为主口径、预览与导出必须严格一致**（2026-08-23 用户拍板）：视频背景固定
  contain（等比缩放完整放入 + 纯黑边，预览 `KeepAspectRatio` + 黑底矩形 = 导出
  `scale=decrease + pad=black`）；静态图/图片序列按 `BackgroundSource.image_fit` 选
  cover（铺满裁切，旧工程默认）或 contain，两侧同语义实现。背景选择/独立音频/画面尺寸入口
  在属性面板「背景/音频」页（`PropertyPanel._PAGE_SPECS` 第 6 页），与导出页宽高帧率双向联动。
- **独立音频只用于非视频背景**：静态图、图片序列和纯色背景可加载独立音频（预览、项目保存与
  导出均已接通）；视频背景只使用内嵌音轨，避免双时钟。
- **不要改 SUG submodule 源码**：优先直接消费 SUG `Project`/`.sug`；`.lrc` 仅为兼容入口。
- **视频成片只输出 MP4、只支持 60/120fps**；另支持附加导出：**PNG 序列**
  （透明字幕层 / 含背景合成，输出到以导出名命名的独立子文件夹，帧名
  `名称_000001.png`，重名非空目录前置拦截，无音频）与**两种透明字幕视频**
  （`.mov`，仅字幕层不带背景，携带与 MP4 同源的音频 pcm_s16le，2026-09 新增）：
  **ProRes 4444**（`prores_ks` + `yuva444p10le`，NLE alpha 兼容性最好，默认推荐）
  与 **QuickTime 动画**（`qtrle` + `rgba`，无损、体积小；多数播放器不合成其
  straight alpha 而显示黑底——这是播放器行为不是导出错误，导入 PR/AE 即正常，
  不要再因「播放黑底」误判为编码缺陷而下线）。仍不做 30fps 原样输出、AVI 或
  N3 ARGB 帧序列；PNG/mov 格式的完成弹窗不提供「进入下一步」（第 6 步混流
  只收 MP4）。输出格式偏好持久化在 app-local `output` 命名空间。
- **不支持假名独立字体族**；假名沿用日文字体，英数字体仍可独立。
- **N3 二重描边严格遵守 `UseEdge2`**，不能因保存了宽度就强制开启。
- 若再次出现 Qt pooled-thread teardown access violation，优先检查预览 worker、
  QMediaPlayer、QApplication 与顶层对话框的销毁顺序；新增的顶层 Fluent 对话框要显式销毁
  （2026-07 在测试清理阶段偶发复现过，补齐显式销毁后未再复现）。

---

## 10. 资源指针

- 主仓库：https://github.com/karaoke-studio/karaoke-studio
- SUG submodule：https://github.com/karaoke-studio/StrangeUtaGame
- Release 页：https://github.com/karaoke-studio/karaoke-studio/releases
- Actions：https://github.com/karaoke-studio/karaoke-studio/actions
- 自动更新日志（本地）：`$env:TEMP\KaraokeStudioUpdater\updater.log`
- License：GPL-3.0（合并自原 SUG，原 karaoke-helper 无 LICENSE）。BASS 音频库非商业免费，商用需购买授权——详见 [AUTHORS.md](AUTHORS.md)。
