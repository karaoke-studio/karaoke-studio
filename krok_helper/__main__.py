from krok_helper.runtime_profile import configure_source_debug_settings_profile


configure_source_debug_settings_profile()

# 必须早于任何会创建用户数据目录的代码（尤其是日志初始化），否则迁移会被永久跳过。
from krok_helper.app_paths import migrate_app_data_dir


migrate_app_data_dir()

from krok_helper.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
