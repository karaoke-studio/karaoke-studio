"""更新器 settings namespace、默认合并与防抖测试。"""

from krok_helper.settings import AppSettings
from krok_helper.updater import settings as updater_settings
from krok_helper.updater.settings import DEFAULT_ORDER, UpdaterSettings, ensure_updater_settings


def test_defaults_and_missing_keys_are_merged():
    app = AppSettings(updater={"enabled": False, "proxy": {"mode": "manual"}})
    value = UpdaterSettings.load(app)
    assert value.enabled is False
    assert value.check_on_startup is True
    assert value.min_check_interval_hours == 8
    assert value.source_order == list(DEFAULT_ORDER)
    assert value.proxy_mode == "manual"
    assert value.proxy_manual_url == ""


def test_invalid_nested_values_fall_back_without_leaking_other_settings():
    app = AppSettings(ui_theme="dark", updater={"proxy": "bad", "source_order": ["bad"]})
    value = UpdaterSettings.load(app)
    assert value.proxy_mode == "system"
    assert value.source_order == list(DEFAULT_ORDER)
    assert app.ui_theme == "dark"


def test_to_payload_roundtrip_and_source_normalization():
    value = UpdaterSettings(
        enabled=False,
        check_on_startup=False,
        min_check_interval_hours=24,
        source_order=["gh-proxy", "github"],  # type: ignore[list-item]
        proxy_mode="manual",
        proxy_manual_url="127.0.0.1:7890",
        skipped_version="3.2.0",
        last_seen_version="3.2.1",
        last_check_at=123,
    )
    app = AppSettings(updater=value.to_payload())
    loaded = UpdaterSettings.load(app)
    assert loaded.enabled is False
    assert loaded.source_order == ["gh-proxy", "github"]
    assert loaded.to_payload()["proxy"] == {"mode": "manual", "manual_url": "127.0.0.1:7890"}


def test_save_only_replaces_updater_namespace(monkeypatch):
    saved = []
    monkeypatch.setattr(updater_settings, "save_app_settings", lambda app: saved.append(app))
    app = AppSettings(ui_theme="dark", updater={"old": True})
    value = UpdaterSettings(enabled=False)
    value.save(app)
    assert saved == [app]
    assert app.ui_theme == "dark"
    assert app.updater == value.to_payload()


def test_ensure_persists_missing_schema_and_is_idempotent(monkeypatch):
    saved = []
    monkeypatch.setattr(updater_settings, "save_app_settings", lambda app: saved.append(app.updater.copy()))
    app = AppSettings(updater={})
    first = ensure_updater_settings(app)
    second = ensure_updater_settings(app)
    assert first.enabled is True and second.enabled is True
    assert len(saved) == 1
    assert "source_order" in app.updater


def test_check_cooldown_boundaries():
    value = UpdaterSettings(min_check_interval_hours=8, last_check_at=1_000)
    assert value.is_within_check_cooldown(now=1_000 + 8 * 3600 - 1) is True
    assert value.is_within_check_cooldown(now=1_000 + 8 * 3600) is False
    value.min_check_interval_hours = 0
    assert value.is_within_check_cooldown(now=1_001) is False
