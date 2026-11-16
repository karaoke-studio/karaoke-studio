#include "font_shaping.h"
#include "include/core/SkTypeface.h"
#include "modules/skshaper/include/SkShaper_harfbuzz.h"
#include "third_party/externals/harfbuzz/src/hb.h"
#include <algorithm>
#include <cmath>
#include <memory>
#include <stdexcept>

namespace subtitle::skia::text {
ShapedRange shapeFontRange(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end, const std::string& language, bool rtl) {
    if (begin >= end || end > utf8.size()) throw std::runtime_error("Skia 字体塑形范围无效");
    auto scripts = SkShapers::HB::ScriptRunIterator(utf8.data(), utf8.size());
    if (!scripts) throw std::runtime_error("Skia script iterator 初始化失败");
    ShapedRange out;
    std::uint32_t scriptStart = 0;
    int scale = 0; hb_font_get_scale(font.shapingFace(), &scale, nullptr);
    if (scale <= 0) throw std::runtime_error("Skia HarfBuzz 塑形尺度无效");
    const double pixels = double(font.shapingFont().getSize())/scale;
    double cursor = 0;
    while (!scripts->atEnd()) {
        scripts->consume();
        const auto scriptEnd = std::uint32_t(scripts->endOfCurrentRun());
        const auto left = std::max(begin, scriptStart), right = std::min(end, scriptEnd);
        scriptStart = scriptEnd;
        if (left >= right) continue;
        auto buffer = std::shared_ptr<hb_buffer_t>(hb_buffer_create(), hb_buffer_destroy);
        if (!hb_buffer_allocation_successful(buffer.get())) throw std::runtime_error("Skia HB buffer 分配失败");
        hb_buffer_add_utf8(buffer.get(), utf8.data(), int(utf8.size()), left, int(right-left));
        hb_buffer_set_direction(buffer.get(), rtl ? HB_DIRECTION_RTL : HB_DIRECTION_LTR);
        hb_buffer_set_script(buffer.get(), hb_script_from_iso15924_tag(scripts->currentScript()));
        hb_buffer_set_language(buffer.get(), hb_language_from_string(language.c_str(), int(language.size())));
        hb_buffer_set_cluster_level(buffer.get(), HB_BUFFER_CLUSTER_LEVEL_MONOTONE_GRAPHEMES);
        // Match the previous shaper: no BOT flag that inserts dotted circles.
        hb_shape(font.shapingFace(), buffer.get(), nullptr, 0);
        if (!hb_buffer_allocation_successful(buffer.get())) throw std::runtime_error("Skia HB 塑形内存不足");
        unsigned count = 0;
        const auto info = hb_buffer_get_glyph_infos(buffer.get(), &count);
        const auto positions = hb_buffer_get_glyph_positions(buffer.get(), nullptr);
        double vertical = 0;
        for (unsigned i = 0; i < count; ++i) {
            if (info[i].codepoint > 65535 || info[i].cluster < left || info[i].cluster >= right)
                throw std::runtime_error("Skia HarfBuzz 返回无效字形或 cluster");
            const double x = cursor+positions[i].x_offset*pixels;
            const double y = vertical-positions[i].y_offset*pixels;
            out.glyphs.push_back({SkGlyphID(info[i].codepoint), {float(x), float(y)}, info[i].cluster, 0});
            cursor += positions[i].x_advance*pixels;
            vertical -= positions[i].y_advance*pixels;
        }
    }
    if (!std::isfinite(cursor) || std::any_of(out.glyphs.begin(), out.glyphs.end(), [](const auto& g) {
            return !std::isfinite(g.position.x()) || !std::isfinite(g.position.y());
        })) throw std::runtime_error("Skia HarfBuzz 返回非有限坐标");
    out.advance = float(cursor);
    return out;
}
}
