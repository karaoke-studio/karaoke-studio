"""Regression tests for the built-in N3-aligned title scheme."""

from krok_helper.subtitle_render.models import (
    default_title_scheme,
    migrate_legacy_app_title_default,
    style_from_dict,
)


def test_old_title_scheme_backfills_own_latin_settings():
    restored = style_from_dict(
        {
            "font_family_latin": "Comic Sans MS",
            "latin_font_size_px": 66,
            "latin_font_weight": 900,
            "latin_stroke_width_px": 1,
            "latin_stroke2_enabled": False,
            "latin_stroke2_width_px": 0,
            "custom_style_schemes": {
                "标题": {
                    "font_family": "游明朝",
                    "font_size_px": 100,
                    "font_weight": 400,
                    "stroke_width_px": 15,
                    "stroke2_enabled": True,
                    "stroke2_width_px": 5,
                }
            },
        }
    )

    title = restored.custom_style_schemes["标题"]
    assert title.font_family_latin == "游明朝"
    assert title.latin_font_size_px == 100
    assert title.latin_font_weight == 400
    assert title.latin_stroke_width_px == 15
    assert title.latin_stroke2_enabled is True
    assert title.latin_stroke2_width_px == 5


def test_application_legacy_builtin_title_migrates_to_information_small():
    legacy = style_from_dict(
        {
            "custom_style_schemes": {
                "标题": {
                    "font_family": "游明朝",
                    "font_size_px": 100,
                    "font_weight": 400,
                    "stroke_width_px": 15,
                    "stroke2_enabled": True,
                    "stroke2_width_px": 5,
                    "decoration_kind": "glow",
                    "glow_radius_px": 10,
                    "glow_before_radius_px": 10,
                    "karaoke_colors": {
                        "before": {
                            "text": {"color": "#FFEBEB"},
                            "stroke": {"color": "#000000"},
                            "stroke2": {"color": "#FFFFFF"},
                            "shadow": {"color": "#E19696"},
                        },
                        "after": {
                            "text": {"color": "#FFEBEB"},
                            "stroke": {"color": "#000000"},
                            "stroke2": {"color": "#FFFFFF"},
                            "shadow": {"color": "#E19696"},
                        },
                    },
                }
            }
        }
    )

    migrated = migrate_legacy_app_title_default(legacy)

    assert migrated.custom_style_schemes["标题"] == default_title_scheme()
