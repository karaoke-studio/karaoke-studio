#pragma once
#include "shaped_cell.h"

namespace subtitle::skia::text {
ShapedCell shapeMixedCell(const std::string& utf8,
    const std::vector<std::shared_ptr<const FontInstance>>& fonts,
    const std::string& language, const std::vector<std::vector<ShapedGlyph>>& candidates);
}
