from krok_helper.runtime_profile import configure_source_debug_settings_profile
from krok_helper.stdio import configure_utf8_stdio


configure_source_debug_settings_profile()
configure_utf8_stdio()

from krok_helper.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
