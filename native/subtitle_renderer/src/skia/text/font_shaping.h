#pragma once
#include "shaped_cell.h"

namespace subtitle::skia::text {
struct ShapedRange {
    std::vector<ShapedGlyph> glyphs;
    float advance = 0;
};
// Preserve full-cell HB context while shaping a font range in logical pixels.
ShapedRange shapeFontRange(const std::string& utf8, const FontInstance& font,
    std::uint32_t begin, std::uint32_t end, const std::string& language, bool rtl = false);
}
