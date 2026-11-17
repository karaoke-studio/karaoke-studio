from __future__ import annotations

import http.cookiejar
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QDateTime
from PyQt6.QtNetwork import QNetworkCookie

from krok_helper.video_download.download_task import SOURCE_YOUTUBE
from krok_helper.video_download.youtube_web_login import cookie_jar_from_webview, verify_and_import, youtube_domain


def qt_cookie(name=b"SAPISID", domain=".youtube.com", *, expires=None):
    cookie = QNetworkCookie(name, b"test-only")
    cookie.setDomain(domain)
    cookie.setPath("/")
    cookie.setSecure(True)
    cookie.setHttpOnly(True)
    if expires is not None:
        cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(expires))
    return cookie


@pytest.mark.parametrize("domain,expected", [
    (".youtube.com", True), ("www.youtube.com", True), ("YOUTUBE.COM", True),
    ("youtube.com.attacker.test", False), ("notyoutube.com", False), (".google.com", False),
])
def test_domain_boundary(domain, expected):
    assert youtube_domain(domain) is expected


def test_cookie_snapshot_preserves_http_only_and_ignores_unrelated_expired():
    jar = cookie_jar_from_webview([
        qt_cookie(), qt_cookie(domain=".google.com"), qt_cookie(b"expired", expires=1),
    ])
    cookies = list(jar)
    assert len(cookies) == 1
    assert cookies[0].has_nonstandard_attr("HttpOnly")
    assert cookies[0].secure and cookies[0].expires is None


def fake_ytdlp(*, fail=False, result=None, captured=None):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options
            self.cookiejar = http.cookiejar.MozillaCookieJar(options["cookiefile"])
            self.cookiejar.load(ignore_discard=True)
            if captured is not None:
                captured.append(options)
            if os.name != "nt":
                assert Path(options["cookiefile"]).stat().st_mode & 0o777 == 0o600

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            assert url == "https://www.youtube.com/playlist?list=WL"
            assert download is False
            if fail:
                raise RuntimeError("network failure")
            # Simulate a server-side cookie update and a foreign cookie.
            for cookie in cookie_jar_from_webview([qt_cookie(b"SID")]):
                self.cookiejar.set_cookie(cookie)
            foreign = next(iter(cookie_jar_from_webview([qt_cookie(b"foreign")])))
            foreign.domain = ".example.com"
            self.cookiejar.set_cookie(foreign)
            return result if result is not None else {"_type": "playlist", "id": "WL", "entries": []}
    return FakeYoutubeDL


def test_verified_import_updates_host_and_preserves_session_cookie(isolated_cookie_manager):
    options = []
    target = verify_and_import(
        cookie_jar_from_webview([qt_cookie()]), isolated_cookie_manager,
        proxy="http://localhost:7890", youtube_dl=fake_ytdlp(captured=options),
    )
    saved = isolated_cookie_manager.load_cookie_jar(SOURCE_YOUTUBE)
    assert {c.name for c in saved} == {"SAPISID", "SID"}
    assert options[0]["proxy"] == "http://localhost:7890"
    assert options[0]["playlistend"] == 1
    assert not list(target.parent.glob("youtube-login-*.tmp"))


@pytest.mark.parametrize("fake", [fake_ytdlp(fail=True), fake_ytdlp(result={"id": "other", "_type": "playlist"})])
def test_failed_verification_preserves_existing_cookie_and_cleans_temp(isolated_cookie_manager, fake):
    target = isolated_cookie_manager.resolved_cookie_path(SOURCE_YOUTUBE)
    target.write_bytes(b"existing-login")
    with pytest.raises((RuntimeError, ValueError)):
        verify_and_import(cookie_jar_from_webview([qt_cookie()]), isolated_cookie_manager, proxy="", youtube_dl=fake)
    assert target.read_bytes() == b"existing-login"
    assert not list(target.parent.glob("youtube-login-*.tmp"))


def test_guest_cookie_never_reaches_network(isolated_cookie_manager):
    def unexpected(_):
        pytest.fail("guest must not reach network")
    with pytest.raises(ValueError, match="尚未取得"):
        verify_and_import(cookie_jar_from_webview([qt_cookie(b"VISITOR_INFO1_LIVE")]),
                          isolated_cookie_manager, proxy="", youtube_dl=unexpected)


def test_atomic_replace_failure_preserves_existing_cookie(isolated_cookie_manager, monkeypatch):
    target = isolated_cookie_manager.resolved_cookie_path(SOURCE_YOUTUBE)
    target.write_bytes(b"existing-login")
    def deny(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(os, "replace", deny)
    with pytest.raises(PermissionError):
        verify_and_import(cookie_jar_from_webview([qt_cookie()]), isolated_cookie_manager,
                          proxy="", youtube_dl=fake_ytdlp())
    assert target.read_bytes() == b"existing-login"
    assert not list(target.parent.glob("youtube-login-*.tmp"))
