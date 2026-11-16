#pragma once
#include "shaped_cell.h"

namespace subtitle::skia::text {
// Conservative intersections of ICU graphemes and all candidate HB clusters.
// Does not create lyric units or allocate their authored time.
std::vector<ShapedFontRun> mixedFontRanges(const std::string& utf8,
    const std::vector<std::shared_ptr<const FontInstance>>& fonts,
    const std::vector<std::vector<ShapedGlyph>>& candidates);
}
