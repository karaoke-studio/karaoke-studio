#include "mixed_cell.h"
#include "fallback_ranges.h"
#include "font_shaping.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPathBuilder.h"
#include "include/core/SkTypeface.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <set>

namespace subtitle::skia::text {
namespace {
using Fonts = std::vector<std::shared_ptr<const FontInstance>>;

}
ShapedCell shapeMixedCell(const std::string& utf8, const Fonts& fonts,
    const std::string& language, const std::vector<std::vector<ShapedGlyph>>& candidates) {
    ShapedCell out;
    out.fontRuns = mixedFontRanges(utf8, fonts, candidates);
    out.selectedFont = -1;
    double cursor = 0;
    for (const auto& run : out.fontRuns) {
        auto shaped = shapeFontRange(utf8, *fonts[run.fontIndex], run.byteStart, run.byteEnd, language);
        const double scale = fonts[run.fontIndex]->horizontalScale();
        for (auto glyph : shaped.glyphs) {
            if (!glyph.id) throw std::runtime_error("Skia 混合字体最终字形无效");
            glyph.position.fX = float(glyph.position.x()*scale+cursor);
            glyph.fontIndex = run.fontIndex;
            out.glyphs.push_back(glyph);
        }
        cursor += shaped.advance*scale;
    }
    out.advance = float(cursor);
    out.fontIdentity = "skia-mixed-cell-v1";
    std::set<std::size_t> used;
    for (const auto& run : out.fontRuns) {
        if (used.insert(run.fontIndex).second)
            out.fontIdentity += ":font"+std::to_string(run.fontIndex)+"="+run.identity;
        out.fontIdentity += ":"+std::to_string(run.byteStart)+"-"+std::to_string(run.byteEnd)+"#"+std::to_string(run.fontIndex);
        const auto& font = *fonts[run.fontIndex];
        out.ascent = std::max(out.ascent, font.ascent()); out.descent = std::max(out.descent, font.descent());
        out.leading = std::max(out.leading, font.leading());
    }
    if (out.glyphs.empty()) throw std::runtime_error("Skia 混合字体塑形结果为空");
    std::vector<std::uint32_t> boundaries{std::uint32_t(utf8.size())};
    for (const auto& glyph : out.glyphs) boundaries.push_back(glyph.clusterStart);
    std::sort(boundaries.begin(), boundaries.end());
    boundaries.erase(std::unique(boundaries.begin(), boundaries.end()), boundaries.end());
    const auto& primary = fonts.front()->shapingFont();
    const float unit = primary.getTypeface()->getUnitsPerEm()/(primary.getSize()*fonts.front()->horizontalScale());
    SkPathBuilder outlines;
    for (std::size_t i = 0; i < out.glyphs.size(); ++i) {
        auto& glyph = out.glyphs[i];
        const auto& instance = *fonts[glyph.fontIndex];
        const auto& font = instance.shapingFont();
        glyph.clusterEnd = *std::upper_bound(boundaries.begin(), boundaries.end(), glyph.clusterStart);
        const auto geometry = instance.glyphGeometry(glyph.id);
        const float scale = instance.horizontalScale();
        const float common = font.getSize()/font.getTypeface()->getUnitsPerEm()*scale*unit;
        out.designAdvance += geometry.advance*common;
        if (i == 0) out.designLeftBearing = geometry.leftBearing*common;
        if (i+1 == out.glyphs.size()) out.designRightBearing = geometry.rightBearing*common;
        auto path = geometry.path;
        SkRect bounds = path ? path->computeTightBounds() : font.getBounds(glyph.id, nullptr);
        bounds.fLeft *= scale; bounds.fRight *= scale;
        bounds.offset(glyph.position.x(), glyph.position.y()); out.ink.join(bounds);
        if (path) outlines.addPath(path->makeTransform(SkMatrix::Scale(scale, 1))
                                      .makeTransform(SkMatrix::Translate(glyph.position.x(), glyph.position.y())));
        if (geometry.requiresColorPaint || (!path && !bounds.isEmpty())) out.outlineAvailable = false;
    }
    out.outline = outlines.detach();
    for (auto value : {out.advance, out.designAdvance, out.designLeftBearing, out.designRightBearing,
                       out.ascent, out.descent, out.leading})
        if (!std::isfinite(value)) throw std::runtime_error("Skia 混合字体返回非有限度量");
    if (!out.ink.isFinite() || std::any_of(out.glyphs.begin(), out.glyphs.end(), [](const auto& g) {
            return !std::isfinite(g.position.x()) || !std::isfinite(g.position.y());
        })) throw std::runtime_error("Skia 混合字体返回非有限坐标");
    return out;
}
}
