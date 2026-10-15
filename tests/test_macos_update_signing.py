from __future__ import annotations

import base64
import plistlib
import zipfile
from types import SimpleNamespace

import pytest

from krok_helper.updater.macos_feed import ASSET_NAME, parse_feed


@pytest.mark.parametrize("public,private", [("", ""), ("configured", ""), ("", "configured"), (" \n", " \n")])
def test_missing_release_key_skips_signing_and_removes_stale_appcast(tmp_path, monkeypatch, public, private):
    import sys
    from scripts import build_macos_appcast
    from krok_helper.updater.macos_feed import FEED_NAME

    archive = tmp_path / ASSET_NAME
    archive.write_bytes(b"ordinary manual-download archive")
    feed = tmp_path / FEED_NAME
    feed.write_bytes(b"stale signature must not be republished")
    monkeypatch.setenv("KROK_SPARKLE_PUBLIC_ED_KEY", public)
    monkeypatch.setenv("KROK_SPARKLE_PRIVATE_ED_KEY", private)
    monkeypatch.setattr(sys, "argv", ["build_macos_appcast.py", str(archive), "--sdk-dir", str(tmp_path / "sdk")])
    monkeypatch.setattr(build_macos_appcast, "sign_archive", lambda *a: pytest.fail("Must not sign without both keys"))
    build_macos_appcast.main()
    assert archive.read_bytes() == b"ordinary manual-download archive"
    assert not feed.exists()


def test_release_signing_keeps_private_key_out_of_arguments_and_verifies_output(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from scripts import build_macos_appcast

    seed = bytes(range(32))
    key = Ed25519PrivateKey.from_private_bytes(seed)
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    private = base64.b64encode(seed).decode()
    archive = tmp_path / ASSET_NAME
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Lin-K Lyrics.app/Contents/Info.plist", plistlib.dumps({
            "SUPublicEDKey": public, "CFBundleVersion": "4.3.7003",
        }))
    calls = []

    def signing(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=base64.b64encode(key.sign(archive.read_bytes())).decode())

    monkeypatch.setattr(build_macos_appcast.subprocess, "run", signing)
    feed = build_macos_appcast.sign_archive(archive, tmp_path, "4.3.7.3", public, private)
    assert parse_feed(feed, tag="v4.3.7.3", version="4.3.7.3").length == archive.stat().st_size
    assert calls[0][1]["input"] == private + "\n"
    assert private not in calls[0][0]
    with pytest.raises(ValueError, match="do not match"):
        build_macos_appcast.sign_archive(archive, tmp_path, "4.3.7.3", public, base64.b64encode(bytes(32)).decode())
    assert len(calls) == 1

    monkeypatch.setattr(build_macos_appcast.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=base64.b64encode(bytes(64)).decode()))
    with pytest.raises(ValueError, match="failed verification"):
        build_macos_appcast.sign_archive(archive, tmp_path, "4.3.7.3", public, private)
