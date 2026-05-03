from __future__ import annotations

import os
import time
from pathlib import Path

from krok_helper.config import APP_NAME


class CookieManager:
    def __init__(self, cookie_path: str = "") -> None:
        self._configured_path = cookie_path.strip()

    def set_cookie_path(self, cookie_path: str) -> None:
        self._configured_path = cookie_path.strip()

    def default_cookie_path(self) -> Path:
        appdata = os.getenv("APPDATA")
        if os.name == "nt" and appdata:
            return Path(appdata) / APP_NAME / "video_download" / "bilibili_cookies.txt"
        return Path.home() / ".config" / APP_NAME.lower().replace(" ", "-") / "bilibili_cookies.txt"

    def resolved_cookie_path(self) -> Path:
        if self._configured_path:
            return Path(self._configured_path).expanduser()
        return self.default_cookie_path()

    def has_cookie(self) -> bool:
        path = self.resolved_cookie_path()
        return path.is_file() and path.stat().st_size > 0

    def get_cookie_path(self) -> str | None:
        path = self.resolved_cookie_path()
        return str(path) if path.exists() else None

    def clear_cookie(self) -> None:
        path = self.resolved_cookie_path()
        if path.exists():
            path.unlink()

    def check_login_status(self) -> bool:
        path = self.resolved_cookie_path()
        if not path.is_file():
            return False

        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, cookie_path, _secure, expires, name, value = parts[:7]
                domain = domain.lower()
                name = name.strip()
                value = value.strip()
                if "bilibili" not in domain:
                    continue
                if name != "SESSDATA" or not value:
                    continue
                if cookie_path not in ("/", ""):
                    continue
                if expires.isdigit() and int(expires) not in (0, 2147483647) and int(expires) < int(time.time()):
                    return False
                return True
        except Exception:
            return False
        return False
