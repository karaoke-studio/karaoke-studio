#include "fallback_ranges.h"
#include "modules/skshaper/include/SkShaper_harfbuzz.h"
#include "modules/skunicode/include/SkUnicode_icu.h"
#include <algorithm>
#include <set>
#include <iterator>
#include <stdexcept>

namespace subtitle::skia::text {
std::vector<ShapedFontRun> mixedFontRanges(const std::string& utf8,
    const std::vector<std::shared_ptr<const FontInstance>>& fonts,
    const std::vector<std::vector<ShapedGlyph>>& candidates) {
    auto unicode = SkUnicodes::ICU::Make();
    auto breaks = unicode ? unicode->makeBreakIterator(SkUnicode::BreakType::kGraphemes) : nullptr;
    if (!breaks || !breaks->setText(utf8.data(), int(utf8.size())) || candidates.size() != fonts.size())
        throw std::runtime_error("Skia 混合字体字素边界初始化失败");
    auto script = SkShapers::HB::ScriptRunIterator(utf8.data(), utf8.size());
    if (!script) throw std::runtime_error("Skia 混合字体 script 初始化失败");
    while (!script->atEnd()) {
        script->consume();
        const auto tag = script->currentScript();
        if (tag != SkSetFourByteTag('L','a','t','n') && tag != SkSetFourByteTag('H','a','n','i')
            && tag != SkSetFourByteTag('H','i','r','a') && tag != SkSetFourByteTag('K','a','n','a')
            && tag != SkSetFourByteTag('H','a','n','g') && tag != SkSetFourByteTag('Z','y','y','y')
            && tag != SkSetFourByteTag('Z','i','n','h'))
            throw std::runtime_error("Skia 混合字体尚不支持该文字的跨字体连接塑形");
    }
    unicode->forEachCodepoint(utf8.data(), int(utf8.size()), [&](SkUnichar c, auto, auto, auto) {
        // Do not duplicate the source model's IPA tailoring or split joined
        // emoji. These remain valid when one explicit face covers the cell.
        if (c == 0x200c || c == 0x200d || (c >= 0x035c && c <= 0x0362)
            || c == 0x1dcd || c == 0x1dfc || c == 0x20e3
            || (c > 0xff && unicode->isEmoji(c)) || unicode->isRegionalIndicator(c))
            throw std::runtime_error("Skia 连结字素或 emoji 的混合字体塑形尚未验收");
    });
    std::set<std::uint32_t> boundaries;
    for (int p = breaks->first(); !breaks->isDone(); p = breaks->next()) {
        if (p < 0 || p > int(utf8.size())) throw std::runtime_error("Skia 字素边界无效");
        boundaries.insert(std::uint32_t(p));
    }
    boundaries.insert(std::uint32_t(utf8.size()));
    std::vector<std::vector<std::uint32_t>> missing, starts;
    for (const auto& glyphs : candidates) {
        std::set<std::uint32_t> clusters{std::uint32_t(utf8.size())};
        std::vector<std::uint32_t> absent, present;
        for (const auto& glyph : glyphs) {
            if (glyph.clusterStart >= utf8.size() || (std::uint8_t(utf8[glyph.clusterStart]) & 0xc0) == 0x80)
                throw std::runtime_error("Skia 混合字体候选 cluster 无效");
            clusters.insert(glyph.clusterStart);
            present.push_back(glyph.clusterStart);
            if (!glyph.id) absent.push_back(glyph.clusterStart);
        }
        std::set<std::uint32_t> common;
        std::set_intersection(boundaries.begin(), boundaries.end(), clusters.begin(), clusters.end(),
                              std::inserter(common, common.end()));
        boundaries = std::move(common);
        std::sort(absent.begin(), absent.end()); std::sort(present.begin(), present.end());
        missing.push_back(std::move(absent)); starts.push_back(std::move(present));
    }
    if (!boundaries.contains(0) || boundaries.size() < 3)
        throw std::runtime_error("Skia 显式字体链缺少不可分字素的完整字形");
    std::vector<ShapedFontRun> ranges;
    for (auto current = boundaries.begin(), next = std::next(current); next != boundaries.end(); ++current, ++next) {
        std::size_t selected = 0;
        for (; selected < fonts.size(); ++selected) {
            auto absent = std::lower_bound(missing[selected].begin(), missing[selected].end(), *current);
            auto present = std::lower_bound(starts[selected].begin(), starts[selected].end(), *current);
            if ((absent == missing[selected].end() || *absent >= *next)
                && present != starts[selected].end() && *present < *next) break;
        }
        if (selected == fonts.size()) throw std::runtime_error("Skia 显式字体链缺少不可分字素的完整字形");
        if (!ranges.empty() && ranges.back().fontIndex == selected) ranges.back().byteEnd = *next;
        else ranges.push_back({*current, *next, selected, fonts[selected]->identity()});
        if (ranges.size() > 1024) throw std::runtime_error("Skia 单元内字体分段数量超限");
    }
    return ranges;
}
}
