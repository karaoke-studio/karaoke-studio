# Lin-K Lyrics 自动更新机制

本文面向用户、维护者和后续接手更新器的开发者。发版操作以
[`release-process.md`](./release-process.md) 为准；设计取舍与跨版本兼容性分析见
[`工作台更新器完善计划.md`](./工作台更新器完善计划.md)。

## 1. Windows 用户视角流程

1. 应用启动后按设置执行异步检查；防抖期内静默跳过，不阻塞主界面。
2. 检查器按用户配置的更新源依次请求 GitHub Releases。API 全部遇到 403 时，
   还会用 GitHub `releases/latest` 的 302 跳转探测最新 tag。
3. 新版本存在时弹出更新说明。跨多个版本升级会按版本分节展示所有中间版本日志。
4. 用户点「立即更新」后进入更新准备窗。主程序先读取远端 manifest；如 app part
   有变化，先下载并换入新版 `Updater.exe`，然后退出并启动它。
5. `Updater.exe` 在临时目录运行，等待主进程退出，优先按 manifest 做增量替换；
   manifest 不可用或 part 失败时自动回退全量 zip。
6. 替换前会备份，失败则回滚；成功后按旧 app 传入的 `--app-exe` 重启主程序
   （新装是 `Lin-K Lyrics.exe`，从改名前版本升上来的则是 `Karaoke Studio.exe`）。

更新器只管理安装目录中的 `Lin-K Lyrics.exe`（及其同内容的 `Karaoke Studio.exe` 兼容副本）、`Updater.exe`、
`krok_subtitle_renderer.exe` 与 `_internal/`。
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
| `krok_helper/updater/macos_metadata.py` | 公钥校验与包内原生框架加载检查 |
| `krok_helper/updater/macos_feed.py` | macOS 版本映射与签名清单校验 |
| `krok_helper/updater/macos_transport.py` | 缓存下载与本机 HTTP 资源服务 |
| `krok_helper/updater/macos_sparkle.py` | 原生 Sparkle 协议适配与安装回调 |
| `krok_helper/updater/macos_controller.py` | 下载子进程监督、进度界面与退出交接 |
| `scripts/prepare_macos_updater.py`、`scripts/build_macos_appcast.py` | Sparkle 打包、代码签名与更新包签名 |

Windows 使用独立 Updater；macOS 使用 Sparkle 2 安装完整 `.app`，仍复用上述
检查器、中文更新说明、代理与源顺序设置。macOS 实现见下节。

### 2.1 macOS：缓存下载与 Sparkle 安装

`macos_transport.py` 先通过现有 HTTP 客户端下载本次发布的
`KaraokeStudio-macos-appcast.xml`，优先查找当前构建版本对应的直接 delta。
发布端仅选择同时提供完整包和 macOS appcast 的正式旧版本，排除未接入自动更新及缺密钥发布的版本；
筛选后取同一主版本、次版本下最近 20 个，第四段版本也计入，不足 20 个则全部取。
首次启用自动更新时没有符合条件的历史基线，只发布完整包和签名清单。
CI 通过 `--app` 直接复用本次构建的 `.app`，只解压下载的历史包；单独以 ZIP 运行脚本时仍可解压目标包。
客户端一次只应用一个补丁，不串联历史版本。
跨次版本、超出覆盖范围或没有有效补丁时，下载 `KaraokeStudio-macos.zip`。
完整包支持代理、失败换源和同一源内的断点续传；换源时清空残片，成功后核对大小及 API 提供的
SHA256。清单只接受用户已确认的版本、构建号和固定的 macOS 资源名。

各 delta 先独立签署 Ed25519 签名，再原样写入 `ZIP_STORED` 容器
`KaraokeStudio-macos-deltas.zip`；appcast 的 `lk:deltas` 扩展记录基线版本、
容器大小及各补丁的偏移、长度、SHA256 和签名。旧客户端忽略扩展，继续全量更新。
新客户端只用 HTTP Range 获取选中补丁的原始字节，要求返回准确的 `206`、
`Content-Range` 和长度；忽略 Range 或返回错误数据的节点会被跳过，不下载整个容器。
截段摘要用于发现传输损坏，最终真实性仍由 Sparkle 使用安装中的公钥验签。
索引无效或所有节点均无法提供正确片段时改用完整包，不做补丁与完整包的体积比较。
下载在 spawn 子进程内执行，由 Qt 线程监督。取消时主动终止并回收下载进程，
关闭连接后删除父进程持有的缓存；无需等待 DNS、响应头、慢速数据或重试计时。

完整文件就绪后，随机端口的本机 HTTP 服务只在 `127.0.0.1` 监听，并只提供
随机令牌路径下的清单、缓存 ZIP 和选中的 `.delta`。它没有目录浏览、外部转发或任意文件读取入口。
`macos_sparkle.py` 用 PyObjC 调用 Sparkle 的 `SPUUpdater`，适配原生 user driver
和 delegate 协议。`macos_controller.py` 管理下载进程、进度窗与应用退出，
通过回调接入原生层。Sparkle 下载请求会被改写为本机缓存地址。
补丁和完整包使用不同地址；补丁会话请求尚未准备的完整包时，本机服务立即返回不可用，
不挂起请求等待网络下载。补丁失败后，控制器先结束原生会话、关闭本机服务并清理补丁缓存，
再由受监督下载子进程按原代理和源顺序准备完整包；下载完成后启动只提供完整包的新会话。
补丁失败最多触发一次完整包回退。取消更新、取消系统管理员授权或恢复快照保存失败不触发回退，也不会为了取消再访问网络。

Sparkle 用已安装应用 `Info.plist` 中的 `SUPublicEDKey` 验证 Ed25519 签名，
`SUVerifyUpdateBeforeExtraction` 要求解压前验签。安装、必要的管理员授权、
替换和重新启动由 Sparkle 的安装器负责。接受安装前，主程序确认最新 SUG 和字幕
恢复快照写入成功；保存失败或超时会拒绝安装，保留工作台和未保存的修改。
应用模态进度窗防止安装期间继续编辑。安装器准备好后，主程序停止 SUG、波形对齐、
字幕预览及音频合成资源，保留恢复文件，再交还退出控制，不调用 Windows 的两秒强制
退出计时器。Sparkle 确认交接后调用 Qt 正常退出；不在 Qt 退出回调中再次调用
Cocoa 的 `terminate`，避免重入导致崩溃。更新结束或取消后清理临时缓存与本机服务。

`prepare_macos_updater.py` 获取固定版本和 SHA256 的官方 Sparkle SDK，保留
framework 的符号链接与辅助程序，再从内到外签署应用。构建末尾用实际
冻结应用执行 `--package-updater-smoke`，验证框架和 PyObjC 能加载
`SPUUpdater`。本地构建设置 `KROK_SPARKLE_PUBLIC_ED_KEY` 时启用更新器；
未设置时不下载或内置 Sparkle，跳过其加载检查，应用仍提示更新并引导手动下载。
CI 缺少公钥或私钥时同样发布普通包，不阻断 macOS 或 Windows 发布；
两者齐全但格式错误或不匹配时仍报错。应用代码签名沿用现有 ad-hoc 方式。更新 ZIP 的
Ed25519 发布签名配置见
[`release-process.md` 的 macOS 构建段](./release-process.md#附录--本地手工构建)。

`updater/__init__.py` 按需加载公共导出，版本映射和签名清单模块只依赖标准库。
构建与 ZIP 签名脚本可读取元数据而不提前导入 Qt。Windows
主程序和独立 Updater 的打包配置均排除 macOS 更新模块与 PyObjC。

旧 macOS 客户端未包含 Sparkle，需要先手动安装带此实现的一版；之后才可自动
更新。开发源码启动不执行安装，必须从包含公钥和 Sparkle 的 `.app` 运行。

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
| `ghproxy` | `ghfast.top` 镜像 |
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
| `KaraokeStudio-windows-app.zip` | `Lin-K Lyrics.exe`、`Karaoke Studio.exe`（兼容副本）、`Updater.exe`、`krok_subtitle_renderer.exe`、`_internal/krok_helper`、`_internal/strange_uta_game` |
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
| 全量下载失败 | GUI 更新器显示失败和日志 | 安装目录不变，用户关闭更新器 |
| 全量 SHA-256 不匹配 | 提示校验失败 | 退出码 4，不替换文件 |
| 替换 EXE / `_internal` 失败 | 提示更新失败 | 自动恢复备份 |
| 安装目录被其他程序占用（3s 等差重试共 6 次耗尽） | 更新器用 Restart Manager 点名占用进程，弹窗可选结束占用进程后重试；系统与安全软件进程只展示、不提供结束入口 | 跳过注定失败的全量下载（全量需重命名同一目录）；增量已回滚，旧版保持可用 |
| 主进程 30 秒未退出 | Updater 强制继续 | 文件仍锁定时写入失败并回滚 |

## 8. 发布不变量

下列契约已经被存量客户端硬编码，任何一项都不可改：

- 全量 zip、app/runtime part、manifest 的现有资产名 —— 即 `KaraokeStudio-windows*`
  / `KaraokeStudio-macos.zip` 这套**改名前**的前缀。应用已更名为 Lin-K Lyrics，
  但资产名刻意保持原样：旧 worker 硬编码全量 zip 名，并从 zip 名派生 manifest 名。
- **`Karaoke Studio.exe` 随 4.3.x 整条线分发为止**（改名后它是 `Lin-K Lyrics.exe`
  的同内容副本），期间必须列在 `build_parts.APP_TARGETS` 里；自 4.4.0 起由
  `build_parts.FIRST_LEGACY_FREE_VERSION` 版本闸自动停发（判定与残余风险见 §8.1
  末尾）。更新器名 `Updater.exe` 同样不可改。（2026-09 起新版主程序固定传新名、
  Updater 在新名会话成功后清理本地旧名副本，活跃安装已收敛到新名。）
- **全量回退路径必须回写包内全部根目录 EXE**（包内主程序名 + `krok_subtitle_renderer.exe`
  GPU sidecar），不只回写 `--app-exe` 指定的那一个。**前置校验必备项为新名主程序
  与 sidecar**（4.3.0 起的 Updater；此前已发布的 Updater 还把旧名副本算作必备，
  因此 4.3.x 及之前的包必须继续带旧名）——包里缺任何一个都按损坏包拒绝，不触碰磁盘。
  2026-09 事故：SUG 原版
  `apply_update` 只回写 `--app-exe` + `_internal`，sidecar 与另一份主程序名被静默
  跳过，走全量回退的用户得到「新 Python 代码 + 旧 schema sidecar」的混合安装，
  GPU 全量回退 Painter 且本地 manifest 谎报已是新版。修复与回归测试见
  `updater_app/main.py::_apply_workbench_update` 与
  `tests/test_workbench_updater_lock_guard.py`。
- onedir 的「根目录 EXE + `_internal/`」布局。
- tag 格式 `vX.Y.Z[.N]`。
- KS 的四段 `_version_key` 比较语义；`3.1.7.4` 必须大于 `3.1.7`。
- 已发布 tag 不得 force-push。

### 8.1 为什么旧名 EXE 不能删（以及 2026-09 起的迁移机制）

执行更新的是**用户机器上的旧版代码**，新版怎么写都救不了它：

1. 旧版客户端的 `updater/installer.py::find_app_exe_name()` 返回
   `Path(sys.executable).name` —— 也就是用户当前装着的那个文件名 —— 并作为
   `--app-exe` 传给 Updater。
2. `updater_app/main.py::apply_update()` 第一句就是
   `if not (new_root / app_exe).exists(): return False`。
3. 同文件的 `launch_main_app()` 更新后按同一个名字重启。

后果分两条路径，都很难看：

| 路径 | 症状 |
|---|---|
| 全量 zip | `apply_update` 直接失败并返回「更新包中找不到 Karaoke Studio.exe」。安装不受损，但该用户**从此再也无法自动更新** |
| 增量 | 文件写入并提交成功，随后 orphan cleanup 删掉旧名 EXE，`launch_main_app` 找不到主程序 —— **更新装上了却永远拉不起来**，桌面快捷方式一并失效 |

`SUPPORTED_MANIFEST_SCHEMA` 那个版本闸门救不了这种情况：把 schema 抬高只会把所有旧
客户端赶到全量路径，而全量路径恰恰是硬失败的那条。

护栏见 [`tests/test_rename_release_invariants.py`](../tests/test_rename_release_invariants.py)；
`scripts/build_parts.py` 在缺少兼容副本时会直接 `SystemExit`，`scripts/build_windows.bat`
复制失败时会中止构建。

#### 2026-09 迁移机制：新名会话 + 快捷方式迁移 + 旧名副本清理

为了让安装逐步收敛到新名、为将来停发旧名副本做准备：

- **主程序固定传新名**：`installer.find_app_exe_name()` 一律返回
  `DEFAULT_APP_EXE_NAME`（`Lin-K Lyrics.exe`），与实际启动的文件名无关。
  改名后的发布包双名并存，本地与包内新名都必然存在，Updater 按新名校验/回写/
  重启总是可行。存量客户端传上来的仍是旧名，它的更新不受影响。
- **清理只挂在成功拉起之后**：`_launch_main_app_workbench` 在新版本进程成功
  `Popen` 后才调用 `_cleanup_legacy_main_exe`——这是全量/增量/「已是最新」三条
  路径共用的唯一挂钩（全量 `_apply_workbench_update` 同时跳过旧名副本的回写，
  增量尾部不做清理）。清理先于成功启动会让「启动失败 + 旧名已删」的安装失去
  任何可用入口；统一后置则保证任何时刻安装里至少有一个能运行的 EXE。
- **安全闸 + 尽力而为的缓解**：唯一硬性安全闸是新名入口必须已存在——旧名可能是
  该安装唯一可运行入口（如降级旧 Updater 的全量更新只回写了旧名），没有新入口
  兜底时绝不清理。删除旧名本体前会先尽力迁移快捷方式
  （`_migrate_legacy_shortcuts`：PowerShell WScript.Shell 原地改写 .lnk 的
  TargetPath，覆盖桌面/开始菜单/快速启动/任务栏固定），但迁移失败（PowerShell
  缺失/被拒/超时）**不阻断清理**——产品决策：宁可留个别坏快捷方式也不留旧名
  副本，坏快捷方式用户重建一次即可。
- 删除被持续拒绝（旧名镜像仍被另一实例占用）时降级 rename 成 `.old` 交给
  下次更新的同一清理回收，纯 best-effort，绝不让清理失败影响更新结果。
- 迁移期 `APP_TARGETS` 与发布包**仍带旧名副本不动**：存量客户端的增量更新依赖
  manifest targets 里有它（否则 orphan cleanup 会在它的更新中途删掉它正在用的 EXE）。

**快捷方式兼容**：更新时会尽力把旧名快捷方式原地改指到新名 EXE（图标沿用目标
程序），多数用户无需重建；迁移工具失败的少数情况下旧名快捷方式失效，重建一次
即可（产品决策接受此代价，换取安装彻底收敛到新名）。残余盲区还有开始菜单磁贴
等极少数非 .lnk 入口。

#### 停发旧名副本：自 4.4.0 起，版本闸自动执行（2026-09-28 拍板）

原先设想的手工收尾清单已固化为版本闸，bump 到 4.4.0 后一切自动翻转，无需手工执行：

1. **判定单一事实源**：`build_parts.FIRST_LEGACY_FREE_VERSION = (4, 4, 0)`。
   `SHIP_LEGACY_APP_EXE` 在导入期按当前 `APP_VERSION` 求值（四段语义，
   `4.3.9.9` 仍属迁移期、`4.4.0.1` 已停发）；`APP_TARGETS`、`main()` 的存在性
   护栏（迁移期缺副本 / 停发期 dist 里多出副本都 `SystemExit`）与
   `build_windows.bat` 的「Creating legacy-name EXE copy」步骤全部跟随它，
   两端口径不可能分叉。护栏见 `tests/test_rename_release_invariants.py`。
2. **放宽必须先行**（随 4.3.0 提前发出）：`_apply_workbench_update` 的必备根目录
   EXE 前置校验只要求新名主程序 + GPU sidecar，旧名可选——执行 4.3.x → 4.4.0
   全量回退的正是 4.3.x 期间发出的 Updater，放宽晚于首个无旧名版本就会把它当
   损坏包拒绝。`_cleanup_legacy_main_exe` / `_migrate_legacy_shortcuts` 长期
   保留：存量安装里的旧名副本要靠它们回收。
3. **release body 头部提示**：`scripts/release.py` 自首个无旧名版本（4.4.0，含）
   起（`LEGACY_FREE_SINCE_VERSION` 同源闸）在横幅下方自动插入「老版本用户可
   重新下载完整包，或直接打开安装目录里的 Lin-K Lyrics.exe」提示；本地 `notes`
   与 CI 的 release body 走同一出口生成，
   两处截止版本有护栏测试对齐，CI 发布前另有 `check-notes` 校验步骤。
4. **残余风险**（接受的代价）：
   - 改名前安装、且在迁移版为最新版期间（4.2.8.9 自 2026-09-14 起）从未检查过
     更新的**旧名会话**用户，直更无旧名版本时：增量路径「装上了却拉不起来」
     （orphan cleanup 删了旧名 EXE、按旧名重启失败、快捷方式失效；手动进目录
     运行新名 EXE 或重装可恢复）；全量路径被通用 `--app-exe` 校验拒绝、停在
     旧版。只能手动重下完整包——release body 提示的受众就是这批人。
   - 4.2.8.9+ 用户（已收敛新名会话）无碍：增量路径本就不做三 EXE 校验；其旧
     Updater 的三 EXE 硬校验只拦全量回退，表现为更新失败、可重试，不砖。
   - 缩小残余的唯一手段：拉长 4.3.x（最后一批双名版本）作为最新版的时间，
     让沉默用户有机会先收敛到新名会话再迎接 4.4.0。

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
8. 监控 release workflow 三个 job 全绿，并核对 Windows 7 个资产、macOS 全量
   ZIP 及签名清单 `KaraokeStudio-macos-appcast.xml`。
9. CI 的 Publish Release job 会从当前 tag 的 `CHANGELOG.md` 自动提取中文正文，
   并在创建 Release 时通过 `body_path` 直接写入；再用
   `gh release view ... --json body --jq .body` 验证中文 body。
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
