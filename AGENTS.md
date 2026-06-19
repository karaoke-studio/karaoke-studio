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
| 5 | 字幕视频生成 | [`krok_helper/subtitle_render/`](krok_helper/subtitle_render/)（骨架已落地，在 `feat/subtitle-render` 分支推进；详见 §10） |
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

- [`krok_helper/updater/worker.py`](krok_helper/updater/worker.py)：在主程序里跑，查询 GitHub Releases API，对比 `APP_VERSION`，下载 zip + sha256。
- [`krok_helper/updater_app/`](krok_helper/updater_app/)：独立 `Updater.exe`，主程序退出后由它替换文件并重启。需要 `build_updater.py` 单独打包。
- 资产命名是硬编码的：`KaraokeStudio-windows.zip` / `KaraokeStudio-macos.zip`（见 `worker.current_asset_name()`）。改名要改三处：worker、`scripts/build_*`、workflow。
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

## 9. 当前开发分支：`feat/subtitle-render`

第 5 步「字幕视频生成」是 1.0 发版前最后、也最复杂的模块，目标对标 NicoKaraMaker3。**所有该模块的工作在 `feat/subtitle-render` 长线分支上进行**，期间 `main` 上的 bugfix 用 `git merge main` 反向并入开发分支，最终一次性 merge 回 main。不要把零散 bugfix 直接提交到本分支。

**接手前必读**：[`docs/字幕渲染模块-需求设计.md`](docs/字幕渲染模块-需求设计.md) —— 含完整需求清单（P0/P1/P2）、双模式数据流、UI 布局、数据模型、引擎选型、已确认的产品决策。

**Sayatoo 参考实现专项**：模仿 Sayatoo「基本」页的任务已持久化到 [`docs/Sayatoo基本页逆向与实现计划.md`](docs/Sayatoo基本页逆向与实现计划.md)。本机 Sayatoo 路径：`C:\Program Files\Sayatoo Software\SubtitleMaker2\2.3.18.9487`。后续新会话如果要继续做「基本」页，先读该文档；里面标了每项实现优先级和难度。

### 骨架现状（提交 `d077b3e`）

- 目录 `krok_helper/subtitle_render/` 已建：`__init__.py` / `__main__.py` / `models.py` / `settings_bridge.py` + `engine/` 与 `frontend/` 子包
- `SubtitleRenderWindow(embedded=False)` + `for_embedding(parent, settings_provider, workflow_context)` 已实现（UI 仅显示"开发中"提示）
- `KrokHelperSubtitleRenderSettingsBridge` 已实现，桥接 `AppSettings.subtitle_render: dict` 命名空间
- `gui_qt.py` 中第 5 步的 `PlaceholderPage` 已替换为本模块的嵌入实例
- 烟测通过：`python -m krok_helper.subtitle_render` 弹空窗；工作台切到第 5 步不报错

### 推进顺序

按 P0 优先级：A1（加载字幕源）→ A2/A3（背景视频 + 音轨）→ A4（卡拉ok逐字高亮，核心）→ A7（实时预览）→ A8/A9（输出 MP4 + 取消）→ A10/A12/A11（双模式接线 + WorkflowContext + standalone 文件 IO）。

### 关键约束

- **引擎选型已定**：QPainter 离屏 + ffmpeg rawvideo pipe（不要改成 ass + libass burn-in）。理由见 plan §E。
- **不要改 SUG submodule 源码**——字幕源走 SUG `NicokaraExporter` 落盘的 Nicokara 逐字 LRC（`.lrc`，含 `@Ruby` / `@Offset` / `@Title` / 演唱者标签）。解析器在 `subtitle_render/subtitle_sources.py`（SUG 自己只有导出器没解析器，由本模块新写）。
- **MVP 仅横书き**，縦書き 推迟到 P1（B9）。
- **MVP 不内置样式预设包**，只实现 `.krstyle.json` 保存/加载能力。
- **所有用户面向字符串中文**。

---

## 10. 资源指针

- 主仓库：https://github.com/karaoke-studio/karaoke-studio
- SUG submodule：https://github.com/karaoke-studio/StrangeUtaGame
- Release 页：https://github.com/karaoke-studio/karaoke-studio/releases
- Actions：https://github.com/karaoke-studio/karaoke-studio/actions
- 自动更新日志（本地）：`$env:TEMP\KaraokeStudioUpdater\updater.log`
- License：GPL-3.0（合并自原 SUG，原 karaoke-helper 无 LICENSE）。BASS 音频库非商业免费，商用需购买授权——详见 [AUTHORS.md](AUTHORS.md)。
