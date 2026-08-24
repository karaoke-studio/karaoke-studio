from __future__ import annotations

from types import SimpleNamespace

from krok_helper.subtitle_render.settings import store as settings_store


class _Provider:
    def __init__(self, value) -> None:
        self.value = value
        self.saved: list[dict] = []

    def load(self):
        return self.value

    def save(self, data: dict) -> None:
        self.saved.append(data)


def test_settings_store_uses_injected_provider_without_global_io(monkeypatch) -> None:
    provider = _Provider({"preview": 1})
    store = settings_store.SubtitleRenderSettingsStore(provider)
    monkeypatch.setattr(
        settings_store,
        "load_app_settings",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected global load")),
    )

    assert store.load() == {"preview": 1}
    store.save({"preview": 2})

    assert provider.saved == [{"preview": 2}]


def test_settings_store_preserves_non_dict_provider_fallback() -> None:
    assert settings_store.SubtitleRenderSettingsStore(_Provider(None)).load() == {}


def test_settings_store_replaces_only_global_module_namespace(monkeypatch) -> None:
    app_settings = SimpleNamespace(subtitle_render={"old": True}, other="kept")
    saved: list[tuple[object, bool]] = []
    monkeypatch.setattr(settings_store, "load_app_settings", lambda: app_settings)
    monkeypatch.setattr(
        settings_store,
        "save_app_settings",
        lambda settings, *, merge_module_namespaces: saved.append(
            (settings, merge_module_namespaces)
        ),
    )
    store = settings_store.SubtitleRenderSettingsStore()

    assert store.load() == {"old": True}
    store.save({"new": True})

    assert app_settings.subtitle_render == {"new": True}
    assert app_settings.other == "kept"
    assert saved == [(app_settings, False)]


def test_incomplete_provider_preserves_global_fallback(monkeypatch) -> None:
    app_settings = SimpleNamespace(subtitle_render={"global": True})
    saved: list[bool] = []
    monkeypatch.setattr(settings_store, "load_app_settings", lambda: app_settings)
    monkeypatch.setattr(
        settings_store,
        "save_app_settings",
        lambda _settings, *, merge_module_namespaces: saved.append(
            merge_module_namespaces
        ),
    )
    store = settings_store.SubtitleRenderSettingsStore(SimpleNamespace())

    assert store.load() == {"global": True}
    store.save({"fallback": True})

    assert app_settings.subtitle_render == {"fallback": True}
    assert saved == [False]
