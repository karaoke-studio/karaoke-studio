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


def _preset(name: str, preset_id: str, size: int = 48):
    from dataclasses import replace as _replace

    from krok_helper.subtitle_render.domain.models import (
        StylePreset,
        SubtitleStyleScheme,
    )

    scheme = SubtitleStyleScheme()
    if hasattr(scheme, "font_size_px"):
        scheme = _replace(scheme, font_size_px=size)
    return StylePreset(name=name, preset_id=preset_id, scheme=scheme)


def test_preset_library_merge_keeps_entries_this_instance_never_touched():
    """另一个实例新增的预设不能被本实例陈旧的内存覆盖掉。"""

    from krok_helper.subtitle_render.settings.preferences import (
        merge_style_preset_library,
        style_presets_from_dict,
        style_presets_to_dict,
    )

    # 本实例启动时只见过 P1，磁盘上现在已经被别的实例加了 P2。
    baseline = {"id1": _preset("P1", "id1")}
    current = {"id1": _preset("P1", "id1")}
    on_disk = style_presets_to_dict(
        {"id1": _preset("P1", "id1"), "id2": _preset("P2", "id2")}
    )

    merged = style_presets_from_dict(
        merge_style_preset_library(on_disk, current, baseline)
    )

    assert sorted(preset.name for preset in merged.values()) == ["P1", "P2"]


def test_preset_library_merge_still_applies_this_instance_deletions():
    """增量合并不能把删除变成空操作，否则预设永远删不掉。"""

    from krok_helper.subtitle_render.settings.preferences import (
        merge_style_preset_library,
        style_presets_from_dict,
        style_presets_to_dict,
    )

    baseline = {"id1": _preset("P1", "id1"), "id2": _preset("P2", "id2")}
    current = {"id2": _preset("P2", "id2")}
    on_disk = style_presets_to_dict(baseline)

    merged = style_presets_from_dict(
        merge_style_preset_library(on_disk, current, baseline)
    )

    assert [preset.name for preset in merged.values()] == ["P2"]


def test_preset_library_merge_keeps_both_sides_of_a_concurrent_edit():
    """一个实例改预设、另一个新增预设，两边的改动都要留下。"""

    from krok_helper.subtitle_render.settings.preferences import (
        merge_style_preset_library,
        style_presets_from_dict,
        style_presets_to_dict,
    )

    baseline = {"id1": _preset("P1", "id1", size=48)}
    # 本实例把 P1 的字号改成 99。
    current = {"id1": _preset("P1", "id1", size=99)}
    # 同时另一个实例已经把 P3 写进磁盘。
    on_disk = style_presets_to_dict(
        {"id1": _preset("P1", "id1", size=48), "id3": _preset("P3", "id3")}
    )

    merged = style_presets_from_dict(
        merge_style_preset_library(on_disk, current, baseline)
    )

    assert sorted(preset.name for preset in merged.values()) == ["P1", "P3"]
    edited = next(preset for preset in merged.values() if preset.name == "P1")
    assert edited.scheme.font_size_px == 99


def test_preset_library_without_a_baseline_keeps_the_wholesale_behaviour():
    """未提供基线的调用方（旧测试 / 旧路径）行为不变。"""

    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.serialization.timing import (
        subtitle_loading_settings_to_dict,
    )
    from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
    from krok_helper.subtitle_render.settings.preferences import (
        AppPreferenceSaveInput,
        prepare_app_preferences,
        style_presets_from_dict,
        style_presets_to_dict,
    )
    from krok_helper.subtitle_render.settings.screen import (
        ScreenSettings,
        screen_settings_to_dict,
    )

    existing = {
        "style_presets": style_presets_to_dict({"id2": _preset("P2", "id2")})
    }
    prepared = prepare_app_preferences(
        existing,
        AppPreferenceSaveInput(
            app_default_style=Style(),
            project_style=Style(),
            layout_assignment=None,
            subtitle_loading_defaults=subtitle_loading_settings_to_dict(
                SubtitleLoadingSettings()
            ),
            style_presets=style_presets_to_dict({"id1": _preset("P1", "id1")}),
            screen=screen_settings_to_dict(ScreenSettings()),
            auto_chorus_role=None,
            auto_chorus_begin_chars=0,
            auto_chorus_end_chars=0,
            auto_chorus_overwrite=False,
            selected_scheme_key="global",
            preview_splitter_ratio=0.5,
            auto_save_enabled=False,
            auto_save_interval_minutes=5,
            project_backup_count=3,
        ),
    )

    written = style_presets_from_dict(prepared.data["style_presets"])
    assert [preset.name for preset in written.values()] == ["P1"]


def test_preset_library_merge_preserves_unknown_fields_from_newer_versions():
    """更高版本写进预设行的未知字段，本版本保存时不能抹掉。"""

    from krok_helper.subtitle_render.settings.preferences import (
        merge_style_preset_library,
        style_presets_to_dict,
    )

    baseline = {"id1": _preset("P1", "id1", size=48)}
    current = {"id1": _preset("P1", "id1", size=99)}
    on_disk = style_presets_to_dict(baseline)
    on_disk[0]["future_preset"] = 9

    rows = merge_style_preset_library(on_disk, current, baseline)

    assert len(rows) == 1
    # 本实例改动的字段写进去了，未知字段仍然在。
    assert rows[0]["scheme"]["font_size_px"] == 99
    assert rows[0]["future_preset"] == 9
