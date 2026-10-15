# Lin-K Lyrics 发版流程

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

### 4.3 准备版本号与 CHANGELOG

按 §2 决定 bump，运行：

```powershell
C:\Python314\python.exe scripts\release.py prepare X.Y.Z
```

脚本会同步 [`krok_helper/config.py`](../krok_helper/config.py) 的 `APP_VERSION`、
[`README.md`](../README.md) 顶部「当前版本」，并在 CHANGELOG 插入当天的中文占位段。

### 4.4 补全 CHANGELOG 并校验 release notes

按 §6 补全新版本段，删除没有内容的分类及所有「待补充」文本，然后运行：

```powershell
C:\Python314\python.exe scripts\release.py notes X.Y.Z
```

生成 `dist/release-notes-vX.Y.Z.md`，用于在打 tag 前校验最终的中文 Release
正文。该文件是本地发版产物，不入库。CI 的 Publish Release job 会从 tag
checkout 的 `CHANGELOG.md` 自动提取同一版本段，并在创建 Release 时直接写入
中文正文。

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

### 5.3 准备版本号与 CHANGELOG

第 4 段递增，按 §2，运行：

```powershell
C:\Python314\python.exe scripts\release.py prepare X.Y.Z.N
```

### 5.4 补全 CHANGELOG 并校验 release notes

补全新版本段。**必须**额外有一行：`更新 StrangeUtaGame 子模块到 SUGvX.Y.Z`
（或短 SHA），第一段标注「仅 submodule 同步，主程序代码无改动」。然后运行：

```powershell
C:\Python314\python.exe scripts\release.py notes X.Y.Z.N
```

### 5.5 两笔 Commit

```
chore(submodule): bump StrangeUtaGame to SUGvX.Y.Z   # 只动 gitlink
Release X.Y.Z.N                                       # APP_VERSION + README + CHANGELOG
```

分开是为了 `git log` 一眼区分 submodule 移动和版本发布。

进入 §7 触发 CI。

---

## 6. CHANGELOG 与 release body 格式

[`CHANGELOG.md`](../CHANGELOG.md) 与 GitHub Release body 都使用中文小标题（例如
`### 新增功能` / `### 特性改变` / `### 修复项目`），release body 由
`scripts/release.py notes` 从对应版本节直接抽取，二者内容保持一致。

每个版本节必须包含：

- **一句性质概述**（例：「仅 submodule 同步，主程序代码无改动」「修复 Firefox cookie 导入失败」）。
- 分类条目，**逐条**列出用户能感知的差异。
- 不要写内部重构、CI、文档之类纯开发者改动——这些放 commit message 里。
- **逐笔核对提交作者并署名**：用 `git log <上一个tag>..HEAD --format='%an <%ae> %s'` 核对作者，必要时通过 GitHub 提交记录确认账号，不得凭猜测署名。GitHub 用户 `Myosotis11037` 的所有提交统一标注 **`@凛夜`**；`Xuan-cc` 的提交标注 `@Xuan-cc`。CHANGELOG 与 release body 使用同一署名映射。
- **发版补改不得转移贡献归属**：agent 在发版检查中对已有贡献做补改、回归修复或整理时，不得仅凭补改提交自动继承的本机 `user.name` / `user.email`，把原贡献重新署给当前机器的账号。须结合原始提交、`git blame`、实际改动范围及用户确认核对归属；用户明确纠正的归属优先。GitHub 将提交邮箱关联到某账号，仅证明提交记录的账号关联，不能据此认定实际贡献作者。也不得反过来把独立的新修复一律归给原代码作者。
- **保留 GitHub Contributors 账号关联**：条目仍使用上述显示署名，但每个版本节末尾（分隔线前）必须添加「本版贡献者」一行，按核实后的实际贡献归属去重列出真实 GitHub 账号。例如两位均有贡献时写：`本版贡献者：凛夜（@Myosotis11037）、@Xuan-cc`。只有其中一位有贡献时只列该位，不得固定添加无贡献的账号。此行同时进入 CHANGELOG 与生成的 release body。

**2026-10-12 · v4.3.7.5 误署名记录**：agent 在发版回归中补改「启动进度不再倒退」
与「字体装卸后回退缓存正确失效」，新增提交 `71f3796b`、`85b25c8b` 自动继承本机
`Myosotis11037` 关联身份，随后错误地将两条日志署为 `@凛夜`。原始相关提交分别为
Xuan-cc 的 `40379168`、`53d2c8a3`；用户已明确确认这两项贡献均属于 Xuan-cc，
正确署名为 **`@Xuan-cc`**。此记录用于纠正归属判断，不能将本机提交身份当成贡献
转移的依据，也不能用「符合 Git 作者记录」为误署名辩解。

GitHub Release 的 Contributors 头像列表依据发布正文中的真实账号 `@mention`
生成，`@凛夜` 仅是显示署名，无法关联 `Myosotis11037`。发布后必须核对头像列表；
若遗漏真实账号提及，同步补全 CHANGELOG 与已发布 Release 正文即可，无需重新打包或
移动已发布标签。参见 [GitHub 官方说明](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。

参考样板：v3.0.2 / v3.0.2.1。

---

## 7. Tag 与 CI

```powershell
git push origin main
git tag vX.Y.Z[.N]
git push origin vX.Y.Z[.N]
```

触发 [`release.yml`](../.github/workflows/release.yml)：

- **Build Windows**：先从上一版 release 下载 `KaraokeStudio-windows.json` + `-runtime.zip` 到 `dist\windows\prev\`（首次分包发布时自动跳过），再跑 `scripts\build_windows.bat`（末尾调用 [`scripts/build_parts.py`](../scripts/build_parts.py)），产出 **7 个 Windows 资产**：

  | 资产 | 作用 |
  |---|---|
  | `KaraokeStudio-windows.zip` (+`.sha256`) | 全量包（含出厂 `_internal/.installed_manifest.json`），旧客户端全量兜底路径 |
  | `KaraokeStudio-windows.json` | 增量清单（schema=1）。**文件名不可改**：存量客户端按 `zip 名 → .json` 派生 |
  | `KaraokeStudio-windows-app.zip` (+`.sha256`) | EXE + Updater + GPU sidecar + `_internal/{krok_helper,strange_uta_game}`，每版必变（约 65 MB） |
  | `KaraokeStudio-windows-runtime.zip` (+`.sha256`) | 其余 `_internal/`，依赖未变时跨版本复用同一文件（约 60 MB） |

- **Build macOS**：跑 `scripts/build_macos.command` 后用 `ditto` 打包成 `KaraokeStudio-macos.zip`。Sparkle 公私钥均已配置时，再签署 ZIP、生成 `KaraokeStudio-macos-appcast.xml`，并调用 `scripts/build_macos_deltas.py` 为同次版本的最近 20 个旧版生成直达补丁；通过应用比对和独立签名验证的补丁合并为 `KaraokeStudio-macos-deltas.zip`，范围索引写入 appcast。无可用补丁时仅发布完整包和 appcast；缺任一密钥时发布不含 Sparkle 的普通包，仅支持手动安装。原有 ZIP 名保持不变。
- **Publish Release**：把全部文件上传到 GitHub Release（仅 tag 推送时执行）。

**发布不变量（违反任何一条都会甩掉存量客户端）**：全量 zip / part / manifest 资产名不可改（保持改名前的 `KaraokeStudio-*` 前缀）；主程序改名后必须继续随包分发同内容的 `Karaoke Studio.exe` 兼容副本并保留在 `APP_TARGETS` 中，onedir 布局不可改结构（详见 [auto_update.md §8.1](auto_update.md)）；tag 保持 `vX.Y.Z[.N]`；runtime 依赖未变时必须复用上一版 zip 原文件（CI 已内置 `--require-runtime-reuse` 安全闸，重打会改变内容哈希令全部老用户重下 runtime）。

3 个 job 必须全绿才会创建 release。监控：

```powershell
gh run watch --exit-status   # 或
gh run list --workflow=release.yml --limit 3
```

失败处理见 §9。

---

## 8. Release body 中文校验

> Publish Release job 会调用 `scripts/release.py notes`，按 tag 版本从
> `CHANGELOG.md` 提取正文，并通过 `body_path` 直接创建中文 Release。
> **主程序更新弹窗会直接展示 body**，所以打 tag 前必须确认对应版本段完整且为中文。

```powershell
python scripts/release.py notes X.Y.Z[.N]
# CI 发布完成后验证
gh release view vX.Y.Z[.N] --json body --jq .body
```

`dist/release-notes-*.md` 不入库。

---

## 9. 自动更新验收

从旧版安装目录启动应用。完整失败矩阵见 [`auto_update.md`](./auto_update.md)：

1. 打开全局设置 → 「应用更新」，关于页应显示旧版本号。
2. 点「检查更新」，弹窗应显示 §8 覆盖好的中文 release notes（跨版本升级时聚合展示中间所有版本）。
3. 点「立即更新」，`Updater.exe` 接管：
   - 本地有 `.installed_manifest.json` 且仅 app part 变化 → 只下载 `-app.zip`（约 65 MB）；
   - 本地无清单（首次增量）→ 下载 app + runtime 两个 part 并写入清单；
   - manifest 拉取失败 → 自动回退全量 zip。
4. 重启后关于页应显示新版本号。

**增量 + Updater 自更新首发的强制验收**：必须使用本地没有
`_internal/.installed_manifest.json` 的旧版客户端，确认首次增量下载 app + runtime
两个 part、写入本地清单并成功重启；再从这份已带清单的安装目录验证下一版只下载
变化的 app part。不得只用新打出的全量包测试。

日志：`$env:TEMP\KaraokeStudioUpdater\updater.log`（开头会标明走了「增量路径」还是「全量路径」）。

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
# 若要保持 runtime 复用（强烈建议），先把上一版 release 的
# KaraokeStudio-windows.json 与 -runtime.zip 下载到 dist\windows\prev\：
gh release download --pattern "KaraokeStudio-windows.json" --dir dist\windows\prev
gh release download --pattern "KaraokeStudio-windows-runtime.zip" --dir dist\windows\prev

scripts\build_windows.bat
# 产出 dist\windows\ 下全部 7 个资产（见 §7 表格）
```

macOS：

首次启用前，用官方 Sparkle SDK 的 `bin/generate_keys --account lin-k-lyrics`
生成专用密钥，再用 `bin/generate_keys --account lin-k-lyrics -x <安全路径>`
导出私钥。将打印的公钥和导出文件中的 Base64 私钥分别配置为仓库 Actions
secrets `SPARKLE_PUBLIC_ED_KEY`、`SPARKLE_PRIVATE_ED_KEY`。私钥不可提交进仓库。
CI 缺少任一密钥时照常打包和发布，但不内置 Sparkle，也不生成签名清单；
应用仍检查更新并展示说明，「前往下载」按钮打开发布页供用户手动安装。
公私钥均已配置时，构建脚本校验公钥，最终 ZIP 签名脚本校验公私钥是否匹配
并验签；格式错误或不匹配仍会阻断对应步骤。
这些 Ed25519 密钥用于更新包认证。

构建保留包内 multiprocessing 和 Sparkle 原生加载检查。播放、安装及重启验收
由开发者按需手工执行，不接入 release workflow。

本地构建可不配置密钥，此时产出仅支持手动安装的普通包。要内置更新器，先设置
`KROK_SPARKLE_PUBLIC_ED_KEY`。签署发布清单还需设置
`KROK_SPARKLE_PRIVATE_ED_KEY`，并安装 `cryptography`。应用代码签名沿用现有 ad-hoc
方式，更新 ZIP 另用 Ed25519 签名。

```bash
bash ./scripts/build_macos.command
ditto -c -k --sequesterRsrc --keepParent "dist/macos/Lin-K Lyrics.app" "KaraokeStudio-macos.zip"
python3 scripts/build_macos_appcast.py KaraokeStudio-macos.zip --sdk-dir build/sparkle-2.9.6
python3 scripts/build_macos_deltas.py KaraokeStudio-macos.zip --sdk-dir build/sparkle-2.9.6
```

增量生成默认两路并行，按版本号数值排序，只选同一主版本、次版本的正式旧版，
第四段版本也计入 20 个基线。历史包必须通过 GitHub 资产大小和 SHA256 校验。
某个基线下载、生成或应用验证失败时省略该补丁，并在构建日志中记录版本与失败类型；
完整包继续可用。没有有效补丁时不生成空容器。完整包签名或公私钥配置错误仍会失败。
Release 工作流执行的是产物生成和校验，不运行整套 pytest 或交互安装测试。

可选的本地手工验收（不随发版自动执行）：

```bash
# 独立临时应用的真实安装/重启验证，不替换已安装的 Lin-K Lyrics：
python3 scripts/smoke_macos_updater.py --sdk-dir build/sparkle-2.9.6
# 合并容器截段下载、原生验签、替换与重启：
python3 scripts/smoke_macos_updater.py --sdk-dir build/sparkle-2.9.6 --container
# 篡改补丁或修改安装基线，验证只下载一次完整包并完成回退：
python3 scripts/smoke_macos_updater.py --sdk-dir build/sparkle-2.9.6 --container --delta tampered
python3 scripts/smoke_macos_updater.py --sdk-dir build/sparkle-2.9.6 --container --delta baseline
# 在完整包回退下载中取消，检查旧应用未被替换：
python3 scripts/smoke_macos_updater.py --sdk-dir build/sparkle-2.9.6 --container --cancel-fallback
# 源码工作台播放 15 秒后验证资源关闭与项目恢复；数据隔离：
smoke_dir="$(mktemp -d)"
KARAOKE_STUDIO_SETTINGS_DIR="$smoke_dir/settings" \
KARAOKE_STUDIO_LOG_DIR="$smoke_dir/logs" \
KARAOKE_STUDIO_TEMP_DIR="$smoke_dir/temp" \
python3 scripts/smoke_macos_updater.py --workbench-exit
```

手工上传到已有 release：

```powershell
gh release upload vX.Y.Z[.N] `
  dist\windows\KaraokeStudio-windows.zip `
  dist\windows\KaraokeStudio-windows.zip.sha256 `
  dist\windows\KaraokeStudio-windows.json `
  dist\windows\KaraokeStudio-windows-app.zip `
  dist\windows\KaraokeStudio-windows-app.zip.sha256 `
  dist\windows\KaraokeStudio-windows-runtime.zip `
  dist\windows\KaraokeStudio-windows-runtime.zip.sha256 `
  KaraokeStudio-macos.zip `
  KaraokeStudio-macos-appcast.xml `
  KaraokeStudio-macos-deltas.zip
```

没有生成增量容器时，手工上传命令省略最后一行。
