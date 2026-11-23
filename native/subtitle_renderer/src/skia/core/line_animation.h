#pragma once

#include <algorithm>

namespace subtitle::skia {
struct LineAnimation {
    struct State { float opacity, dx, dy; };

    int inMs = 0;
    int outMs = 0;
    float inDx = 0, inDy = 0, outDx = 0, outDy = 0;
    bool quantizeOpacity = false;

    bool active() const { return inMs > 0 || outMs > 0; }

    State entryAt(int time, int start) const {
        float entry = 1.f;
        if (inMs > 0) {
            const float remaining = 1.f - std::clamp(float(time - start) / float(inMs), 0.f, 1.f);
            entry = 1.f - remaining * remaining;
        }
        return {entry, (1.f - entry) * inDx, (1.f - entry) * inDy};
    }

    State at(int time, int start, int end) const {
        const auto entry = entryAt(time, start);
        float exit = 1.f;
        if (outMs > 0) {
            const float remaining = std::clamp(float(end - time) / float(outMs), 0.f, 1.f);
            exit = remaining * remaining;
        }
        return {entry.opacity * exit, entry.dx + (1.f - exit) * outDx, entry.dy + (1.f - exit) * outDy};
    }
};
}
