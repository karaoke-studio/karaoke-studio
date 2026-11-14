#pragma once
#include "include/core/SkPath.h"
#include <cstdint>
#include <vector>

namespace subtitle::skia {
// Final physical geometry. No font lookup, shaping, layout or timing allocation
// is allowed in PreparedScene's resolved-input path or in a frame draw.
struct ResolvedCell {
    SkPath path;
    float left = 0, right = 0;
    int startMs = 0, endMs = 0;
};
struct ResolvedLine {
    std::vector<ResolvedCell> cells;
    std::uint32_t before = 0, after = 0;
    int startMs = 0, endMs = 0;
};
struct ResolvedSubtitleInput {
    int width = 0, height = 0;
    std::vector<ResolvedLine> lines;
};
}
