from __future__ import annotations

import base64
import plistlib
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


@pytest.mark.parametrize("value", [None, "", " \n"])
def test_missing_key_skips_sdk_preparation(tmp_path, monkeypatch, value):
    sdk = tmp_path / "sdk"
    monkeypatch.delenv("KROK_SPARKLE_PUBLIC_ED_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("KROK_SPARKLE_PUBLIC_ED_KEY", value)
    monkeypatch.setattr(sys, "argv", ["prepare_macos_updater.py", "--sdk-dir", str(sdk)])
    monkeypatch.setattr(prepare_macos_updater, "prepare_sdk", lambda _: pytest.fail("Must not fetch Sparkle"))
    prepare_macos_updater.main()
    assert not sdk.exists()


def test_invalid_configured_key_still_fails_before_sdk_preparation(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    monkeypatch.setenv("KROK_SPARKLE_PUBLIC_ED_KEY", base64.b64encode(bytes(31)).decode())
    monkeypatch.setattr(sys, "argv", ["prepare_macos_updater.py", "--sdk-dir", str(sdk)])
    with pytest.raises(ValueError, match="32 字节"):
        prepare_macos_updater.main()
    assert not sdk.exists()


def test_rebuild_without_key_removes_old_updater_and_resigns_app(tmp_path, monkeypatch):
    app = tmp_path / "Test.app"
    framework = app / "Contents/Frameworks/Sparkle.framework"
    framework.mkdir(parents=True)
    (framework / "Sparkle").write_bytes(b"old framework")
    resources = app / "Contents/Resources"
    resources.mkdir()
    (resources / "Sparkle-LICENSE.txt").write_text("old license")
    plist = app / "Contents/Info.plist"
    plist.write_bytes(plistlib.dumps({
        "CFBundleVersion": "1.0.0", "SUPublicEDKey": base64.b64encode(bytes(32)).decode(),
        "SUEnableInstallerLauncherService": True, "SUVerifyUpdateBeforeExtraction": True,
    }))
    monkeypatch.delenv("KROK_SPARKLE_PUBLIC_ED_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["prepare_macos_updater.py", "--sdk-dir", str(tmp_path / "sdk"), "--app", str(app)])
    monkeypatch.setattr(prepare_macos_updater, "prepare_sdk", lambda _: pytest.fail("Must not fetch Sparkle"))
    signed = []
    monkeypatch.setattr(prepare_macos_updater, "sign_bundle", signed.append)
    prepare_macos_updater.main()
    assert signed == [app]
    assert not framework.exists()
    assert not (resources / "Sparkle-LICENSE.txt").exists()
    assert plistlib.loads(plist.read_bytes()) == {"CFBundleVersion": "1.0.0"}


@pytest.mark.parametrize("public,framework,expected", [("", False, False), ("", True, False),
                         (base64.b64encode(bytes(32)).decode(), False, False),
                         (base64.b64encode(bytes(32)).decode(), True, True)])
def test_runtime_capability_requires_bundled_key_and_framework(tmp_path, monkeypatch, public, framework, expected):
    from krok_helper.updater.macos_metadata import is_auto_update_available
    app = tmp_path / "Test.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"SUPublicEDKey": public}))
    if framework:
        (app / "Contents/Frameworks/Sparkle.framework").mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Contents/MacOS/Test"))
    assert is_auto_update_available() is expected


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
