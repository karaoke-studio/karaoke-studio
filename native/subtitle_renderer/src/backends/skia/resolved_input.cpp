#include "resolved_input.h"
#include "resolved_wipe.h"
#include "resolved_glyph.h"
#include "resolved_ruby.h"
#include "scene_input.h"
#include "../render_backend.h"
#include "../../scene/skia_ir_projection.h"
#include "../../scene/skia_text_projection.h"
#include <QtCore/QJsonArray>
#include <cmath>
#include <algorithm>

namespace krok::subtitle::native {
namespace {
std::uint32_t argb(const RgbaColor& c) {
    return (std::uint32_t(c.alpha) << 24) | (std::uint32_t(c.red) << 16) | (std::uint32_t(c.green) << 8) | c.blue;
}
}
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir) {
    auto projected = scene::skiaSceneFromIr(ir);
    validateSkiaScene(projected, true);
    const auto layout = ir.value("skia_layout").toObject();
    if (layout.value("version").toInt() != 2 || layout.value("metrics_identity").toString().size() != 64
        || !layout.value("fonts").isObject() || !layout.value("lines").isArray()
        || !layout.value("runs").isArray()
        || !ir.value("required_features").toArray().contains("p05_resolved_horizontal"))
        throw BackendError("Skia 缺少版本化的最终布局与度量快照");
    const auto lines = layout.value("lines").toArray();
    const auto resources = lines.isEmpty() && layout.value("fonts").toObject().isEmpty()
        ? scene::FontResourceTable{} : scene::loadSkiaFontResources(layout.value("fonts").toObject());
    const auto originalLines = ir.value("track").toObject().value("lines").toArray();
    if (lines.size() != int(projected.lines.size()) || lines.size() > 4096)
        throw BackendError("Skia 最终布局行数不一致或超限");
    const auto runs = layout.value("runs").toArray();
    if (runs.size() > 4096) throw BackendError("Skia 字体实例数量超限");
    ResolvedGlyphContext glyphs;
    for (const auto& value : runs) {
        if (!value.isObject()) throw BackendError("Skia 字体实例必须为对象");
        const auto run = value.toObject();
        if (run.value("mixed_fallback").toBool()
            && (!ir.value("required_features").toArray().contains("p05_mixed_fonts")
                || layout.value("mixed_font_schema").toDouble(-1) != 1))
            throw BackendError("Skia 混合字体缺少版本化能力声明");
        if ((run.value("font_ids").toArray().size() > 1 || run.contains("font_options"))
            && (!ir.value("required_features").toArray().contains("p05_font_fallback")
                || layout.value("font_chain_schema").toDouble(-1) != 1))
            throw BackendError("Skia 显式字体链缺少版本化能力声明");
        if (run.value("rtl").toBool()) throw BackendError("Skia 当前规范布局尚不支持 RTL");
        glyphs.fonts.push_back(scene::skiaRunFonts(run, resources));
        glyphs.mixedFallback.push_back(run.value("mixed_fallback").toBool());
        glyphs.languages.push_back(run.value("language").toString("und").toStdString());
    }
    glyphs.dpr = ir.value("screen").toObject().value("dpr").toDouble(1);
    ::subtitle::skia::ResolvedSubtitleInput out{projected.width, projected.height, {}};
    std::size_t totalCells = 0, totalPoints = 0, totalGroups = 0;
    for (int index = 0; index < lines.size(); ++index) {
        const auto source = lines[index].toObject();
        const auto& line = projected.lines[index];
        const auto& style = projected.lineStyles[index];
        if (!source.value("cells").isArray()) throw BackendError("Skia 最终布局缺少单元");
        const auto cells = source.value("cells").toArray();
        const auto intervals = originalLines[index].toObject().value("resolved_intervals").toArray();
        if (cells.size() != int(line.chars.size()) || intervals.size() != cells.size() || (totalCells += cells.size()) > 65536)
            throw BackendError("Skia 最终布局与解析计时单元不一致或超限");
        ::subtitle::skia::ResolvedLine prepared{{}, line.displayWindows[0].startMs, line.displayWindows[0].endMs};
        prepared.noWipe = line.karaokeAnimation == "no_wipe";
        // vertical_solid_schema=1: the reverse flag means bottom-to-top on a column.
        prepared.vertical = style.vertical;
        prepared.wipeRightToLeft = style.vertical ? line.wipeReverse : style.rightToLeft != line.wipeReverse;
        if (style.vertical) {
            if (!ir.value("required_features").toArray().contains("p05_vertical_solid")
                || layout.value("vertical_solid_schema").toDouble(-1) != 1
                || !source.value("text_flow").isString() || source.value("text_flow").toString() != "vertical"
                || !source.value("ruby_groups").toArray().isEmpty())
                throw BackendError("Skia 竖排缺少能力声明或最终方向无效");
        } else if (style.rightToLeft || source.contains("text_flow")) {
            if (!ir.value("required_features").toArray().contains("p05_horizontal_rtl")
                || layout.value("horizontal_rtl_schema").toDouble(-1) != 1
                || !source.value("text_flow").isString()
                || source.value("text_flow").toString() != (style.rightToLeft ? "rtl" : "ltr")
                || !source.value("ruby_groups").toArray().isEmpty())
                throw BackendError("Skia 右至左单元排列缺少能力声明或最终方向无效");
        }
        // The existing GPU places vertical columns from margins only; page offsets do not apply.
        if (!style.vertical)
            for (const auto& window : line.placementWindows)
                prepared.translations.push_back({window.startMs, window.endMs, window.offsetX, window.offsetY});
        for (int j = 0; j < cells.size(); ++j) {
            const auto cell = cells[j].toObject();
            const int clock = line.wipeReverse ? cells.size()-1-j : j;
            if ((line.wipeReverse && (!cell.value("clock_index").isDouble()
                    || cell.value("clock_index").toDouble(-1) != clock))
                || (!line.wipeReverse && cell.contains("clock_index")))
                throw BackendError("Skia 反向走字时钟所有权无效");
            const auto interval = intervals[clock].toArray();
            if (interval.size() != 2 || !interval[0].isDouble() || !interval[1].isDouble()
                || !std::isfinite(interval[0].toDouble()) || !std::isfinite(interval[1].toDouble())
                || std::floor(interval[0].toDouble()) != interval[0].toDouble()
                || std::floor(interval[1].toDouble()) != interval[1].toDouble()
                || interval[0].toDouble() < -86400000 || interval[1].toDouble() > 86400000
                || interval[1].toDouble() < interval[0].toDouble())
                throw BackendError("Skia 最终 IR 必须提供有效且完整的解析计时区间");
            const auto& paint = line.chars[j].styleIndex >= 0 ? projected.charStyles[line.chars[j].styleIndex] : style;
            auto glyph = glyphs.cell(cell, QString::fromStdWString(line.chars[j].text), line.chars[clock].startMs, line.chars[clock].endMs,
                argb(paint.beforeFillPaint.color), argb(paint.afterFillPaint.color));
            glyph.wipePoints = resolvedSkiaWipePoints(cell, ir, line.chars[clock].startMs - int(interval[0].toDouble()), totalPoints);
            if (const auto end = resolvedSkiaWipeEnd(cell, ir)) {
                if (glyph.wipePoints.empty()) glyph.wipePoints = {{glyph.startMs, 0.f}, {glyph.endMs, 1.f}};
                glyph.wipePoints.back().position = *end;
                glyph.adjustedWipeEnd = true;
            }
            if (style.vertical) {
                const auto top = cell.value("wipe_top"), bottom = cell.value("wipe_bottom");
                if (!top.isDouble() || !bottom.isDouble() || !std::isfinite(top.toDouble()) || !std::isfinite(bottom.toDouble())
                    || bottom.toDouble() <= top.toDouble() || std::abs(top.toDouble()) > 1e6 || std::abs(bottom.toDouble()) > 1e6)
                    throw BackendError("Skia 竖排单元框无效");
                glyph.top = float(top.toDouble() * glyphs.dpr);
                glyph.bottom = float(bottom.toDouble() * glyphs.dpr);
            } else if (cell.contains("wipe_top") || cell.contains("wipe_bottom")) {
                throw BackendError("Skia 横排单元不能携带竖排单元框");
            }
            prepared.cells.push_back(std::move(glyph));
        }
        const int timingOffset = cells.isEmpty() ? 0
            : line.chars.front().startMs - int(intervals[0].toArray()[0].toDouble());
        prepared.rubyGroups = resolvedSkiaRubyGroups(source, ir, glyphs, projected, index, timingOffset, totalCells, totalGroups);
        if (line.wipeReverse) std::reverse(prepared.cells.begin(), prepared.cells.end());
        out.lines.push_back(std::move(prepared));
    }
    return out;
}
}
