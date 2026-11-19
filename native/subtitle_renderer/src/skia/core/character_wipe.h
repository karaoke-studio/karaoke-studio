#pragma once

#include "../../effects/wipe_clock.h"
#include <cmath>

namespace subtitle::skia {
// The transformed glyph uses the original authored curve, without the
// static-ink overlap endpoint applied to ordinary horizontal coverage.
inline float transformedWipeRatio(std::span<const krok::subtitle::native::WipePoint> points,
                                  bool adjusted, int start, int end, int time) {
    if (time <= start) return 0.f;
    if (points.size() < 2)
        return float(double(time) - start) / float(std::max(double(end) - start, 1.0));
    const auto& previous = points[points.size() - 2];
    if (time < previous.timeMs || (!adjusted && time <= end))
        return krok::subtitle::native::effects::wipePositionAt(points, time);
    if (end == previous.timeMs) return 1.f;
    return previous.position + (1.f - previous.position)
        * float(double(time) - previous.timeMs) / float(double(end) - previous.timeMs);
}

inline float transformedWipeEdge(float originX, float left, float right, float ratio) {
    // The existing GPU rounds the transformed ink in line-local coordinates.
    const float lo = std::floor(left - originX);
    const float hi = std::ceil(right - originX);
    return originX + lo + std::max(hi - lo, 1.f) * ratio;
}
}
