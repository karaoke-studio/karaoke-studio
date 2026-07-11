from multiprocessing import freeze_support


# PyInstaller 的 multiprocessing runtime hook 会在这里识别 worker 命令行并直接
# 进入 Pool worker；必须早于 PyQt6 / GUI 导入，否则 spawn 子进程会重新启动主界面，
# 导出端将永久等不到第一批渲染帧。普通 Python 运行时此调用是安全的 no-op。
freeze_support()

from krok_helper.runtime_profile import configure_source_debug_settings_profile
from krok_helper.stdio import configure_utf8_stdio


configure_source_debug_settings_profile()
configure_utf8_stdio()

from krok_helper.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
