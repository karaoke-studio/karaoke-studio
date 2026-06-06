# Changelog

本项目所有重大变更都记录在此。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

打轴模块（StrangeUtaGame）合并前的历史见
[`krok_helper/lyrics_timing/CHANGELOG.md`](./krok_helper/lyrics_timing/CHANGELOG.md)。

---

## [Unreleased]

---

## [3.0.2] — 2026-06-06

### Fixed

- 修复从 Firefox 导入 YouTube Cookie 后生成无效 Netscape cookie 文件的问题。
- 修复部分 YouTube 播放列表链接解析失败的问题。
- 修复歌词打轴模块嵌入工作台后，横向拖动窗口时可能误拖动内部内容宽度的问题。

### Changed

- 更新弹窗现在会展示 GitHub Release 发布说明，方便用户在更新前查看本次变更。
- Windows 打包产物现在使用工作台 logo 作为 `Karaoke Studio.exe` 图标。
- 更新 StrangeUtaGame 子模块，包含注音/用户词典稳定性修复与嵌入工作台相关调整。

---

## [3.0.1] — 2026-06-06

### Changed

- 仓库迁移至 `github.com/karaoke-studio/karaoke-studio`，由 Myosotis11037 与 Xuan-cc 共同维护
- 合并 `Myosotis11037/karaoke-helper` 与 `Xuan-cc/StrangeUtaGame` 为单一仓库；StrangeUtaGame 源码重定位至 `krok_helper/lyrics_timing/`，git history 完整保留
- 仓库整体采用 GPL-3.0 协议（合并前 krok-helper 无 LICENSE，StrangeUtaGame 为 GPL-3.0）
- 将 `krok_helper/lyrics_timing` 拆为 `karaoke-studio/StrangeUtaGame` submodule，保留 SUG 社交资产与独立历史
- 增加工作台自动更新的 Windows release zip / SHA-256 生成流程

### Added

- 增加本地 updater 集成测试，覆盖全量 zip 下载、解压和安装目录替换

### 下一步计划

下一个发布版本 `v3.1.0` 将包含**打轴模块与主程序工作流的 UI 集成**（当前模块代码已合入，但主窗口第 4 步仍为占位）。

---

## [3.0.0] — 2026-06（合并前）

`Myosotis11037/karaoke-helper` 作为独立项目发布的最后一个版本。

详细变更见该版本 tag `v3.0.0` 的 commit 历史。

StrangeUtaGame 同期版本为 `SUGv1.1.1`，相关历史见
[`krok_helper/lyrics_timing/CHANGELOG.md`](./krok_helper/lyrics_timing/CHANGELOG.md)。
