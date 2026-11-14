#pragma once
#include "include/core/SkFont.h"
#include "include/core/SkPath.h"
#include <memory>
#include <optional>

namespace subtitle::skia::text {
struct FontSpec;
struct GlyphGeometry {
    std::optional<SkPath> path;
    float advance = 0, leftBearing = 0, rightBearing = 0; // Design units, not pixel bounds.
    bool requiresColorPaint = false;
};
// Uses the locked FreeType public API at the face's own em scale. SkFont's
// 64px-normalized paths round valid widths across integer cell-box boundaries.
class GlyphOutlineSource {
public:
    explicit GlyphOutlineSource(const FontSpec&);
    ~GlyphOutlineSource();
    std::optional<GlyphGeometry> load(SkGlyphID) const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
