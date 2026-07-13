# AGENTS.md

给 Claude Code / Codex / 其他 coding agent 看的项目入口。读完应该能在不打扰用户的前提下定位代码、跑测试、走完发版流程。

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
- CI 默认 release body 是英文，**必须用 `gh release edit --notes-file` 覆盖成中文**，否则更新弹窗会给用户看英文 compare 链接。
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
5. **`generate_release_notes: true` 会在 release 创建时立刻生成英文 body**，所以 `gh release edit` 必须在 CI 跑完后**立刻**做，免得用户先看到英文。
6. **不要 `--force` push 已经发布过 release 的 tag**——已经下载过的用户的客户端不会重拉。
7. **README 版本号容易漏改**——`APP_VERSION` 与 README 顶部「当前版本」必须同步。

---

## 9. 字幕渲染模块

第 5 步「字幕视频生成」目标对标 NicoKaraMaker3，已于 2026-07-11 从
`feat/subtitle-render` 整体合入 `main`。后续修复直接按工作台流程 A 在 `main` 开发，
不再继续使用该长线分支。

### 接手前必读（2026-07-11 校准）

1. [`docs/字幕渲染模块-需求设计.md`](docs/字幕渲染模块-需求设计.md)：总体状态、产品决策和剩余主线。
2. [`docs/N3项目导入兼容性与实施计划.md`](docs/N3项目导入兼容性与实施计划.md)：N3 字段矩阵、明确不做项、下一步 TDD 顺序。
3. [`docs/字幕布局-N3对齐改造计划.md`](docs/字幕布局-N3对齐改造计划.md)：布局 P1-P4 历史、字符排版参数现状与兼容边界。
4. [`docs/SUG与字幕渲染模块-Python走字差异.md`](docs/SUG与字幕渲染模块-Python走字差异.md)：SUG/LRC 数据保真边界。

### 当前现状

- P0 主路径已完成：`.sug`/`.lrc`/`.n3proj`、视频预览、QPainter 逐字渲染、MP4 导出、取消、工作流嵌入和 `.yurika`。
- ruby、角色/多歌手、行内混合字体/字号/配色、渐变/图片填充、glow/stroke2、竖排/RTL、标题、时间轴和多字幕源均已实现。
- N3 TACTIC 对齐已覆盖：三档发光、蓝白 after 配色、ruby 样式、7px 默认布局字间距，以及 `UseEdge2` 关闭时不强制二重描边。
- 逐行特效（四列表格、批量编辑、N3 行动作、持久化、撤销/重做、Painter）已合入。
- `BackgroundSource` 已支持视频、静态图、图片序列和纯色；独立音频已接入预览、项目保存与 MP4 导出。
- native C++ sidecar 产品路径硬关闭，Python QPainter 是唯一正式路径。

### 下一步顺序

1. N3 提示策略清理；
2. 无交互 CLI 与端到端 CI MP4 烟测。

Windows PyInstaller onedir 已完成实际构建、包内 Multimedia 校验和 frozen
multiprocessing spawn 冒烟；完整测试基线为 `901 passed, 50 skipped`。macOS 脚本已同步收集 QtMultimedia，仍需在 macOS runner
完成真实构建验证。

### 关键约束

- **引擎选型已定**：QPainter 离屏 + ffmpeg rawvideo pipe，不改成 ASS/libass 主路径。
- **不要改 SUG submodule 源码**：优先直接消费 SUG `Project`/`.sug`；`.lrc` 仅为兼容入口。
- **只输出 MP4、只支持 60/120fps**；不做 30fps 原样输出、AVI 或 ARGB/透明 PNG 序列。
- **不支持假名独立字体族**；假名沿用日文字体，英数字体仍可独立。
- **N3 二重描边严格遵守 `UseEdge2`**，不能因保存了宽度就强制开启。
- **所有用户面向字符串中文**。
- 若以后再次出现 Qt pooled-thread teardown access violation，优先检查预览 worker
  的销毁顺序；2026-07-13 完整测试 `901 passed, 50 skipped`，未复现。

---

## 10. 资源指针

- 主仓库：https://github.com/karaoke-studio/karaoke-studio
- SUG submodule：https://github.com/karaoke-studio/StrangeUtaGame
- Release 页：https://github.com/karaoke-studio/karaoke-studio/releases
- Actions：https://github.com/karaoke-studio/karaoke-studio/actions
- 自动更新日志（本地）：`$env:TEMP\KaraokeStudioUpdater\updater.log`
- License：GPL-3.0（合并自原 SUG，原 karaoke-helper 无 LICENSE）。BASS 音频库非商业免费，商用需购买授权——详见 [AUTHORS.md](AUTHORS.md)。
