from __future__ import annotations

import base64
import plistlib
import zipfile
from types import SimpleNamespace

import pytest

from krok_helper.updater.macos_feed import ASSET_NAME, parse_feed


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
