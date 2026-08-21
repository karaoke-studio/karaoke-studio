from pathlib import Path

#: 应用身份名。用于 %APPDATA% / 日志 / Cookie 目录名与网络 User-Agent，
#: 也是 :func:`krok_helper.settings.migrate_app_data_dir` 的迁移目标名。
#: 改动它会搬走用户数据目录 —— 必须同步 ``settings.LEGACY_APP_NAMES``。
APP_NAME = "Lin-K Lyrics"
APP_VERSION = "4.2.6.5"
#: 短标题。用在对话框标题（``f"{APP_TITLE} - 全局设置"``）等空间有限的位置，
#: 所以刻意只放中文名，不要塞中英组合。
APP_TITLE = "凛K"
#: 主窗口标题栏与启动画面用的完整品牌串（英文名 + 中文名）。
APP_WINDOW_TITLE = f"{APP_NAME} {APP_TITLE}"
MIN_HIRES_SAMPLE_RATE = 48_000
DURATION_WARNING_SECONDS = 2.0
WINDOW_WIDTH = 1480
WINDOW_HEIGHT = 960
WINDOW_MIN_WIDTH = 1180
WINDOW_MIN_HEIGHT = 820

#: ffmpeg 目录留空时各处输入框/标签的统一占位文案。
FFMPEG_DIR_PLACEHOLDER = "未设置，将优先使用系统 PATH 中的 ffmpeg"

#: 关于页与任务栏图标用的产品 logo。
APP_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo.jpg"
