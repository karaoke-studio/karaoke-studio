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

QJsonArray skiaFeatures(const QJsonObject& ir) {
    return ir.value("required_features").toArray();
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
    // The sidecar and the IR builder ship together: only the data itself is
    // validated here. Content is described by data, never by declared features.
    if (integer(ir, "schema", 3, 3) != 3) throw BackendError("Skia requires IR schema 3");
    if (ir.contains("required_features") && !ir.value("required_features").isArray())
        throw BackendError("Skia IR: required_features must be an array");
    if (ir.contains("skia_extra_sources") || ir.contains("skia_titles"))
        throw BackendError("Skia: 场景配置缺少规范布局或包含嵌套来源");
    const auto style = ir.value("style").toObject();
    if (style.contains("right_to_left") && !style.value("right_to_left").isBool())
        throw BackendError("Skia 右至左单元排列标记必须是布尔值");
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
            // Vector guides arrive as table references only; inline outlines stay rejected.
            if (cv.toObject().contains("vector_glyph_id")
                && (!cv.toObject().value("vector_glyph_id").isString()
                    || !ir.value("vector_glyphs").toObject().contains(cv.toObject().value("vector_glyph_id").toString())))
                throw BackendError("Skia 导唱符引用了字形表中不存在的轮廓");
            if (cv.toObject().contains("bitmap_guide") && !cv.toObject().value("bitmap_guide").isObject())
                throw BackendError("Skia 图片导唱符数据无效");
            for (const auto* key : {"vector_glyph", "wipe_points"})
                if (cv.toObject().contains(QLatin1String(key)))
                    throw BackendError("Skia P04: guide symbols and custom wipe curves require later phases");
        }
    }
}
}
