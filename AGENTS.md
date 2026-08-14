# AGENTS.md

给 Claude Code / Codex / 其他 coding agent 看的项目入口。读完应该能在不打扰用户的前提下定位代码、跑测试、走完发版流程。

---

## 0. 用户持久化偏好（最高优先级）

以下规则适用于本仓库的所有新会话，除非用户在当前请求中主动、明确地要求例外：

1. **禁止创建新分支**：始终在当前分支（通常为 `main`）工作、提交和推送。不得因 agent、skill、插件或通用 Git 工作流的默认建议自行创建功能分支；只有用户明确要求“创建分支”时才允许创建。
2. **禁止新建文档**：不得自行新增计划、说明、报告、设计稿、Markdown 或其他文档文件。只有用户明确要求“新建文档”时才允许创建；现有文档仅在任务确有必要时原地更新。

---

## 1. 这是什么

**Karaoke Studio（卡拉OK工作台）**——面向卡拉 OK / B 站投稿制作的 Windows 桌面工具，Python + PyQt6 + PyInstaller，仓库主体是 `karaoke-studio/karaoke-studio`，由 [Myosotis11037](https://github.com/Myosotis11037)（原 karaoke-helper 作者）与 [Xuan-cc](https://github.com/Xuan-cc)（原 StrangeUtaGame 作者）于 2026-06 合并而成。详情见 [AUTHORS.md](AUTHORS.md) / [NOTICE](NOTICE)。

UI 的核心是 [`WORKFLOW_STEPS`](krok_helper/gui_qt.py)（约第 1064 行）定义的 **6 步工作流**：

| 步骤 | 模块 | 实现位置 |
|---|---|---|
| 1 | 视频下载 | [`krok_helper/video_download/`](krok_helper/video_download/) |
| 2 | 波形对齐 | [`krok_helper/audio_alignment.py`](krok_helper/audio_alignment.py)（999 行） |
| 3 | 歌词检索 | [`krok_helper/lyrics.py`](krok_helper/lyrics.py)（1426 行） |
| 4 | 歌词打轴 | [`krok_helper/lyrics_timing/`](krok_helper/lyrics_timing/) — **SUG submodule** |
| 5 | 字幕视频生成 | [`krok_helper/subtitle_render/`](krok_helper/subtitle_render/)（已合入 `main`；详见 §9） |
| 6 | Hi-Res 混流 | [`krok_helper/pipeline.py`](krok_helper/pipeline.py) |

---

## 2. 仓库结构

```
karaoke-studio/
├── app.py                          # 入口 → krok_helper.cli.main
├── krok_helper/
│   ├── __init__.py                 # 把 lyrics_timing/src 加到 sys.path
│   ├── cli.py                      # argparse + 启动 GUI
│   ├── config.py                   # APP_VERSION 等常量
│   ├── gui_qt.py                   # 主窗口、7000+ 行，所有 UI 都在这里
│   ├── pipeline.py                 # Hi-Res 混流流水线
│   ├── audio_alignment.py          # 波形对齐
│   ├── lyrics.py                   # 歌词检索 + 转换
│   ├── settings.py                 # 用户设置序列化
│   ├── ffmpeg.py / network.py / windows.py
│   ├── updater/                    # 自动更新客户端（worker / installer / sources / settings）
│   ├── updater_app/                # 独立 Updater.exe（PyInstaller 单独打包）
│   ├── video_download/             # yt-dlp 封装
│   ├── lyrics_timing/              # ⚠️ Git submodule（StrangeUtaGame）— 不要直接改源码
│   └── assets/                     # 图标、logo、平台 SVG
├── scripts/
│   ├── build_windows.bat           # 本地 + CI Windows 打包
│   └── build_macos.command         # 本地 + CI macOS 打包
├── tests/                          # pytest，对应主程序各模块
├── docs/
│   ├── release-process.md          # 发版流程（committed）
│   └── release-runbook.local.md    # 个人 cheat sheet（gitignored）
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

CLI 选项见 [`krok_helper/cli.py`](krok_helper/cli.py)（`--video` / `--on-audio` / `--off-audio` / `--output-dir` / `--ffmpeg-dir` 等）。无参数则直接进 GUI。

测试：

```powershell
C:\Python314\python.exe -m pytest tests\
```

Qt 嵌入冒烟（无显示器环境）：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Python314\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.lyrics_timing_page).__name__)"
```

---

## 4. Submodule（StrangeUtaGame）边界

`krok_helper/lyrics_timing/` 是独立仓库 [`karaoke-studio/StrangeUtaGame`](https://github.com/karaoke-studio/StrangeUtaGame) 的 submodule。

**规则**：

- **不要直接改 `krok_helper/lyrics_timing/src/strange_uta_game/` 里的代码**。要改先去 SUG 仓库提 PR，merge 后再 bump submodule。
- 嵌入主程序的入口是 [`krok_helper/gui_qt.py`](krok_helper/gui_qt.py) 里的 `self.lyrics_timing_page`（约第 2380 行附近构造）。SUG 自己暴露 `MainWindow` class，宿主把它当一个 Qt widget 挂上去。
- 嵌入契约见 SUG 仓库的 `docs/embedding-contract*.md`（在 submodule 内）。
- [`krok_helper/__init__.py`](krok_helper/__init__.py) 会自动把 `lyrics_timing/src/` 加进 `sys.path`，所以 `import strange_uta_game` 在主程序里直接可用。

**新会话第一件事**：

```powershell
git submodule status
```

如果显示 `-<sha>`（前面有 `-`），说明 submodule 没初始化，跑 `git submodule update --init --recursive` 否则什么都跑不起来。

---

## 5. 自动更新机制

- [`krok_helper/updater/worker.py`](krok_helper/updater/worker.py)：在主程序里跑，查询 GitHub Releases API（全 403 时用 github.com 网页 302 跳转兜底），对比 `APP_VERSION`，跨版本聚合 changelog。
- [`krok_helper/updater_app/`](krok_helper/updater_app/)：独立 GUI `Updater.exe`（复用 SUG `updater_app` 的 PyQt6 界面与增量更新逻辑），主程序退出后由它显示进度、替换文件并重启，不弹控制台。需要 `build_updater.py` 单独打包。
- **增量更新**：[`scripts/build_parts.py`](scripts/build_parts.py) 产出 `KaraokeStudio-windows.json`（manifest）+ `-app.zip` + `-runtime.zip`；Updater 按 manifest diff 只下变化的 part，失败自动回退全量 zip。依赖未变时 CI 复用上一版 runtime zip 原文件（`--require-runtime-reuse` 安全闸）。完整机制、配置与失败矩阵见 [`docs/auto_update.md`](docs/auto_update.md)；设计取舍见 [`docs/工作台更新器完善计划.md`](docs/工作台更新器完善计划.md)。
- 资产命名是硬编码的：`KaraokeStudio-windows.zip` / `KaraokeStudio-macos.zip`（见 `worker.current_asset_name()`）。**manifest 名 `KaraokeStudio-windows.json` 由存量客户端从 zip 名派生，全都不可改**。改名要改四处：worker、`scripts/build_*`、`scripts/build_parts.py`、workflow。
- **更新弹窗会直接展示 GitHub Release 的 body**，所以 release body 必须是中文。详见 §6。

---

## 6. 发版

所有发版规则在 [`docs/release-process.md`](docs/release-process.md)，**必读**。要点：

- 工作台与 submodule **不会同时发版**，分两条流程：
  - **流程 A · 工作台更新**：SemVer 3 段 bump（`3.0.2 → 3.0.3` 等）。
  - **流程 B · Submodule 更新**：第 4 段递增（`3.0.2 → 3.0.2.1`）。
- Tag 格式 `vX.Y.Z[.N]`，push tag 触发 [`.github/workflows/release.yml`](.github/workflows/release.yml) 自动打包+发 release。
- CI 会从 `CHANGELOG.md` 自动提取当前 tag 对应的中文 release body；打 tag 前必须运行 notes 命令校验，避免更新弹窗展示错误内容。
- 改 `APP_VERSION` 时同时改 [`README.md`](README.md) 顶部「当前版本」（容易漏）。
- 发版准备统一运行 `python scripts/release.py prepare X.Y.Z[.N]`；补全 CHANGELOG 后运行 `python scripts/release.py notes X.Y.Z[.N]` 生成中文 release body。详见 [`docs/release-process.md`](docs/release-process.md)。

---

## 7. 代码约定

- **语言**：用户可见字符串（UI / CHANGELOG / release body / 弹窗）一律**中文**；commit message / code comment / docstring 用英文或中文都可以，跟现有文件保持一致。
- **commit 风格**：现有历史混用 `fix:` / `feat:` / `chore:` 前缀（Conventional Commits）和 `Release X.Y.Z` 形式。新功能/修复用前缀；发版 commit 用 `Release X.Y.Z`。
- **测试**：tests 目录下；命名 `test_<module>.py`。改一个模块时优先扩对应测试。
- **GUI 改动**：`gui_qt.py` 是 7000+ 行的单文件，定位时 grep `class XxxCard` / `class XxxPage` 比硬翻快得多。

---

## 8. 已知坑（别再踩）

1. **CI checkout 必须带 `submodules: recursive`**，否则 runner 上 `krok_helper/lyrics_timing/src/` 是空的，构建脚本会在「Checking bundled SUG source path」失败。
2. **构建脚本和 workflow 资产名要对齐**：`scripts\build_windows.bat` 产出 `KaraokeStudio-windows.zip` + `.sha256`，workflow 直接 upload；不要在 workflow 里再加一层 `Compress-Archive`，会覆盖出错误的 zip。
3. **macOS 构建**：build script 只输出 `.app`，workflow 用 `ditto -c -k --sequesterRsrc --keepParent` 打 zip。
4. **macOS 构建过程会临时改 `strange_uta_game/__version__.py` 的 `VARIANT` 为 `"mac"`**，脚本有 `trap` 恢复（[`scripts/build_macos.command`](scripts/build_macos.command) 第 162-170 行）。如果中断了，手工还原 SUG。
5. **Release body 直接来自当前 tag 的 `CHANGELOG.md` 版本段**；不得恢复 `generate_release_notes: true`，否则更新弹窗会展示英文 compare 链接。
6. **不要 `--force` push 已经发布过 release 的 tag**——已经下载过的用户的客户端不会重拉。
7. **README 版本号容易漏改**——`APP_VERSION` 与 README 顶部「当前版本」必须同步。
8. **Qt6\bin 自带的旧 MSVC 运行时会让打包应用无声闪退**——某些 PyQt6-Qt6 wheel 在
   `Qt6\bin` 捆绑 VS2019 时代的 `MSVCP140*.dll`/`VCRUNTIME140*.dll`（14.26 vcwrkspc）。
   Qt6 按同目录优先加载后全进程同名唯一，VS2022 编译的 pedalboard_native 等扩展在新代码
   路径（如 TSM「准备变速缓存」的 MP3 编码）会在旧 MSVCP140.dll 内 0xc0000005 访问冲突。
   首发于 v4.1.0：`build_windows.bat` 曾把 `PYQT6_QT_VERSION` pin 成 6.10.2，而该 wheel 恰好
   带 14.26 旧运行时（v4.0.x 未 pin，装到的 6.11.0 wheel 自带 14.44 所以正常）。**根因已修**：
   PyQt6 binding、打包用的 `PyQt6-Qt6` runtime 与 native SDK 全部固定为 6.11.0；
   不要把 6.11.0 binding 与 6.11.1 runtime 混用，该组合虽符合 wheel 依赖范围，但实测 `PyQt6.QtSvg`
   会因程序入口不匹配而加载失败。`KROK_EXPECTED_QT_VERSION` 会按实际 runtime 指纹严格校验
   （6.11 编的 sidecar exe 也不能加载 6.10 的 Qt DLL）。
   PyPI 上的 `aqtinstall` 3.3.0 无法读取 Qt 6.11 仓库结构，因此 native smoke 脚本固定安装已验证的
   upstream commit `bbfb1f7c0590b9eb3fa91356e75bb64fb15d3643`，不可改回无版本约束的 `aqtinstall`。
   `scripts/build_windows.bat` 的「Refreshing bundled MSVC runtime DLLs」步骤保留作为防御纵深：
   用构建机 System32 的新运行时覆盖 Qt6\bin 里低于 14.38 下限的副本，防止将来 PyQt6 升级再次带入
   旧文件；改收集规则时同步 bump `scripts/build_parts.py` 的 `RUNTIME_PROFILE`，否则 CI 会复用含旧运行时的 runtime zip。
   排查此类闪退：`Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'}` 看故障模块。

9. **受限/沙箱环境下全量回归会被 SUG 的配置目录写探测卡死**——SUG 的
   `app_dirs.program_dir()` 取 `Path(sys.argv[0]).parent` 并用
   `NamedTemporaryFile` 做可写探测；`python -m pytest` 的 argv[0] 是
   site-packages（ProgramData）里的 pytest 包目录，沙箱对它的写探测会
   **无限阻塞并持续烧 CPU**（无报错、无超时，进度条停住）。触发点包括
   `tests/test_module_settings_persist.py` 的 teardown（closeEvent →
   SUG `cleanup_temp_files → config_dir`）与 SUG 自身任何触发
   `AppSettings` 首次构造的测试。定位：`py-spy dump` 栈顶为
   `tempfile._mkstemp_inner ← app_dirs._is_dir_writable`。规避（不改
   产品代码）：把仅含 `sys.exit(console_main())` 的 `run_pytest.py` 放到
   **可写目录**（如 `%TEMP%`）后以它启动 pytest（argv[0] 解析到可写目录），
   并加 `--basetemp=%TEMP%/xxx`（沙箱可能拒绝在旧 `pytest-of-<user>` 下建
   目录）与 `PYTHONPATH=<仓库根>`（脚本方式不像 `python -m` 会自动加
   CWD）。正常终端 / CI 不受影响。该卡点的完整分析也写在
   `tests/test_module_settings_persist.py` 的 `workbench` fixture 注释里。

10. **沙箱下工作台自身 pytest 也有两个独立坑（SUG 坑之外的）**——
    ① 收集期 `PyQt6.QtSvg` 报「DLL load failed: 找不到指定的程序」：
    同一解释器直接 `python -c "import krok_helper..."` 正常，仅 pytest
    的导入环境触发；规避 = 先在进程内预导入
    `krok_helper.audio_processing.separation.ai_timing_host`（Qt/
    qfluentwidgets 链预热）再进 pytest。② `tmp_path` fixture 报
    `PermissionError: pytest-of-<user>`（同 §8.9 条 9 的 basetemp 问题）。
    两个一起规避：`%TEMP%\run_ks_pytest.py`（预热导入 + 注入
    `--basetemp=%TEMP%/ks_pytest_basetemp`），在仓库根目录运行。
    正常终端 / CI 不受影响。

---

## 9. 字幕渲染模块

第 5 步「字幕视频生成」目标对标 NicoKaraMaker3，已于 2026-07-11 从
`feat/subtitle-render` 整体合入 `main`。后续修复直接按工作台流程 A 在 `main` 开发，
不再继续使用该长线分支。

### 接手前必读（2026-07-19 校准）

1. [`docs/字幕渲染模块-需求设计.md`](docs/字幕渲染模块-需求设计.md)：总体状态、产品决策和剩余主线。
2. [`docs/N3项目导入兼容性与实施计划.md`](docs/N3项目导入兼容性与实施计划.md)：N3 字段矩阵、明确不做项、下一步 TDD 顺序。
3. [`docs/字幕布局-N3对齐改造计划.md`](docs/字幕布局-N3对齐改造计划.md)：布局 P1-P4 历史、字符排版参数现状与兼容边界。
4. [`docs/SUG与字幕渲染模块-Python走字差异.md`](docs/SUG与字幕渲染模块-Python走字差异.md)：SUG/LRC 数据保真边界。
5. 若任务涉及 GIL、GPU、Direct2D 或 native 预览，读
   [`docs/字幕渲染-GPU后端逆向与实施计划.md`](docs/字幕渲染-GPU后端逆向与实施计划.md)：
   N3 10.74.80.0 逆向证据、Windows C++ Direct2D 决策、G0-G6 实施与验收门槛。

### 当前现状

- P0 主路径已完成：`.sug`/`.lrc`/`.n3proj`、视频预览、QPainter 逐字渲染、MP4 导出、取消、工作流嵌入和 `.yurika`。
- ruby、角色/多歌手、行内混合字体/字号/配色、渐变/图片填充、glow/stroke2、竖排/RTL、标题、时间轴和多字幕源均已实现。
- N3 TACTIC 对齐已覆盖：三档发光、蓝白 after 配色、ruby 样式、7px 默认布局字间距，以及 `UseEdge2` 关闭时不强制二重描边。
- 逐行特效（四列表格、批量编辑、N3 行动作、持久化、撤销/重做、Painter）已合入。
- `BackgroundSource` 已支持视频、静态图、图片序列和纯色；独立音频已接入预览、项目保存与 MP4 导出，但仅允许用于非视频背景。视频背景只使用内嵌音轨，避免双时钟。
- native C++ sidecar 产品路径硬关闭，Python QPainter 是唯一正式路径。
- 2026-07-19 已完成 N3 GPU 预览管线逆向、Direct2D sidecar 方案持久化与 G0 最小
  GPU 探针（硬件/WARP、透明 texture、staging RGBA readback、shared-memory ring）；
  若用户要求继续，默认从上述 GPU 文档的 G1 横排字幕核心开始。

### 下一步顺序

1. N3 提示策略清理；
2. 无交互 CLI 与端到端 CI MP4 烟测。

Windows PyInstaller onedir 已完成实际构建、包内 Multimedia 校验和 frozen
multiprocessing spawn 冒烟；完整测试基线为 `916 passed, 50 skipped`。macOS 脚本已同步收集 QtMultimedia，仍需在 macOS runner
完成真实构建验证。

### 关键约束

- **引擎选型已定**：离屏栅格化 + ffmpeg rawvideo pipe，不改成 ASS/libass 主路径。
- **GPU（Direct2D）是主渲染口径**：Windows 交互会话默认使用 G5 shared-memory 预览并默认
  启用 GPU 字幕导出，改动以 GPU 表现为准。CPU QPainter 保留为 fallback（无 GPU / 非 Windows /
  GPU 初始化失败）与离线校验路径，**不再是判定正确性的 oracle**。G6 DirectComposition 已按用户
  决定停止且产品入口硬关闭，后续不得继续或重新开放。
- **两条后端必须给出同一布局结果**，出现分歧时按 `layout_semantics` 对应的语义判对错
  （`n3_1074` 以 N3 10.74 逆向结论为准，`legacy` 以旧工程既有画面为准），不要因为"GPU 是主口径"
  就直接把 GPU 的现状当成正确答案——行网格属于页级量，任何依赖行内容的实现都要先存疑。
- **不要改 SUG submodule 源码**：优先直接消费 SUG `Project`/`.sug`；`.lrc` 仅为兼容入口。
- **只输出 MP4、只支持 60/120fps**；不做 30fps 原样输出、AVI 或 ARGB/透明 PNG 序列。
- **不支持假名独立字体族**；假名沿用日文字体，英数字体仍可独立。
- **N3 二重描边严格遵守 `UseEdge2`**，不能因保存了宽度就强制开启。
- **所有用户面向字符串中文**。
- 若以后再次出现 Qt pooled-thread teardown access violation，优先检查预览 worker、
  QMediaPlayer、QApplication 与顶层对话框的销毁顺序；2026-07-13 一次前序运行在清理
  阶段偶发复现，补齐新增顶层 Fluent 对话框显式销毁后完整测试 `916 passed, 50 skipped`。

---

## 10. 资源指针

- 主仓库：https://github.com/karaoke-studio/karaoke-studio
- SUG submodule：https://github.com/karaoke-studio/StrangeUtaGame
- Release 页：https://github.com/karaoke-studio/karaoke-studio/releases
- Actions：https://github.com/karaoke-studio/karaoke-studio/actions
- 自动更新日志（本地）：`$env:TEMP\KaraokeStudioUpdater\updater.log`
- License：GPL-3.0（合并自原 SUG，原 karaoke-helper 无 LICENSE）。BASS 音频库非商业免费，商用需购买授权——详见 [AUTHORS.md](AUTHORS.md)。
