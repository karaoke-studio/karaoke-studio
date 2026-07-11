# Karaoke Studio 自动更新机制

本文面向用户、维护者和后续接手更新器的开发者。发版操作以
[`release-process.md`](./release-process.md) 为准；设计取舍与跨版本兼容性分析见
[`工作台更新器完善计划.md`](./工作台更新器完善计划.md)。

## 1. 用户视角流程

1. 应用启动后按设置执行异步检查；防抖期内静默跳过，不阻塞主界面。
2. 检查器按用户配置的更新源依次请求 GitHub Releases。API 全部遇到 403 时，
   还会用 GitHub `releases/latest` 的 302 跳转探测最新 tag。
3. 新版本存在时弹出更新说明。跨多个版本升级会按版本分节展示所有中间版本日志。
4. 用户点「立即更新」后进入更新准备窗。主程序先读取远端 manifest；如 app part
   有变化，先下载并换入新版 `Updater.exe`，然后退出并启动它。
5. `Updater.exe` 在临时目录运行，等待主进程退出，优先按 manifest 做增量替换；
   manifest 不可用或 part 失败时自动回退全量 zip。
6. 替换前会备份，失败则回滚；成功后重启 `Karaoke Studio.exe`。

更新器只管理安装目录中的 `Karaoke Studio.exe`、`Updater.exe` 与 `_internal/`。
用户设置和工作文件不在更新范围内。日志位于
`%TEMP%\KaraokeStudioUpdater\updater.log`。

## 2. 组件与职责

| 组件 | 职责 |
|---|---|
| `krok_helper/updater/settings.py` | 更新开关、防抖、源顺序、代理、跳过版本等设置 |
| `krok_helper/updater/sources.py` | 官方源与三个镜像源的 API/资产 URL 构造 |
| `krok_helper/updater/http_client.py` | HTTP 请求、续传下载、重试、取消与逐源接力 |
| `krok_helper/updater/worker.py` | 异步检查、四段版本比较、302 兜底、跨版本日志聚合 |
| `krok_helper/updater/installer.py` | 生成交接参数、Updater 自更新、临时副本与进程启动 |
| `krok_helper/updater/progress_window.py` | 主程序退出前的下载/准备进度和取消入口 |
| `krok_helper/updater_app/` | 独立 Updater 包装层；核心实现复用 SUG updater_app |
| `scripts/build_parts.py` | 构建 app/runtime parts、manifest 和出厂清单 |

Windows 支持检查与自动安装；macOS 当前只支持检查和全量包发布，不走独立更新器。

## 3. 配置项

更新设置保存在工作台 `settings.json` 的 `updater` 节点。旧配置缺字段时会与默认值
合并，无需迁移：

```json
{
  "updater": {
    "enabled": true,
    "check_on_startup": true,
    "min_check_interval_hours": 8,
    "source_order": ["github", "ghproxy", "gh-proxy", "ghproxy-net"],
    "proxy": {
      "mode": "system",
      "manual_url": ""
    },
    "skipped_version": "",
    "last_seen_version": "",
    "last_check_at": 0
  }
}
```

- `enabled`：自动更新总开关。
- `check_on_startup`：是否在启动时检查。
- `min_check_interval_hours`：启动检查防抖时间；默认 8 小时，设为 0 关闭防抖。
- `source_order`：更新源优先级；未知项会丢弃，缺少的内置源自动补到末尾。
- `proxy.mode`：`off` / `system` / `auto` / `manual`。
- `proxy.manual_url`：手动代理地址；省略 scheme 时自动补 `http://`。
- `skipped_version`：用户选择跳过的版本。
- `last_seen_version`、`last_check_at`：最近检查状态和防抖时间戳。

代理的「系统」模式读取 Windows 注册表；「自动」模式先用系统代理，再扫描常见本地
代理端口；「关闭」模式会阻止 HTTP 客户端和子进程继承环境代理。

## 4. 更新源

| ID | 说明 |
|---|---|
| `github` | GitHub 官方 API / Release |
| `ghproxy` | `mirror.ghproxy.com` 镜像 |
| `gh-proxy` | `gh-proxy.com` 镜像 |
| `ghproxy-net` | `ghproxy.net` 镜像 |

检查和下载均按 `source_order` 接力。镜像不可用只会切换到下一源，不会改变资产名或
manifest 协议。

## 5. 增量 part 划分

每个 Windows release 必须同时提供全量资产和增量资产：

| 资产 | 内容与用途 |
|---|---|
| `KaraokeStudio-windows.zip` | 完整 onedir 包，全量兜底；内含出厂 `.installed_manifest.json` |
| `KaraokeStudio-windows.zip.sha256` | 全量 zip 校验 |
| `KaraokeStudio-windows.json` | schema 1 manifest；名字由存量 Updater 从 zip 名派生，不可改 |
| `KaraokeStudio-windows-app.zip` | `Karaoke Studio.exe`、`Updater.exe`、`_internal/krok_helper`、`_internal/strange_uta_game` |
| `KaraokeStudio-windows-app.zip.sha256` | app part 文件校验 |
| `KaraokeStudio-windows-runtime.zip` | `_internal/` 中除应用代码和本地清单外的运行库 |
| `KaraokeStudio-windows-runtime.zip.sha256` | runtime part 文件校验 |

manifest 中 part 的 `sha256` 是解压内容哈希，用于判断是否需要替换；旁边的
`.sha256` 是 zip 文件哈希，用于下载校验，两者用途不同。

`app` 每版重新构建。依赖指纹与 `build.runtime_profile` 都不变时，CI 必须复用
上一版 runtime zip 原文件；profile 用于标记 PyInstaller 模块/插件收集策略，避免
包版本未变时误复用缺少新 Qt 组件的旧 runtime。
`--require-runtime-reuse` 会在「应复用但找不到旧 zip」时中止构建，避免无意义地改变
runtime 内容哈希、迫使所有用户重下运行库。

没有本地清单的旧客户端第一次看到 manifest 时，会下载 app 和 runtime 两个 part，
写入 `.installed_manifest.json`；之后才会获得只下载变化 part 的收益。parts 是完整内容
快照而非补丁链，因此旧版本可直接升级到最新版。

## 6. Updater 自更新与全量兜底

主程序启动 Updater 前会先读取 `parts.app.asset`，比较远端与本地 app 内容哈希：

- 哈希一致：不下载，直接交接。
- 临时 parts 目录已有同哈希 zip：直接复用，不重复下载。
- 哈希变化：下载 app part、校验内容哈希、提取并替换安装目录中的 `Updater.exe`。
- manifest 或自更新失败：保留旧 Updater，继续交接，由其尝试增量或全量路径。
- 用户取消：停止准备，不退出主程序。

独立 Updater 的增量路径如遇 manifest 不存在、schema 不支持、part 下载或哈希失败，
会自动回退全量 zip。替换文件失败会从 `.bak` 恢复。全量 zip 必须永久保留，这既是
失败兜底，也是老客户端的唯一自动下载入口。

## 7. 失败处理矩阵

| 场景 | 用户侧行为 | 数据/降级行为 |
|---|---|---|
| 启动自动检查全部源失败 | 静默，不打断启动 | 记录日志，下次再试 |
| 手动检查全部源失败 | 中文错误弹窗列出逐源结果 | 403 单独提示共享出口 API 限流 |
| API 全 403 | 尝试 GitHub latest 302 | 能探测 tag 时继续构造 release |
| 防抖期内启动检查 | 无提示 | 不发请求 |
| 用户跳过该版本 | 不再提示该版本 | 新版本出现后恢复提示 |
| 自更新 manifest 不可用 | 准备流程继续 | 使用旧 Updater |
| app part 下载/校验失败 | 准备流程继续 | 旧 Updater 再尝试增量/全量 |
| 用户在准备窗取消 | 留在当前版本 | 不退出主程序，不替换文件 |
| Updater manifest 不可用或 schema 过新 | 更新仍继续 | 回退全量 zip |
| part 下载或内容哈希失败 | 更新仍继续 | 回退全量 zip |
| 全量下载失败 | 控制台提示失败 | 退出码 3，安装目录不变 |
| 全量 SHA-256 不匹配 | 提示校验失败 | 退出码 4，不替换文件 |
| 替换 EXE / `_internal` 失败 | 提示更新失败 | 自动恢复备份 |
| 主进程 30 秒未退出 | Updater 强制继续 | 文件仍锁定时写入失败并回滚 |

## 8. 发布不变量

下列契约已经被存量客户端硬编码，任何一项都不可改：

- 全量 zip、app/runtime part、manifest 的现有资产名。
- 主程序名 `Karaoke Studio.exe`、更新器名 `Updater.exe`。
- onedir 的「根目录 EXE + `_internal/`」布局。
- tag 格式 `vX.Y.Z[.N]`。
- KS 的四段 `_version_key` 比较语义；`3.1.7.4` 必须大于 `3.1.7`。
- 已发布 tag 不得 force-push。

## 9. 发布 checklist

1. 确认流程 A（工作台）或流程 B（仅 SUG gitlink），不要同一 release 混发。
2. 检查工作区、分支与 submodule：`git status --short --branch`、
   `git submodule status`。
3. 运行 `python scripts/release.py prepare X.Y.Z[.N]`；它会同步
   `APP_VERSION`、README「当前版本」并插入 CHANGELOG 中文占位段。
4. 补全该版本 CHANGELOG；删除没有内容的分类和所有“待补充”文本。
5. 运行完整测试和需要的 Qt 冒烟测试。
6. 运行 `python scripts/release.py notes X.Y.Z[.N]`，检查
   `dist/release-notes-vX.Y.Z[.N].md` 为完整中文内容。
7. 完成 release commit，推送 `main`，再创建并推送 `vX.Y.Z[.N]` tag。
8. 监控 release workflow 三个 job 全绿，并核对 Windows 7 个资产及 macOS 全量 zip。
9. CI 创建 Release 后，立即执行脚本打印的 `gh release edit ... --notes-file ...`，
   再用 `gh release view ... --json body --jq .body` 验证中文 body。
10. 从旧版客户端验收更新。增量 + 自更新首发必须额外验证「无本地清单的首次增量」：
    下载 app + runtime、写入清单、重启成功，并检查 updater.log。

`dist/release-notes-*.md` 是本地发版产物，不提交仓库。

## 10. 测试

```powershell
C:\Python314\python.exe -m pytest tests\
```

更新器测试按职责拆分为 sources、settings、proxy、version、manifest、installer、
HTTP/check、self-update、Updater apply 和 parts build 等文件。涉及发布资产或安装替换的
改动，除单元测试外还需按 [`release-process.md`](./release-process.md) 的自动更新验收
执行真实旧客户端测试。
