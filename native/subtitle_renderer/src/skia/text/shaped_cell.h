#pragma once
#include "font_instance.h"
#include "include/core/SkPath.h"
#include "include/core/SkPoint.h"
#include "include/core/SkRect.h"
#include <string>

namespace subtitle::skia::text {
struct ShapedGlyph {
    SkGlyphID id;
    SkPoint position;
    std::uint32_t clusterStart, clusterEnd; // UTF-8 byte range within the lyric cell.
};
struct ShapedCell {
    std::string fontIdentity;
    std::size_t selectedFont = 0;
    float advance = 0;
    float designAdvance = 0, designLeftBearing = 0, designRightBearing = 0;
    float ascent = 0, descent = 0, leading = 0;
    SkRect ink = SkRect::MakeEmpty();
    std::vector<ShapedGlyph> glyphs;
    SkPath outline;
    bool outlineAvailable = true;
};
// Cell boundaries are authored timing boundaries. Never form ligatures across them.
ShapedCell shapeCell(const std::string& utf8, const std::vector<std::shared_ptr<const FontInstance>>& fonts,
                     const std::string& language = "und", bool rightToLeft = false);
}
