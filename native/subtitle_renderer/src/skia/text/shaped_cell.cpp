#include "text/shaped_cell.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPathBuilder.h"
#include "include/core/SkTypeface.h"
#include "modules/skshaper/include/SkShaper_harfbuzz.h"
#include "modules/skunicode/include/SkUnicode_icu.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace subtitle::skia::text {
namespace {
std::vector<SkUnichar> decode(const std::string& text) {
    std::vector<SkUnichar> out;
    for (std::size_t i = 0; i < text.size();) {
        const auto first = std::uint8_t(text[i++]);
        unsigned count = first < 0x80 ? 0 : (first & 0xe0) == 0xc0 ? 1 : (first & 0xf0) == 0xe0 ? 2 : (first & 0xf8) == 0xf0 ? 3 : 99;
        if (count == 99 || i + count > text.size()) throw std::runtime_error("Skia 文本不是有效 UTF-8");
        std::uint32_t c = count ? first & ((1u << (6 - count)) - 1) : first;
        for (unsigned j = 0; j < count; ++j) {
            auto next = std::uint8_t(text[i++]);
            if ((next & 0xc0) != 0x80) throw std::runtime_error("Skia 文本不是有效 UTF-8");
            c = (c << 6) | (next & 0x3f);
        }
        if ((count == 1 && c < 0x80) || (count == 2 && c < 0x800) || (count == 3 && c < 0x10000)
            || c > 0x10ffff || (c >= 0xd800 && c <= 0xdfff) || c < 0x20 || c == 0x7f)
            throw std::runtime_error("Skia 文本包含无效码点或换行/控制字符");
        out.push_back(SkUnichar(c));
    }
    return out;
}
class Handler final : public SkShaper::RunHandler {
public:
    std::vector<SkGlyphID> ids;
    std::vector<SkPoint> positions;
    std::vector<std::uint32_t> clusters;
    std::vector<ShapedGlyph> output;
    float advance = 0;
    void beginLine() override {}
    void runInfo(const RunInfo&) override {}
    void commitRunInfo() override {}
    Buffer runBuffer(const RunInfo& info) override {
        ids.resize(info.glyphCount); positions.resize(info.glyphCount); clusters.resize(info.glyphCount);
        return {ids.data(), positions.data(), nullptr, clusters.data(), {advance, 0}};
    }
    void commitRunBuffer(const RunInfo& info) override {
        for (std::size_t i = 0; i < ids.size(); ++i) output.push_back({ids[i], positions[i], clusters[i], 0});
        advance += info.fAdvance.x();
    }
    void commitLine() override {}
};
}
ShapedCell shapeCell(const std::string& utf8, const std::vector<std::shared_ptr<const FontInstance>>& fonts,
                     const std::string& language, bool rightToLeft) {
    if (utf8.empty() || utf8.size() > 65536 || fonts.empty()) throw std::runtime_error("Skia 度量单元或字体链无效");
    decode(utf8);
    auto unicode = SkUnicodes::ICU::Make();
    auto shaper = SkShapers::HB::ShapeDontWrapOrReorder(unicode, nullptr);
    if (!unicode || !shaper) throw std::runtime_error("Skia HarfBuzz/ICU shaping 初始化失败");
    Handler handler;
    std::size_t selected = 0;
    for (; selected < fonts.size(); ++selected) {
        if (!fonts[selected]) throw std::runtime_error("Skia 字体链包含空实例");
        const auto& font = fonts[selected]->shapingFont();
        auto script = SkShapers::HB::ScriptRunIterator(utf8.data(), utf8.size());
        if (!script) throw std::runtime_error("Skia script iterator 初始化失败");
        SkShaper::TrivialFontRunIterator fontRuns(font, utf8.size());
        SkShaper::TrivialBiDiRunIterator bidi(rightToLeft ? 1 : 0, utf8.size());
        SkShaper::TrivialLanguageRunIterator languages(language.c_str(), utf8.size());
        Handler candidate;
        shaper->shape(utf8.data(), utf8.size(), fontRuns, bidi, *script, languages, nullptr, 0,
                      std::numeric_limits<float>::max(), &candidate);
        // Coverage is the SHAPED result: cmap-only checks wrongly reject a +
        // combining mark when the face supplies its precomposed glyph instead.
        if (!candidate.output.empty() && std::all_of(candidate.output.begin(), candidate.output.end(),
                                                    [](const auto& glyph) { return glyph.id != 0; })) {
            handler = std::move(candidate);
            break;
        }
    }
    if (selected == fonts.size()) throw std::runtime_error("Skia 显式字体链缺少该歌词单元的完整字形");
    const auto& instance = *fonts[selected];
    const auto& font = instance.shapingFont();
    ShapedCell result;
    result.fontIdentity = instance.identity(); result.selectedFont = selected;
    result.advance = handler.advance * instance.horizontalScale();
    result.ascent = instance.ascent(); result.descent = instance.descent(); result.leading = instance.leading();
    result.glyphs = std::move(handler.output);
    std::vector<GlyphGeometry> geometries;
    for (const auto& glyph : result.glyphs) {
        geometries.push_back(instance.glyphGeometry(glyph.id));
        if (geometries.back().requiresColorPaint) result.outlineAvailable = false;
        result.designAdvance += geometries.back().advance;
    }
    result.designLeftBearing = geometries.front().leftBearing;
    result.designRightBearing = geometries.back().rightBearing;
    std::vector<std::uint32_t> boundaries{std::uint32_t(utf8.size())};
    for (const auto& glyph : result.glyphs) {
        if (glyph.clusterStart >= utf8.size() || (std::uint8_t(utf8[glyph.clusterStart]) & 0xc0) == 0x80)
            throw std::runtime_error("Skia shaping 返回无效 cluster 偏移");
        boundaries.push_back(glyph.clusterStart);
    }
    std::sort(boundaries.begin(), boundaries.end());
    boundaries.erase(std::unique(boundaries.begin(), boundaries.end()), boundaries.end());
    SkPathBuilder outlines;
    std::size_t geometryIndex = 0;
    for (auto& glyph : result.glyphs) {
        if (!glyph.id) throw std::runtime_error("Skia shaping 返回缺字 glyph");
        glyph.clusterEnd = *std::upper_bound(boundaries.begin(), boundaries.end(), glyph.clusterStart);
        const auto& path = geometries[geometryIndex++].path;
        SkRect bounds = path ? path->computeTightBounds() : font.getBounds(glyph.id, nullptr);
        bounds.offset(glyph.position.x(), glyph.position.y());
        bounds.fLeft *= instance.horizontalScale(); bounds.fRight *= instance.horizontalScale();
        result.ink.join(bounds);
        if (path) {
            auto positioned = path->makeTransform(SkMatrix::Translate(glyph.position.x(), glyph.position.y()));
            outlines.addPath(positioned.makeTransform(SkMatrix::Scale(instance.horizontalScale(), 1)));
        }
        else if (!bounds.isEmpty()) result.outlineAvailable = false;
        glyph.position.fX *= instance.horizontalScale();
    }
    result.outline = outlines.detach();
    for (const auto value : {result.advance, result.ascent, result.descent, result.leading,
                            result.designAdvance, result.designLeftBearing, result.designRightBearing})
        if (!std::isfinite(value)) throw std::runtime_error("Skia shaping 返回非有限度量");
    if (!result.ink.isFinite() || std::any_of(result.glyphs.begin(), result.glyphs.end(), [](const auto& glyph) {
            return !std::isfinite(glyph.position.x()) || !std::isfinite(glyph.position.y());
        })) throw std::runtime_error("Skia shaping 返回非有限字形坐标");
    return result;
}
}
