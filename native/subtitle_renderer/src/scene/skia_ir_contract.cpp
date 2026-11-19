#include "skia_ir_contract.h"
#include "../model/backend_caps.h"
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
        "p05_horizontal_rtl_cells",
        "p05_vertical_solid",
        "p05_vertical_japanese_glyphs",
        "p05_vertical_ruby",
        "p05_vector_guides",
        "p06_primary_solid_outline",
        "p06_secondary_solid_outline",
        "p06_body_gradient",
        "p06_outline_gradient",
        "p06_image_fill",
        "p06_outline_ruby",
        "p06_ruby_solid_outline",
        "p06_static_shadow",
        "p06_static_glow",
        "p07_line_fade",
        "p07_line_motion",
        "p07_ruby_line_animation",
        "p07_singing_scale",
        "p07_ruby_singing_scale",
        "p07_utopia_transition",
        "p07_character_fade",
        "p07_scanline_band",
        "p07_character_transform",
        "p07_sing_twinkle",
        "p07_stroke_flash",
        "p07_noise_transition",
        "p05_horizontal_rtl_kana_cells",
        "p05_horizontal_rtl_kana_marks",
        "p05_reverse_wipe",
        "p05_reverse_overlap",
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

void validateSkiaIrEnvelope(const QJsonObject& ir) {
    if (integer(ir, "schema", 3, 3) != 3) throw BackendError("Skia requires IR schema 3");
    const auto features = ir.value("required_features").toArray();
    const auto layout = ir.value("skia_layout").toObject();
    const auto style = ir.value("style").toObject();
    for (const auto& feature : features)
        if (!skiaIrProfiles().contains(feature))
            throw BackendError("Skia IR: unknown required feature");
    if (ir.contains("required_features") && !ir.value("required_features").isArray())
        throw BackendError("Skia IR: required_features must be an array");
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
    if (features.contains("p07_zero_ruby_no_wipe")
        && (layout.value("zero_ruby_no_wipe_schema").toDouble(-1) != 1
            || !features.contains("p07_no_wipe")))
        throw BackendError("Skia 零时长注音整字变色缺少版本化能力声明");
    if (features.contains("p05_horizontal_rtl")
        && layout.value("horizontal_rtl_schema").toDouble(-1) != 1)
        throw BackendError("Skia 右至左单元排列布局版本无效");
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
    if (features.contains("p05_vertical_ruby")) {
        const double version = layout.value("vertical_ruby_schema").toDouble(-1);
        if ((version != 1 && version != 2)
            || !features.contains("p05_vertical_solid")
            || !features.contains("p05_resolved_ruby")
            || !features.contains("p05_upper_ruby")
            || !style.value("vertical").toBool())
            throw BackendError("Skia 竖排注音布局版本无效");
    }
    if (features.contains("p05_vector_guides")
        && layout.value("vector_guide_schema").toDouble(-1) != 1)
        throw BackendError("Skia 导唱符布局版本无效");
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
    if (features.contains("p07_ruby_singing_scale")
        && (!features.contains("p05_resolved_ruby") || !features.contains("p07_singing_scale")))
        throw BackendError("Skia Utopia 注音缺少注音或唱字能力声明");
    if (features.contains("p07_utopia_transition") && layout.value("utopia_transition_schema").toDouble(-1) != 1)
        throw BackendError("Skia Utopia 出入场布局版本无效");
    if (features.contains("p07_character_fade")
        && layout.value("character_fade_schema").toDouble(-1) != 1)
        throw BackendError("Skia 逐字淡化布局版本无效");
    const auto scanlineVersion = layout.value("scanline_band_schema").toDouble(-1);
    if (features.contains("p07_scanline_band")
        && (scanlineVersion < 1 || scanlineVersion > kScanlineBandSchema || std::floor(scanlineVersion) != scanlineVersion))
        throw BackendError("Skia 扫字线布局版本无效");
    const auto transformVersion = layout.value("character_transform_schema").toDouble(-1);
    if (features.contains("p07_character_transform")
        && (transformVersion < 1 || transformVersion > kCharacterTransformSchema || std::floor(transformVersion) != transformVersion))
        throw BackendError("Skia 逐字变换布局版本无效");
    const auto twinkleVersion = layout.value("sing_twinkle_schema").toDouble(-1);
    if (features.contains("p07_sing_twinkle")
        && (twinkleVersion < 1 || twinkleVersion > kSingingParticleSchema || std::floor(twinkleVersion) != twinkleVersion))
        throw BackendError("Skia 唱字粒子布局版本无效");
    if (features.contains("p06_outline_ruby")
        && (!features.contains("p06_primary_solid_outline") || !features.contains("p05_resolved_ruby")))
        throw BackendError("Skia 描边正文与注音组合缺少描边或注音能力声明");
    if (features.contains("p06_ruby_solid_outline")
        && (!features.contains("p05_resolved_ruby") || layout.value("ruby_solid_outline_schema").toDouble(-1) != 1))
        throw BackendError("Skia 注音描边缺少版本化布局或注音能力声明");
    if (features.contains("p06_outline_gradient")
        && (!features.contains("p06_body_gradient") || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 描边渐变缺少正文渐变或描边能力声明");
    if (features.contains("p06_primary_solid_outline")
        && layout.value("primary_solid_outline_schema").toDouble(-1) != 1)
        throw BackendError("Skia 实色描边布局版本无效");
    if (features.contains("p06_secondary_solid_outline")
        && (layout.value("secondary_solid_outline_schema").toDouble(-1) != 1
            || !features.contains("p06_primary_solid_outline")))
        throw BackendError("Skia 二重实色描边布局版本无效");
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
            for (const auto* key : {"vector_glyph", "bitmap_guide", "wipe_points"})
                if (cv.toObject().contains(QLatin1String(key)))
                    throw BackendError("Skia P04: guide symbols and custom wipe curves require later phases");
        }
    }
}
}
