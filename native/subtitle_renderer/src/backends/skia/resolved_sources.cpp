#include "resolved_input.h"
#include "image_fill_cache.h"
#include "../../model/backend_caps.h"
#include <QtCore/QJsonArray>
#include <algorithm>
#include <cmath>
#include <iterator>
#include <limits>

namespace krok::subtitle::native {
namespace {
struct SceneSize {
    qsizetype lines = 0;
    qsizetype cells = 0;
    qsizetype readings = 0;
};

QJsonObject bandOptions(const QJsonObject& source) {
    const auto style = source.value("style").toObject();
    QJsonObject result;
    for (auto it = style.constBegin(); it != style.constEnd(); ++it)
        if (it.key().startsWith(QLatin1String("line_band_"))) result.insert(it.key(), it.value());
    return result;
}

void countSource(const QJsonObject& source, const QJsonValue& screen, SceneSize& size, bool title = false) {
    if (source.value("screen") != screen || source.contains("skia_extra_sources"))
        throw BackendError("Skia 多轨尺寸不一致或包含嵌套来源");
    // These features carry only source-local geometry. Other combinations need
    // their own validation of draw order before sharing the scene.
    static const QJsonArray allowed{
        "p05_resolved_horizontal", "p05_single_line_placement", "p05_inline_roles",
        "p05_fixed_row_grid", "p05_page_collisions", "p05_smart_horizontal",
        "p05_segmented_wipe", "p05_overlap_wipe", "p05_font_fallback",
        "p05_mixed_fonts", "p05_reverse_wipe", "p05_reverse_overlap",
        "p05_resolved_ruby", "p05_upper_ruby", "p05_lower_ruby",
        "p05_ruby_units", "p05_ruby_markers", "p05_standalone_marks",
        "p06_primary_solid_outline", "p06_secondary_solid_outline", "p06_multi_lane_outline",
        "p06_role_solid_outlines", "p06_role_body_paints", "p06_ruby_role_paints",
        "p06_outline_ruby", "p06_ruby_solid_outline", "p07_ruby_line_animation",
        "p06_body_gradient", "p06_outline_gradient", "p06_image_fill",
        "p06_ruby_main_paint", "p06_utopia_main_paint", "p06_ruby_fill_gradient", "p06_ruby_fill_image",
        "p06_static_glow", "p06_role_static_glow", "p06_ruby_glow", "p06_ruby_role_glow", "p07_singing_glow",
        "p06_static_shadow", "p06_ruby_shadow",
        "p07_singing_scale", "p07_role_singing_scale", "p07_ruby_singing_scale", "p07_role_singing_glow",
        "p07_no_wipe", "p07_line_fade", "p07_line_motion", "p08_viewport",
        "p07_scanline_band", "p07_ruby_scanline", "p07_lower_ruby_scanline",
        "p07_scanline_image", "p07_scanline_shape", "p07_scanline_line_animation",
        "p07_scanline_shape_line_animation", "p07_ruby_scanline_particles",
        "p07_shaped_scanline_singing_particles", "p07_ruby_band_singing_particles", "p07_sing_twinkle",
        "p08_line_band_with_effects",
        "p08_static_line_band", "p08_line_band_fit", "p08_line_band_slices",
        "p08_ruby_line_band", "p08_lower_ruby_line_band", "p08_tinted_line_band",
        "p08_staff_line_band", "p08_animated_staff_line_band", "p08_animated_line_band",
        "p08_line_band_image_sequence", "p08_line_band_outer_stroke", "p08_line_band_stroke_position",
        "p08_line_band_blur", "p08_line_band_animated_decoration"
    };
    for (const auto& feature : source.value("required_features").toArray())
        if (!allowed.contains(feature)
            && !(title && (feature == QJsonValue("p05_vector_guides") || feature == QJsonValue("p08_bitmap_guides")
                           || feature == QJsonValue("p08_animated_title_guides")
                           || feature == QJsonValue("p08_title_role_outlines"))))
            throw BackendError("Skia 多轨暂不支持此绘制能力 " + feature.toString().toStdString());
    const auto layout = source.value("skia_layout").toObject().value("lines").toArray();
    if ((size.lines += layout.size()) > 4096) throw BackendError("Skia 多轨行数超限");
    for (const auto& value : layout) {
        const auto line = value.toObject();
        const auto readings = line.value("ruby_groups").toArray();
        if ((size.readings += readings.size()) > 4096) throw BackendError("Skia 多轨注音组总数超限");
        size.cells += line.value("cells").toArray().size();
        for (const auto& reading : readings)
            size.cells += reading.toObject().value("units").toArray().size();
        if (size.cells > 65536)
            throw BackendError("Skia 多轨字形单元总数超限");
    }
}
::subtitle::skia::ResolvedSubtitleInput resolvedLyricsInput(const QJsonObject& ir, SkiaImageFillCache& images) {
    auto features = ir.value("required_features").toArray();
    const auto feature = std::find(features.begin(), features.end(), QJsonValue("p08_multiple_sources"));
    if (feature == features.end()) return resolvedSkiaSource(ir, images);

    const auto input = ir.value("skia_extra_sources");
    const auto extras = input.toArray();
    if (!input.isArray() || extras.isEmpty() || extras.size() > 15)
        throw BackendError("Skia 多轨配置缺少附加轨道或数量超限");
    features.erase(feature);
    auto primary = ir;
    primary.remove("skia_extra_sources");
    primary.insert("required_features", features);

    const auto screen = primary.value("screen");
    const auto sharedBand = bandOptions(primary);
    const bool hasBand = features.contains("p08_static_line_band");
    SceneSize size;
    countSource(primary, screen, size);
    for (const auto& value : extras) {
        if (!value.isObject()) throw BackendError("Skia 附加轨道格式无效");
        const auto source = value.toObject();
        if (source.value("required_features").toArray().contains("p08_static_line_band") != hasBand
            || (hasBand && bandOptions(source) != sharedBand))
            throw BackendError("Skia 多轨行背景带必须共享场景素材与装饰");
        countSource(source, screen, size);
    }

    auto result = resolvedSkiaSource(primary, images);
    result.lines.reserve(size.lines);
    for (const auto& value : extras) {
        auto source = resolvedSkiaSource(value.toObject(), images, hasBand ? &result.lineBand : nullptr);
        if (source.viewport != result.viewport)
            throw BackendError("Skia 多轨字幕视口不一致");
        result.lines.insert(result.lines.end(), std::make_move_iterator(source.lines.begin()),
                            std::make_move_iterator(source.lines.end()));
    }
    return result;
}

std::vector<::subtitle::skia::ResolvedLine::TitleWindow> titleWindows(const QJsonValue& value) {
    const auto windows = value.toArray();
    if (windows.isEmpty() || windows.size() > 256) throw BackendError("Skia 标题显示窗数量无效");
    std::vector<::subtitle::skia::ResolvedLine::TitleWindow> result;
    result.reserve(windows.size());
    const auto integer = [](const QJsonValue& field) {
        const auto n = field.toDouble(std::numeric_limits<double>::quiet_NaN());
        if (!std::isfinite(n) || std::floor(n) != n || n < 0 || n > std::numeric_limits<int>::max())
            throw BackendError("Skia 标题显示窗时间无效");
        return int(n);
    };
    for (const auto& item : windows) {
        const auto window = item.toArray();
        if (window.size() != 4) throw BackendError("Skia 标题显示窗格式无效");
        const auto start = integer(window[0]), end = integer(window[1]);
        if (start >= end) throw BackendError("Skia 标题显示窗长度无效");
        result.push_back({start, end, integer(window[2]), integer(window[3])});
    }
    return result;
}
}

::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir) {
    SkiaImageFillCache images;
    auto features = ir.value("required_features").toArray();
    const auto feature = std::find(features.begin(), features.end(), QJsonValue("p08_solid_title"));
    if (feature == features.end()) return resolvedLyricsInput(ir, images);
    const auto titles = ir.value("skia_titles").toArray();
    if (titles.isEmpty() || titles.size() > 16) throw BackendError("Skia 标题数量无效");
    auto lyrics = ir;
    lyrics.remove("skia_titles");
    features.erase(feature);
    lyrics.insert("required_features", features);
    auto result = resolvedLyricsInput(lyrics, images);

    std::vector<::subtitle::skia::ResolvedLine> lines;
    lines.reserve(titles.size() + result.lines.size());
    // The existing GPU paints later title entries below earlier ones.
    SceneSize size;
    for (qsizetype index = titles.size(); index > 0; --index) {
        const auto title = titles.at(index - 1).toObject();
        const auto source = title.value("source").toObject();
        auto windows = titleWindows(title.value("windows"));
        countSource(source, ir.value("screen"), size, true);
        auto prepared = resolvedSkiaSource(source, images, nullptr, /*staticTitle=*/true);
        if (prepared.lines.empty() || prepared.lines.size() > 32
            || !prepared.viewport.isIdentity() || prepared.lineBand.image)
            throw BackendError("Skia 标题布局无效");
        for (auto& line : prepared.lines) {
            if (line.cells.empty() || line.animation.active() || !line.rubyGroups.empty() || !line.particles.empty())
                throw BackendError("Skia 标题必须使用静态文字布局");
            line.titleWindows = windows;
            lines.push_back(std::move(line));
        }
    }
    lines.insert(lines.end(), std::make_move_iterator(result.lines.begin()), std::make_move_iterator(result.lines.end()));
    result.lines = std::move(lines);
    return result;
}
}
