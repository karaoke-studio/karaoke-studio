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

bool isVerticalSource(const QJsonObject& source) {
    return source.value("required_features").toArray().contains("p05_vertical_solid");
}

QJsonObject bandOptions(const QJsonObject& source) {
    const auto style = source.value("style").toObject();
    QJsonObject result;
    for (auto it = style.constBegin(); it != style.constEnd(); ++it)
        if (it.key().startsWith(QLatin1String("line_band_"))) result.insert(it.key(), it.value());
    return result;
}

void countSource(const QJsonObject& source, const QJsonValue& screen, SceneSize& size) {
    if (source.value("screen") != screen || source.contains("skia_extra_sources"))
        throw BackendError("Skia 多轨尺寸不一致或包含嵌套来源");
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
    const bool verticalSources = features.contains("p08_multiple_vertical_sources");
    if (feature == features.end()) {
        if (verticalSources) throw BackendError("Skia 多轨竖排缺少轨道来源");
        return resolvedSkiaSource(ir, images);
    }

    const auto input = ir.value("skia_extra_sources");
    const auto extras = input.toArray();
    if (!input.isArray() || extras.isEmpty() || extras.size() > 15)
        throw BackendError("Skia 多轨配置缺少附加轨道或数量超限");
    features.erase(feature);
    if (const auto marker = std::find(features.begin(), features.end(), QJsonValue("p08_multiple_vertical_sources"));
        marker != features.end())
        features.erase(marker);
    auto primary = ir;
    primary.remove("skia_extra_sources");
    primary.insert("required_features", features);

    const auto screen = primary.value("screen");
    const auto sharedBand = bandOptions(primary);
    const bool hasBand = features.contains("p08_static_line_band");
    bool hasVerticalSource = isVerticalSource(primary);
    SceneSize size;
    countSource(primary, screen, size);
    for (const auto& value : extras) {
        if (!value.isObject()) throw BackendError("Skia 附加轨道格式无效");
        const auto source = value.toObject();
        if (source.value("required_features").toArray().contains("p08_static_line_band") != hasBand
            || (hasBand && bandOptions(source) != sharedBand))
            throw BackendError("Skia 多轨行背景带必须共享场景素材与装饰");
        countSource(source, screen, size);
        hasVerticalSource |= isVerticalSource(source);
    }
    if (verticalSources != hasVerticalSource)
        throw BackendError("Skia 多轨竖排能力与实际来源不一致");

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
        countSource(source, ir.value("screen"), size);
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
