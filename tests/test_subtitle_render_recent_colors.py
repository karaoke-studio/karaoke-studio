"""Recent colors persist through the same store as the color preset library."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from krok_helper.settings import get_settings_path, load_app_settings, save_app_settings
from krok_helper.subtitle_render.settings.recent_colors import RecentColorStore
from krok_helper.subtitle_render.settings.store import SubtitleRenderSettingsStore


def disk_payload():
    return json.loads(get_settings_path().read_text(encoding="utf-8"))


def test_remember_and_remove_write_immediately_with_exact_alpha():
    store = RecentColorStore()
    assert store.load() == []
    store.remember("#80445566")
    store.remember("#FF445566")
    assert disk_payload()["subtitle_render"]["recent_colors"] == ["#FF445566", "#80445566"]
    store.remove("#80445566")
    assert disk_payload()["subtitle_render"]["recent_colors"] == ["#FF445566"]
    store.remove("#FF445566")
    assert disk_payload()["subtitle_render"]["recent_colors"] == []


def test_history_preserves_preset_library_output_and_other_namespaces():
    host = load_app_settings()
    host.ffmpeg_dir = "D:/ffmpeg"
    host.updater = {"auto_check": False}
    host.subtitle_render = {
        "style_presets": [{"id": "p1", "name": "配色一", "future": {"value": 3}}],
        "layout_presets": [{"future_layout": True}],
        "output": {"codec": "h264", "unknown": "保留"},
        "future_settings": {"nested": [1, 2, 3]},
    }
    save_app_settings(host)
    before = disk_payload()
    store = RecentColorStore()
    store.remember("#20123456")
    after = disk_payload()
    assert after["subtitle_render"].pop("recent_colors") == ["#20123456"]
    assert after == before
    store.clear()
    after = disk_payload()
    assert after["subtitle_render"].pop("recent_colors") == []
    assert after == before


def test_stale_instances_apply_only_their_addition_or_deletion():
    first, second = RecentColorStore(), RecentColorStore()
    first.remember("#80112233")
    assert second.load() == ["#80112233"]
    first.remember("#80445566")
    second.remember("#80778899")
    assert first.load() == ["#80778899", "#80445566", "#80112233"]
    first.remove("#80112233")
    second.remove("#80445566")
    assert first.load() == ["#80778899"]
    first.remember("#80010203")
    assert second.load() == ["#80010203", "#80778899"]


def test_old_host_snapshot_cannot_restore_deleted_colors_or_lose_new_ones():
    store = RecentColorStore()
    store.remember("#80112233")
    stale_host = load_app_settings()
    store.remove("#80112233")
    store.remember("#80445566")
    stale_host.ui_theme = "dark"
    save_app_settings(stale_host)
    assert RecentColorStore().load() == ["#80445566"]
    assert disk_payload()["ui_theme"] == "dark"


def test_preset_creation_uses_fresh_payload_and_preserves_color_history():
    from krok_helper.subtitle_render.domain.models import Style, StylePreset, SubtitleStyleScheme
    from krok_helper.subtitle_render.settings.preferences import (
        AppPreferenceSaveInput, prepare_app_preferences, style_presets_to_dict,
    )

    settings = SubtitleRenderSettingsStore()
    history = RecentColorStore()
    history.remember("#80112233")
    values = AppPreferenceSaveInput(
        app_default_style=Style(), project_style=Style(), layout_assignment=None,
        subtitle_loading_defaults={},
        style_presets=style_presets_to_dict({
            "new": StylePreset(name="新配色", preset_id="new", scheme=SubtitleStyleScheme()),
        }),
        style_presets_baseline={}, screen={}, auto_chorus_role="",
        auto_chorus_begin_chars="", auto_chorus_end_chars="", auto_chorus_overwrite=False,
        selected_scheme_key="global", preview_splitter_ratio=0.5,
        auto_save_enabled=False, auto_save_interval_minutes=5, project_backup_count=3,
    )
    # Follow SubtitleRenderWindow._save_persisted_state, including its fresh load.
    settings.save(prepare_app_preferences(settings.load(), values).data)
    assert history.load() == ["#80112233"]
    history.remove("#80112233")
    assert disk_payload()["subtitle_render"]["style_presets"][0]["name"] == "新配色"
    settings.save(prepare_app_preferences(settings.load(), values).data)
    assert history.load() == []


def test_history_restores_in_a_fresh_process_from_a_different_release_directory(tmp_path):
    store = RecentColorStore()
    store.remember("#00112233")
    store.remember("#80445566")
    release_dir = tmp_path / "new-release"
    release_dir.mkdir()
    env = os.environ.copy()
    repo = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import json; "
        "from krok_helper.subtitle_render.settings.recent_colors import RecentColorStore; "
        "print('RECENT_COLORS=' + json.dumps(RecentColorStore().load()))"
    )

    def restarted_history():
        result = subprocess.run([sys.executable, "-c", code], cwd=release_dir, env=env,
                                capture_output=True, text=True, encoding="utf-8", check=True)
        line = next(line for line in result.stdout.splitlines() if line.startswith("RECENT_COLORS="))
        return json.loads(line.split("=", 1)[1])

    assert restarted_history() == ["#80445566", "#00112233"]
    store.remove("#80445566")
    assert restarted_history() == ["#00112233"]


def test_history_normalizes_bad_rows_and_deduplicates_without_touching_disk_on_read():
    settings = SubtitleRenderSettingsStore()
    settings.save({"recent_colors": [
        None, 12, {}, "bad", "#80445566", "#80445566", "#445566", "#ff445566", "#00112233",
    ]})
    before = get_settings_path().read_bytes()
    history = RecentColorStore(settings)
    assert history.load() == ["#80445566", "#FF445566", "#00112233"]
    assert get_settings_path().read_bytes() == before
    history.remember("#00112233")
    assert disk_payload()["subtitle_render"]["recent_colors"] == ["#00112233", "#80445566", "#FF445566"]


@pytest.mark.parametrize("bad", [None, {}, "#112233", 20])
def test_malformed_history_falls_back_to_empty_without_affecting_other_settings(bad):
    settings = SubtitleRenderSettingsStore()
    settings.save({"recent_colors": bad, "future": {"value": 42}})
    history = RecentColorStore(settings)
    assert history.load() == []
    history.remember("#112233")
    assert settings.load() == {"recent_colors": ["#FF112233"], "future": {"value": 42}}


def test_history_caps_at_20_and_moves_existing_rgba_to_the_front():
    store = RecentColorStore()
    for value in range(24):
        store.remember(f"#80{value:06X}")
    expected = [f"#80{value:06X}" for value in range(23, 3, -1)]
    assert store.load() == expected
    store.remember(expected[7])
    assert store.load() == [expected[7], *expected[:7], *expected[8:]]
    assert len(disk_payload()["subtitle_render"]["recent_colors"]) == 20


def test_atomic_write_failure_preserves_last_saved_history(monkeypatch):
    import krok_helper.settings as settings_module

    store = RecentColorStore()
    store.remember("#80112233")
    before = get_settings_path().read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.remember("#80445566")
    assert get_settings_path().read_bytes() == before
    assert store.load() == ["#80112233"]
