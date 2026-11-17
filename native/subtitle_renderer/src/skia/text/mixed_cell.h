#pragma once
#include "shaped_cell.h"

namespace subtitle::skia::text {
// opentype is the authored cell's single cellUsesOpenTypeShaping decision.
ShapedCell shapeMixedCell(const std::string& utf8,
    const std::vector<std::shared_ptr<const FontInstance>>& fonts,
    const std::string& language, const std::vector<std::vector<ShapedGlyph>>& candidates, bool opentype);
}
