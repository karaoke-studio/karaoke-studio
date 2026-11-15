#include "resolved_ruby.h"
#include "resolved_wipe.h"
#include "../render_backend.h"
#include <QtCore/QJsonArray>
#include <cmath>

namespace krok::subtitle::native {
namespace {
int integer(const QJsonValue& value, int minimum, int maximum) {
    const double n = value.toDouble();
    if (!value.isDouble() || !std::isfinite(n) || std::floor(n) != n || n < minimum || n > maximum)
        throw BackendError("Skia 注音最终时钟或样式索引无效");
    return int(n);
}
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
}
std::vector<std::vector<::subtitle::skia::ResolvedCell>> resolvedSkiaRubyGroups(
        const QJsonObject& source, const QJsonObject& ir, const ResolvedGlyphContext& glyphs,
        const RenderScene& scene, int lineIndex, int offset, std::size_t& totalCells, std::size_t& totalGroups) {
    if (!source.contains("ruby_groups")) return {};
    if (!ir.value("required_features").toArray().contains("p05_resolved_ruby")
        || ir.value("skia_layout").toObject().value("ruby_schema").toDouble(-1) != 1
        || !source.value("ruby_groups").isArray())
        throw BackendError("Skia 注音最终布局缺少版本或能力声明");
    std::vector<std::vector<::subtitle::skia::ResolvedCell>> groups;
    const auto& line = scene.lines[lineIndex];
    if (source.value("ruby_groups").toArray().size() > 4096 - totalGroups)
        throw BackendError("Skia 注音组总数超限");
    for (const auto& value : source.value("ruby_groups").toArray()) {
        if (!value.isObject() || ++totalGroups > 4096 || line.chars.empty())
            throw BackendError("Skia 注音组无效或总数超限");
        const auto object = value.toObject();
        const int index = integer(object.value("style_cell"), 0, int(line.chars.size()) - 1);
        const auto& paint = line.chars[index].styleIndex >= 0
            ? scene.charStyles[line.chars[index].styleIndex] : scene.lineStyles[lineIndex];
        if (paint.rubyBeforeFillPaint.mode != "solid" || paint.rubyAfterFillPaint.mode != "solid"
            || paint.rubyStrokeWidth || paint.rubyStroke2Width || paint.rubyLatinStrokeWidth || paint.rubyLatinStroke2Width
            || paint.rubyDecorationKindForState(false) != "none" || paint.rubyDecorationKindForState(true) != "none")
            throw BackendError("Skia 注音最终绘制当前仅支持实色、无描边和装饰");
        const auto units = object.value("units");
        if (!units.isArray() || units.toArray().isEmpty() || units.toArray().size() > 4096
            || (totalCells += units.toArray().size()) > 65536)
            throw BackendError("Skia 注音最终单元数量无效或超限");
        std::vector<::subtitle::skia::ResolvedCell> cells;
        for (const auto& unit : units.toArray()) {
            if (!unit.isObject()) throw BackendError("Skia 注音最终单元必须为对象");
            const auto cell = unit.toObject();
            const auto text = cell.value("text"), times = cell.value("interval_ms");
            if (!text.isString() || text.toString().isEmpty() || text.toString().toUtf8().size() > 4096
                || !times.isArray() || times.toArray().size() != 2 || cell.contains("wipe_points"))
                throw BackendError("Skia 注音文本或最终时间区间无效");
            const auto interval = times.toArray();
            const int start = integer(interval[0], -86400000, 86400000);
            const int end = integer(interval[1], start, 86400000);
            const auto shiftedStart = std::int64_t(start) + offset, shiftedEnd = std::int64_t(end) + offset;
            if (shiftedStart < -172800000 || shiftedEnd > 172800000)
                throw BackendError("Skia 注音最终时间偏移超限");
            auto glyph = glyphs.cell(cell, text.toString(), int(shiftedStart), int(shiftedEnd),
                argb(paint.rubyBeforeFillPaint.color), argb(paint.rubyAfterFillPaint.color));
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                glyph.wipePoints = {{int(shiftedStart), 0.f}, {int(shiftedEnd), *end}};
                glyph.adjustedWipeEnd = true;
            }
            cells.push_back(std::move(glyph));
        }
        groups.push_back(std::move(cells));
    }
    return groups;
}
}
