# Karaoke Helper

当前版本：`2.1.0`

`Karaoke Helper` 是一个面向卡拉 OK 制作流程的桌面工具，当前包含三个主要模块：

- `歌词检索`
- `波形对齐`
- `Hi-Res 生成`

它适合处理这类工作：

- 搜索并预览多来源歌词
- 对齐字幕视频与原唱音频
- 生成适合投稿或归档的 `on vocal / off vocal` 成品文件

## 当前能力

### 1. 歌词检索

- 支持多来源歌词搜索：`QQ 音乐 / 酷狗音乐 / 网易云音乐 / LRCLIB`
- 支持单源搜索，也支持聚合搜索
- 聚合搜索会优先保留各来源原始搜索顺位，再结合歌名、歌手、专辑匹配度修正
- 搜索结果表格支持继续下滑加载更多结果
- 歌词预览支持两种格式：
  - `按行 LRC`
  - `按字 LRC`
- 可选“省略歌曲介绍”：
  - 开启时会省略开头的介绍性行，例如歌名介绍、词、曲、编曲、制作人等
  - 关闭时显示完整内容
- 支持一键复制当前预览中的歌词
- 会自动做一些歌词清理：
  - 时间戳统一到百分之一秒
  - 删除纯时间戳空行
  - 清理 QQ 歌词中开头的注音段
  - 去掉时间戳后的多余空格
  - 将全角空格替换为半角空格
- 会记住这些偏好，下次启动自动恢复：
  - 上次选择的歌词来源
  - 上次选择的 LRC 显示格式
  - 是否省略歌曲介绍

### 2. 波形对齐

- 将字幕视频音轨和原唱音源绘制为双波形
- 支持自动对齐
- 支持点击时间轴跳转播放位置
- 支持播放预览，便于确认对齐效果
- 支持两种对齐目标：
  - 调整字幕视频
  - 调整原唱音源

当目标是“调整字幕视频”时，支持：

- 开头裁剪
- 前导画面补黑 / 补白 / 首帧定格
- 视频尾部裁剪
- 导出对齐后视频
- 可选额外导出一份对齐后的 WAV

当目标是“调整原唱音源”时，支持：

- 正偏移补静音
- 负偏移裁掉开头
- 导出修正后的无损 WAV

### 3. Hi-Res 生成

- 输入：
  - 字幕视频
  - 原唱音频
  - 伴奏音频
- 自动标准化外部音频为 `Hi-Res FLAC / 2ch`
- 当采样率低于 `48kHz` 时自动提升到 `48kHz`
- 自动保留原视频中的非音频流，并移除原音轨
- 最终输出两份成品：
  - `on_vocal.mkv`
  - `off_vocal.mkv`
- 支持自定义命名模板

## 项目结构

```text
krok-helper/
├─ app.py
├─ README.md
├─ krok_helper/
│  ├─ __init__.py
│  ├─ __main__.py
│  ├─ audio_alignment.py
│  ├─ cli.py
│  ├─ config.py
│  ├─ errors.py
│  ├─ ffmpeg.py
│  ├─ gui_qt.py
│  ├─ lyrics.py
│  ├─ models.py
│  ├─ pipeline.py
│  ├─ settings.py
│  ├─ types.py
│  ├─ windows.py
│  └─ assets/
├─ scripts/
│  ├─ build_macos.command
│  └─ build_windows.bat
├─ 启动桌面版.bat
└─ 一键HiRes（mkv）.bat
```

## 启动方式

双击：

```text
启动桌面版.bat
```

或在当前目录运行：

```powershell
python -m krok_helper
```

兼容旧入口：

```powershell
python app.py
```

## 桌面版说明

左侧栏可以切换三个模块：

- `歌词检索`
- `波形对齐`
- `Hi-Res 生成`

### 歌词检索

- 可以先选择来源，再输入关键词搜索
- 搜索结果按表格显示，可继续滚动加载更多
- 预览区支持切换 `按行 LRC / 按字 LRC`
- 预览区可选择是否省略歌曲介绍
- 预览区可直接复制当前歌词

### 波形对齐

- 先放入字幕视频与原唱音源
- 生成波形后再进行自动对齐或手动修正
- 支持播放预览与导出

快捷键：

- `Space`：生成波形后切换播放 / 停止
- `Alt+V`：切换拖动模式
- `Ctrl+D`：自动对齐
- `Ctrl+S`：导出当前对齐目标

### Hi-Res 生成

- 选择字幕视频、原唱音频、伴奏音频
- 自动完成音频标准化与封装
- 输出最终 `mkv` 文件

## 输出命名

默认输出名称：

- `on_vocal.mkv`
- `off_vocal.mkv`

如果切换到自定义模板，可分别设置：

- 原唱模板
- 伴奏模板

支持占位符：

- `{video_name}`：字幕视频文件名，不含扩展名

示例：

```text
原唱模板: {video_name}_orig_master
伴奏模板: {video_name}_inst_master
```

最终输出：

```text
你的视频名_orig_master.mkv
你的视频名_inst_master.mkv
```

注意：

- 模板里不需要写 `.mkv`
- 模板中不能包含路径分隔符

波形对齐的命名模板同理：

- 对齐后视频模板支持 `{video_name}`
- 对齐后音频模板支持 `{audio_name}` 和 `{video_name}`

## 命令行用法

最基本用法：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac"
```

如果系统 `PATH` 中没有 `ffmpeg` / `ffprobe`，可以额外指定目录：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac" `
  --ffmpeg-dir "D:\tools\ffmpeg\bin"
```

如需通过命令行指定自定义命名：

```powershell
python -m krok_helper `
  --video "D:\path\subtitle_video.mkv" `
  --on-audio "D:\path\song.flac" `
  --off-audio "D:\path\inst.flac" `
  --output-name-mode template `
  --on-name-template "{video_name}_orig_master" `
  --off-name-template "{video_name}_inst_master"
```

支持的命名模式：

- `fixed`
- `template`

如果没有显式传入命名参数或 `ffmpeg` 目录参数，命令行会优先读取本地保存的设置。

## FFmpeg 目录

程序查找 `ffmpeg` / `ffprobe` 的顺序：

1. 系统环境变量 `PATH`
2. 你在设置窗口或命令行中指定的 `ffmpeg` 目录

推荐直接选择：

```text
...\ffmpeg\bin
```

例如：

```text
D:\tools\ffmpeg\bin
```

## 本地设置保存位置

Windows 默认保存在：

```text
%APPDATA%\Karaoke Helper\settings.json
```

当前会保存：

- 输出命名模式
- 原唱模板
- 伴奏模板
- `ffmpeg` 目录
- 歌词来源选择
- LRC 显示格式
- 是否省略歌曲介绍

## 打包

项目使用 `PyInstaller` 打包。

### Windows

```powershell
.\scripts\build_windows.bat
```

输出目录：

```text
dist\windows\Karaoke Helper\
```

主程序：

```text
dist\windows\Karaoke Helper\Karaoke Helper.exe
```

### macOS

先给脚本执行权限：

```bash
chmod +x ./scripts/build_macos.command
```

然后运行：

```bash
./scripts/build_macos.command
```

输出目录：

```text
dist/macos/Karaoke Helper.app
```

## 依赖

- Python 3.10+
- `ffmpeg`
- `ffprobe`
- PySide6

## 说明

- 如果字幕视频和音频时长差异较大，程序会给出警告，但仍会继续处理
- 当前流程会先标准化输入音频，再封装进最终 `mkv`
- B 站是否最终显示 `Hi-Res`，上传时仍需要你在投稿页手动勾选对应选项
