#pragma once
#include "include/core/SkPath.h"
#include "../../model/wipe_point.h"
#include <cstdint>
#include <vector>

namespace subtitle::skia {
// Final physical geometry. No font lookup, shaping, layout or timing allocation
// is allowed in PreparedScene's resolved-input path or in a frame draw.
struct ResolvedCell {
    SkPath path;
    float left = 0, right = 0;
    int startMs = 0, endMs = 0;
    std::uint32_t before = 0, after = 0;
    std::vector<krok::subtitle::native::WipePoint> wipePoints;
    bool adjustedWipeEnd = false; // Configuration-only validation marker; never changes time.
};
struct ResolvedLine {
    std::vector<ResolvedCell> cells;
    int startMs = 0, endMs = 0;
    struct Translation { int startMs, endMs; float x, y; };
    std::vector<Translation> translations;
    // One continuous wipe front clips every path in the group. Unit clocks
    // are final values; neither grouping nor timing is resolved while drawing.
    std::vector<std::vector<ResolvedCell>> rubyGroups;
};
struct ResolvedSubtitleInput {
    int width = 0, height = 0;
    std::vector<ResolvedLine> lines;
};
}
