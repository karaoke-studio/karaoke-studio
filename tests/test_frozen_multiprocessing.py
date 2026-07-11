"""PyInstaller 多进程导出入口契约。"""

from pathlib import Path


def test_freeze_support_runs_before_gui_imports() -> None:
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    freeze_call = source.index("freeze_support()")
    cli_import = source.index("from krok_helper.cli import main")
    runtime_profile_import = source.index(
        "from krok_helper.runtime_profile import configure_source_debug_settings_profile"
    )
    assert freeze_call < runtime_profile_import < cli_import
