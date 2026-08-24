"""NicoKaraMaker3-compatible DirectWrite font catalog."""

from __future__ import annotations

import ctypes
import sys

import pytest

import krok_helper.subtitle_render.n3.font_catalog as font_catalog
from krok_helper.subtitle_render.n3.font_catalog import (
    N3FontCatalog,
    _FamilyRecord,
    _build_catalog,
    get_n3_font_catalog,
    normalize_scheme_font_families,
    normalize_style_font_families,
    resolve_qt_font_family,
)
from krok_helper.subtitle_render.models import Style, SubtitleStyleScheme, TitleOverlay


def _compare(left: str, right: str) -> int:
    return (left > right) - (left < right)


@pytest.fixture(autouse=True)
def _isolate_sug_font_cache():
    """Keep SUG's process-level font snapshot from leaking across tests.

    Without this, a snapshot built under one test's monkeypatched
    ``QFontDatabase.families`` would resurface in later tests via
    ``_sug_installed_families`` (EMBEDDING §8 snapshot semantics).
    """

    def _invalidate() -> None:
        try:
            from strange_uta_game.frontend import font_cache

            font_cache.invalidate(clear_alias_map=False)
        except Exception:
            pass

    _invalidate()
    yield
    _invalidate()


def test_build_catalog_prefers_japanese_name_and_maps_every_alias():
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "English Alias"), ("ja-jp", "日本語名")),
                styles=(0, 2),
            )
        ],
        compare=_compare,
    )

    assert catalog.families == ("日本語名",)
    assert catalog.canonicalize("English Alias") == "日本語名"
    assert catalog.canonicalize("日本語名") == "日本語名"
    assert catalog.canonicalize("english alias") == "日本語名"


def test_build_catalog_keeps_display_name_but_resolves_qt_visible_alias():
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "English Alias"), ("ja-jp", "Japanese Display")),
                styles=(0,),
            )
        ],
        compare=_compare,
        qt_families=("English Alias",),
    )

    assert catalog.families == ("Japanese Display",)
    assert catalog.canonicalize("English Alias") == "Japanese Display"
    assert catalog.qt_family("Japanese Display") == "English Alias"
    assert catalog.qt_family("English Alias") == "English Alias"
    assert catalog.aliases_for("Japanese Display") == (
        "Japanese Display",
        "English Alias",
    )


def test_build_catalog_prefers_qt_canonical_spelling_when_both_are_visible():
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "English Alias"), ("ja-jp", "Japanese Display")),
                styles=(0,),
            )
        ],
        compare=_compare,
        qt_families=("English Alias", "Japanese Display"),
    )

    assert catalog.qt_family("English Alias") == "Japanese Display"


def test_resolve_qt_font_family_uses_catalog_runtime_name(monkeypatch):
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "Arial"), ("ja-jp", "Japanese Arial")),
                styles=(0,),
            )
        ],
        compare=_compare,
        qt_families=("Arial",),
    )
    monkeypatch.setattr(
        "krok_helper.subtitle_render.n3.font_catalog.get_n3_font_catalog",
        lambda: catalog,
    )
    resolve_qt_font_family.cache_clear()
    try:
        assert resolve_qt_font_family("Japanese Arial") == "Arial"
    finally:
        resolve_qt_font_family.cache_clear()


def test_font_catalog_refreshes_qt_aliases_after_application_start(monkeypatch):
    records = [
        _FamilyRecord(
            names=(("en-us", "Meiryo"), ("ja-jp", "メイリオ")),
            styles=(0,),
        )
    ]
    monkeypatch.setattr(font_catalog.sys, "platform", "win32")
    monkeypatch.setattr(font_catalog, "_sug_alias_records", lambda: [])
    monkeypatch.setattr(font_catalog, "_directwrite_records", lambda: records)
    monkeypatch.setattr(font_catalog, "_compare_ja_jp", _compare)
    monkeypatch.setattr(font_catalog.QFontDatabase, "families", lambda: ["Meiryo"])
    font_catalog._get_n3_font_catalog.cache_clear()
    try:
        before_qt = font_catalog._get_n3_font_catalog(0)
        with_qt = font_catalog._get_n3_font_catalog(123)

        assert before_qt.qt_family("Meiryo") == "メイリオ"
        assert with_qt.qt_family("Meiryo") == "Meiryo"
    finally:
        font_catalog._get_n3_font_catalog.cache_clear()


def test_font_resolution_cache_is_partitioned_by_application_lifecycle(monkeypatch):
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "English Alias"), ("ja-jp", "表示名")),
                styles=(0,),
            )
        ],
        compare=_compare,
        qt_families=("English Alias",),
    )
    application_keys = iter((0, 123))
    inspected: list[object] = []

    class FontInfo:
        def family(self):
            return "English Alias"

    monkeypatch.setattr(font_catalog, "get_n3_font_catalog", lambda: catalog)
    monkeypatch.setattr(
        font_catalog, "_qt_application_cache_key", lambda: next(application_keys)
    )
    monkeypatch.setattr(font_catalog, "QFont", lambda family: family)
    monkeypatch.setattr(
        font_catalog,
        "QFontInfo",
        lambda font: inspected.append(font) or FontInfo(),
    )
    resolve_qt_font_family.cache_clear()
    try:
        assert resolve_qt_font_family("表示名") == "English Alias"
        assert inspected == []
        assert resolve_qt_font_family("表示名") == "English Alias"
        assert inspected == ["English Alias"]
    finally:
        resolve_qt_font_family.cache_clear()


def test_font_resolution_probes_alias_registered_after_catalog_build(monkeypatch):
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "Meiryo"), ("ja-jp", "Japanese Meiryo")),
                styles=(0,),
            )
        ],
        compare=_compare,
    )
    resolved = {
        "Japanese Meiryo": "Arial",
        "Meiryo": "Meiryo",
    }

    class FontInfo:
        def __init__(self, family):
            self._family = family

        def family(self):
            return resolved[self._family]

    monkeypatch.setattr(font_catalog, "get_n3_font_catalog", lambda: catalog)
    monkeypatch.setattr(font_catalog, "QFont", lambda family: family)
    monkeypatch.setattr(font_catalog, "QFontInfo", FontInfo)
    resolve_qt_font_family.cache_clear()
    try:
        assert resolve_qt_font_family("Meiryo") == "Meiryo"
    finally:
        resolve_qt_font_family.cache_clear()


def test_build_catalog_filters_families_without_normal_face():
    catalog = _build_catalog(
        [
            _FamilyRecord(names=(("en-us", "Italic only"),), styles=(2,)),
            _FamilyRecord(names=(("en-us", "Normal"),), styles=(0,)),
        ],
        compare=_compare,
    )

    assert catalog.families == ("Normal",)
    assert catalog.canonicalize("Italic only") is None


def test_build_catalog_sorts_japanese_name_group_before_fallback_name_group():
    catalog = _build_catalog(
        [
            _FamilyRecord(names=(("en-us", "A Latin"),), styles=(0,)),
            _FamilyRecord(names=(("ja-jp", "い"),), styles=(0,)),
            _FamilyRecord(names=(("ja-jp", "あ"),), styles=(0,)),
            _FamilyRecord(names=(("en-us", "B Latin"),), styles=(0,)),
        ],
        compare=_compare,
    )

    assert catalog.families == ("あ", "い", "A Latin", "B Latin")


def test_non_authoritative_catalog_still_resolves_exact_fallback_names():
    catalog = N3FontCatalog(
        families=("Arial",), aliases={"arial": "Arial"}, authoritative=False
    )

    assert catalog.canonicalize("ARIAL") == "Arial"
    assert catalog.canonicalize("Missing") is None


def _normalization_catalog(*, authoritative: bool = True) -> N3FontCatalog:
    return N3FontCatalog(
        families=("游明朝", "UD デジタル 教科書体 N-B"),
        aliases={
            "游明朝".casefold(): "游明朝",
            "ud digi kyokasho n-b": "UD デジタル 教科書体 N-B",
            "UD デジタル 教科書体 N-B".casefold(): "UD デジタル 教科書体 N-B",
        },
        authoritative=authoritative,
    )


def test_normalize_style_canonicalizes_aliases_and_clears_missing_optional_fonts():
    style = Style(
        font_family="UD Digi Kyokasho N-B",
        font_family_latin="Missing Latin",
        ruby_font_family="Missing Ruby",
        title_overlay=TitleOverlay(
            font_family="UD Digi Kyokasho N-B",
            font_family_latin="Missing Title Latin",
        ),
    )

    normalized, changed = normalize_style_font_families(
        style, _normalization_catalog()
    )

    assert changed is True
    assert normalized.font_family == "UD デジタル 教科書体 N-B"
    assert normalized.font_family_latin is None
    assert normalized.ruby_font_family is None
    assert normalized.title_overlay is not None
    assert normalized.title_overlay.font_family == "UD デジタル 教科書体 N-B"
    assert normalized.title_overlay.font_family_latin is None


def test_normalize_style_resolves_physical_family_from_n3_face_weight():
    catalog = N3FontCatalog(
        families=("Fallback", "UD Kyokasho N-B", "UD Kyokasho N-R"),
        aliases={
            "fallback": "Fallback",
            "ud kyokasho n-b": "UD Kyokasho N-B",
            "ud kyokasho n-r": "UD Kyokasho N-R",
        },
        authoritative=True,
    )

    bold, changed = normalize_style_font_families(
        Style(font_family="UD Kyokasho N", font_weight=700), catalog
    )
    regular, _ = normalize_style_font_families(
        Style(font_family="UD Kyokasho N", font_weight=400), catalog
    )

    assert changed is True
    assert bold.font_family == "UD Kyokasho N-B"
    assert regular.font_family == "UD Kyokasho N-R"


def test_normalize_style_normalizes_nested_schemes_without_mutating_input():
    inherited = SubtitleStyleScheme(
        font_family="Missing Root",
        font_family_latin="UD Digi Kyokasho N-B",
        n3_font_inheritance=True,
    )
    ordinary = SubtitleStyleScheme(
        font_family="Missing Root", ruby_font_family="Missing Ruby"
    )
    style = Style(
        custom_style_schemes={"N3": inherited},
        singer_style_overrides={0: ordinary},
    )

    normalized, changed = normalize_style_font_families(
        style, _normalization_catalog()
    )

    assert changed is True
    assert style.custom_style_schemes["N3"].font_family == "Missing Root"
    assert normalized.custom_style_schemes["N3"].font_family == "游明朝"
    assert (
        normalized.custom_style_schemes["N3"].font_family_latin
        == "UD デジタル 教科書体 N-B"
    )
    assert normalized.singer_style_overrides[0].font_family is None
    assert normalized.singer_style_overrides[0].ruby_font_family is None


def test_normalize_scheme_keeps_saved_names_when_catalog_is_not_authoritative():
    scheme = SubtitleStyleScheme(
        font_family="Unverified Font", font_family_latin="Unverified Latin"
    )

    normalized, changed = normalize_scheme_font_families(
        scheme, _normalization_catalog(authoritative=False)
    )

    assert normalized == scheme
    assert changed is False


def test_build_catalog_merges_duplicate_family_records():
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "Dup Font"), ("ja-jp", "重複フォント")),
                styles=(0,),
            ),
            _FamilyRecord(
                names=(
                    ("en-us", "Dup Font"),
                    ("ja-jp", "重複フォント"),
                    ("zh-cn", "重复字体"),
                ),
                styles=(0,),
            ),
        ],
        compare=_compare,
    )

    assert catalog.families == ("重複フォント",)
    assert catalog.canonicalize("重复字体") == "重複フォント"
    assert catalog.aliases_for("重複フォント") == (
        "重複フォント",
        "Dup Font",
        "重复字体",
    )


def test_build_catalog_appends_families_only_qt_can_see():
    catalog = _build_catalog(
        [
            _FamilyRecord(
                names=(("en-us", "Meiryo"), ("ja-jp", "メイリオ")),
                styles=(0,),
            )
        ],
        compare=_compare,
        qt_families=("Meiryo", "Late Installed Font"),
    )

    assert "Late Installed Font" in catalog.families
    assert catalog.canonicalize("late installed font") == "Late Installed Font"
    assert catalog.qt_family("Late Installed Font") == "Late Installed Font"
    assert catalog.aliases_for("Late Installed Font") == ("Late Installed Font",)


def test_catalog_prefers_sug_cached_records_over_directwrite(monkeypatch):
    records = [
        _FamilyRecord(
            names=(("en-us", "Meiryo"), ("ja-jp", "メイリオ")),
            styles=(0,),
        )
    ]

    def _directwrite_must_not_run():
        raise AssertionError("DirectWrite walk must not run when the SUG cache hits")

    monkeypatch.setattr(font_catalog.sys, "platform", "win32")
    monkeypatch.setattr(font_catalog, "_sug_alias_records", lambda: records)
    monkeypatch.setattr(font_catalog, "_directwrite_records", _directwrite_must_not_run)
    monkeypatch.setattr(font_catalog, "_compare_ja_jp", _compare)
    monkeypatch.setattr(font_catalog.QFontDatabase, "families", lambda: ["Meiryo"])
    font_catalog._get_n3_font_catalog.cache_clear()
    try:
        catalog = font_catalog._get_n3_font_catalog(123)
    finally:
        font_catalog._get_n3_font_catalog.cache_clear()

    assert catalog.authoritative is True
    assert catalog.families == ("メイリオ",)
    assert catalog.canonicalize("Meiryo") == "メイリオ"
    assert catalog.qt_family("メイリオ") == "Meiryo"


def test_sug_alias_records_map_langids_to_catalog_locales(monkeypatch):
    from strange_uta_game.frontend import font_names

    monkeypatch.setattr(
        font_names,
        "localized_alias_map",
        lambda: {
            "UD Digi Kyokasho N-B": {
                0x0411: "UD デジタル 教科書体 N-B",
                0x0804: "UD 数字教科书体 N-B",
            }
        },
    )
    records = font_catalog._sug_alias_records()

    assert len(records) == 1
    names = dict(records[0].names)
    assert names["en-us"] == "UD Digi Kyokasho N-B"
    assert names["ja-jp"] == "UD デジタル 教科書体 N-B"
    assert names["zh-cn"] == "UD 数字教科书体 N-B"

    catalog = _build_catalog(records, compare=_compare)
    assert catalog.canonicalize("UD Digi Kyokasho N-B") == "UD デジタル 教科書体 N-B"
    assert catalog.canonicalize("UD 数字教科书体 N-B") == "UD デジタル 教科書体 N-B"


def test_empty_directwrite_catalog_falls_back_to_qt(monkeypatch):
    monkeypatch.setattr(font_catalog.sys, "platform", "win32")
    monkeypatch.setattr(font_catalog, "_sug_alias_records", lambda: [])
    monkeypatch.setattr(font_catalog, "_directwrite_records", lambda: [])
    monkeypatch.setattr(font_catalog.QFontDatabase, "families", lambda: ["Arial"])
    font_catalog._get_n3_font_catalog.cache_clear()
    try:
        catalog = font_catalog._get_n3_font_catalog(123)
    finally:
        font_catalog._get_n3_font_catalog.cache_clear()

    assert catalog.families == ("Arial",)
    assert catalog.authoritative is False


def test_unexpected_directwrite_error_falls_back_to_qt(monkeypatch):
    def broken_enum():
        raise ctypes.ArgumentError("vtable mismatch")

    monkeypatch.setattr(font_catalog.sys, "platform", "win32")
    monkeypatch.setattr(font_catalog, "_sug_alias_records", lambda: [])
    monkeypatch.setattr(font_catalog, "_directwrite_records", broken_enum)
    monkeypatch.setattr(font_catalog.QFontDatabase, "families", lambda: ["Arial"])
    font_catalog._get_n3_font_catalog.cache_clear()
    try:
        catalog = font_catalog._get_n3_font_catalog(123)
    finally:
        font_catalog._get_n3_font_catalog.cache_clear()

    assert catalog.families == ("Arial",)
    assert catalog.authoritative is False


@pytest.mark.skipif(sys.platform != "win32", reason="DirectWrite is Windows-only")
def test_windows_catalog_canonicalizes_ud_kyokasho_alias_when_installed():
    catalog = get_n3_font_catalog()
    canonical = catalog.canonicalize("UD Digi Kyokasho N-B")
    if canonical is None:
        pytest.skip("UD Digi Kyokasho N-B is not installed")

    assert catalog.authoritative is True
    assert canonical == "UD デジタル 教科書体 N-B"
    assert canonical in catalog.families
    assert len(catalog.families) == len(set(catalog.families))
