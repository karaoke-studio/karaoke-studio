#pragma once
#include "include/gpu/ganesh/GrContextOptions.h"
namespace subtitle::skia {
inline GrContextOptions subtitleContextOptions() {
    GrContextOptions options;
    // Even single-sample surfaces otherwise use Ganesh's internal 4x MSAA
    // path atlas: only four coverage levels, too coarse for subtitle edges.
    // Use the same non-MSAA coverage paths for owned and borrowed devices.
    options.fInternalMultisampleCount = 0;
    // Distance-field paths introduce a second approximation of already-shaped
    // glyph edges. Preserve the canonical outline's coverage instead.
    options.fDisableDistanceFieldPaths = true;
    return options;
}
}
