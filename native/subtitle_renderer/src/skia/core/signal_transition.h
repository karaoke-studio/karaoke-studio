#pragma once

#include "character_fade.h"

namespace subtitle::skia {
struct SignalTransition {
    CharacterFade fade;
    int startMs, endMs;

    float opacityAt(int time, int index, int count, bool entryOnly = false) const {
        auto group = fade;
        group.delay = count > 1 ? 350 / (count - 1) : 0;
        if (entryOnly) group.exit = false;
        return group.progressAt(time, startMs, endMs, index, count);
    }
};
}
