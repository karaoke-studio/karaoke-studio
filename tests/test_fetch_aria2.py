from __future__ import annotations

import io
import sys

from scripts import fetch_aria2


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b"archive"


def test_download_log_is_safe_on_cp1252_stdout(monkeypatch) -> None:
    """Windows CI must reach urlopen even when its console is not UTF-8."""
    monkeypatch.setattr(fetch_aria2.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    assert fetch_aria2._download(fetch_aria2.ARIA2_URL) == b"archive"
    stdout.flush()
    assert b"Downloading https://" in raw.getvalue()


def test_cached_log_does_not_print_a_non_ascii_install_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fetch_aria2, "_up_to_date", lambda _path: True)
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)

    result = fetch_aria2.fetch(tmp_path / "カラオケ")
    stdout.flush()

    assert result.name == "aria2c.exe"
    assert b"present and verified" in raw.getvalue()
