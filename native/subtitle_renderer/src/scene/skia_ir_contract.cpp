#include "skia_ir_contract.h"
#include "../model/backend_caps.h"
#include <algorithm>
#include <cmath>
#include <limits>

namespace krok::subtitle::native::scene {
namespace {
int integer(const QJsonObject& o, const char* key, int minimum, int maximum) {
    const auto v = o.value(QLatin1String(key));
    const double number = v.toDouble(std::numeric_limits<double>::quiet_NaN());
    if (!v.isDouble() || !std::isfinite(number) || std::floor(number) != number || number < minimum || number > maximum)
        throw BackendError(std::string("Skia IR: invalid ") + key);
    return int(number);
}
}

const QJsonArray& skiaIrProfiles() {
    static const QJsonArray profiles{
        "p04_ascii_solid_wipe",
        "p05_resolved_horizontal",
        "p05_single_line_placement",
        "p05_inline_roles",
        "p05_fixed_row_grid",
        "p05_page_collisions",
        "p05_smart_horizontal",
        "p05_segmented_wipe",
        "p05_resolved_ruby",
        "p05_upper_ruby",
        "p05_overlap_wipe",
        "p05_lower_ruby",
        "p05_ruby_markers",
        "p05_ruby_units",
        "p05_font_fallback",
        "p05_mixed_fonts",
        "p05_standalone_marks",
        "p07_no_wipe",
        "p07_zero_ruby_no_wipe",
        "p05_horizontal_rtl",
        "p05_horizontal_rtl_ruby",
        "p05_rtl_ruby_without_body_stroke",
        "p05_rtl_lower_ruby",
        "p06_rtl_ruby_shadow",
        "p07_rtl_ruby_singing_scale",
        "p07_rtl_ruby_no_wipe",
        "p07_rtl_ruby_line_animation",
        "p07_rtl_ruby_unscaled_line_animation",
        "p05_horizontal_rtl_cells",
        "p05_vertical_solid",
        "p05_vertical_japanese_glyphs",
        "p05_vertical_text_cells",
        "p05_vertical_column_offsets",
        "p05_vertical_ruby",
        "p05_vertical_ruby_extended_units",
        "p05_vertical_columns",
        "p06_vertical_outline",
        "p06_vertical_glow",
        "p06_vertical_reverse_glow",
        "p06_vertical_reverse_shadow",
        "p06_vertical_shadow",
        "p06_vertical_ruby_decoration",
        "p07_vertical_glow_no_wipe",
        "p07_vertical_shadow_no_wipe",
        "p07_vertical_mixed_decoration_no_wipe",
        "p07_vertical_columns_decoration_no_wipe",
        "p07_vertical_reverse_decoration_no_wipe",
        "p07_vertical_decoration_fade",
        "p07_vertical_decoration_line_motion",
        "p07_vertical_decoration_no_wipe_animation",
        "p06_vertical_reverse_outline",
        "p06_vertical_columns_outline",
        "p06_vertical_columns_decoration",
        "p06_vertical_columns_ruby_decoration",
        "p06_vertical_mixed_column_decoration",
        "p07_vertical_columns_decoration_fade",
        "p06_vertical_ruby_outline",
        "p06_vertical_ruby_group_paint",
        "p05_vertical_ruby_groups",
        "p05_vertical_columns_ruby",
        "p05_vertical_shifted_ruby",
        "p05_vertical_rotated_ruby",
        "p05_vector_guides",
        "p05_vertical_vector_guides",
        "p05_rtl_vector_guides",
        "p05_role_vector_guides",
        "p05_vector_guide_composition",
        "p05_vector_guide_lower_ruby",
        "p06_inline_vector_guide_outline",
        "p08_bitmap_guides",
        "p08_vertical_bitmap_guides",
        "p08_bitmap_lower_ruby",
        "p08_inline_bitmap_guides",
        "p08_inline_bitmap_decoration",
        "p08_reverse_bitmap_guides",
        "p08_mixed_reverse_bitmap_guides",
        "p08_rtl_bitmap_guides",
        "p08_bitmap_guide_shadow",
        "p08_bitmap_guide_gradient_shadow",
        "p08_animated_title_guides",
        "p08_animated_bitmap_guides",
        "p08_static_line_band",
        "p08_line_band_fit",
        "p08_line_band_slices",
        "p08_ruby_line_band",
        "p08_lower_ruby_line_band",
        "p08_staff_line_band",
        "p08_animated_staff_line_band",
        "p08_animated_line_band",
        "p08_line_band_image_sequence",
        "p08_tinted_line_band",
        "p08_line_band_with_effects",
        "p08_line_band_transition",
        "p08_line_band_outer_stroke",
        "p08_line_band_stroke_position",
        "p08_line_band_blur",
        "p08_line_band_animated_decoration",
        "p08_multiple_sources",
        "p08_multiple_vertical_sources",
        "p08_solid_title",
        "p08_title_role_outlines",
        "p06_primary_solid_outline",
        "p06_latin_primary_outline",
        "p06_latin_secondary_outline",
        "p06_secondary_solid_outline",
        "p06_role_solid_outlines",
        "p06_role_body_paints",
        "p06_ruby_role_paints",
        "p06_multi_lane_outline",
        "p06_body_gradient",
        "p06_unscaled_paint_fade",
        "p06_unscaled_paint_scanline",
        "p06_outline_gradient",
        "p06_image_fill",
        "p06_ruby_main_paint",
        "p06_utopia_main_paint",
        "p06_ruby_fill_gradient",
        "p06_faded_scanline_gradient_fill",
        "p06_ruby_fill_image",
        "p06_independent_ruby_fill",
        "p06_outline_ruby",
        "p06_ruby_solid_outline",
        "p06_ruby_outline_gradient",
        "p06_ruby_horizontal_outline",
        "p06_role_outline_gradient",
        "p06_role_outline_paints",
        "p06_animated_singing_gradient_fill",
        "p06_faded_scanline_palette_gradient",
        "p06_static_shadow",
        "p06_body_gradient_shadow",
        "p06_ruby_shadow",
        "p06_ruby_gradient_shadow",
        "p06_static_glow",
        "p06_reverse_decoration",
        "p07_reverse_decoration_no_wipe",
        "p07_reverse_decoration_line_animation",
        "p07_horizontal_shadow_line_animation",
        "p07_horizontal_glow_line_animation",
        "p06_gradient_glow",
        "p06_role_static_glow",
        "p06_vertical_gradient_glow",
        "p06_role_gradient_glow",
        "p06_ruby_glow",
        "p06_ruby_role_glow",
        "p06_lower_ruby_glow",
        "p06_lower_ruby_shadow",
        "p06_lower_ruby_shadow_gradient",
        "p07_line_fade",
        "p07_role_line_fade",
        "p07_line_motion",
        "p07_ruby_line_animation",
        "p07_lower_ruby_decoration_line_animation",
        "p07_lower_ruby_unscaled_line_animation",
        "p07_lower_ruby_no_wipe_decoration",
        "p07_singing_scale",
        "p07_vertical_singing_scale",
        "p07_vertical_reverse_singing",
        "p07_horizontal_reverse_singing",
        "p07_vertical_zoom_pulse",
        "p07_vertical_singing_line_animation",
        "p07_vertical_singing_shadow",
        "p07_vertical_ruby_singing_shadow",
        "p07_vertical_columns_singing_scale",
        "p07_role_singing_scale",
        "p07_role_singing_glow",
        "p07_singing_glow",
        "p07_ruby_singing_scale",
        "p07_ruby_zoom_pulse",
        "p07_ruby_zoom_line_animation",
        "p07_ruby_zoom_noise",
        "p07_zoom_pulse_decoration",
        "p07_utopia_line_animation",
        "p07_utopia_transition",
        "p07_ruby_utopia_exit",
        "p07_ruby_utopia_entry",
        "p07_ruby_utopia_entry_glow",
        "p07_ruby_utopia_exit_glow",
        "p07_ruby_utopia_exit_body_glow",
        "p07_character_fade",
        "p07_character_entry_fade_composition",
        "p07_ruby_character_exit_fade",
        "p07_scanline_band",
        "p07_scanline_shadow",
        "p07_scanline_static_cells",
        "p07_scanline_character_transform",
        "p07_no_wipe_character_transform",
        "p07_directional_character_transform",
        "p07_character_transform_cells",
        "p07_scanline_utopia_entry",
        "p07_vertical_scanline_band",
        "p07_vertical_reverse_scanline",
        "p07_vertical_mixed_scanline",
        "p07_vertical_scanline_line_animation",
        "p07_vertical_scanline_singing",
        "p07_vertical_reverse_scanline_singing",
        "p07_vertical_singing_scanline_line_animation",
        "p07_vertical_zoom_scanline",
        "p07_vertical_scanline_palette",
        "p07_vertical_reverse_scanline_palette",
        "p07_vertical_singing_scanline_palette",
        "p07_vertical_scanline_decoration",
        "p07_scanline_line_animation",
        "p07_faded_scanline_singing_particles",
        "p07_inline_role_faded_scanline",
        "p07_faded_scanline_prefix_guides",
        "p07_faded_scanline_particle_paint",
        "p07_scanline_unscaled_line_animation",
        "p07_scanline_shape",
        "p07_scanline_shape_line_animation",
        "p07_shaped_scanline_singing_particles",
        "p07_ruby_band_singing_particles",
        "p07_scanline_image",
        "p07_reverse_scanline_image",
        "p07_original_scanline_image_palette",
        "p07_composed_scanline",
        "p07_role_scanline_layers",
        "p07_scanline_role_gradient",
        "p07_role_scanline_singing",
        "p07_scanline_shape_palette",
        "p07_rtl_scanline_band",
        "p07_ruby_scanline",
        "p07_lower_ruby_scanline",
        "p07_lower_ruby_scanline_line_animation",
        "p07_lower_ruby_scanline_singing_particles",
        "p06_lower_ruby_fill_gradient",
        "p06_lower_ruby_static_gradient",
        "p06_lower_ruby_static_image",
        "p07_ruby_scanline_particles",
        "p07_character_transform",
        "p07_translucent_character_outline",
        "p07_energy_transition_outlines",
        "p07_energy_transition_line_animation",
        "p07_energy_transition_singing_stars",
        "p07_sing_twinkle",
        "p07_singing_particle_line_animation",
        "p07_vertical_sing_twinkle",
        "p07_vertical_sing_shapes",
        "p07_vertical_sing_falling",
        "p07_vertical_sing_lightning",
        "p07_vertical_sing_flame",
        "p07_vertical_singing_with_particles",
        "p07_vertical_reverse_singing_particles",
        "p07_reverse_singing_particles",
        "p07_no_wipe_singing_particles",
        "p07_vertical_singing_particle_line_animation",
        "p07_vertical_scanline_singing_particles",
        "p07_vertical_scanline_particles",
        "p07_vertical_reverse_scanline_particles",
        "p07_vertical_scanline_particle_line_animation",
        "p07_vertical_outlined_singing_particles",
        "p07_vertical_decorated_singing_particles",
        "p07_vertical_no_wipe_singing_particles",
        "p07_stroke_flash",
        "p07_composed_stroke_flash",
        "p07_no_wipe_decoration",
        "p07_noise_transition",
        "p07_noise_composition",
        "p07_ruby_character_exit_spin",
        "p07_ruby_character_entry_spin",
        "p07_ruby_character_drip",
        "p07_ruby_character_drip_pair",
        "p07_ruby_character_drip_glow",
        "p07_ruby_character_translation",
        "p07_ruby_character_affine_exit",
        "p07_ruby_character_stretch_exit",
        "p07_ruby_character_stretch_entry",
        "p07_ruby_character_glow_transition",
        "p07_ruby_character_wave_scatter",
        "p07_ruby_particle_assembly",
        "p07_ruby_particle_assembly_pair",
        "p07_ruby_particle_fade",
        "p07_ruby_energy_pair",
        "p07_ruby_same_energy_pair",
        "p07_ruby_particle_entry",
        "p07_ruby_particle_entry_glow",
        "p07_ruby_particle_entry_reading_glow",
        "p07_ruby_symbol_entry_glow",
        "p07_ruby_particle_entry_exit",
        "p07_ruby_mixed_particle_entry_exit",
        "p07_ruby_petal_entry_flame_singing",
        "p07_ruby_petal_entry_scanline",
        "p07_ruby_petal_scanline_note_exit",
        "p07_ruby_petal_scanline_image",
        "p08_viewport",
        "p08_vertical_viewport",
        "p08_vertical_shadow_viewport",
        "p08_vertical_scanline_viewport",
        "p08_vertical_scanline_palette_viewport",
        "p08_vertical_singing_scanline_viewport",
        "p08_custom_shape_signal",
        "p08_custom_image_signal",
        "p08_decorated_shape_signal",
        "p08_vector_shape_signal",
        "p08_volume_signal",
        "p05_horizontal_rtl_kana_cells",
        "p05_horizontal_rtl_kana_marks",
        "p05_reverse_wipe",
        "p05_reverse_overlap",
        "p05_vertical_reverse_ruby_track",
    };
    return profiles;
}

bool isSkiaFallingParticle(const QString& kind) {
    return kind == "petal" || kind == "feather" || kind == "snow" || kind == "snow_solid";
}

QString skiaParticleKindForTransition(const QString& animation, bool entry) {
    if (isSkiaFallingParticle(animation) || animation == "note" || animation == "heart"
        || animation == "ripple" || animation == "sparkle" || isSkiaEnergyTransition(animation))
        return animation;
    if (entry && animation == "assemble_in") return QStringLiteral("assemble");
    if (!entry && animation == "dissolve_out") return QStringLiteral("dissolve");
    return {};
}

bool isSkiaEnergyTransition(const QString& animation) {
    return animation == "flame" || animation == "lightning";
}

bool allowsSkiaRubyParticleTransition(const QString& entry, const QString& exit,
                                      const QJsonArray& features) {
    if (entry == "petal" && exit == "note")
        return features.contains("p07_ruby_petal_scanline_note_exit");
    if (entry == "assemble" && exit == "dissolve")
        return features.contains("p07_ruby_particle_assembly_pair");
    const auto canPair = [](const QString& kind) { return kind == "petal" || kind == "feather" || kind == "heart"; };
    if ((canPair(entry) && canPair(exit)) || (entry == "petal" && (exit == "sparkle" || exit == "ripple")))
        return features.contains(entry == exit ? "p07_ruby_particle_entry_exit" : "p07_ruby_mixed_particle_entry_exit");
    if (entry.isEmpty() == exit.isEmpty())
        return features.contains("p07_ruby_energy_pair")
            && (entry != exit || features.contains("p07_ruby_same_energy_pair"))
            && isSkiaEnergyTransition(entry) && isSkiaEnergyTransition(exit);
    const auto& kind = exit.isEmpty() ? entry : exit;
    if (kind == "assemble" || kind == "dissolve") return features.contains("p07_ruby_particle_assembly");
    const bool allowFade = features.contains("p07_ruby_particle_fade");
    if (isSkiaEnergyTransition(kind)) return allowFade;
    const bool ordinaryParticle = isSkiaFallingParticle(kind) || kind == "note" || kind == "heart"
        || kind == "ripple" || kind == "sparkle";
    return ordinaryParticle && (exit.isEmpty() ? features.contains("p07_ruby_particle_entry") : allowFade);
}

bool isSkiaTransitionLayer(const QString& transition, const QString& kind, bool entry) {
    // Ignition/strike impacts accompany both energy transitions.
    const bool impact = kind == "flash" || kind == "shockwave" || kind == "spark";
    if (transition == "flame")
        return impact || kind == "flame" || kind == "flame_alt" || kind == "flame_glow" || kind == "ember"
            || kind == "fireball" || (!entry && kind == "ash");
    if (transition == "lightning")
        return impact || kind == "lightning" || kind == "lightning_glow";
    return kind == transition;
}

bool hasSkiaVerticalParticleProfile(const QJsonArray& features) {
    return features.contains("p07_vertical_sing_twinkle")
        || features.contains("p07_vertical_sing_shapes") || features.contains("p07_vertical_sing_falling")
        || features.contains("p07_vertical_sing_lightning") || features.contains("p07_vertical_sing_flame")
        || features.contains("p07_vertical_scanline_particles");
}

void validateSkiaIrEnvelope(const QJsonObject& ir) {
    if (integer(ir, "schema", 3, 3) != 3) throw BackendError("Skia requires IR schema 3");
    const auto features = ir.value("required_features").toArray();
    const auto layout = ir.value("skia_layout").toObject();
    const auto style = ir.value("style").toObject();
    for (const auto& feature : features)
        if (!skiaIrProfiles().contains(feature))
            throw BackendError("Skia IR: unknown required feature");
    if (features.contains("p07_ruby_same_energy_pair") && !features.contains("p07_ruby_energy_pair"))
        throw BackendError("Skia 同类粒子出入场缺少组合能力");
    if (features.contains("p07_ruby_particle_assembly_pair") && !features.contains("p07_ruby_particle_assembly"))
        throw BackendError("Skia 拼接入场与消散退场缺少粒子能力");
    if ((features.contains("p07_ruby_particle_entry_exit") || features.contains("p07_ruby_mixed_particle_entry_exit"))
        && !features.contains("p07_ruby_particle_entry"))
        throw BackendError("Skia 无装饰粒子出入场缺少粒子能力");
    if (features.contains("p07_ruby_particle_entry_glow") && !features.contains("p07_ruby_particle_entry"))
        throw BackendError("Skia 上注音粒子入场光晕缺少粒子入场能力");
    if (features.contains("p07_ruby_particle_entry_reading_glow")
        && (!features.contains("p07_ruby_particle_entry") || !features.contains("p06_ruby_glow")))
        throw BackendError("Skia 粒子入场的独立上注音光晕缺少对应能力");
    if (features.contains("p07_ruby_symbol_entry_glow") && !features.contains("p07_ruby_particle_entry_glow"))
        throw BackendError("Skia 星光、涟漪或音符入场缺少光晕能力");
    if (features.contains("p06_reverse_decoration") && (!features.contains("p05_reverse_wipe")
        || !(features.contains("p06_static_glow") || features.contains("p06_static_shadow"))))
        throw BackendError("Skia 横排反向走字的实色装饰缺少对应能力");
    if (features.contains("p07_reverse_decoration_no_wipe") && (!features.contains("p06_reverse_decoration")
        || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 横排反向整字变色缺少装饰或变色能力");
    if (features.contains("p07_reverse_decoration_line_animation") && (!features.contains("p06_reverse_decoration")
        || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 横排反向实色装饰缺少整行出入场能力");
    if (features.contains("p07_horizontal_shadow_line_animation")
        && (!(features.contains("p06_static_shadow") || features.contains("p06_ruby_shadow"))
            || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 横排阴影缺少整行出入场能力");
    if (features.contains("p07_horizontal_glow_line_animation")
        && (!(features.contains("p06_static_glow") || features.contains("p06_ruby_glow"))
            || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 横排光晕缺少整行出入场能力");
    if (features.contains("p07_ruby_petal_entry_flame_singing") && !features.contains("p07_ruby_particle_entry"))
        throw BackendError("Skia 花瓣入场与唱字火焰缺少粒子入场能力");
    if (features.contains("p07_ruby_petal_entry_scanline")
        && (!features.contains("p07_ruby_petal_entry_flame_singing") || !features.contains("p07_ruby_scanline")))
        throw BackendError("Skia 花瓣入场的扫字线缺少粒子或上注音扫字线能力");
    if (features.contains("p07_ruby_petal_scanline_note_exit") && !features.contains("p07_ruby_petal_entry_scanline"))
        throw BackendError("Skia 音符退场缺少对应花瓣入场扫字线能力");
    if (features.contains("p07_ruby_petal_scanline_image")
        && (!features.contains("p07_ruby_petal_scanline_note_exit") || !features.contains("p07_scanline_image")
            || !features.contains("p07_shaped_scanline_singing_particles")))
        throw BackendError("Skia 粒子出入场缺少对应图片扫字线能力");
    if (ir.contains("required_features") && !ir.value("required_features").isArray())
        throw BackendError("Skia IR: required_features must be an array");
    if (ir.contains("skia_extra_sources") || features.contains("p08_multiple_sources")
        || ir.contains("skia_titles") || features.contains("p08_solid_title"))
        throw BackendError("Skia: 场景配置缺少规范布局或包含嵌套来源");
    if (features.contains("p07_noise_composition")
        && (!features.contains("p07_noise_transition") || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 噪声出入场组合缺少对应布局能力");
    if (features.contains("p08_vertical_viewport")
        && (!features.contains("p08_viewport") || !features.contains("p05_vertical_solid")
            || !style.value("vertical").toBool()))
        throw BackendError("Skia 竖排视口变换缺少竖排布局或视口能力声明");
    const bool hasStaticShadows = features.contains("p06_static_shadow") || features.contains("p06_ruby_shadow");
    const bool verticalShadowViewport = features.contains("p08_vertical_shadow_viewport");
    if (verticalShadowViewport != (features.contains("p08_vertical_viewport") && hasStaticShadows))
        throw BackendError("Skia 竖排阴影视口变换缺少对应布局能力");
    const bool verticalScanlineViewport = features.contains("p08_vertical_scanline_viewport");
    const bool verticalScanlinePalette = features.contains("p07_vertical_scanline_palette");
    if (verticalScanlineViewport
        && (!features.contains("p08_vertical_viewport") || !features.contains("p07_vertical_scanline_band")
            || features.contains("p07_vertical_reverse_scanline") || features.contains("p07_vertical_scanline_line_animation")
            || features.contains("p07_vertical_singing_scanline_line_animation")))
        throw BackendError("Skia 竖排扫字线视口当前仅支持无整行出入场的正向走字");
    if (features.contains("p08_vertical_scanline_palette_viewport") != (verticalScanlinePalette && verticalScanlineViewport))
        throw BackendError("Skia 竖排扫字线取色缺少对应视口能力");
    if (features.contains("p08_vertical_singing_scanline_viewport")
        != (verticalScanlineViewport && features.contains("p07_vertical_scanline_singing")))
        throw BackendError("Skia 竖排唱字扫字线缺少对应视口能力");
    if (features.contains("p08_viewport")) {
        integer(style, "viewport_scale_pct", 1, 400);
        integer(style, "viewport_rotation_deg", -180, 180);
        integer(style, "viewport_offset_x", -4000, 4000);
        integer(style, "viewport_offset_y", -4000, 4000);
        const QJsonArray anchors{"top_left", "top_center", "top_right", "center_left", "center", "center_right",
                                 "bottom_left", "bottom_center", "bottom_right"};
        if (!anchors.contains(style.value("viewport_align")))
            throw BackendError("Skia 字幕视口锚点无效");
        const QJsonArray incompatible{"p06_image_fill", "p06_body_gradient",
            "p07_noise_transition", "p07_sing_twinkle", "p07_stroke_flash",
            "p07_character_transform", "p07_character_fade", "p07_utopia_transition"};
        if (hasStaticShadows && !verticalShadowViewport)
            throw BackendError("Skia 字幕视口变换暂不支持此阴影组合");
        if (features.contains("p07_scanline_band") && !verticalScanlineViewport)
            throw BackendError("Skia 字幕视口变换暂不支持此扫字线组合");
        for (const auto& capability : incompatible) {
            if (features.contains(capability))
                throw BackendError("Skia 字幕视口变换暂不支持此材质或特效");
        }
    }
    if (features.contains("p05_overlap_wipe")
        && layout.value("overlap_schema").toDouble(-1) != 1)
        throw BackendError("Skia 重叠走字布局版本无效");
    if (features.contains("p05_ruby_units")
        && layout.value("ruby_unit_schema").toDouble(-1) != 1)
        throw BackendError("Skia 注音计时单元布局版本无效");
    if (features.contains("p05_font_fallback")
        && layout.value("font_chain_schema").toDouble(-1) != 1)
        throw BackendError("Skia 字体链布局版本无效");
    if (features.contains("p05_mixed_fonts")
        && layout.value("mixed_font_schema").toDouble(-1) != 1)
        throw BackendError("Skia 混合字体布局版本无效");
    if (features.contains("p05_standalone_marks")
        && layout.value("standalone_mark_schema").toDouble(-1) != 1)
        throw BackendError("Skia 独立组合标记布局版本无效");
    if (features.contains("p07_no_wipe")
        && layout.value("no_wipe_schema").toDouble(-1) != 1)
        throw BackendError("Skia 整字变色布局版本无效");
    if (features.contains("p07_rtl_ruby_no_wipe")
        && (!features.contains("p05_horizontal_rtl_ruby") || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 右至左上注音整字变色缺少对应布局能力");
    if (features.contains("p05_rtl_ruby_without_body_stroke") && !features.contains("p05_horizontal_rtl_ruby"))
        throw BackendError("Skia 无正文描边的右至左上注音缺少布局能力");
    if (features.contains("p05_rtl_lower_ruby")
        && (!features.contains("p05_horizontal_rtl_ruby") || !features.contains("p05_lower_ruby")))
        throw BackendError("Skia 右至左下方注音缺少方向或布局能力");
    if (features.contains("p07_rtl_ruby_line_animation")
        && (!features.contains("p07_rtl_ruby_singing_scale") || !features.contains("p07_utopia_line_animation")))
        throw BackendError("Skia 右至左上注音整行出入场缺少对应能力");
    if (features.contains("p07_rtl_ruby_unscaled_line_animation")
        && (!features.contains("p05_horizontal_rtl_ruby") || !features.contains("p07_ruby_line_animation")))
        throw BackendError("Skia 右至左上注音整行出入场缺少对应能力");
    if (features.contains("p07_zero_ruby_no_wipe")
        && (layout.value("zero_ruby_no_wipe_schema").toDouble(-1) != 1
            || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 零时长注音整字变色缺少版本化能力声明");
    if (features.contains("p05_horizontal_rtl")
        && layout.value("horizontal_rtl_schema").toDouble(-1) != 1)
        throw BackendError("Skia 右至左单元排列布局版本无效");
    if (features.contains("p06_rtl_ruby_shadow") && (!features.contains("p05_horizontal_rtl_ruby")
        || !(features.contains("p06_static_shadow") || features.contains("p06_ruby_shadow"))))
        throw BackendError("Skia 右至左阴影缺少对应布局或装饰能力");
    if (features.contains("p05_horizontal_rtl_cells")
        && (layout.value("horizontal_rtl_cell_schema").toDouble(-1) != 1
            || !features.contains("p05_horizontal_rtl")))
        throw BackendError("Skia 右至左多码点单元布局版本无效");
    if (features.contains("p05_horizontal_rtl_kana_cells")
        && (layout.value("horizontal_rtl_kana_schema").toDouble(-1) != 1
            || !features.contains("p05_horizontal_rtl_cells")))
        throw BackendError("Skia 右至左假名单元布局版本无效");
    if (features.contains("p05_horizontal_rtl_kana_marks")
        && (layout.value("kana_mark_schema").toDouble(-1) != 1
            || !features.contains("p05_horizontal_rtl_kana_cells")))
        throw BackendError("Skia 右至左假名浊音布局版本无效");
    if (features.contains("p05_vertical_solid")) {
        const auto version = layout.value("vertical_solid_schema").toDouble(-1);
        if (version != 1 && version != 2)
            throw BackendError("Skia 竖排实色正文布局版本无效");
    }
    if (features.contains("p05_vertical_japanese_glyphs")
        && (layout.value("vertical_japanese_glyph_schema").toDouble(-1) != 1
            || !features.contains("p05_vertical_solid")
            || !style.value("vertical").toBool()))
        throw BackendError("Skia 竖排日文字形布局版本无效");
    if ((features.contains("p05_vertical_text_cells") || features.contains("p05_vertical_column_offsets"))
        && !features.contains("p05_vertical_solid"))
        throw BackendError("Skia 竖排文本单元或列偏移缺少固定列布局能力声明");
    if (features.contains("p05_vertical_ruby")) {
        const double version = layout.value("vertical_ruby_schema").toDouble(-1);
        if ((version != 1 && version != 2)
            || !features.contains("p05_vertical_solid")
            || !features.contains("p05_resolved_ruby")
            || !features.contains("p05_upper_ruby")
            || !style.value("vertical").toBool())
            throw BackendError("Skia 竖排注音布局版本无效");
    }
    if (features.contains("p05_vertical_ruby_groups") && !features.contains("p05_vertical_ruby"))
        throw BackendError("Skia 竖排多组注音缺少基础布局能力");
    const bool verticalRuby = features.contains("p05_vertical_ruby");
    if (features.contains("p05_vertical_ruby_extended_units") && !verticalRuby)
        throw BackendError("Skia 竖排注音扩展字符缺少竖排读音布局能力");
    const bool reverseVerticalRuby = features.contains("p05_vertical_reverse_ruby_track");
    if (reverseVerticalRuby != (verticalRuby && features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 含注音的竖排反向走字轨道缺少对应布局能力");
    const bool verticalColumns = features.contains("p05_vertical_columns");
    if (verticalColumns && (!features.contains("p05_vertical_solid") || !style.value("vertical").toBool()))
        throw BackendError("Skia 多列竖排缺少基础布局能力");
    if (features.contains("p05_vertical_columns_ruby") != (verticalColumns && verticalRuby))
        throw BackendError("Skia 多列竖排注音缺少对应布局能力");
    if (features.contains("p06_vertical_outline")
        && (!features.contains("p05_vertical_solid") || !style.value("vertical").toBool()
            || !features.contains("p06_primary_solid_outline")
            || (features.contains("p05_vertical_columns") && !features.contains("p06_vertical_columns_outline"))
            || (features.contains("p05_vertical_ruby") && !features.contains("p06_vertical_ruby_outline"))
            || (features.contains("p05_reverse_wipe") && !features.contains("p06_vertical_reverse_outline"))))
        throw BackendError("Skia 竖排描边缺少对应布局能力");
    if (features.contains("p06_vertical_columns_outline")
        && (!features.contains("p06_vertical_outline") || !features.contains("p05_vertical_columns")
            || !features.contains("p06_multi_lane_outline")))
        throw BackendError("Skia 多列竖排描边缺少对应布局和描边能力");
    const bool verticalGlow = features.contains("p06_vertical_glow");
    const bool reverseVerticalGlow = features.contains("p06_vertical_reverse_glow");
    const bool verticalShadow = features.contains("p06_vertical_shadow");
    const bool reverseVerticalShadow = features.contains("p06_vertical_reverse_shadow");
    const bool reverseVertical = features.contains("p05_reverse_wipe");
    const bool verticalRubyDecoration = features.contains("p06_vertical_ruby_decoration");
    const bool wholeGlow = features.contains("p07_vertical_glow_no_wipe");
    const bool wholeShadow = features.contains("p07_vertical_shadow_no_wipe");
    const bool wholeMixed = features.contains("p07_vertical_mixed_decoration_no_wipe");
    const bool wholeColumns = features.contains("p07_vertical_columns_decoration_no_wipe");
    const bool wholeReverse = features.contains("p07_vertical_reverse_decoration_no_wipe");
    const bool animatedWhole = features.contains("p07_vertical_decoration_no_wipe_animation");
    if (animatedWhole != ((wholeGlow || wholeShadow || wholeMixed)
        && (features.contains("p07_vertical_decoration_fade") || features.contains("p07_vertical_decoration_line_motion"))))
        throw BackendError("Skia 竖排装饰整字变色与出入场能力不匹配");
    if (wholeGlow || wholeShadow || wholeMixed || wholeColumns || wholeReverse) {
        const bool hasGlow = verticalGlow || (verticalRubyDecoration && features.contains("p06_ruby_glow"));
        const bool hasShadow = verticalShadow || (verticalRubyDecoration && features.contains("p06_ruby_shadow"));
        if (!features.contains("p05_vertical_solid") || !features.contains("p07_no_wipe")
            || !style.value("vertical").toBool() || (hasGlow && !features.contains("p07_no_wipe_decoration"))
            || wholeReverse != reverseVertical
            || wholeGlow != (hasGlow && !hasShadow) || wholeShadow != (hasShadow && !hasGlow)
            || wholeMixed != (hasGlow && hasShadow)
            || wholeColumns != features.contains("p05_vertical_columns")
            || (wholeColumns && !features.contains("p06_vertical_columns_decoration"))
            || !(hasGlow || hasShadow))
            throw BackendError("Skia 竖排装饰整字变色缺少对应布局或装饰能力");
    }
    if ((verticalGlow || verticalShadow)
        && (!features.contains("p05_vertical_solid") || !style.value("vertical").toBool()
            || (features.contains("p05_vertical_columns") != features.contains("p06_vertical_columns_decoration"))
            || (features.contains("p05_vertical_ruby") && !verticalRubyDecoration)
            || (reverseVertical && ((verticalGlow && !reverseVerticalGlow) || (verticalShadow && !reverseVerticalShadow)))
            || verticalGlow != features.contains("p06_static_glow")
            || verticalShadow != features.contains("p06_static_shadow")))
        throw BackendError("Skia 竖排静态装饰缺少对应布局能力");
    if ((reverseVerticalGlow || reverseVerticalShadow) && (!reverseVertical || (reverseVerticalGlow && !verticalGlow)
        || (reverseVerticalShadow && !verticalShadow)))
        throw BackendError("Skia 反向竖排静态装饰缺少对应布局能力");
    const bool columnsDecoration = features.contains("p06_vertical_columns_decoration");
    if (columnsDecoration
        && (!verticalColumns || !(verticalGlow || verticalShadow || verticalRubyDecoration)
            || (features.contains("p07_vertical_decoration_fade")
                && !features.contains("p07_vertical_columns_decoration_fade"))))
        throw BackendError("Skia 多列竖排静态装饰缺少对应能力");
    if (features.contains("p06_vertical_columns_ruby_decoration") != (columnsDecoration && verticalRubyDecoration))
        throw BackendError("Skia 多列竖排注音装饰缺少对应能力");
    if (features.contains("p07_vertical_columns_decoration_fade")
        && (!features.contains("p06_vertical_columns_decoration") || !features.contains("p07_vertical_decoration_fade")))
        throw BackendError("Skia 多列竖排装饰淡化缺少对应能力");
    if (features.contains("p06_vertical_mixed_column_decoration")
        && (!features.contains("p06_vertical_columns_decoration") || !reverseVertical))
        throw BackendError("Skia 正反混合竖排列缺少相应走字装饰能力");
    const bool rubyGroupPaint = features.contains("p06_vertical_ruby_group_paint");
    if (verticalRubyDecoration
        && (!features.contains("p05_vertical_ruby") || (features.contains("p05_vertical_ruby_groups") && !rubyGroupPaint)
            || !(verticalGlow || verticalShadow || features.contains("p06_ruby_glow") || features.contains("p06_ruby_shadow"))))
        throw BackendError("Skia 竖排注音静态装饰缺少对应布局能力");
    if (features.contains("p07_vertical_decoration_fade")
        && (!features.contains("p07_line_fade") || !(verticalGlow || verticalShadow || verticalRubyDecoration)))
        throw BackendError("Skia 竖排静态装饰淡化缺少对应能力");
    if (features.contains("p07_vertical_decoration_line_motion")
        && (!features.contains("p07_line_motion") || !(verticalGlow || verticalShadow || verticalRubyDecoration)))
        throw BackendError("Skia 竖排静态装饰位移缺少对应能力");
    if (features.contains("p06_vertical_reverse_outline")
        && (!features.contains("p06_vertical_outline") || !features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 反向竖排描边缺少对应能力");
    if (features.contains("p06_vertical_ruby_outline")
        && (!features.contains("p05_vertical_ruby")
            || !(features.contains("p06_vertical_outline") || features.contains("p06_ruby_solid_outline"))))
        throw BackendError("Skia 竖排注音描边缺少对应能力");
    if (rubyGroupPaint != (features.contains("p05_vertical_ruby_groups")
        && (verticalRubyDecoration || features.contains("p06_vertical_ruby_outline"))))
        throw BackendError("Skia 同列多组竖排注音缺少描边或装饰能力声明");
    if (features.contains("p05_vertical_shifted_ruby")
        && (!features.contains("p05_vertical_ruby") || layout.value("vertical_ruby_shift_schema").toDouble(-1) != 1))
        throw BackendError("Skia 竖排注音字位需要版本化能力");
    if (features.contains("p05_vertical_rotated_ruby")
        && (!features.contains("p05_vertical_ruby") || layout.value("vertical_ruby_rotation_schema").toDouble(-1) != 1))
        throw BackendError("Skia 竖排注音旋转需要版本化能力");
    if (features.contains("p05_vector_guides")
        && layout.value("vector_guide_schema").toDouble(-1) != 1)
        throw BackendError("Skia 导唱符布局版本无效");
    const bool rtlVectorGuides = features.contains("p05_rtl_vector_guides");
    const bool verticalVectorGuides = features.contains("p05_vertical_vector_guides");
    static const QJsonArray verticalGuideLayout{
        "p05_vertical_solid", "p05_vertical_japanese_glyphs", "p05_vertical_text_cells", "p05_vertical_columns",
        "p05_vertical_column_offsets",
        "p05_reverse_wipe", "p05_reverse_overlap",
        "p06_vertical_outline", "p06_vertical_columns_outline", "p06_vertical_reverse_outline",
        "p05_vertical_ruby", "p05_vertical_ruby_groups", "p05_vertical_ruby_extended_units", "p05_ruby_units",
        "p05_vertical_shifted_ruby", "p05_vertical_rotated_ruby", "p06_vertical_ruby_outline",
        "p06_vertical_ruby_group_paint", "p05_vertical_reverse_ruby_track", "p05_vertical_columns_ruby"};
    if (verticalVectorGuides && (!features.contains("p05_vector_guides") || !features.contains("p05_vertical_solid")))
        throw BackendError("Skia 竖排矢量导唱符缺少竖排或导唱符布局能力");
    if (rtlVectorGuides && (!features.contains("p05_vector_guides") || !features.contains("p05_horizontal_rtl")))
        throw BackendError("Skia 右至左导唱符缺少方向或布局能力声明");
    const bool roleVectorGuides = features.contains("p05_role_vector_guides");
    const bool lowerVectorGuides = features.contains("p05_vector_guide_lower_ruby");
    if (lowerVectorGuides != (features.contains("p05_vector_guides") && features.contains("p05_lower_ruby"))
        || (lowerVectorGuides && !features.contains("p05_vector_guide_composition")))
        throw BackendError("Skia 矢量导唱符与下方注音缺少组合能力声明");
    if (features.contains("p06_inline_vector_guide_outline")
        && (!features.contains("p05_vector_guide_composition") || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 行内矢量导唱符缺少布局或描边能力声明");
    if (roleVectorGuides && (!features.contains("p05_vector_guide_composition")
        || !features.contains("p07_role_singing_scale") || !features.contains("p07_role_line_fade")))
        throw BackendError("Skia 角色前置导唱符缺少角色唱字或整行淡化能力声明");
    if (features.contains("p05_vector_guide_composition")) {
        // The vector guides are a prefix of the final cells, including lyrics replacements.
        static const QJsonArray allowed{
            "p05_fixed_row_grid", "p05_inline_roles", "p05_overlap_wipe",
            "p05_page_collisions", "p05_resolved_horizontal", "p05_resolved_ruby",
            "p05_segmented_wipe", "p05_single_line_placement", "p05_smart_horizontal",
            "p05_upper_ruby", "p05_vector_guides", "p05_vector_guide_composition",
            "p06_outline_ruby", "p06_primary_solid_outline", "p06_ruby_glow",
            "p06_ruby_solid_outline", "p06_secondary_solid_outline", "p06_static_glow",
            "p07_ruby_singing_scale", "p07_singing_glow", "p07_singing_scale", "p06_inline_vector_guide_outline"};
        static const QJsonArray animatedGradient{
            "p05_ruby_units", "p05_ruby_markers", "p05_standalone_marks",
            "p06_body_gradient", "p06_outline_gradient", "p06_ruby_main_paint",
            "p06_utopia_main_paint", "p06_ruby_fill_gradient", "p06_ruby_outline_gradient", "p06_ruby_horizontal_outline",
            "p06_static_shadow", "p06_ruby_shadow", "p06_gradient_glow", "p06_animated_singing_gradient_fill"};
        static const QJsonArray lineMotion{
            "p06_multi_lane_outline", "p06_latin_primary_outline", "p06_latin_secondary_outline",
            "p07_line_fade", "p07_line_motion",
            "p07_utopia_line_animation", "p07_ruby_line_animation"};
        static const QJsonArray fadedScanline{
            "p06_faded_scanline_gradient_fill", "p07_faded_scanline_prefix_guides",
            "p07_scanline_band", "p07_ruby_scanline", "p07_scanline_line_animation"};
        static const QJsonArray fadedScanlineParticles{
            "p07_faded_scanline_singing_particles", "p07_faded_scanline_particle_paint",
            "p07_sing_twinkle", "p07_ruby_band_singing_particles"};
        static const QJsonArray roleGuides{
            "p05_role_vector_guides", "p06_role_solid_outlines", "p06_role_static_glow", "p06_ruby_role_glow",
            "p06_static_shadow", "p06_ruby_shadow",
            "p07_role_singing_scale", "p07_role_singing_glow", "p07_role_line_fade"};
        static const QJsonArray rtlGuides{
            "p05_rtl_vector_guides", "p05_horizontal_rtl", "p05_horizontal_rtl_cells",
            "p05_horizontal_rtl_kana_cells", "p05_horizontal_rtl_kana_marks",
            "p05_reverse_wipe", "p05_reverse_overlap"};
        static const QJsonArray verticalGuides{"p05_vertical_vector_guides", "p07_no_wipe"};
        static const QJsonArray lowerGuides{
            "p05_vector_guide_lower_ruby", "p05_lower_ruby", "p05_ruby_units", "p05_ruby_markers",
            "p05_standalone_marks", "p06_lower_ruby_glow"};
        const bool useFadedScanline = features.contains("p07_faded_scanline_prefix_guides");
        const bool useAnimatedGradients = useFadedScanline || features.contains("p06_animated_singing_gradient_fill");
        const bool useLineMotion = useAnimatedGradients || features.contains("p07_utopia_line_animation");
        const bool useFadedScanlineParticles = useFadedScanline && features.contains("p07_faded_scanline_singing_particles");
        if (!features.contains("p05_vector_guides"))
            throw BackendError("Skia 导唱符组合缺少导唱符能力");
        for (const auto& feature : features)
            if (!allowed.contains(feature) && !(useAnimatedGradients && animatedGradient.contains(feature))
                && !(useLineMotion && lineMotion.contains(feature))
                && !(useFadedScanline && fadedScanline.contains(feature))
                && !(useFadedScanlineParticles && fadedScanlineParticles.contains(feature))
                && !(roleVectorGuides && roleGuides.contains(feature)) && !(rtlVectorGuides && rtlGuides.contains(feature))
                && !(lowerVectorGuides && lowerGuides.contains(feature))
                && !(verticalVectorGuides && (verticalGuides.contains(feature) || verticalGuideLayout.contains(feature))))
                throw BackendError("Skia 导唱符组合包含未验证能力");
    }
    if (features.contains("p07_faded_scanline_prefix_guides")
        && (!features.contains("p05_vector_guide_composition") || !features.contains("p07_scanline_line_animation")
            || !features.contains("p07_line_fade")))
        throw BackendError("Skia 前置导唱符扫字线缺少淡化或导唱符布局能力声明");
    const bool inlineBitmapGuides = features.contains("p08_inline_bitmap_guides");
    const bool reverseBitmapGuides = features.contains("p08_reverse_bitmap_guides");
    const bool mixedReverseBitmapGuides = features.contains("p08_mixed_reverse_bitmap_guides");
    const bool rtlBitmapGuides = features.contains("p08_rtl_bitmap_guides");
    const bool verticalBitmapGuides = features.contains("p08_vertical_bitmap_guides");
    if (verticalBitmapGuides && (!features.contains("p08_bitmap_guides") || !features.contains("p05_vertical_solid")))
        throw BackendError("Skia 竖排图片导唱符缺少竖排或图片布局能力");
    const bool lowerRubyBitmapGuides = features.contains("p08_bitmap_lower_ruby");
    if (lowerRubyBitmapGuides != (features.contains("p08_bitmap_guides") && features.contains("p05_lower_ruby")))
        throw BackendError("Skia 图片导唱符与下方注音缺少组合能力声明");
    if (reverseBitmapGuides && (!inlineBitmapGuides || !features.contains("p05_reverse_overlap")))
        throw BackendError("Skia 反向头像缺少图片或走字布局能力声明");
    if (mixedReverseBitmapGuides && (!features.contains("p08_bitmap_guides") || !features.contains("p05_reverse_overlap")))
        throw BackendError("Skia 含正文的反向图片导唱符缺少图片或走字布局能力声明");
    if (inlineBitmapGuides && !features.contains("p08_bitmap_guides"))
        throw BackendError("Skia 行内头像缺少图片导唱符能力声明");
    if (features.contains("p08_inline_bitmap_decoration") && !inlineBitmapGuides)
        throw BackendError("Skia 行内图片装饰缺少行内头像布局能力");
    if (features.contains("p08_animated_bitmap_guides") && !features.contains("p08_bitmap_guides"))
        throw BackendError("Skia 动态图片导唱符缺少图片导唱符布局能力");
    if (rtlBitmapGuides && (!features.contains("p08_bitmap_guides") || !features.contains("p05_horizontal_rtl")))
        throw BackendError("Skia 右至左图片导唱符缺少方向或布局能力声明");
    const bool bitmapGuideShadow = features.contains("p08_bitmap_guide_shadow");
    const bool bitmapGuideGradientShadow = features.contains("p08_bitmap_guide_gradient_shadow");
    if (bitmapGuideGradientShadow && (!bitmapGuideShadow || !features.contains("p06_body_gradient_shadow")))
        throw BackendError("Skia 图片导唱符渐变阴影缺少画笔或布局能力声明");
    if (bitmapGuideShadow && !features.contains("p08_bitmap_guides"))
        throw BackendError("Skia 图片导唱符阴影缺少布局能力声明");
    if (features.contains("p08_bitmap_guides")) {
        // Verified line-prefix image guides with Utopia, upper ruby, solid outlines and glow only.
        static const QJsonArray allowed{
            "p05_fixed_row_grid", "p05_inline_roles", "p05_overlap_wipe",
            "p05_page_collisions", "p05_resolved_horizontal", "p05_resolved_ruby",
            "p05_segmented_wipe", "p05_single_line_placement", "p05_smart_horizontal",
            "p05_upper_ruby", "p05_vector_guides", "p08_bitmap_guides", "p08_animated_title_guides", "p08_animated_bitmap_guides",
            "p06_outline_ruby", "p06_primary_solid_outline", "p06_ruby_glow",
            "p06_ruby_solid_outline", "p06_secondary_solid_outline", "p06_static_glow",
            "p07_ruby_singing_scale", "p07_singing_glow", "p07_singing_scale"};
        static const QJsonArray inlineAllowed{
            "p08_inline_bitmap_guides", "p08_inline_bitmap_decoration", "p05_font_fallback", "p05_mixed_fonts",
            "p05_ruby_units", "p05_ruby_markers", "p05_standalone_marks",
            "p06_role_solid_outlines", "p06_multi_lane_outline", "p06_role_body_paints",
            "p06_ruby_role_paints", "p06_role_static_glow", "p06_ruby_role_glow",
            "p06_role_outline_gradient", "p06_role_outline_paints", "p06_ruby_outline_gradient", "p06_ruby_horizontal_outline",
            "p06_static_shadow", "p06_ruby_shadow",
            "p06_gradient_glow", "p06_vertical_gradient_glow", "p06_role_gradient_glow",
            "p07_role_singing_scale", "p07_role_singing_glow", "p07_line_fade", "p07_role_line_fade",
            "p07_ruby_line_animation", "p07_utopia_line_animation"
        };
        static const QJsonArray reverseAllowed{
            "p08_reverse_bitmap_guides", "p05_reverse_wipe", "p05_reverse_overlap", "p06_static_shadow"
        };
        static const QJsonArray mixedReverseAllowed{
            "p08_mixed_reverse_bitmap_guides", "p05_reverse_wipe", "p05_reverse_overlap", "p06_reverse_decoration"
        };
        static const QJsonArray rtlAllowed{
            "p08_rtl_bitmap_guides", "p05_horizontal_rtl", "p05_horizontal_rtl_cells",
            "p05_horizontal_rtl_kana_cells", "p05_horizontal_rtl_kana_marks"
        };
        static const QJsonArray shadowAllowed{"p08_bitmap_guide_shadow", "p06_static_shadow", "p06_ruby_shadow"};
        static const QJsonArray lowerRubyAllowed{
            "p08_bitmap_lower_ruby", "p05_lower_ruby", "p05_ruby_units", "p05_ruby_markers", "p05_standalone_marks",
            "p06_lower_ruby_glow", "p06_lower_ruby_shadow", "p06_lower_ruby_shadow_gradient", "p06_lower_ruby_static_gradient",
            "p06_lower_ruby_static_image"
        };
        if (layout.value("bitmap_guide_schema").toDouble(-1) != 1)
            throw BackendError("Skia 图片导唱符布局版本无效");
        for (const auto& feature : features)
            if (!allowed.contains(feature) && !(inlineBitmapGuides && inlineAllowed.contains(feature))
                && !(reverseBitmapGuides && reverseAllowed.contains(feature))
                && !(mixedReverseBitmapGuides && mixedReverseAllowed.contains(feature))
                && !(rtlBitmapGuides && rtlAllowed.contains(feature))
                && !(lowerRubyBitmapGuides && lowerRubyAllowed.contains(feature))
                && !(verticalBitmapGuides && (feature == QJsonValue("p08_vertical_bitmap_guides")
                    || verticalGuideLayout.contains(feature)))
                && !(bitmapGuideGradientShadow && (feature == QJsonValue("p08_bitmap_guide_gradient_shadow")
                    || feature == QJsonValue("p06_body_gradient_shadow")))
                && !(bitmapGuideShadow && shadowAllowed.contains(feature)))
                throw BackendError("Skia 图片导唱符组合包含未验证能力");
    }
    if (features.contains("p07_line_fade")
        && layout.value("line_fade_schema").toDouble(-1) != 1)
        throw BackendError("Skia 出入场淡化布局版本无效");
    if (features.contains("p07_line_motion")
        && layout.value("line_motion_schema").toDouble(-1) != 1)
        throw BackendError("Skia 出入场位移布局版本无效");
    const bool rubyLineAnimation = features.contains("p05_resolved_ruby") && !features.contains("p05_vertical_ruby")
        && (features.contains("p07_line_fade") || features.contains("p07_line_motion"));
    if (features.contains("p07_ruby_line_animation") != rubyLineAnimation)
        throw BackendError("Skia 横排注音与整行动画的能力声明不匹配");
    if (features.contains("p07_singing_scale") && layout.value("singing_scale_schema").toDouble(-1) != 1)
        throw BackendError("Skia 唱字放大布局版本无效");
    if (features.contains("p07_vertical_singing_scale")
        && (!features.contains("p05_vertical_solid") || !features.contains("p07_singing_scale")))
        throw BackendError("Skia 竖排 Utopia 唱字缺少竖排或唱字放大能力");
    if (features.contains("p07_vertical_reverse_singing")
        && (!reverseVertical || !features.contains("p07_vertical_singing_scale")))
        throw BackendError("Skia 反向竖排唱字缺少走字或逐字缩放能力");
    if (features.contains("p07_horizontal_reverse_singing")
        && (!features.contains("p05_reverse_wipe") || !features.contains("p07_singing_scale")
            || features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 反向横排缺少对应唱字放大能力");
    if (features.contains("p07_vertical_zoom_pulse")
        && !features.contains("p07_vertical_singing_scale"))
        throw BackendError("Skia 竖排整字放大缺少对应布局能力");
    if (features.contains("p07_vertical_singing_line_animation")
        && (!features.contains("p07_vertical_singing_scale") || !features.contains("p07_utopia_line_animation")))
        throw BackendError("Skia 竖排唱字缺少对应整行出入场能力");
    if (features.contains("p07_vertical_singing_shadow")
        && (!features.contains("p07_vertical_singing_scale") || !features.contains("p06_vertical_shadow")))
        throw BackendError("Skia 竖排唱字缺少对应阴影能力");
    if (features.contains("p07_vertical_ruby_singing_shadow")
        && (!features.contains("p07_vertical_singing_scale") || !features.contains("p07_ruby_singing_scale")
            || !features.contains("p06_vertical_ruby_decoration") || !features.contains("p06_ruby_shadow")))
        throw BackendError("Skia 竖排注音唱字缺少对应阴影能力");
    if (features.contains("p07_vertical_columns_singing_scale")
        != (verticalColumns && features.contains("p07_vertical_singing_scale")))
        throw BackendError("Skia 多列竖排 Utopia 唱字缺少对应布局能力");
    const bool roleSinging = features.contains("p07_role_singing_scale");
    if (roleSinging && (!features.contains("p07_singing_scale") || !features.contains("p05_inline_roles")))
        throw BackendError("Skia 角色唱字放大缺少对应能力声明");
    const bool roleFadedScanline = features.contains("p07_inline_role_faded_scanline");
    if (roleFadedScanline && (!roleSinging || !features.contains("p07_scanline_line_animation")
        || !features.contains("p07_line_fade") || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 行内角色扫字线缺少唱字或整行淡化能力声明");
    const bool roleSingingGlow = features.contains("p07_role_singing_glow");
    const bool hasRoleGlow = features.contains("p06_role_static_glow") || features.contains("p06_ruby_role_glow");
    if (roleSingingGlow != (roleSinging && hasRoleGlow)
        || (roleSingingGlow && !features.contains("p07_singing_glow")))
        throw BackendError("Skia 角色唱字光晕缺少对应能力声明");
    if (features.contains("p07_ruby_singing_scale")
        && (!features.contains("p05_resolved_ruby") || !features.contains("p07_singing_scale")))
        throw BackendError("Skia Utopia 注音缺少注音或唱字能力声明");
    if (features.contains("p07_ruby_zoom_pulse")
        && (!features.contains("p07_ruby_singing_scale") || features.contains("p05_lower_ruby")
            || features.contains("p05_ruby_markers")))
        throw BackendError("Skia 注音整字放大缺少正向上注音能力声明");
    if (features.contains("p07_ruby_zoom_line_animation")
        && (!features.contains("p07_ruby_zoom_pulse") || !features.contains("p07_ruby_line_animation")
            || !features.contains("p07_utopia_line_animation") || features.contains("p05_vertical_solid")))
        throw BackendError("Skia 注音整字放大缺少整行出入场能力声明");
    if (features.contains("p07_ruby_zoom_noise")
        && (!features.contains("p07_ruby_zoom_pulse") || !features.contains("p07_noise_composition")))
        throw BackendError("Skia 注音整字放大缺少噪声出入场能力声明");
    if (features.contains("p07_utopia_transition") && layout.value("utopia_transition_schema").toDouble(-1) != 1)
        throw BackendError("Skia Utopia 出入场布局版本无效");
    if (features.contains("p07_ruby_utopia_exit")
        && (!features.contains("p07_utopia_transition") || !features.contains("p07_ruby_singing_scale")))
        throw BackendError("Skia 上注音 Utopia 退场缺少对应能力");
    if (features.contains("p07_ruby_utopia_entry")
        && (!features.contains("p07_utopia_transition") || !features.contains("p07_ruby_singing_scale")
            || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 上注音 Utopia 入场缺少对应能力");
    if (features.contains("p07_ruby_utopia_entry_glow")
        && (!features.contains("p07_ruby_utopia_entry")
            || !(features.contains("p06_static_glow") || features.contains("p06_ruby_glow"))))
        throw BackendError("Skia 上注音 Utopia 入场光晕缺少对应能力");
    if (features.contains("p07_ruby_utopia_exit_glow")
        && (!features.contains("p07_ruby_utopia_exit") || !features.contains("p06_ruby_glow")))
        throw BackendError("Skia 上注音 Utopia 退场光晕缺少对应能力");
    if (features.contains("p07_ruby_utopia_exit_body_glow")
        && (!features.contains("p07_ruby_utopia_exit") || !features.contains("p06_static_glow")))
        throw BackendError("Skia Utopia 退场的正文光晕缺少对应能力");
    if (features.contains("p07_character_fade")
        && layout.value("character_fade_schema").toDouble(-1) != 1)
        throw BackendError("Skia 逐字淡化布局版本无效");
    if (features.contains("p07_character_entry_fade_composition") && !features.contains("p07_character_fade"))
        throw BackendError("Skia 逐字淡化组合缺少动画能力声明");
    if (features.contains("p07_ruby_character_exit_fade")
        && (!features.contains("p07_character_fade") || !features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 注音逐字淡出缺少动画或注音能力声明");
    if (features.contains("p07_scanline_shape")
        && (layout.value("scanline_shape_schema").toDouble(-1) != 1 || !features.contains("p07_scanline_band")))
        throw BackendError("Skia 扫字线形态布局版本无效");
    if (features.contains("p07_scanline_image") && !features.contains("p07_scanline_shape"))
        throw BackendError("Skia 图片扫字线缺少形态能力声明");
    if (features.contains("p07_reverse_scanline_image")
        && (!features.contains("p07_scanline_image") || !features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 反向图片扫字线缺少走字方向或图片能力声明");
    if (features.contains("p07_original_scanline_image_palette") && !features.contains("p07_scanline_image"))
        throw BackendError("Skia 原图扫字线缺少图片能力声明");
    if (features.contains("p07_scanline_shape_line_animation")
        && (!features.contains("p07_scanline_shape")
            || !(features.contains("p07_scanline_line_animation")
                || features.contains("p07_scanline_unscaled_line_animation"))))
        throw BackendError("Skia 异形扫字线缺少形态或整行出入场能力声明");
    const bool fittedLineBand = features.contains("p08_line_band_fit");
    const bool slicedLineBand = features.contains("p08_line_band_slices");
    if (features.contains("p08_staff_line_band") && !features.contains("p08_static_line_band"))
        throw BackendError("Skia 五线谱背景带缺少对应布局能力");
    if (features.contains("p08_animated_staff_line_band") && !features.contains("p08_staff_line_band"))
        throw BackendError("Skia 五线谱动画缺少对应布局能力");
    const bool customBandSequence = features.contains("p08_line_band_image_sequence");
    const bool builtinBandAnimation = features.contains("p08_animated_line_band");
    const bool simpleBandAnimation = builtinBandAnimation || customBandSequence;
    const bool tintedBand = features.contains("p08_tinted_line_band");
    const bool outerBandStroke = features.contains("p08_line_band_outer_stroke");
    const bool positionedBandStroke = features.contains("p08_line_band_stroke_position");
    const bool blurredBand = features.contains("p08_line_band_blur");
    const bool decoratedBand = outerBandStroke || positionedBandStroke || blurredBand;
    const bool animatedBand = simpleBandAnimation || features.contains("p08_animated_staff_line_band");
    if ((decoratedBand && !features.contains("p08_static_line_band")) || (outerBandStroke && positionedBandStroke)
        || features.contains("p08_line_band_animated_decoration") != (decoratedBand && animatedBand))
        throw BackendError("Skia 行背景带装饰缺少对应布局能力");
    if (features.contains("p08_line_band_with_effects")
        && (!features.contains("p08_static_line_band")
            || (!features.contains("p07_scanline_band") && !features.contains("p07_sing_twinkle"))))
        throw BackendError("Skia 行背景带缺少对应走字线或粒子能力");
    if (features.contains("p08_line_band_transition")
        && (!features.contains("p08_static_line_band")
            || (!features.contains("p07_character_fade") && !features.contains("p07_character_transform"))))
        throw BackendError("Skia 行背景带缺少对应逐字动画能力");
    if (((simpleBandAnimation || tintedBand) && !features.contains("p08_static_line_band"))
        || (customBandSequence && builtinBandAnimation)
        || (simpleBandAnimation && features.contains("p08_staff_line_band"))
        || (tintedBand && (simpleBandAnimation || features.contains("p08_animated_staff_line_band"))))
        throw BackendError("Skia 行背景带缺少对应动画或着色能力");
    const bool lowerRubyBand = features.contains("p08_lower_ruby_line_band");
    if ((lowerRubyBand && !features.contains("p08_ruby_line_band"))
        || (features.contains("p08_ruby_line_band")
            && (!features.contains("p08_static_line_band") || !features.contains("p05_resolved_ruby")
                || lowerRubyBand != features.contains("p05_lower_ruby"))))
        throw BackendError("Skia 行背景带注音缺少对应布局能力");
    if (((fittedLineBand || slicedLineBand) && !features.contains("p08_static_line_band"))
        || (fittedLineBand && slicedLineBand)
        || (features.contains("p08_static_line_band")
            && layout.value("line_band_schema").toDouble(-1) != (slicedLineBand ? 3 : fittedLineBand ? 2 : 1)))
        throw BackendError("Skia 行背景带布局版本无效");
    const auto scanlineVersion = layout.value("scanline_band_schema").toDouble(-1);
    if (features.contains("p07_rtl_scanline_band") && (!features.contains("p07_scanline_band")
        || !features.contains("p05_horizontal_rtl") || scanlineVersion < 3))
        throw BackendError("Skia 右至左扫字线缺少方向或布局能力声明");
    if (features.contains("p07_scanline_band")
        && (scanlineVersion < 1 || scanlineVersion > kScanlineBandSchema || std::floor(scanlineVersion) != scanlineVersion))
        throw BackendError("Skia 扫字线布局版本无效");
    if (features.contains("p07_vertical_scanline_band")
        && (!features.contains("p05_vertical_solid") || !features.contains("p07_scanline_band")))
        throw BackendError("Skia 竖排扫字线缺少对应布局能力");
    if (features.contains("p07_vertical_reverse_scanline")
        && (!features.contains("p07_vertical_scanline_band") || !reverseVertical
            || (features.contains("p05_resolved_ruby") && !features.contains("p07_vertical_mixed_scanline"))
            || scanlineVersion < 4))
        throw BackendError("Skia 竖排反向扫字线缺少对应布局能力");
    if (features.contains("p07_vertical_mixed_scanline")
        && (!features.contains("p07_vertical_reverse_scanline") || !reverseVerticalRuby))
        throw BackendError("Skia 正反混合的竖排扫字线缺少对应注音轨道能力");
    if (features.contains("p07_vertical_scanline_line_animation")
        && (!features.contains("p07_vertical_scanline_band") || !features.contains("p07_scanline_unscaled_line_animation")))
        throw BackendError("Skia 竖排扫字线缺少对应行出入场能力");
    if (features.contains("p07_vertical_scanline_singing")
        && (!features.contains("p07_vertical_scanline_band") || !features.contains("p07_vertical_singing_scale")
            || scanlineVersion < 5))
        throw BackendError("Skia 竖排扫字线缺少对应 Utopia 唱字能力");
    if (features.contains("p07_vertical_reverse_scanline_singing")
        && (!features.contains("p07_vertical_scanline_singing") || !features.contains("p07_vertical_reverse_singing")
            || !features.contains("p07_vertical_reverse_scanline")))
        throw BackendError("Skia 反向竖排扫字线缺少对应唱字能力");
    if (features.contains("p07_vertical_singing_scanline_line_animation")
        && (!features.contains("p07_vertical_scanline_singing") || !features.contains("p07_vertical_singing_line_animation")))
        throw BackendError("Skia 竖排唱字扫字线缺少对应整行出入场能力");
    if (features.contains("p07_vertical_zoom_scanline")
        && (!features.contains("p07_vertical_scanline_singing") || !features.contains("p07_vertical_zoom_pulse")))
        throw BackendError("Skia 竖排扫字线缺少对应整字放大能力");
    if (features.contains("p07_vertical_scanline_palette")
        && (!features.contains("p07_vertical_scanline_band") || scanlineVersion < 3))
        throw BackendError("Skia 竖排扫字线取色缺少高亮带能力");
    if (features.contains("p07_vertical_reverse_scanline_palette")
        && (!features.contains("p07_vertical_reverse_scanline") || !verticalScanlinePalette))
        throw BackendError("Skia 竖排反向扫字线缺少对应取色能力");
    if (features.contains("p07_vertical_singing_scanline_palette")
        && (!features.contains("p07_vertical_scanline_singing") || !verticalScanlinePalette))
        throw BackendError("Skia 竖排 Utopia 扫字线缺少对应取色能力");
    if (features.contains("p07_vertical_scanline_decoration")
        && (!features.contains("p07_vertical_scanline_band")
            || !(features.contains("p06_vertical_glow") || features.contains("p06_vertical_shadow")
                 || features.contains("p06_vertical_ruby_decoration"))))
        throw BackendError("Skia 竖排扫字线缺少实色装饰能力");
    if (features.contains("p07_composed_scanline")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 3))
        throw BackendError("Skia 扫字线组合缺少横排高亮带能力声明");
    if (features.contains("p07_scanline_line_animation")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 5
            || !features.contains("p07_utopia_line_animation")))
        throw BackendError("Skia 整行出入场扫字线缺少高亮带或 Utopia 行动画能力声明");
    if (features.contains("p07_scanline_utopia_entry")
        && (!features.contains("p07_scanline_band") || !features.contains("p07_utopia_transition")
            || !features.contains("p07_singing_scale") || features.contains("p05_resolved_ruby")
            || scanlineVersion < 5))
        throw BackendError("Skia Utopia 入场缺少高亮带或逐字入场能力声明");
    if (features.contains("p07_scanline_unscaled_line_animation")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 3
            || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 整行出入场扫字线缺少高亮带或行动画能力声明");
    if (features.contains("p07_role_scanline_layers")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 3))
        throw BackendError("Skia 角色扫字线组合缺少横排高亮带能力声明");
    if (features.contains("p07_role_scanline_singing")
        && (!features.contains("p07_scanline_band") || !features.contains("p07_singing_scale") || scanlineVersion < 5))
        throw BackendError("Skia 角色扫字线缺少唱字放大或扫字线能力声明");
    if (features.contains("p07_scanline_role_gradient")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 3))
        throw BackendError("Skia 渐变扫字线缺少高亮带能力声明");
    if (features.contains("p07_scanline_shape_palette") && !features.contains("p07_scanline_shape"))
        throw BackendError("Skia 扫字线配色缺少形状能力声明");
    if (features.contains("p07_scanline_shadow")
        && (!features.contains("p07_scanline_band") || !features.contains("p06_static_shadow") || scanlineVersion < 3))
        throw BackendError("Skia 扫字线阴影缺少对应协议");
    if (features.contains("p07_scanline_static_cells")
        && (!features.contains("p07_scanline_band") || scanlineVersion < 3
            || !(features.contains("p05_inline_roles") || features.contains("p05_vector_guides"))))
        throw BackendError("Skia 扫字线缺少行内角色或矢量导唱符布局");
    if (features.contains("p07_ruby_scanline")
        && (!features.contains("p07_scanline_band") || !features.contains("p05_resolved_ruby")
            || (features.contains("p05_lower_ruby") && !features.contains("p07_lower_ruby_scanline"))))
        throw BackendError("Skia 注音扫字线缺少上注音或扫字线能力声明");
    if (features.contains("p07_lower_ruby_scanline")
        && (!features.contains("p07_ruby_scanline") || !features.contains("p05_lower_ruby") || scanlineVersion < 3))
        throw BackendError("Skia 下方注音扫字线缺少布局或高亮带能力声明");
    if (features.contains("p07_lower_ruby_scanline_line_animation")
        && (!features.contains("p07_lower_ruby_scanline")
            || !(features.contains("p07_scanline_line_animation")
                || features.contains("p07_scanline_unscaled_line_animation"))))
        throw BackendError("Skia 下方注音扫字线缺少整行出入场能力");
    const auto transformVersion = layout.value("character_transform_schema").toDouble(-1);
    if (features.contains("p07_character_transform_cells")
        && (!features.contains("p07_character_transform") || transformVersion < 3
            || features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 角色或导唱符缺少逐字变换能力声明");
    if (features.contains("p07_directional_character_transform")
        && (!features.contains("p07_character_transform") || transformVersion < 3
            || (!features.contains("p05_horizontal_rtl") && !features.contains("p05_reverse_overlap"))
            || features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 逐字变换缺少走字方向能力声明");
    if (features.contains("p07_no_wipe_character_transform")
        && (!features.contains("p07_no_wipe") || !features.contains("p07_character_transform")
            || transformVersion < 3 || features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 逐字变换缺少整字变色能力声明");
    if (features.contains("p07_scanline_character_transform")
        && (!features.contains("p07_scanline_band") || !features.contains("p07_character_transform")
            || scanlineVersion < 3 || features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 扫字线缺少对应的正文逐字变换能力");
    if (features.contains("p07_character_transform")
        && (transformVersion < 1 || transformVersion > kCharacterTransformSchema || std::floor(transformVersion) != transformVersion))
        throw BackendError("Skia 逐字变换布局版本无效");
    if (features.contains("p07_translucent_character_outline")
        && (!features.contains("p07_character_transform") || transformVersion < 3
            || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 逐字半透明描边缺少逐字变换或描边能力声明");
    if (features.contains("p07_energy_transition_outlines")
        && (!features.contains("p07_character_transform") || transformVersion < 8
            || !features.contains("p07_sing_twinkle") || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 能量过渡描边缺少逐字变换、粒子或描边能力声明");
    if ((features.contains("p07_energy_transition_line_animation")
            || features.contains("p07_energy_transition_singing_stars"))
        && (!features.contains("p07_character_transform") || transformVersion < 8
            || !features.contains("p07_sing_twinkle")))
        throw BackendError("Skia 能量过渡组合缺少逐字变换或粒子能力声明");
    if (features.contains("p07_energy_transition_line_animation")
        && !(features.contains("p07_line_fade") || features.contains("p07_line_motion")))
        throw BackendError("Skia 能量过渡的整行动画缺少对应能力");
    if ((features.contains("p07_ruby_character_entry_spin") || features.contains("p07_ruby_character_exit_spin")
            || features.contains("p07_ruby_character_drip"))
        && (!features.contains("p07_character_transform") || !features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 逐字滴落或翻转缺少逐字变换或注音布局能力声明");
    if (features.contains("p07_ruby_character_drip_glow")
        && (!features.contains("p07_ruby_character_drip")
            || !(features.contains("p06_static_glow") || features.contains("p06_ruby_glow"))))
        throw BackendError("Skia 上注音逐字滴落光晕缺少对应能力");
    if (features.contains("p07_ruby_character_drip_pair") && !features.contains("p07_ruby_character_drip"))
        throw BackendError("Skia 上注音双向逐字滴落缺少对应能力");
    const auto twinkleVersion = layout.value("sing_twinkle_schema").toDouble(-1);
    if (features.contains("p07_reverse_singing_particles")
        && (!features.contains("p07_sing_twinkle") || !features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 横排反向唱字粒子缺少粒子或反向走字能力声明");
    if (features.contains("p07_no_wipe_singing_particles")
        && (!features.contains("p07_sing_twinkle") || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 整字变色缺少唱字粒子能力声明");
    const bool verticalParticles = hasSkiaVerticalParticleProfile(features);
    if (verticalParticles
        && (!features.contains("p07_sing_twinkle") || !features.contains("p05_vertical_solid")))
        throw BackendError("Skia 竖排唱字粒子缺少对应布局或粒子能力");
    if (features.contains("p07_vertical_singing_with_particles")
        && (!verticalParticles || !features.contains("p07_vertical_singing_scale")))
        throw BackendError("Skia 竖排唱字粒子组合缺少缩放或粒子能力");
    if (features.contains("p07_vertical_reverse_singing_particles")
        && (!verticalParticles || !features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 竖排反向唱字粒子缺少方向或粒子能力");
    if (features.contains("p07_vertical_singing_particle_line_animation")
        && (!verticalParticles || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 竖排唱字粒子缺少整行出入场能力");
    if (features.contains("p07_singing_particle_line_animation")
        && (!features.contains("p07_sing_twinkle")
            || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia 唱字粒子缺少整行出入场能力");
    const bool verticalBandParticles = features.contains("p07_vertical_scanline_singing_particles")
        || features.contains("p07_vertical_scanline_particles");
    if (verticalBandParticles && (twinkleVersion < 8 || !verticalParticles || !features.contains("p07_vertical_scanline_band")))
        throw BackendError("Skia 竖排唱字粒子缺少高亮带或粒子能力");
    if (features.contains("p07_vertical_reverse_scanline_particles")
        && (!verticalBandParticles || !features.contains("p07_vertical_reverse_singing_particles")
            || !features.contains("p07_vertical_reverse_scanline")))
        throw BackendError("Skia 竖排扫字线粒子缺少反向走字能力");
    if (features.contains("p07_vertical_scanline_particle_line_animation")
        && (!verticalBandParticles || !features.contains("p07_vertical_singing_particle_line_animation")
            || !(features.contains("p07_vertical_scanline_line_animation")
                || features.contains("p07_vertical_singing_scanline_line_animation"))))
        throw BackendError("Skia 竖排扫字线粒子缺少整行出入场能力");
    if (features.contains("p07_vertical_outlined_singing_particles")
        && (!verticalParticles || !features.contains("p06_vertical_outline")))
        throw BackendError("Skia 竖排唱字粒子缺少描边能力");
    if (features.contains("p07_vertical_decorated_singing_particles")
        && (!verticalParticles || !(verticalGlow || verticalShadow || verticalRubyDecoration)))
        throw BackendError("Skia 竖排唱字粒子缺少光晕或阴影能力");
    if (features.contains("p07_vertical_no_wipe_singing_particles")
        && (!verticalParticles || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 竖排唱字粒子缺少整字变色能力");
    if (features.contains("p07_sing_twinkle")
        && (twinkleVersion < 1 || twinkleVersion > kSingingParticleSchema || std::floor(twinkleVersion) != twinkleVersion))
        throw BackendError("Skia 唱字粒子布局版本无效");
    if (twinkleVersion >= 12
        && (!features.contains("p07_sing_twinkle") || !features.contains("p05_resolved_ruby")
            || (features.contains("p05_lower_ruby") && !features.contains("p07_lower_ruby_scanline_singing_particles"))))
        throw BackendError("Skia 注音粒子缺少上注音或唱字粒子能力声明");
    if (features.contains("p07_ruby_scanline_particles")
        && (twinkleVersion < 13 || !features.contains("p07_ruby_scanline")))
        throw BackendError("Skia 注音扫字线粒子缺少高亮带或粒子能力声明");
    if (features.contains("p07_shaped_scanline_singing_particles")
        && (twinkleVersion < 13 || !features.contains("p07_ruby_scanline") || !features.contains("p07_scanline_shape")))
        throw BackendError("Skia 异形扫字线同场缺少唱字粒子或上注音能力声明");
    if (features.contains("p07_ruby_band_singing_particles")
        && (twinkleVersion < 13 || !features.contains("p07_ruby_scanline")))
        throw BackendError("Skia 高亮带同场缺少唱字粒子或上注音能力声明");
    if (features.contains("p07_faded_scanline_singing_particles")
        && (twinkleVersion < 8 || (twinkleVersion >= 12 && !features.contains("p07_ruby_band_singing_particles"))
            || !features.contains("p07_sing_twinkle") || !features.contains("p07_scanline_line_animation")
            || !features.contains("p07_line_fade")))
        throw BackendError("Skia 扫字线与唱字粒子组合缺少整行淡化能力声明");
    if (features.contains("p07_faded_scanline_particle_paint") && !features.contains("p07_faded_scanline_singing_particles"))
        throw BackendError("Skia 唱字粒子画笔缺少淡化扫字线能力声明");
    if (features.contains("p06_outline_ruby")
        && (!features.contains("p06_primary_solid_outline") || !features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 描边正文与注音组合缺少描边或注音能力声明");
    if (features.contains("p06_multi_lane_outline") && !features.contains("p06_primary_solid_outline"))
        throw BackendError("Skia 多行描边缺少正文描边能力声明");
    if (features.contains("p06_ruby_solid_outline")
        && (!features.contains("p05_resolved_ruby") || layout.value("ruby_solid_outline_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音描边缺少版本化布局或注音能力声明");
    if (features.contains("p06_ruby_outline_gradient")
        && (!features.contains("p06_ruby_solid_outline") || !features.contains("p05_upper_ruby")
            || layout.value("ruby_outline_gradient_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音渐变描边缺少版本化布局或注音能力声明");
    if (features.contains("p06_ruby_horizontal_outline")
        && (!features.contains("p06_ruby_outline_gradient")
            || layout.value("ruby_horizontal_outline_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音横向渐变描边缺少版本化布局或注音能力声明");
    if (features.contains("p06_role_outline_gradient")
        && (!inlineBitmapGuides || !features.contains("p06_role_body_paints")
            || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 角色渐变描边缺少图片或正文画笔能力声明");
    if (features.contains("p06_role_outline_paints")
        && (!features.contains("p06_role_body_paints") || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 角色复杂描边缺少正文画笔或描边能力声明");
    if (features.contains("p06_ruby_shadow")
        && (!features.contains("p05_resolved_ruby") || layout.value("ruby_shadow_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音阴影缺少版本化布局或注音能力声明");
    if ((features.contains("p06_body_gradient_shadow") && !features.contains("p06_static_shadow"))
        || (features.contains("p06_ruby_gradient_shadow") && !features.contains("p06_ruby_shadow")))
        throw BackendError("Skia 渐变阴影缺少阴影能力声明");
    if (features.contains("p06_ruby_glow")
        && (!features.contains("p05_resolved_ruby") || layout.value("ruby_glow_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音光晕缺少版本化布局或注音能力声明");
    const bool rubyRoleGlow = features.contains("p06_ruby_role_glow");
    if (rubyRoleGlow && (!features.contains("p06_ruby_glow") || !features.contains("p05_inline_roles")
                        || !features.contains("p05_upper_ruby")))
        throw BackendError("Skia 上注音角色光晕缺少对应能力声明");
    if (features.contains("p06_lower_ruby_glow")
        && (!features.contains("p05_lower_ruby") || layout.value("lower_ruby_glow_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音光晕缺少版本化布局或注音能力声明");
    if (features.contains("p06_lower_ruby_shadow")
        && (!features.contains("p05_lower_ruby") || layout.value("lower_ruby_shadow_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音阴影缺少版本化布局或注音能力声明");
    if (features.contains("p06_lower_ruby_shadow_gradient")
        && (!features.contains("p06_lower_ruby_shadow")
            || layout.value("lower_ruby_shadow_gradient_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音渐变阴影缺少版本化布局或阴影能力声明");
    if (features.contains("p07_lower_ruby_decoration_line_animation")
        && (!features.contains("p07_utopia_line_animation") || !features.contains("p07_ruby_line_animation")
            || !(features.contains("p06_lower_ruby_glow") || features.contains("p06_lower_ruby_shadow"))))
        throw BackendError("Skia 下方注音装饰动画缺少 Utopia 行动画或装饰能力声明");
    if (features.contains("p07_lower_ruby_unscaled_line_animation")
        && (!features.contains("p07_ruby_line_animation")
            || !(features.contains("p06_lower_ruby_glow") || features.contains("p06_lower_ruby_shadow"))))
        throw BackendError("Skia 下方注音整行出入场缺少行动画或装饰能力声明");
    if (features.contains("p07_singing_glow")
        && (!features.contains("p07_singing_scale")
            || !(hasRoleGlow || features.contains("p06_static_glow") || features.contains("p06_ruby_glow"))))
        throw BackendError("Skia 唱字光晕缺少唱字放大或光晕能力声明");
    if (features.contains("p07_zoom_pulse_decoration")
        && (!features.contains("p07_singing_scale")
            || !(features.contains("p07_singing_glow") || features.contains("p07_composed_stroke_flash"))))
        throw BackendError("Skia 整字放大缺少唱字或装饰能力声明");
    if (features.contains("p07_utopia_line_animation")
        && (!features.contains("p07_singing_scale")
            || !(features.contains("p07_line_fade") || features.contains("p07_line_motion"))))
        throw BackendError("Skia Utopia 整行出入场缺少唱字或整行动画能力声明");
    if (features.contains("p07_composed_stroke_flash")
        && (!features.contains("p07_stroke_flash") || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 描边闪光组合缺少对应布局能力");
    const bool lowerNoWipeDecoration = features.contains("p07_lower_ruby_no_wipe_decoration");
    if (lowerNoWipeDecoration && (!features.contains("p07_no_wipe") || !features.contains("p05_lower_ruby")
            || !(features.contains("p06_lower_ruby_glow") || features.contains("p06_lower_ruby_shadow"))))
        throw BackendError("Skia 下方注音整字变色缺少装饰能力声明");
    if (features.contains("p07_no_wipe_decoration")
        && (!features.contains("p07_no_wipe") || (features.contains("p05_lower_ruby") && !lowerNoWipeDecoration)
            || !(features.contains("p06_static_glow") || features.contains("p06_ruby_glow")
                 || features.contains("p07_composed_stroke_flash"))))
        throw BackendError("Skia 整字变色装饰缺少对应布局能力");
    if (features.contains("p06_outline_gradient")
        && (!features.contains("p06_body_gradient") || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 描边渐变缺少正文渐变或描边能力声明");
    if (features.contains("p06_unscaled_paint_fade")
        && (!features.contains("p07_line_fade")
            || !(features.contains("p06_body_gradient") || features.contains("p06_image_fill"))))
        throw BackendError("Skia 正文画笔淡化缺少淡化或画笔能力声明");
    if (features.contains("p06_unscaled_paint_scanline")
        && (!features.contains("p07_scanline_band")
            || !(features.contains("p06_body_gradient") || features.contains("p06_image_fill"))))
        throw BackendError("Skia 正文画笔扫字线缺少高亮带或画笔能力声明");
    if (features.contains("p07_lower_ruby_scanline_singing_particles")
        && (!features.contains("p07_lower_ruby_scanline_line_animation")
            || !features.contains("p07_faded_scanline_singing_particles")))
        throw BackendError("Skia 下方注音的淡化扫字线缺少唱字粒子能力");
    if (features.contains("p06_lower_ruby_fill_gradient")
        && (!features.contains("p07_lower_ruby_scanline_singing_particles")
            || layout.value("lower_ruby_fill_gradient_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音渐变缺少对应布局能力");
    if (features.contains("p06_lower_ruby_static_gradient")
        && (!features.contains("p05_lower_ruby") || layout.value("lower_ruby_static_gradient_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音渐变缺少版本化布局");
    if (features.contains("p06_lower_ruby_static_image")
        && (!features.contains("p05_lower_ruby") || layout.value("lower_ruby_static_image_schema").toDouble(-1) != 1))
        throw BackendError("Skia 下方注音图片画笔缺少版本化布局");
    const bool animatedSingingGradient = features.contains("p06_animated_singing_gradient_fill");
    const bool rubySingingPaint = features.contains("p06_ruby_main_paint") && features.contains("p07_ruby_singing_scale");
    if (features.contains("p06_utopia_main_paint")
        && ((!rubySingingPaint && !animatedSingingGradient)
            || !(features.contains("p06_body_gradient") || features.contains("p06_image_fill"))))
        throw BackendError("Skia Utopia 正文画笔缺少注音唱字或画笔能力声明");
    if (features.contains("p06_ruby_fill_gradient")
        && (!features.contains("p06_ruby_main_paint") || !features.contains("p06_body_gradient")))
        throw BackendError("Skia 上注音渐变缺少正文画笔或注音能力声明");
    if (features.contains("p06_faded_scanline_gradient_fill")
        && (!features.contains("p06_body_gradient") || !features.contains("p07_singing_scale")
            || !(features.contains("p07_faded_scanline_singing_particles")
                 || features.contains("p07_faded_scanline_prefix_guides"))))
        throw BackendError("Skia 淡化扫字线渐变缺少正文画笔或组合能力声明");
    if (animatedSingingGradient
        && (!features.contains("p06_body_gradient") || !features.contains("p06_utopia_main_paint")
            || (features.contains("p05_upper_ruby") && !rubySingingPaint)
            || !features.contains("p07_utopia_line_animation")))
        throw BackendError("Skia Utopia 出入场渐变缺少正文画笔或唱字动画能力声明");
    if (features.contains("p06_faded_scanline_palette_gradient")
        && (!features.contains("p06_body_gradient") || !features.contains("p07_singing_scale")
            || !features.contains("p07_scanline_line_animation") || !features.contains("p07_line_fade")))
        throw BackendError("Skia 提亮扫字线渐变缺少正文画笔或整行淡化能力声明");
    if (features.contains("p06_ruby_fill_image")
        && (!features.contains("p06_ruby_main_paint") || !features.contains("p06_image_fill")))
        throw BackendError("Skia 上注音图片缺少正文画笔或注音能力声明");
    if (features.contains("p06_independent_ruby_fill") && !features.contains("p05_upper_ruby"))
        throw BackendError("Skia 独立注音画笔缺少上注音能力声明");
    if (features.contains("p06_ruby_role_paints")
        && (!features.contains("p06_role_body_paints") || !features.contains("p05_upper_ruby")))
        throw BackendError("Skia 上注音角色画笔缺少正文角色画笔或上注音能力声明");
    if (features.contains("p06_primary_solid_outline")
        && layout.value("primary_solid_outline_schema").toDouble(-1) != 1)
        throw BackendError("Skia 实色描边布局版本无效");
    if (features.contains("p06_latin_primary_outline")) {
        static const QJsonArray allowed{
            "p05_resolved_horizontal", "p05_single_line_placement", "p05_inline_roles",
            "p05_fixed_row_grid", "p05_page_collisions", "p05_smart_horizontal", "p05_segmented_wipe",
            "p05_overlap_wipe", "p05_font_fallback", "p05_mixed_fonts", "p05_standalone_marks",
            "p06_primary_solid_outline", "p06_secondary_solid_outline", "p06_multi_lane_outline",
            "p06_role_solid_outlines", "p06_latin_primary_outline", "p06_latin_secondary_outline", "p06_role_body_paints",
            "p05_resolved_ruby", "p05_upper_ruby", "p05_ruby_units", "p05_ruby_markers",
            "p06_outline_ruby", "p06_ruby_solid_outline", "p06_ruby_role_paints",
            "p06_static_glow", "p06_role_static_glow", "p06_ruby_glow", "p06_ruby_role_glow",
            "p06_gradient_glow", "p06_vertical_gradient_glow", "p06_role_gradient_glow",
            "p07_line_fade", "p07_role_line_fade", "p07_ruby_line_animation"
        };
        static const QJsonArray animatedGuides{
            "p05_vector_guides", "p05_vector_guide_composition", "p07_singing_scale",
            "p07_ruby_singing_scale", "p07_utopia_line_animation", "p07_singing_glow",
            "p05_role_vector_guides", "p07_role_singing_scale", "p07_role_singing_glow"
        };
        const bool animatedGuide = features.contains("p05_vector_guide_composition")
            && features.contains("p07_utopia_line_animation");
        if (!features.contains("p06_primary_solid_outline")
            || (features.contains("p07_line_fade") && !features.contains("p07_role_line_fade") && !animatedGuide)
            || std::any_of(features.begin(), features.end(), [&](const auto& v) {
                return !allowed.contains(v) && !(animatedGuide && animatedGuides.contains(v));
            }))
            throw BackendError("Skia 英数独立首层描边需要正向横排、普通走字和受支持的画笔组合");
    }
    if (features.contains("p06_secondary_solid_outline")
        && (layout.value("secondary_solid_outline_schema").toDouble(-1) != 1
            || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 二重实色描边布局版本无效");
    if (features.contains("p06_latin_secondary_outline") && !features.contains("p06_secondary_solid_outline"))
        throw BackendError("Skia 英数第二道描边缺少二重描边布局声明");
    const bool roleStaticGlow = features.contains("p06_role_static_glow");
    if (features.contains("p06_gradient_glow")
        && (!(roleStaticGlow || rubyRoleGlow || features.contains("p06_static_glow") || features.contains("p06_ruby_glow"))
            || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 基础渐变光晕缺少对应正文或上注音能力");
    if ((features.contains("p06_vertical_gradient_glow") || features.contains("p06_role_gradient_glow"))
        && (!features.contains("p06_role_body_paints")
            || !(roleStaticGlow || rubyRoleGlow || features.contains("p06_static_glow")
                 || features.contains("p06_ruby_glow"))
            || (features.contains("p07_singing_scale") && !inlineBitmapGuides) || features.contains("p05_lower_ruby")))
        throw BackendError("Skia 渐变光晕需要正向横排角色画笔和普通走字");
    const bool roleLayers = features.contains("p06_role_solid_outlines") || features.contains("p06_role_body_paints")
        || roleStaticGlow || rubyRoleGlow;
    const bool roleLineFade = features.contains("p07_role_line_fade");
    if (roleLineFade != (roleLayers && features.contains("p07_line_fade"))
        || (roleLineFade && (features.contains("p05_lower_ruby")
            || (features.contains("p07_singing_scale") && !(roleFadedScanline || inlineBitmapGuides || roleVectorGuides)))))
        throw BackendError("Skia 角色画笔整行淡化需要正向横排、普通走字和受支持的上注音画笔");
    if (roleLayers) {
        static const QJsonArray allowed{
            "p05_resolved_horizontal", "p05_single_line_placement", "p05_inline_roles",
            "p05_fixed_row_grid", "p05_page_collisions", "p05_smart_horizontal", "p05_segmented_wipe",
            "p05_overlap_wipe", "p05_font_fallback", "p05_mixed_fonts",
            "p05_resolved_ruby", "p05_upper_ruby", "p05_lower_ruby", "p05_ruby_units",
            "p05_ruby_markers", "p05_standalone_marks", "p06_outline_ruby", "p06_ruby_solid_outline",
            "p06_primary_solid_outline", "p06_secondary_solid_outline", "p06_role_solid_outlines", "p06_latin_primary_outline",
            "p06_multi_lane_outline", "p06_role_body_paints", "p06_ruby_role_paints", "p08_title_role_outlines",
            "p06_role_outline_paints",
            "p06_static_glow", "p06_role_static_glow", "p06_ruby_glow", "p06_ruby_role_glow",
            "p06_gradient_glow", "p06_vertical_gradient_glow", "p06_role_gradient_glow",
            "p07_singing_scale", "p07_role_singing_scale", "p07_ruby_singing_scale",
            "p07_singing_glow", "p07_role_singing_glow",
            "p07_line_fade", "p07_role_line_fade", "p07_ruby_line_animation"
        };
        static const QJsonArray fadedScanline{
            "p07_scanline_band", "p07_ruby_scanline", "p07_scanline_line_animation",
            "p07_utopia_line_animation", "p07_inline_role_faded_scanline"
        };
        static const QJsonArray avatarFeatures{
            "p08_bitmap_guides", "p08_inline_bitmap_guides", "p06_role_outline_gradient", "p06_ruby_outline_gradient",
            "p08_inline_bitmap_decoration", "p08_animated_bitmap_guides", "p06_ruby_horizontal_outline",
            "p07_utopia_line_animation", "p06_static_shadow", "p06_ruby_shadow"
        };
        static const QJsonArray roleGuideFeatures{
            "p05_vector_guides", "p05_vector_guide_composition", "p05_role_vector_guides", "p07_utopia_line_animation",
            "p06_static_shadow", "p06_ruby_shadow"
        };
        const bool roleBodyPaints = features.contains("p06_role_body_paints");
        if ((features.contains("p06_role_solid_outlines") && !features.contains("p06_primary_solid_outline"))
            || ((roleBodyPaints || roleStaticGlow) && !features.contains("p05_inline_roles"))
            || std::any_of(features.begin(), features.end(), [&](const auto& v) {
                return !allowed.contains(v) && !(roleFadedScanline && fadedScanline.contains(v))
                    && !(inlineBitmapGuides && avatarFeatures.contains(v))
                    && !(roleVectorGuides && roleGuideFeatures.contains(v))
                    && !(reverseBitmapGuides && (v == QJsonValue("p05_reverse_wipe") || v == QJsonValue("p05_reverse_overlap")
                                                 || v == QJsonValue("p08_reverse_bitmap_guides") || v == QJsonValue("p06_static_shadow")));
            }))
            throw BackendError("Skia 正文角色画笔当前支持正向横排和普通走字");
    }
    if (style.contains("right_to_left")
        && !style.value("right_to_left").isBool())
        throw BackendError("Skia 右至左单元排列标记必须是布尔值");
    if (features.contains("p05_reverse_wipe")
        && layout.value("reverse_wipe_schema").toDouble(-1) != 1)
        throw BackendError("Skia 反向走字布局版本无效");
    if (features.contains("p05_reverse_overlap")
        && (layout.value("reverse_overlap_schema").toDouble(-1) != 1
            || !features.contains("p05_reverse_wipe")))
        throw BackendError("Skia 反向走字重叠终点布局版本无效");
    if (!ir.value("screen").isObject() || !ir.value("style").isObject() || !ir.value("track").isObject())
        throw BackendError("Skia IR: screen/style/track objects are required");
    const auto screen = ir.value("screen").toObject();
    integer(screen, "width", 1, 8192);
    integer(screen, "height", 1, 8192);
    integer(screen, "fps", 1, 240);
    const double dpr = screen.value("dpr").toDouble(1);
    if (!std::isfinite(dpr) || dpr < 0.1 || dpr > 8
        || screen.value("width").toDouble() * dpr > 8192 || screen.value("height").toDouble() * dpr > 8192)
        throw BackendError("Skia IR: invalid DPR/physical size");
    if (!ir.value("extra_tracks").toArray().isEmpty() || !ir.value("titles").toArray().isEmpty())
        throw BackendError("Skia P04: extra tracks and titles require later phases");
    const auto track = ir.value("track").toObject();
    if (!track.value("lines").isArray()) throw BackendError("Skia IR: track.lines is required");
    for (const auto& value : track.value("lines").toArray()) {
        if (!value.isObject()) throw BackendError("Skia IR: invalid line");
        const auto line = value.toObject();
        if (line.contains("lane")) integer(line, "lane", 0, 31);
        if (line.contains("wipe_reverse") && !line.value("wipe_reverse").isBool())
            throw BackendError("Skia 反向走字标记必须是布尔值");
        if (line.contains("layout_offset_windows")) {
            const auto windows = line.value("layout_offset_windows");
            if (!windows.isArray() || windows.toArray().size() > 4096)
                throw BackendError("Skia 页面位移窗口数量或类型无效");
            int previousEnd = -86400000;
            for (const auto& window : windows.toArray()) {
                if (!window.isObject()) throw BackendError("Skia 页面位移窗口必须是对象");
                const auto object = window.toObject();
                const auto start = integer(object, "start_ms", -86400000, 86400000);
                const auto end = integer(object, "end_ms", -86400000, 86400000);
                if (end <= start || start < previousEnd) throw BackendError("Skia 页面位移窗口无效或重叠");
                previousEnd = end;
                for (const auto key : {"offset_x", "offset_y"}) {
                    const auto value = object.value(QLatin1String(key));
                    if (!value.isDouble() || !std::isfinite(value.toDouble()) || std::abs(value.toDouble()) > 1e6)
                        throw BackendError("Skia 页面位移包含无效或非有限值");
                }
            }
        }
        if (!line.value("chars").isArray()) throw BackendError("Skia IR: line.chars is required");
        for (const auto& cv : line.value("chars").toArray()) {
            if (!cv.isObject() || !cv.toObject().value("text").isString()) throw BackendError("Skia IR: invalid character");
            integer(cv.toObject(), "start_ms", -86400000, 86400000);
            // vector_guide_schema=1 admits table references only; inline outlines stay rejected.
            if (cv.toObject().contains("vector_glyph_id")
                && (!features.contains("p05_vector_guides")
                    || layout.value("vector_guide_schema").toDouble(-1) != 1))
                throw BackendError("Skia 导唱符缺少版本化能力声明");
            if (cv.toObject().contains("bitmap_guide")
                && (!features.contains("p08_bitmap_guides") || !cv.toObject().value("bitmap_guide").isObject()))
                throw BackendError("Skia 图片导唱符缺少版本化能力声明");
            for (const auto* key : {"vector_glyph", "wipe_points"})
                if (cv.toObject().contains(QLatin1String(key)))
                    throw BackendError("Skia P04: guide symbols and custom wipe curves require later phases");
        }
    }
}
}
