#pragma once

#include "character_transform.h"

namespace subtitle::skia {
struct SingingScale {
    bool active = false;
    bool zoomPulse = false;
    int curveLevel = 1;
    int peakPercent = 125;

    CharacterTransformState at(int time, int start, int end, const CharacterPivot& pivot) const {
        if (!active || end <= start || time <= start) return {};
        float scale = 1.f;
        if (zoomPulse && time < end + 300) {
            const double fraction = time < end ? 1.0 - double(time - start) / (end - start)
                                               : double(time - end) / 300;
            double powered = fraction;
            for (int exponent = 1; exponent < curveLevel; ++exponent) powered *= fraction;
            scale = float(1.0 + (peakPercent / 100.0 - 1.0) * (1.0 - powered));
        } else if (!zoomPulse && time < end) {
            const int ramp = std::min(int(float(end - start) * .25f), 100);
            if (ramp == 0) return {};
            const int peak = start + ramp;
            const float progress = time <= peak ? float(time - start) / ramp : float(end - time) / (end - peak);
            scale = 1.f + .15f * progress;
        }
        if (scale == 1.f) return {};
        const float x = zoomPulse ? (pivot.layoutLeft + pivot.layoutRight) * .5f : pivot.layoutLeft;
        const float y = zoomPulse ? pivot.centerY : pivot.baseline;
        CharacterTransformState state;
        state.matrix = SkMatrix::MakeAll(scale, 0, x * (1.f - scale), 0, scale, y * (1.f - scale), 0, 0, 1);
        state.transformed = true;
        return state;
    }
};
}
