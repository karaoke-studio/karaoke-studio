#pragma once

#include <algorithm>

namespace subtitle::skia {
struct CharacterFade {
    bool entry = false;
    bool exit = false;
    int singingEnd = 0;
    int delay = 0;

    bool active() const { return entry || exit; }

    float opacityAt(int time, int displayStart, int displayEnd, int index, int count) const {
        constexpr int window = 600;
        constexpr float duration = 250.f;
        if (exit && time >= std::max(singingEnd, displayEnd - window)) {
            const int end = displayEnd - delay * (count - index - 1);
            return std::clamp(float(end - time) / duration, 0.f, 1.f);
        }
        if (entry && time <= displayStart + window) {
            const int start = displayStart + delay * index;
            return std::clamp(float(time - start) / duration, 0.f, 1.f);
        }
        return 1.f;
    }
};
}
