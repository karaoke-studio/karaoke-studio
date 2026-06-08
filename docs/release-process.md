# Karaoke Studio 发版流程

本文档定义主仓库的发版规范，**重点是 StrangeUtaGame（SUG）submodule 更新触发的发版**。所有维护者按本文档操作。

本地构建/验证细节见 [`release-runbook.local.md`](./release-runbook.local.md)（本地、不入库）。GitHub Actions 配置见 [`.github/workflows/release.yml`](../.github/workflows/release.yml)。

---

## 1. Submodule 更新硬性规则

> **一旦决定要更新 SUG submodule，必须更新到 `karaoke-studio/StrangeUtaGame` `origin/main` 的最新 commit，不允许停在中间 commit。**

理由：

- SUG 历史上既出现过 `release vX.Y.Z` 这种带 tag 的发布，也出现过仅作为 commit message 的发布（如 `6f55a05 release v1.1.5`），停在 tag 之间的中间 commit 没有可追溯性。
- 主程序自动更新弹窗会展示 release notes，用户能感知 SUG 改动；停在落后 commit 会让 release notes 与实际行为不一致。
- 多个 SUG 修复堆积时一次性合入更省事，CI/验收也只跑一次。

落地方式：

```powershell
git -C krok_helper/lyrics_timing fetch --tags
git -C krok_helper/lyrics_timing checkout origin/main
git -C krok_helper/lyrics_timing log --oneline <旧gitlink>..HEAD   # 用于写 changelog
git add krok_helper/lyrics_timing
```

如果 `origin/main` 的 HEAD 恰好有 tag（用 `git -C krok_helper/lyrics_timing tag --points-at origin/main` 查），**commit message 与 CHANGELOG 一律引用 tag 名**（例如 `SUGv1.1.5`）。没有 tag 时引用短 SHA。

不要 checkout 到具体 tag 然后停在那里——`origin/main` 可能已经领先于最新 tag。

---

## 2. 触发条件

满足下面任一条且 SUG `origin/main` 领先于当前 gitlink 时，启动本流程：

- SUG 有面向用户的修复/功能合入；
- 主程序有自己的发版需求（功能/修复），顺带把 SUG 一并刷到最新；
- 距上次 SUG 同步超过两个月，做一次例行同步以避免漂移。

**仅文档/CI/社交资产的 SUG 改动**不强制立即发版，可以推到下一次例行发版一起带上。

---

## 3. 版本号

主仓库 `APP_VERSION`（[`krok_helper/config.py`](../krok_helper/config.py)）按下面规则确定。区分**纯 submodule 更新**和**主程序也有改动**两种情况。

### 3.1 纯 submodule 更新（仅 gitlink 变化，主程序代码无改动）

无论 SUG 累计改动是修复还是新功能，**一律在第 4 段递增**：

| 当前版本 | 新版本 |
|---|---|
| `x.y.z` | `x.y.z.1` |
| `x.y.z.n` | `x.y.z.(n+1)` |

例：`3.0.2` → `3.0.2.1`；`3.0.2.1` → `3.0.2.2`。

理由：主程序代码没动、构建产物只在嵌入 SUG 这一层有差异，用 SemVer 的 PATCH 段会和"主程序自己的修复"混在一起。第 4 段专门标记 SUG 同步轮次，回溯时一眼能看出哪些 release 是纯子模块刷新。

### 3.2 主程序也有改动（含或不含 SUG 同步）

按 SemVer，取**主程序改动**与 **SUG `<旧gitlink>..origin/main` 累计改动**两边的最大 bump，并**丢掉第 4 段**：

| 累计改动性质 | bump | 例子 |
|---|---|---|
| 仅 bug 修复 / 内部重构 / 词典数据 | **PATCH** | `3.0.2.1` → `3.0.3` |
| 新增用户可见的功能、参数、流程 | **MINOR** | `3.0.2.1` → `3.1.0` |
| 改了对外接口/数据格式（不向后兼容） | **MAJOR** | `3.0.2.1` → `4.0.0` |

判定原则：**只看用户能感知到的差异**。SUG CHANGELOG 不够清楚时跑 `git -C krok_helper/lyrics_timing log --oneline <旧>..origin/main` 自己看 commit。

### 3.3 版本比较

Updater 的 [`_version_key`](../krok_helper/updater/worker.py) 按段比较且自动补零，`3.0.2 < 3.0.2.1 < 3.0.3`，4 段版本对自动更新流程无影响。

---

## 4. 操作流程

每次按以下顺序执行，不要跳步。

### 4.1 把 submodule 推到 `origin/main`

见 §1。完成后 `git status` 应能看到 `krok_helper/lyrics_timing` 的 gitlink 变化。

### 4.2 冒烟测试

```powershell
..\karaoke-studio\.venv\Scripts\python.exe -m pytest tests\test_workbench_updater.py tests\test_workbench_updater_apply.py
$env:QT_QPA_PLATFORM='offscreen'
..\karaoke-studio\.venv\Scripts\python.exe -c "from PyQt6.QtWidgets import QApplication; app=QApplication([]); from krok_helper.gui_qt import KrokHelperQtApp; w=KrokHelperQtApp(); print(type(w.lyrics_timing_page).__name__)"
```

如果 SUG 改动涉及自动检测/打轴/LLM 流程，再跑一遍 SUG 自己的回归脚本。

### 4.3 改 `APP_VERSION`

按 §3 编辑 [`krok_helper/config.py`](../krok_helper/config.py) 第 2 行。

### 4.4 更新 CHANGELOG

把 [`CHANGELOG.md`](../CHANGELOG.md) 的 `[Unreleased]` 内容落到新版本节，**必须**包含：

- 一行 `Changed`：`更新 StrangeUtaGame 子模块到 SUGvX.Y.Z`（或短 SHA）。
- **逐条**列出 SUG 里用户能感知到的改动，中文撰写，因为更新弹窗会直接展示这段 release notes。
- 不要把内部重构、CI、文档之类纯开发者改动写进去。

### 4.5 两笔 commit

历史对齐（参考 `f1f2c5b Release 3.0.2`）：

```
chore(submodule): bump StrangeUtaGame to SUGvX.Y.Z   # 仅 gitlink
Release X.Y.Z                                         # APP_VERSION + CHANGELOG
```

分开是为了 `git log` 一眼区分 submodule 移动和版本发布。

### 4.6 打 tag 发版

```powershell
git push
git tag vX.Y.Z
git push origin vX.Y.Z
```

触发 [`release.yml`](../.github/workflows/release.yml)，自动构建 Windows + macOS zip 并发布 GitHub Release。

### 4.7 覆盖 release body 为中文

> Workflow 配的是 `generate_release_notes: true`，**默认生成的是英文 commit log**。主程序更新弹窗会直接展示 release body，所以 CI 跑完一定要立刻用 §4.4 写的中文 CHANGELOG 段覆盖一次。

CI 上传完 zip / sha256 后：

```powershell
gh release view vX.Y.Z --json body   # 检查当前 body
gh release edit vX.Y.Z --notes-file <(...)   # 或用 --notes "...中文内容..."
```

中文 body 结构对齐 v3.0.2 的格式：

- 用 `### 新增` / `### 修复` / `### 其他更新` 这类中文小标题（不要直接用 Keep-a-Changelog 的英文标题）。
- 第一段一句话说清楚本次发版的性质（如：「仅 submodule 同步，主程序代码无改动」）。
- 内容与 [`CHANGELOG.md`](../CHANGELOG.md) 对应版本节保持一致。

### 4.8 自动更新验收

从旧版安装目录启动应用，走完检查更新 → 弹窗展示 §4.7 覆盖好的中文 release notes → 立即更新 → 重启 → 关于页版本号变化。详见 [`release-runbook.local.md`](./release-runbook.local.md) §5。

---

## 5. 边界与回滚

- **SUG 改了 URL/路径**：同步改 [`.gitmodules`](../.gitmodules)，跑 `git submodule sync --recursive`，CI 重跑一次 checkout 步骤。
- **SUG 新 API 主程序还没接进去**：仍然把 submodule 推到最新（§1 硬性规则），CHANGELOG 中**只列已经接好的能力**，未接的能力等主程序集成完再单独 MINOR 发版。
- **回滚 SUG**：把 gitlink checkout 回旧 commit（这是唯一允许停在历史 commit 的情况），主仓库 bump PATCH，CHANGELOG 写明回滚原因与目标 commit。
- **CI 构建失败**：删 tag (`git push --delete origin vX.Y.Z; git tag -d vX.Y.Z`)，修复后重打。**不要**用 `--force` 推已发布出去的 release tag。
