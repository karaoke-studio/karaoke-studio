from pathlib import Path

from krok_helper import directory_probe
from strange_uta_game import app_dirs


def test_writable_probe_leaves_no_files(tmp_path):
    target = tmp_path / "new"
    assert directory_probe.is_dir_writable(target)
    assert list(target.iterdir()) == []


def test_denied_probe_attempts_once_and_falls_back(tmp_path, monkeypatch):
    denied = tmp_path / "protected"
    fallback = tmp_path / "user"
    real_open = directory_probe.os.open
    attempts = []

    def guarded_open(path, flags, mode=0o777):
        attempts.append(Path(path).parent)
        if Path(path).parent == denied:
            raise PermissionError("access denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(directory_probe.os, "open", guarded_open)
    monkeypatch.setattr(app_dirs, "program_dir", lambda: denied)
    monkeypatch.setattr(app_dirs, "_FALLBACK_ROOT", fallback)
    monkeypatch.delenv("SUG_CONFIG_DIR", raising=False)
    assert app_dirs.config_dir() == fallback
    assert attempts == [denied, fallback]


def test_existing_file_is_not_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(directory_probe, "uuid4", lambda: type("ID", (), {"hex": "fixed"})())
    existing = tmp_path / ".lin-k-write-probe-fixed"
    existing.write_text("keep")
    assert not directory_probe.is_dir_writable(tmp_path)
    assert existing.read_text() == "keep"
