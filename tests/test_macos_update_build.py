from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest

from krok_helper.updater.macos_metadata import validate_ed_key
from scripts import prepare_macos_updater


@pytest.mark.parametrize("value", ["", "invalid!", base64.b64encode(bytes(31)).decode()])
def test_rejects_invalid_build_public_key(value):
    with pytest.raises(ValueError):
        validate_ed_key(value)


def test_accepts_trimmed_public_key():
    key = base64.b64encode(bytes(32)).decode()
    assert validate_ed_key(" " + key + "\n") == key


def test_build_configuration_import_needs_no_gui_dependencies():
    result = subprocess.run(
        [sys.executable, "-S", "-c", """
import sys
from krok_helper.updater.macos_metadata import validate_ed_key
from scripts.prepare_macos_updater import configure_bundle
assert validate_ed_key('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
assert not any(name.split('.')[0] in ('PyQt6', 'requests', 'qfluentwidgets', 'objc') for name in sys.modules)
"""], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_missing_key_stops_before_sdk_preparation(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    monkeypatch.delenv("KROK_SPARKLE_PUBLIC_ED_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["prepare_macos_updater.py", "--sdk-dir", str(sdk)])
    with pytest.raises(ValueError, match="32 字节"):
        prepare_macos_updater.main()
    assert not sdk.exists()


def test_sdk_checksum_failure_stops_before_extraction(tmp_path, monkeypatch):
    archive = tmp_path / "bad-sdk.tar.xz"
    archive.write_bytes(b"not the official Sparkle SDK")
    monkeypatch.setenv("KROK_SPARKLE_SDK_ARCHIVE", str(archive))
    sdk = tmp_path / "sdk"
    with pytest.raises(ValueError, match="checksum mismatch"):
        prepare_macos_updater.prepare_sdk(sdk)
    assert not (sdk / "Sparkle.framework").exists()


def test_lazy_public_api_preserves_existing_exports():
    from krok_helper import updater
    from krok_helper.updater.settings import UpdaterSettings
    from krok_helper.updater.worker import CheckResult, UpdateChecker

    assert updater.UpdaterSettings is UpdaterSettings
    assert updater.CheckResult is CheckResult
    assert updater.UpdateChecker is UpdateChecker
