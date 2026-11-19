#pragma once

#include <algorithm>
#include <cstdint>

namespace subtitle::skia {
// The existing GPU's per-character transition clock: char_fade, char_drip and
// spin_flip share the 350ms stagger, 250ms ramp and the exit-over-entry priority.
struct CharacterFade {
    enum class Kind : std::uint8_t { Fade, Drip, Spin };
    bool entry = false;
    bool exit = false;
    int singingEnd = 0;
    int delay = 0;
    Kind entryKind = Kind::Fade;
    Kind exitKind = Kind::Fade;

    bool active() const { return entry || exit; }
    bool transforms() const {
        return (entry && entryKind != Kind::Fade) || (exit && exitKind != Kind::Fade);
    }
    // +1 while the exit window owns the line, -1 for the entry window, else 0.
    int side(int time, int displayStart, int displayEnd) const {
        constexpr int window = 600;
        if (exit && time >= std::max(singingEnd, displayEnd - window)) return 1;
        if (entry && time <= displayStart + window) return -1;
        return 0;
    }
    float progressAt(int time, int displayStart, int displayEnd, int index, int count) const {
        constexpr float duration = 250.f;
        const int owner = side(time, displayStart, displayEnd);
        if (owner > 0) {
            const int end = displayEnd - delay * (count - index - 1);
            return std::clamp(float(end - time) / duration, 0.f, 1.f);
        }
        if (owner < 0) {
            const int start = displayStart + delay * index;
            return std::clamp(float(time - start) / duration, 0.f, 1.f);
        }
        return 1.f;
    }
    float opacityAt(int time, int displayStart, int displayEnd, int index, int count) const {
        return progressAt(time, displayStart, displayEnd, index, count);
    }
};
}
