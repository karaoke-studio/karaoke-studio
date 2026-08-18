# 开发与运维参考 / Developer & Ops Reference

面向开发者与需要排查问题的高级用户。面向普通用户的介绍在仓库根目录的 [`README.md`](../README.md)；
协作约定与代码规范在 [`AGENTS.md`](../AGENTS.md)；发布流程在 [`release-process.md`](release-process.md)。

---

## 1. 命令行用法

命令行**只覆盖 Hi-Res 混流**这一步，其余模块必须走图形界面。

触发条件（[`krok_helper/cli.py`](../krok_helper/cli.py)）：给了 `--video`，且 `--on-audio` / `--off-audio` 至少给了一个，且没带 `--gui`。不满足就一律启动 GUI。

### 参数表

| 参数 | 说明 |
|---|---|
| `project`（位置参数） | 启动 GUI 并直接打开该工程，支持 `.sug` 与 `.yurika` |
| `--video` | 字幕视频路径 |
| `--on-audio` | 原唱无损音频路径 |
| `--off-audio` | 伴奏无损音频路径 |
| `--output-dir` | 输出目录，默认用字幕视频所在目录 |
| `--ffmpeg-dir` | FFmpeg 所在目录（查找顺序见 §3） |
| `--output-name-mode` | `fixed` / `template` / `video_name` |
| `--on-name-template` | 原唱输出模板，不用写扩展名 |
| `--off-name-template` | 伴奏输出模板，不用写扩展名 |
| `--gui` | 带参数时强制启动图形界面 |

另有两个 `argparse.SUPPRESS` 的隐藏参数 `--package-spawn-smoke` / `--package-gpu-smoke`，供
`scripts/build_windows.bat` 在打包后自检用，实现在
[`krok_helper/package_smoke.py`](../krok_helper/package_smoke.py)，日常不用管。

### 示例

最基本用法：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac"
```

FFmpeg 不在 `PATH` 里时额外指定目录：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac" `
  --ffmpeg-dir "D:\tools\ffmpeg\bin"
```

自定义命名：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac" `
  --output-name-mode template `
  --on-name-template "{video_name}_orig_master" `
  --off-name-template "{video_name}_inst_master"
```

没显式传命名参数或 FFmpeg 目录时，命令行会先读本地保存的设置，读不到再用内置默认值。

---

## 2. 输出命名规则

实现在 [`krok_helper/pipeline.py`](../krok_helper/pipeline.py)。输出一律是 `.mkv`。

| 模式 | 行为 |
|---|---|
| `fixed` | 固定输出 `on_vocal.mkv` / `off_vocal.mkv` |
| `template` | 按模板生成，默认 `{video_name}_on` / `{video_name}_off` |
| `video_name` | 等价于用默认模板的 `template` |

模板占位符只支持 `{video_name}` 与 `{audio_name}`，会自动剔除 Windows 非法文件名字符 `<>:"/\|?*`。

**多条伴奏**（[`resolve_off_output_paths`](../krok_helper/pipeline.py)）：一条伴奏出一个视频。
只放一条、且模板里没用到 `{audio_name}` 时，命名与单伴奏时代**完全一致**，不会打乱老用户的习惯；
放了两条以上才会改用 `{video_name}_{伴奏文件名}` 来区分。

音频侧固定规范化为 **FLAC 32-bit / 2 声道**，采样率低于 `MIN_HIRES_SAMPLE_RATE`（48 kHz）时提到
48 kHz；音轨标题按 `Hi-Res Audio (FLAC 32bit/{sample_rate}Hz)` 写入。视频、字幕、附件等非音频轨
原样保留，原始音轨丢弃，容器加 `+faststart`。视频与音频时长相差超过 2 秒会告警但继续处理。

---

## 3. FFmpeg / FFprobe / FFplay 查找顺序

实现在 [`krok_helper/ffmpeg.py`](../krok_helper/ffmpeg.py) 的 `find_tool()`：

1. **如果配置了 FFmpeg 目录**：先查 `<目录>/<工具名>`，再查 `<目录>/bin/<工具名>`（Windows 下自动补 `.exe`）
2. 都没命中才回退到 `shutil.which()`，也就是系统 `PATH`

也就是说 **你在界面或 `--ffmpeg-dir` 里指定的目录优先级高于 `PATH`**。设置里留空时才纯靠 `PATH`。

> ⚠️ 已知文案缺陷：`--ffmpeg-dir` 的 argparse 帮助文本与 `config.py` 里的输入框占位符目前都写成
> 「系统 PATH 优先」，与实际实现相反。行为以本节为准。

---

## 4. 数据与路径

### 改名后的目录迁移

应用从 `Karaoke Studio` 改名为 `Lin-K Lyrics` 后，用户数据目录也跟着换了位置。
[`krok_helper/app_paths.py`](../krok_helper/app_paths.py) 的 `migrate_app_data_dir()`
在启动早期用 `os.rename` 把整个旧目录搬到新名下 —— 同盘原子、不拷贝，几 GB 的 AI 模型
不用重下。幂等条件是「新目录已存在就跳过」，刻意不用标记位（标记位本身要存在被迁移的
`settings.json` 里，会形成先有鸡还是先有蛋的问题）。

> ⚠️ **调用时机**：必须在 `configure_application_logging()` **之前**。日志会在新目录下
> 建 `logs/`，一旦它先跑，「新目录不存在」的前提就被破坏，迁移会被永久跳过、用户数据
> 全留在旧目录。`app.py` 与 `krok_helper/__main__.py` 两个入口都已按这个顺序接好。

迁移失败（目录被占用、跨盘）不致命：`settings.json` 仍有 `LEGACY_APP_NAMES` 的读取兜底，
Cookie 与 `%LOCALAPPDATA%` 下的外部 PyMSS 环境各自也有回退路径。
`LEGACY_APP_NAMES` 的顺序是「越新越靠前」，不可颠倒。

规格见 [`tests/test_app_data_migration.py`](../tests/test_app_data_migration.py)。

### 设置文件

`get_settings_path()`（[`krok_helper/settings.py`](../krok_helper/settings.py)）按以下顺序解析：

1. `$KARAOKE_STUDIO_SETTINGS_DIR/settings.json`（环境变量覆盖，测试隔离用）
2. Windows 且有 `%APPDATA%`：`%APPDATA%\<应用名>\settings.json`，应用名取
   `$KARAOKE_STUDIO_SETTINGS_APP_NAME`，默认 `Lin-K Lyrics`
3. `$XDG_CONFIG_HOME/karaoke-studio/settings.json`
4. `~/.config/karaoke-studio/settings.json`

`LEGACY_APP_NAMES = ("Karaoke Studio", "Karaoke Helper")` 下的设置会被当作 legacy 路径读取兜底，顺序「越新越靠前」不可颠倒。

> ⚠️ **`$KARAOKE_STUDIO_SETTINGS_DIR` 被指定时，legacy 兜底整体关闭**（`get_legacy_settings_paths()` 返回空列表）。这个环境变量的语义是「就用这个目录」，不该在它还没有 settings.json 时偷偷回退到 `%APPDATA%` 下的历史目录。
> 漏掉这条会让测试隔离形同虚设 —— 隔离目录初始是空的，于是每个用例都读到开发机上的真实配置，并可能把测试值写回去。
`settings.json` 解析失败时会先备份成 `settings.json.corrupt-<时间戳>`，主窗口起来后弹窗告知用户，
不会静默丢配置。

除扁平字段外，设置里还有这些命名空间：`updater`、`lyrics_timing`、`lyrics_timing_dictionary`、
`lyrics_timing_singers`、`lyrics_timing_network_dictionary`、`subtitle_render`、`pymss`。

### 日志

`get_log_dir()`（[`krok_helper/logging_config.py`](../krok_helper/logging_config.py)）：

1. `$KARAOKE_STUDIO_LOG_DIR`
2. `$KARAOKE_STUDIO_SETTINGS_DIR/logs`
3. Windows：`%APPDATA%\<应用名>\logs`
4. `$XDG_STATE_HOME/karaoke-studio/logs`
5. `~/.local/state/karaoke-studio/logs`

日志文件名 `lin-k-lyrics.log`（常量 `logging_config.LOG_FILE_NAME`），另有一个独立的 native
崩溃日志。用户可从 **全局设置 → 关于 → 打开日志目录** 直达。

### 其他路径

| 用途 | 位置 |
|---|---|
| 打轴模块缓存 | `<设置目录>/lyrics_timing_cache`，通过 `SUG_CACHE_DIR` 注入给 SUG |
| AI 打轴缓存 | 同上目录下，由宿主的 `KaraokeAiTimingHost` 注入 |
| B 站 Cookie | `%APPDATA%\Lin-K Lyrics\video_download\bilibili_cookies.txt` |
| 人声分离运行时 | 默认 `<便携基准>/ai_runtime`（打包版是 exe 所在目录，源码运行是当前工作目录）。目录在基准内时按**相对路径**保存，整个文件夹搬家不会失效 |
| 分离运行时清单 | `<安装目录>/manifests/runtime-manifest.json` |
| 更新器临时目录与日志 | `%TEMP%\KaraokeStudioUpdater\`，日志 `updater.log` |
| 旧版 SUG 数据迁移源 | `~/.strange_uta_game` |

---

## 5. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `KARAOKE_STUDIO_SETTINGS_DIR` | — | 覆盖设置目录，测试隔离靠它 |
| `KARAOKE_STUDIO_SETTINGS_APP_NAME` | `Lin-K Lyrics` | 覆盖 `%APPDATA%` 下的应用目录名 |
| `KARAOKE_STUDIO_LOG_DIR` | — | 覆盖日志目录 |
| `SUG_CACHE_DIR` | `<设置目录>/lyrics_timing_cache` | 由宿主写入，供内嵌 SUG 使用 |
| `KROK_SUBTITLE_ASYNC_PREVIEW` | `1`（开） | 字幕异步预览调度 |
| `KROK_SUBTITLE_GPU_PREVIEW` | Windows 交互会话下 `1`，`QT_QPA_PLATFORM` 为 `offscreen`/`minimal` 时 `0` | Direct2D 共享内存预览 |
| `KROK_SUBTITLE_GPU_EXPORT` | Windows 下开 | Direct2D 导出后端 |
| `KROK_SUBTITLE_NATIVE_RENDER` | `0`（关） | native 预览实验开关，产品 UI 不暴露 |
| `KROK_SUBTITLE_GPU_FORCE_WARP` | `0` | 强制走 WARP 软件适配器，排查显卡问题用 |
| `KROK_SUBTITLE_RENDER_WORKERS` | — | 覆盖渲染进程数 |
| `KROK_PYMSS_RUNTIME_VARIANT` / `KROK_PYMSS_RUNTIME_MANIFEST_URL` | — | 覆盖人声分离运行时的变体与清单地址 |

`krok_helper/subtitle_render/engine/` 下还有一批 `KROK_SUBTITLE_*` 的缓存与分层开关
（`LAYOUT_CACHE` / `GLOW_CACHE` / `RENDER_BANDS` / `GPU_EXPORT_DIAGNOSTICS` 等），
默认值直接看各自的 `os.environ.get` 调用点，仅供性能调试。

---

## 6. 打包

### Windows

```bat
scripts\build_windows.bat
```

脚本流程：校验依赖版本 → 校验内嵌 SUG 源码路径 → 构建并用 WARP 冒烟测试 Direct2D 渲染侧车
（`run_native_renderer_smoke.ps1`）→ 构建 `Updater.exe` → 拉取 aria2c（`fetch_aria2.py`，SHA-256 固定）
→ 把 SUG 的 `VARIANT` 打成 `noWinIME` → PyInstaller `--onedir --windowed` → 裁剪产物 → 刷新 MSVC 运行时
DLL → 改名为 `Lin-K Lyrics` → **复制一份旧名 `Karaoke Studio.exe` 兼容副本** →
复制更新器与 SUG 源码树（AI 打轴 worker 需要）→ 校验包内容 →
跑 `--package-spawn-smoke` 与 `--package-gpu-smoke` → 调 `build_parts.py`。

成品在 `dist\windows\Lin-K Lyrics\`，主程序 `Lin-K Lyrics.exe`，同目录另有同内容的 `Karaoke Studio.exe` 兼容副本 —— 缺了它，存量客户端的更新会整包失败并且再也重启不起来。

仓库里**没有提交 `.spec` 文件**，PyInstaller 完全靠命令行参数驱动。`torch` / `pymss` / `pymss_core`
是显式 `--exclude-module` 的 —— 它们属于运行时按需下载的托管环境，不进安装包。

### 增量分包

`scripts/build_parts.py` 产出整包 zip、`-app.zip`、`-runtime.zip` 与 `KaraokeStudio-windows.json`
清单（内容哈希，schema 1）。应用内更新器拿这份清单和本地
`_internal/.installed_manifest.json` 比对，只下发生变化的那部分；清单异常时回退整包。

### 独立更新器

`krok_helper/updater_app/build_updater.py` 单独打成 onefile windowed 的 `Updater.exe`，
由 `build_windows.bat` 调用并复制进产物目录。

### macOS

```bash
scripts/build_macos.command
```

会把 SUG 的 `VARIANT` 打成 `mac` 并在结束时还原。**该路径尚未在真实 macOS runner 上验证过**，
按实验性对待。

### 发布

统一走 `python scripts/release.py prepare X.Y.Z[.N]`（同步改 `APP_VERSION`、README 顶部
「当前版本」、插入 CHANGELOG 占位段），补完 CHANGELOG 后跑
`python scripts/release.py notes X.Y.Z[.N]` 生成中文 release body。完整流程见
[`release-process.md`](release-process.md)。

> ⚠️ `release.py` 用正则 `^当前版本：\`...\`$` 在 README.md 里定位版本行，匹配不到会直接退出。
> 改 README 时不要动那一行的格式（必须顶格、独立成行）。

---

## 7. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests\
```

仓库根目录没有 `pytest.ini` / `pyproject.toml`，pytest 跑默认配置，测试环境全部由
[`tests/conftest.py`](../tests/conftest.py) 兜底：

- 导入期就设 `QT_QPA_PLATFORM=offscreen`
- session 级 autouse fixture 钉住一个**进程级 `QApplication`**。各测试文件惯用
  `QApplication.instance() or QApplication([])` 自建应用，模块跑完引用被 GC 时 PyQt6 会连带销毁
  qfluentwidgets 的全局 `qconfig` 单例，导致后续测试撞
  `RuntimeError: wrapped C/C++ object of type QConfig has been deleted`。钉住引用即可消除这种
  「单跑全过、合跑互相污染」。
- autouse fixture 给每个测试独立的 `KARAOKE_STUDIO_SETTINGS_DIR`（`test_settings_atomic_io` 除外）

子模块有自己的 `pytest.ini` 与测试套件，改 SUG 的话在子模块目录里单独跑。

---

## 8. 子模块工作规则

`krok_helper/lyrics_timing` 是 [StrangeUtaGame](https://github.com/karaoke-studio/StrangeUtaGame)
的 git 子模块。[`krok_helper/__init__.py`](../krok_helper/__init__.py) 在导入时会把
`lyrics_timing/src` 前置到 `sys.path`，所以 `import strange_uta_game` 直接可用。

- **不要直接改 `krok_helper/lyrics_timing/src/strange_uta_game/`**。先给 SUG 提 PR，合并后在本仓库
  更新子模块指针（gitlink）。
- 嵌入契约见子模块内的 `docs/EMBEDDING.md`：宿主调 `MainWindow.for_embedding(...)`，SUG 从顶层
  `MSFluentWindow` 降级为普通子控件，配置来自宿主注入的 `SettingsProvider` 而非自己的
  `config.json`，缓存目录来自 `SUG_CACHE_DIR`。
- 宿主还注入了 `KaraokeAiTimingHost`，让 SUG 的 AI 打轴复用工作台的人声分离后端。
- **版本号约定**：工作台自身的改动走三段 SemVer；**只同步子模块**的发版用第 4 段递增
  （如 `4.2.6.3` → `4.2.6.4`）。

---

## 9. 性能基线与诊断脚本

`scripts/` 下有一批性能与一致性脚本，改字幕渲染相关代码时用得上：

| 脚本 | 用途 |
|---|---|
| `bench_render.py` | 渲染性能门禁基线 |
| `bench_native_renderer.py` · `bench_parallel_paint.py` | native 侧车与并行绘制基准 |
| `benchmark_gpu_renderer.py` · `benchmark_gpu_export_pipeline.py` · `benchmark_gpu_preview_scheduler.py` | GPU 三条链路的基准 |
| `compare_export_backends.py` · `compare_preview_backends.py` | CPU / GPU 后端画面一致性比对 |
| `compare_gpu_n3_reference.py` · `compare_gpu_painter_corpus.py` | 对 N3 参考图与 painter 语料的回归比对 |
| `probe_gpu_renderer.py` · `probe_multiprocess_preview.py` · `probe_native_preview_stats.py` | 现场探针 |
| `profile_preview.py` · `stress_gpu_preview_gui.py` | 预览性能剖析与压测 |

具体的门禁口径与「值键缓存」原则见 [`AGENTS.md`](../AGENTS.md)。
