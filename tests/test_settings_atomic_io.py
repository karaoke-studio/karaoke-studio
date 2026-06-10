from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from krok_helper.settings import (
    AppSettings,
    consume_corruption_backup,
    load_app_settings,
    save_app_settings,
)


@pytest.fixture(autouse=True)
def _isolated_appdata(monkeypatch, tmp_path: Path):
    """每个测试都获得自己的 %APPDATA%/Karaoke Studio/ 目录，避免污染真实 settings.json。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # POSIX 兜底
    # 清掉模块级 corruption 状态，避免上一个测试污染本测试
    consume_corruption_backup()
    yield


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / "Karaoke Studio" / "settings.json"


def test_save_uses_atomic_replace_and_writes_complete_json(tmp_path: Path):
    settings = AppSettings(ffmpeg_dir="D:/ffmpeg", lyrics_timing={"audio": {"default_volume": 42}})
    save_app_settings(settings)

    target = _settings_path(tmp_path)
    assert target.is_file()
    # 不允许残留 .tmp（成功路径里 os.replace 已经把 .tmp 重命名为本体）
    assert not (target.parent / "settings.json.tmp").exists()
    # 内容应该是完整 JSON
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["ffmpeg_dir"] == "D:/ffmpeg"
    assert data["lyrics_timing"]["audio"]["default_volume"] == 42


def test_save_preserves_existing_file_when_write_fails_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """模拟「写 .tmp 阶段崩」——本体不应被破坏。

    这是 v3.0.x 真实事故的回归保护：先把 settings.json 直接 truncate 再写的
    路径会让进程被杀时留下空文件；改成 tmp+os.replace 后，写失败只会留下
    未完成的 .tmp，本体仍是上一次的完整版本。
    """
    # 先正常写一份完整的
    good = AppSettings(ffmpeg_dir="D:/ffmpeg", lyrics_timing_dictionary=[{"word": "猫"}])
    save_app_settings(good)
    target = _settings_path(tmp_path)
    good_text = target.read_text(encoding="utf-8")

    # 模拟「写 .tmp 中途失败」：第二次 save 时让 Path.write_text 在 .tmp 上抛
    from pathlib import Path as _Path
    real_write_text = _Path.write_text

    def fail_when_tmp(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("simulated mid-write crash")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", fail_when_tmp)

    bad = AppSettings(ffmpeg_dir="D:/nope", lyrics_timing_dictionary=[])
    with pytest.raises(OSError):
        save_app_settings(bad)

    # 本体应该还是第一次的完整内容（绝不能是空文件 / 半截）
    assert target.read_text(encoding="utf-8") == good_text


def test_load_backs_up_corrupt_file_and_returns_defaults(tmp_path: Path):
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 写入一个无效 JSON（模拟真实事故里 truncate 完没写完的残骸）
    target.write_text("{not valid json", encoding="utf-8")

    loaded = load_app_settings()

    # 应该回落到默认 AppSettings
    assert loaded.ffmpeg_dir == ""
    assert loaded.lyrics_timing == {}
    assert loaded.lyrics_timing_dictionary == []
    assert loaded.lyrics_timing_migrated_v1 is False

    # 损坏文件应该被备份到 settings.json.corrupt-<ts>，而**不是**被默默删掉
    backups = list(target.parent.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"

    # consume_corruption_backup 第一次返回备份路径并清零；第二次返回 None
    first = consume_corruption_backup()
    assert first == backups[0]
    assert consume_corruption_backup() is None


def test_load_backs_up_non_object_top_level(tmp_path: Path):
    target = _settings_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # JSON 合法但顶层是数组——也算损坏（schema 期望 object）
    target.write_text("[1, 2, 3]", encoding="utf-8")

    loaded = load_app_settings()
    assert loaded.lyrics_timing == {}
    backups = list(target.parent.glob("settings.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "[1, 2, 3]"


def test_load_does_not_emit_backup_for_missing_file(tmp_path: Path):
    """settings.json 根本不存在 → 是首次运行 / 全新安装，不该误报损坏。"""
    loaded = load_app_settings()
    assert loaded == AppSettings()
    backups = list((tmp_path / "Karaoke Studio").glob("settings.json.corrupt-*")) if (tmp_path / "Karaoke Studio").exists() else []
    assert backups == []
    assert consume_corruption_backup() is None


def test_settings_app_name_env_uses_separate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio Dev")

    save_app_settings(AppSettings(ffmpeg_dir="D:/dev-ffmpeg"))

    dev_settings = tmp_path / "Karaoke Studio Dev" / "settings.json"
    release_settings = tmp_path / "Karaoke Studio" / "settings.json"
    assert dev_settings.is_file()
    assert not release_settings.exists()
    assert load_app_settings().ffmpeg_dir == "D:/dev-ffmpeg"


def test_settings_dir_env_overrides_app_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    custom_dir = tmp_path / "custom-settings"
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio Dev")
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(custom_dir))

    save_app_settings(AppSettings(ffmpeg_dir="D:/custom-ffmpeg"))

    assert (custom_dir / "settings.json").is_file()
    assert not (tmp_path / "Karaoke Studio Dev" / "settings.json").exists()
    assert load_app_settings().ffmpeg_dir == "D:/custom-ffmpeg"


def test_source_debug_profile_defaults_to_dev_settings(monkeypatch: pytest.MonkeyPatch):
    from krok_helper.runtime_profile import configure_source_debug_settings_profile

    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_DIR", raising=False)

    configure_source_debug_settings_profile()

    assert os.environ["KARAOKE_STUDIO_SETTINGS_APP_NAME"] == "Karaoke Studio Dev"
