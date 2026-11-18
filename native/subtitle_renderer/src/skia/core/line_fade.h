#pragma once

#include <algorithm>

namespace subtitle::skia {
struct LineFade {
    int inMs = 0;
    int outMs = 0;

    bool active() const { return inMs > 0 || outMs > 0; }

    float opacityAt(int time, int start, int end) const {
        float opacity = 1.f;
        if (inMs > 0) {
            const float remaining = 1.f - std::clamp(float(time - start) / float(inMs), 0.f, 1.f);
            opacity *= 1.f - remaining * remaining;
        }
        if (outMs > 0) {
            const float remaining = std::clamp(float(end - time) / float(outMs), 0.f, 1.f);
            opacity *= remaining * remaining;
        }
        return opacity;
    }
};
}
