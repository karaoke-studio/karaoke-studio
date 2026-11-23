#pragma once

#include "character_geometry.h"
#include "utopia_transition.h"

namespace subtitle::skia {
struct SignalTransition {
    const CharacterFade& fade;
    const CharacterGeometry& geometry;
    const SingingScale& singingScale;
    const UtopiaTransition& utopia;
    std::optional<float> lineCenter;
    int startMs, endMs;

    CharacterTransformState at(int time, int index, int count, float x, float y, bool entryOnly = false) const {
        auto group = fade;
        group.delay = count > 1 ? 350 / (count - 1) : 0;
        if (entryOnly) group.exit = false;
        const CharacterPivot pivot{x, x, y};
        // Lamps and bars shear about their own centers.
        if (group.side(time, startMs, endMs) != 0)
            return characterTransformAt(group, time, startMs, endMs, index, count, pivot);
        const bool geometric = entryOnly ? geometry.inEntryWindow(time, startMs) : geometry.inWindow(time, startMs, endMs);
        if (geometric)
            return geometry.at(time, startMs, endMs, index, count, pivot,
                               entryOnly ? std::nullopt : lineCenter, entryOnly);
        if (utopia.inEntryWindow(time, startMs))
            return UtopiaTransition::entryAt(time, startMs, index, count, x, y);
        return {};
    }

    void applyZoomPulse(CharacterTransformState& pose, std::int64_t elapsed, std::int64_t start, std::int64_t end,
                        float x, float y) const {
        const auto pulse = singingScale.at(elapsed, start, end, {x, x, y});
        if (!pulse.transformed) return;
        pose.matrix.preConcat(pulse.matrix);
        pose.transformed = true;
    }
};
}
