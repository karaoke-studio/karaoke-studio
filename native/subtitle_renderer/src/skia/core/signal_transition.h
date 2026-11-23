#pragma once

#include "character_transform.h"

namespace subtitle::skia {
struct SignalTransition {
    const CharacterFade& fade;
    int startMs, endMs;

    CharacterTransformState at(int time, int index, int count, float x, float y, bool entryOnly = false) const {
        auto group = fade;
        group.delay = count > 1 ? 350 / (count - 1) : 0;
        if (entryOnly) group.exit = false;
        // Lamps and bars shear about their own centers.
        return characterTransformAt(group, time, startMs, endMs, index, count, {x, x, y});
    }
};
}
