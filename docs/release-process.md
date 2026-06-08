# Karaoke Studio 发版流程

工作台与 SUG submodule **不会同时发版**——一次 release 要么仅工作台代码改动，要么仅 submodule 同步。两条流程在版本号规则和操作步骤上不同，CI 触发与 release body 处理共用。

本地补充（venv 路径、个人 cheat sheet）见 [`release-runbook.local.md`](./release-runbook.local.md)（gitignored）。

---

## 1. 选哪条流程

- 你这次只改了 `krok_helper/**` 主程序代码、`scripts/`、`build/`、`.github/`、文档或者打包脚本（**`krok_helper/lyrics_timing` 的 gitlink 不动**）→ **流程 A · 工作台更新**。
- 你这次只把 `krok_helper/lyrics_timing` gitlink 推到了 SUG 上游（主程序代码不动）→ **流程 B · Submodule 更新**。

如果发现两边都动了：停手，回退其中一边到原状态，分两次发版。

---

## 2. 版本号规则

`APP_VERSION` 在 [`krok_helper/config.py`](../krok_helper/config.py) 第 2 行；[`README.md`](../README.md) 顶部「当前版本」文本同步。

| 流程 | bump 规则 | 例子 |
|---|---|---|
| A · 工作台 | SemVer 3 段：PATCH（修复/重构）/ MINOR（用户可见功能）/ MAJOR（破坏性接口变更）。若上一版有第 4 段，**丢掉它**。 | `3.0.2.1` → `3.0.3`（修复） / `3.1.0`（新功能） |
| B · Submodule | 第 4 段递增。不论 SUG 改的是修复还是功能。 | `3.0.2` → `3.0.2.1`；`3.0.2.1` → `3.0.2.2` |

理由（流程 B 为什么不走 SemVer）：主程序代码没动，差异只在嵌入的 SUG 模块。用 SemVer 的 PATCH 会和主程序自己的修复混淆，第 4 段专门标记 SUG 同步轮次，回溯时一眼能看出哪些 release 是纯子模块刷新。

Updater 的 [`_version_key`](../krok_helper/updater/worker.py) 按段比较且自动补零（`3.0.2 < 3.0.2.1 < 3.0.3`），4 段版本对自动更新无副作用。

---

## 3. 共通前置检查

每次发版前：

```powershell
git status --short --branch    # 工作区干净，main 同步到 origin
git submodule status           # 当前 gitlink 是否符合预期
```

确认主仓库是 `karaoke-studio/karaoke-studio`，SUG submodule 是 `karaoke-studio/StrangeUtaGame`。

---

## 4. 流程 A · 工作台更新发版

### 4.1 改代码

主程序代码、scripts、CI、文档等等。**不要碰 `krok_helper/lyrics_timing/` 的 gitlink。** 如果发现需要 SUG 改动，切去流程 B 单独发版。

### 4.2 本地验证

```powershell
..\karaoke-studio\.venv\Scripts\python.exe -m pytest tests\
..\karaoke-studio\.venv\Scripts\python.exe app.py --help
```

涉及 UI / 打轴嵌入 / updater 时：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
..\karaoke-studio\.venv\Scripts\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.lyrics_timing_page).__name__)"
```

### 4.3 改版本号

按 §2 决定 bump，编辑：

- [`krok_helper/config.py`](../krok_helper/config.py) 的 `APP_VERSION`
- [`README.md`](../README.md) 顶部「当前版本」

### 4.4 写 CHANGELOG

把 [`CHANGELOG.md`](../CHANGELOG.md) 的 `[Unreleased]` 落到新版本节，格式见 §6。

### 4.5 Commit

先按改动性质把代码 commit 完（一笔或多笔，按现有 commit 风格），最后单独一笔：

```
Release X.Y.Z
```

只含 `APP_VERSION` / `README` / `CHANGELOG` 三个文件。

进入 §7 触发 CI。

---

## 5. 流程 B · Submodule 更新发版

### 5.1 把 submodule 推到 origin/main

> **硬性规则：决定要更新就推到 `karaoke-studio/StrangeUtaGame` `origin/main` 的 HEAD，不允许停在中间 commit。**

```powershell
git -C krok_helper/lyrics_timing fetch --tags
git -C krok_helper/lyrics_timing checkout origin/main
git -C krok_helper/lyrics_timing log --oneline <旧gitlink>..HEAD   # 用于写 changelog
git -C krok_helper/lyrics_timing tag --points-at HEAD               # 看是不是恰好落在 SUG tag 上
git add krok_helper/lyrics_timing
```

如果 `--points-at HEAD` 拿到 tag，commit message 与 CHANGELOG 一律引用 tag 名（例如 `SUGv1.1.5`）；否则引用短 SHA。

### 5.2 冒烟测试

```powershell
..\karaoke-studio\.venv\Scripts\python.exe -m pytest tests\test_workbench_updater.py tests\test_workbench_updater_apply.py
$env:QT_QPA_PLATFORM='offscreen'
..\karaoke-studio\.venv\Scripts\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.lyrics_timing_page).__name__)"
```

如果 SUG 涉及自动检测/打轴/LLM 流程，再跑一遍 SUG 自己的回归脚本。

### 5.3 改版本号

第 4 段递增，按 §2。编辑：

- [`krok_helper/config.py`](../krok_helper/config.py) 的 `APP_VERSION`
- [`README.md`](../README.md) 顶部「当前版本」

### 5.4 写 CHANGELOG

把 [`CHANGELOG.md`](../CHANGELOG.md) 的 `[Unreleased]` 落到新版本节，格式见 §6。**必须**额外有一行：`更新 StrangeUtaGame 子模块到 SUGvX.Y.Z`（或短 SHA），第一段标注「仅 submodule 同步，主程序代码无改动」。

### 5.5 两笔 Commit

```
chore(submodule): bump StrangeUtaGame to SUGvX.Y.Z   # 只动 gitlink
Release X.Y.Z.N                                       # APP_VERSION + README + CHANGELOG
```

分开是为了 `git log` 一眼区分 submodule 移动和版本发布。

进入 §7 触发 CI。

---

## 6. CHANGELOG 与 release body 格式

[`CHANGELOG.md`](../CHANGELOG.md) 沿用 Keep a Changelog 英文小标题（`### Added` / `### Fixed` / `### Changed`）；GitHub Release body 用中文小标题（`### 新增` / `### 修复` / `### 其他更新`），内容与 CHANGELOG 对应版本节保持一致。

每个版本节必须包含：

- **一句性质概述**（例：「仅 submodule 同步，主程序代码无改动」「修复 Firefox cookie 导入失败」）。
- 分类条目，**逐条**列出用户能感知的差异。
- 不要写内部重构、CI、文档之类纯开发者改动——这些放 commit message 里。

参考样板：v3.0.2 / v3.0.2.1。

---

## 7. Tag 与 CI

```powershell
git push origin main
git tag vX.Y.Z[.N]
git push origin vX.Y.Z[.N]
```

触发 [`release.yml`](../.github/workflows/release.yml)：

- **Build Windows**：跑 `scripts\build_windows.bat`，产出 `dist\windows\KaraokeStudio-windows.zip` + `KaraokeStudio-windows.zip.sha256`。
- **Build macOS**：跑 `scripts/build_macos.command` 后用 `ditto` 打包成 `KaraokeStudio-macos.zip`。
- **Publish Release**：把三个文件上传到 GitHub Release（仅 tag 推送时执行）。

3 个 job 必须全绿才会创建 release。监控：

```powershell
gh run watch --exit-status   # 或
gh run list --workflow=release.yml --limit 3
```

失败处理见 §9。

---

## 8. 覆盖 release body 为中文

> Workflow 配的是 `generate_release_notes: true`，默认 body 是英文 `compare/...` 链接。**主程序更新弹窗会直接展示 body**，必须在 CI 完成后立刻覆盖。

```powershell
# 把 §4.4 / §5.4 写好的中文 CHANGELOG 段保存到本地，例如 release-notes-vX.Y.Z.md
gh release edit vX.Y.Z[.N] --notes-file release-notes-vX.Y.Z.md
gh release view vX.Y.Z[.N] --json body --jq .body   # 验证一下
```

`release-notes-*.md` 不入库，用完删掉。

---

## 9. 自动更新验收

从旧版安装目录启动应用：

1. 打开全局设置 → 「应用更新」，关于页应显示旧版本号。
2. 点「检查更新」，弹窗应显示 §8 覆盖好的中文 release notes。
3. 点「立即更新」，`Updater.exe` 会下载 zip → 替换 `Karaoke Studio.exe` 和 `_internal\` → 重启应用。
4. 重启后关于页应显示新版本号。

日志：`$env:TEMP\KaraokeStudioUpdater\updater.log`。

---

## 10. 回滚与边界

- **CI 构建失败 / 想撤回未发布 tag**：
  ```powershell
  git push origin --delete vX.Y.Z
  git tag -d vX.Y.Z
  # 修代码并 commit
  git push origin main
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```
  不要用 `--force` 推**已经创建过 GitHub Release** 的 tag——已经下载过的用户不会重拉。
- **release 已发布但有问题**：在 GitHub Release 页面把它标 prerelease，按流程 A 出一个 PATCH 修复版（`x.y.(z+1)`），CHANGELOG 写明被替代的版本。
- **SUG 改了 URL/路径**：同步改 [`.gitmodules`](../.gitmodules)，跑 `git submodule sync --recursive`。
- **SUG 新 API 主程序还没接进去**：仍按流程 B 推到 `origin/main`，CHANGELOG **只列已经接好的能力**；未接的能力等主程序集成完，按流程 A 单独 MINOR 发版。
- **回滚 SUG**：把 gitlink checkout 回旧 commit（这是唯一允许停在历史 commit 的情况），按流程 B 第 4 段递增发版，CHANGELOG 写明回滚原因与目标 commit。

---

## 附录 · 本地手工构建

CI 不可用或要离线验证时：

```powershell
scripts\build_windows.bat
# 产出 dist\windows\KaraokeStudio-windows.zip + .sha256
```

macOS：

```bash
bash ./scripts/build_macos.command
cd dist/macos && ditto -c -k --sequesterRsrc --keepParent "Karaoke Studio.app" "../../KaraokeStudio-macos.zip"
```

手工上传到已有 release：

```powershell
gh release upload vX.Y.Z[.N] `
  dist\windows\KaraokeStudio-windows.zip `
  dist\windows\KaraokeStudio-windows.zip.sha256 `
  KaraokeStudio-macos.zip
```
