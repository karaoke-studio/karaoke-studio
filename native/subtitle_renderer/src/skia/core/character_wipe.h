#pragma once

#include "../../effects/wipe_clock.h"
#include "include/core/SkCanvas.h"
#include <cmath>

namespace subtitle::skia {
inline void clipViewportWipe(SkCanvas& canvas, SkRect clip, float viewportHeight, bool antialias) {
    if (viewportHeight == 0.f) {
        canvas.clipRect(clip, SkClipOp::kIntersect, antialias);
        return;
    }
    // Direct2D clips each wipe side to the device-aligned bounds of its
    // transformed rectangle. Glow sources keep their own unrotated clips.
    clip.fTop = -2.f * viewportHeight;
    clip.fBottom = 2.f * viewportHeight;
    const SkMatrix transform = canvas.getTotalMatrix();
    canvas.resetMatrix();
    canvas.clipRect(transform.mapRect(clip), SkClipOp::kIntersect, antialias);
    canvas.setMatrix(transform);
}

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

inline float transformedWipeEdge(float originX, float left, float right, float ratio, float pad) {
    // The existing GPU rounds the transformed ink in line-local coordinates.
    const float lo = std::floor(left - originX) - pad;
    const float hi = std::ceil(right - originX) + pad;
    return originX + lo + std::max(hi - lo, 1.f) * ratio;
}
}
