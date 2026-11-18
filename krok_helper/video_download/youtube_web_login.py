"""Cookie transport for the user-operated YouTube login window."""
from __future__ import annotations

import http.cookiejar
import copy
import os
import tempfile
import time
from pathlib import Path

from .download_task import SOURCE_YOUTUBE

YOUTUBE_AUTH_COOKIES = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}


def youtube_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == "youtube.com" or host.endswith(".youtube.com")


def cookie_jar_from_webview(cookies) -> http.cookiejar.MozillaCookieJar:
    """Snapshot Qt cookies, including HttpOnly cookies, without logging secrets."""
    jar = http.cookiejar.MozillaCookieJar()
    for cookie in cookies:
        if not youtube_domain(cookie.domain()):
            continue
        expires = None if cookie.isSessionCookie() else cookie.expirationDate().toSecsSinceEpoch()
        if expires is not None and expires <= time.time():
            continue
        domain = cookie.domain()
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=bytes(cookie.name()).decode("utf-8"),
            value=bytes(cookie.value()).decode("utf-8"), port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."),
            domain_initial_dot=domain.startswith("."), path=cookie.path() or "/",
            path_specified=True, secure=cookie.isSecure(), expires=expires, discard=False,
            comment=None, comment_url=None,
            rest={"HttpOnly": None} if cookie.isHttpOnly() else {}, rfc2109=False,
        ))
    return jar


class WebViewCookieSnapshot:
    """Own Python values, never borrowed QNetworkCookie signal arguments.

    WebEngine emits a const QNetworkCookie reference. Its Python wrapper can
    outlive the native value after the signal returns, so conversion must happen
    inside the callback, before any timer, user action or worker can read it.
    """

    def __init__(self):
        self._cookies = {}

    @staticmethod
    def _key(cookie):
        return cookie.domain(), cookie.path() or "/", bytes(cookie.name()).decode("utf-8")

    def added(self, cookie):
        key = self._key(cookie)
        self._cookies.pop(key, None)
        for value in cookie_jar_from_webview([cookie]):
            self._cookies[key] = value

    def removed(self, cookie):
        self._cookies.pop(self._key(cookie), None)

    def to_jar(self):
        jar = http.cookiejar.MozillaCookieJar()
        for cookie in self._cookies.values():
            if not cookie.is_expired():
                jar.set_cookie(copy.deepcopy(cookie))
        return jar


class _SilentLogger:
    # yt-dlp errors/debug output can contain account or request information.
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def verify_and_import(jar, manager, *, proxy: str, youtube_dl=None) -> Path:
    """Check an authenticated endpoint before atomically replacing saved cookies."""
    if not any(c.name in YOUTUBE_AUTH_COOKIES and c.value and not c.is_expired() for c in jar):
        raise ValueError("尚未取得 YouTube 登录 Cookie，请在网页中完成登录。")
    if youtube_dl is None:
        from yt_dlp import YoutubeDL
        youtube_dl = YoutubeDL
    target = manager.resolved_cookie_path(SOURCE_YOUTUBE)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="youtube-login-", suffix=".tmp", dir=target.parent)
    os.close(fd)
    candidate = Path(name)
    try:
        jar.save(str(candidate), ignore_discard=True, ignore_expires=False)
        options = {
            "cookiefile": str(candidate), "proxy": proxy,
            "quiet": True, "no_warnings": True, "logger": _SilentLogger(),
            "skip_download": True, "extract_flat": True, "playlistend": 1,
            "socket_timeout": 20, "retries": 0, "extractor_retries": 0,
        }
        with youtube_dl(options) as ydl:
            result = ydl.extract_info("https://www.youtube.com/playlist?list=WL", download=False)
            if not result or result.get("id") != "WL" or result.get("_type") != "playlist":
                raise ValueError("未能确认 YouTube 账号状态。")
            # Retain server-side cookie updates but never save unrelated domains.
            refreshed = http.cookiejar.MozillaCookieJar()
            for cookie in ydl.cookiejar:
                if youtube_domain(cookie.domain) and not cookie.is_expired():
                    refreshed.set_cookie(cookie)
        if not any(c.name in YOUTUBE_AUTH_COOKIES and c.value for c in refreshed):
            raise ValueError("验证后登录 Cookie 已失效。")
        refreshed.save(str(candidate), ignore_discard=True, ignore_expires=False)
        os.replace(candidate, target)
        return target
    finally:
        candidate.unlink(missing_ok=True)
