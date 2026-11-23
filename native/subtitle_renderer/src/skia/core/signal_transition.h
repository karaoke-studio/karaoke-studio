#pragma once

#include "character_geometry.h"

namespace subtitle::skia {
struct SignalTransition {
    const CharacterFade& fade;
    const CharacterGeometry& geometry;
    std::optional<float> lineCenter;
    int startMs, endMs;

    CharacterTransformState at(int time, int index, int count, float x, float y, bool entryOnly = false) const {
        auto group = fade;
        group.delay = count > 1 ? 350 / (count - 1) : 0;
        if (entryOnly) group.exit = false;
        const CharacterPivot pivot{x, x, y};
        if (geometry.active() && group.side(time, startMs, endMs) == 0)
            return geometry.at(time, startMs, endMs, index, count, pivot,
                               entryOnly ? std::nullopt : lineCenter, entryOnly);
        // Lamps and bars shear about their own centers.
        return characterTransformAt(group, time, startMs, endMs, index, count, pivot);
    }
};
}
