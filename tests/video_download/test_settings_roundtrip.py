from __future__ import annotations

import json

from krok_helper.settings import AppSettings, load_app_settings, save_app_settings


def test_save_and_load_yields_equal_settings(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("krok_helper.settings.get_settings_path", lambda: settings_path)
    settings = AppSettings(
        video_download_save_dir="D:/Downloads",
        video_download_custom_template="{title} - {author}",
        video_download_download_thumbnail=True,
        video_download_concurrent_count=4,
        video_download_timeout=10,
        video_download_retry_count=5,
    )

    save_app_settings(settings)

    assert load_app_settings() == settings


def test_save_includes_video_download_fields(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("krok_helper.settings.get_settings_path", lambda: settings_path)

    save_app_settings(AppSettings())

    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    expected_keys = {
        "video_download_save_dir",
        "video_download_naming_rule",
        "video_download_custom_template",
        "video_download_concurrent_count",
        "video_download_timeout",
        "video_download_retry_count",
        "video_download_merge_video_audio",
        "video_download_download_thumbnail",
    }
    assert expected_keys <= payload.keys()


def test_load_with_unknown_keys_doesnt_crash(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"unknown": "ignored", "video_download_save_dir": "D:/Downloads"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("krok_helper.settings.get_settings_path", lambda: settings_path)

    assert load_app_settings().video_download_save_dir == "D:/Downloads"
