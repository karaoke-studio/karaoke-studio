#pragma once
#include "../model/wipe_point.h"
#include <algorithm>
#include <span>

namespace krok::subtitle::native::effects {
// Configuration validates sorted timestamps. Boundary behavior matches the
// existing GPU clock: first point at the initial time, last equal-time point
// at internal discontinuities, and the final point at/after completion.
inline float wipePositionAt(std::span<const WipePoint> points, int timeMs) {
    if (points.empty()) return 0;
    if (timeMs <= points.front().timeMs) return points.front().position;
    if (timeMs >= points.back().timeMs) return points.back().position;
    const auto next = std::upper_bound(points.begin(), points.end(), timeMs,
        [](int t, const WipePoint& p) { return t < p.timeMs; });
    const auto& previous = *(next - 1);
    const int duration = next->timeMs - previous.timeMs;
    if (duration <= 0) return next->position;
    const float local = std::clamp(float(timeMs - previous.timeMs) / float(duration), 0.f, 1.f);
    return previous.position + (next->position - previous.position) * local;
}
}
