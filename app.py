from multiprocessing import freeze_support


# PyInstaller 的 multiprocessing runtime hook 会在这里识别 worker 命令行并直接
# 进入 Pool worker；必须早于 PyQt6 / GUI 导入，否则 spawn 子进程会重新启动主界面，
# 导出端将永久等不到第一批渲染帧。普通 Python 运行时此调用是安全的 no-op。
freeze_support()

# 启动面包屑要第一个就位：出现过「窗口起来了、日志一条没有、进程被 abort 打死」
# 的现场，那种情况下只有不依赖日志系统的记录才问得出它死在哪一步。
from krok_helper.startup_trace import mark as _trace


_trace("boot.enter")

from krok_helper.runtime_profile import configure_source_debug_settings_profile
from krok_helper.stdio import configure_utf8_stdio


configure_source_debug_settings_profile()
configure_utf8_stdio()
_trace("boot.stdio_ready")

# 必须早于 configure_application_logging()：日志会在新应用名目录下建 logs/，
# 一旦它先跑，迁移的「新目录不存在」前提就被破坏，用户数据会永久留在旧目录。
from krok_helper.app_paths import migrate_app_data_dir


migrate_app_data_dir()
_trace("boot.data_dir_ready")

from krok_helper.logging_config import configure_application_logging


configure_application_logging()
_trace("boot.logging_ready")

from krok_helper.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
