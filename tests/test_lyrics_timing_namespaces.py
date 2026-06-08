import json

import pytest

from krok_helper.settings import (
    AppSettings as HostSettings,
    import_legacy_sug_settings,
    load_app_settings,
    migrate_strange_uta_game_settings,
    save_app_settings,
)


def test_load_app_settings_preserves_lyrics_timing_list_namespaces(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = HostSettings(
        lyrics_timing_dictionary=[{"word": "猫", "reading": "ねこ"}],
        lyrics_timing_singers=[{"id": "s1", "name": "Vocal"}],
        lyrics_timing_network_dictionary={"custom": {"entries": [{"word": "青"}]}},
    )

    save_app_settings(settings)
    loaded = load_app_settings()

    assert loaded.lyrics_timing_dictionary == [{"word": "猫", "reading": "ねこ"}]
    assert loaded.lyrics_timing_singers == [{"id": "s1", "name": "Vocal"}]
    assert loaded.lyrics_timing_network_dictionary == {"custom": {"entries": [{"word": "青"}]}}


def test_migrate_strange_uta_game_settings_imports_list_namespaces(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "config.json").write_text(
        json.dumps({"audio": {"default_volume": 66}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (legacy_dir / "dictionary.json").write_text(
        json.dumps([{"word": "猫", "reading": "ねこ"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (legacy_dir / "singers.json").write_text(
        json.dumps([{"id": "s1", "name": "Vocal"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (legacy_dir / "network_dictionary.json").write_text(
        json.dumps({"custom": {"entries": [{"word": "青"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    settings = HostSettings()
    assert migrate_strange_uta_game_settings(settings, legacy_dir=legacy_dir) is True

    assert settings.lyrics_timing_dictionary == [{"word": "猫", "reading": "ねこ"}]
    assert settings.lyrics_timing_singers == [{"id": "s1", "name": "Vocal"}]
    assert settings.lyrics_timing_network_dictionary == {"custom": {"entries": [{"word": "青"}]}}


def test_strange_uta_game_provider_extra_namespaces_round_trip():
    import krok_helper  # noqa: F401 - installs bundled src path
    from strange_uta_game.frontend.settings.app_settings import AppSettings

    class Provider:
        def __init__(self):
            self.config = {}
            self.extras = {"dictionary": [], "singers": [], "network": {}}

        def load(self):
            return dict(self.config)

        def save(self, data):
            self.config = dict(data)

        def load_extra(self, key, default):
            return self.extras.get(key, default)

        def save_extra(self, key, data):
            self.extras[key] = data

    provider = Provider()
    AppSettings.set_default_provider(provider)
    try:
        settings = AppSettings()
        settings.register_dictionary_word("猫", "ねこ")
        assert AppSettings().load_dictionary()[0]["reading"] == "ねこ"
        assert provider.extras["dictionary"][0]["word"] == "猫"

        singers = [{"id": "s1", "name": "Vocal", "color": "#ff5a6f"}]
        settings.save_singer_presets(singers)
        assert AppSettings().load_singer_presets() == singers
        assert provider.extras["singers"] == singers

        network_doc = {
            "enabled": True,
            "sources": [
                {
                    "id": "custom",
                    "name": "Custom",
                    "url": "https://example.invalid/dict.json",
                    "enabled": True,
                    "entries": [{"word": "青", "reading": "あお"}],
                    "last_fetched": "2026-06-04T00:00:00",
                }
            ],
            "source_order": ["custom"],
        }
        pytest.importorskip("numpy")
        settings.save_network_dictionary(network_doc)
        assert provider.extras["network"]["custom"]["entries"][0]["word"] == "青"
        assert any(source.get("id") == "custom" for source in AppSettings().load_network_dictionary()["sources"])
    finally:
        AppSettings.set_default_provider(None)


def test_strange_uta_game_provider_partial_save_preserves_newer_shortcuts():
    import krok_helper  # noqa: F401 - installs bundled src path
    from strange_uta_game.frontend.settings.app_settings import AppSettings

    class Provider:
        def __init__(self):
            self.config = {}

        def load(self):
            return json.loads(json.dumps(self.config))

        def save(self, data):
            self.config = json.loads(json.dumps(data))

        def save_partial(self, changes):
            for path, value in changes.items():
                self._set_nested(path, value)

        def _set_nested(self, path, value):
            cursor = self.config
            keys = path.split(".")
            for key in keys[:-1]:
                child = cursor.get(key)
                if not isinstance(child, dict):
                    child = {}
                    cursor[key] = child
                cursor = child
            cursor[keys[-1]] = value

        def load_extra(self, key, default):
            return default

        def save_extra(self, key, data):
            _ = key, data

    provider = Provider()
    AppSettings.set_default_provider(provider)
    try:
        stale_settings = AppSettings()

        fresh_settings = AppSettings()
        fresh_settings.set("shortcuts.timing_mode.tag_now", "SPACE:short")
        fresh_settings.save()

        stale_settings.set("export.last_export_dir", "D:/lyrics")
        stale_settings.save()

        reloaded = AppSettings()
        assert reloaded.get("shortcuts.timing_mode.tag_now") == "SPACE:short"
        assert reloaded.get("export.last_export_dir") == "D:/lyrics"
    finally:
        AppSettings.set_default_provider(None)


def test_import_legacy_sug_settings_filters_unknown_keys_and_merges_lists(tmp_path):
    legacy_dir = tmp_path / "sug"
    legacy_dir.mkdir()
    (legacy_dir / "config.json").write_text(
        json.dumps(
            {
                "audio": {"default_volume": 55, "totally_fake_setting": 999},
                "an_unknown_top_level_namespace": {"x": 1},
                "export": {"default_format": "Nicokara (带注音)"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (legacy_dir / "dictionary.json").write_text(
        json.dumps(
            [
                {"enabled": True, "word": "猫", "reading": "ねこ-legacy"},
                {"enabled": True, "word": "犬", "reading": "いぬ"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (legacy_dir / "singers.json").write_text(
        json.dumps(
            [
                {"name": "Vocal A", "color": "#111"},
                {"name": "Vocal B", "color": "#222"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (legacy_dir / "network_dictionary.json").write_text(
        json.dumps({"src1": {"entries": [{"word": "青"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    settings = HostSettings(
        lyrics_timing_dictionary=[{"enabled": True, "word": "猫", "reading": "ネコ-host"}],
        lyrics_timing_singers=[{"name": "Vocal A", "color": "#999"}],
    )
    report = import_legacy_sug_settings(legacy_dir, settings)

    # 主 config：已知 key 保留、未知 key 被过滤
    assert settings.lyrics_timing["audio"]["default_volume"] == 55
    assert "totally_fake_setting" not in settings.lyrics_timing["audio"]
    assert "an_unknown_top_level_namespace" not in settings.lyrics_timing
    assert settings.lyrics_timing["export"]["default_format"] == "Nicokara (带注音)"
    assert "audio.totally_fake_setting" in report["skipped_unknown_keys"]
    assert "an_unknown_top_level_namespace" in report["skipped_unknown_keys"]

    # 列表合并：host 已有 word="猫" 不被旧版覆盖，犬 是新增
    readings = {item["word"]: item["reading"] for item in settings.lyrics_timing_dictionary}
    assert readings["猫"] == "ネコ-host"
    assert readings["犬"] == "いぬ"
    assert report["added_dict_entries"] == 1

    # 演唱者合并：Vocal A 保留 host 的 color，Vocal B 新增
    singers = {item["name"]: item["color"] for item in settings.lyrics_timing_singers}
    assert singers["Vocal A"] == "#999"
    assert singers["Vocal B"] == "#222"
    assert report["added_singers"] == 1

    # 网络词典整体覆盖
    assert settings.lyrics_timing_network_dictionary == {"src1": {"entries": [{"word": "青"}]}}

    assert sorted(report["imported"]) == [
        "config.json",
        "dictionary.json",
        "network_dictionary.json",
        "singers.json",
    ]
    assert report["missing"] == []
    assert report["errors"] == []


def test_import_legacy_sug_settings_handles_missing_and_corrupt_files(tmp_path):
    legacy_dir = tmp_path / "sug"
    legacy_dir.mkdir()
    # dictionary.json 内容是非法 JSON
    (legacy_dir / "dictionary.json").write_text("{not valid json", encoding="utf-8")
    # 其它三个文件不存在

    settings = HostSettings()
    report = import_legacy_sug_settings(legacy_dir, settings)

    assert report["imported"] == []
    assert "dictionary.json" not in report["missing"]
    assert set(report["missing"]) == {"config.json", "singers.json", "network_dictionary.json"}
    assert any(name == "dictionary.json" for name, _ in report["errors"])
    # settings 维持初始默认值
    assert settings.lyrics_timing == {}
    assert settings.lyrics_timing_dictionary == []


def test_krok_helper_settings_bridge_partial_save_merges_lyrics_timing(monkeypatch, tmp_path):
    from krok_helper.gui_qt import KrokHelperSettingsBridge

    monkeypatch.setenv("APPDATA", str(tmp_path))
    saved = HostSettings(
        lyrics_timing={
            "shortcuts": {"timing_mode": {"tag_now": "SPACE:short"}},
        }
    )
    save_app_settings(saved)

    in_memory = HostSettings(lyrics_timing={})
    bridge = KrokHelperSettingsBridge(in_memory, lambda: save_app_settings(in_memory))

    bridge.save_partial({"export.last_export_dir": "D:/lyrics"})

    loaded = load_app_settings()
    assert loaded.lyrics_timing["shortcuts"]["timing_mode"]["tag_now"] == "SPACE:short"
    assert loaded.lyrics_timing["export"]["last_export_dir"] == "D:/lyrics"
    assert in_memory.lyrics_timing == loaded.lyrics_timing
