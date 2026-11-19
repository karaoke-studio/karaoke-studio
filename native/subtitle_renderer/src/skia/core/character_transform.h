#pragma once

#include "character_fade.h"
#include "include/core/SkMatrix.h"
#include <algorithm>
#include <cmath>

namespace subtitle::skia {
// character_transform_schema=1: the existing GPU's char_drip and spin_flip.
// Pivots are the canonical layout cell (left/right) and the glyph run's
// ascent/descent box around the baseline, never ink centers.
struct CharacterPivot {
    float layoutLeft = 0, layoutRight = 0, centerY = 0;
    float inkLeft = 0, inkRight = 0, wipePad = 0;
};
struct CharacterTransformState {
    float opacity = 1;
    SkMatrix matrix = SkMatrix::I();
    bool transformed = false;
    float glowAlpha = 0, glowRadiusEm = 0;
    bool glowEcho = false;
};

inline CharacterTransformState characterTransformAt(const CharacterFade& fade, int time, int displayStart,
                                                    int displayEnd, int index, int count,
                                                    const CharacterPivot& pivot) {
    const float progress = fade.progressAt(time, displayStart, displayEnd, index, count);
    const int side = fade.side(time, displayStart, displayEnd);
    const auto kind = side > 0 ? fade.exitKind : side < 0 ? fade.entryKind : CharacterFade::Kind::Fade;
    CharacterTransformState state;
    if (kind == CharacterFade::Kind::Fade || side == 0) {
        state.opacity = progress;
        return state;
    }
    constexpr float pi = 3.14159265358979323846f;
    const float clamped = std::clamp(progress, 0.f, 1.f);
    const float skewOf = std::tan(std::min((pi * .5f) * (1.f - clamped), pi * 89.f / 180.f));
    if (kind == CharacterFade::Kind::Drip) {
        // Vertical shear about the layout cell's right edge; fully opaque once started.
        state.opacity = progress > 0 ? 1.f : 0.f;
        if (clamped < 1.f) {
            const float skew = float(-side) * skewOf;
            state.matrix = SkMatrix::MakeAll(1, 0, 0, skew, 1, -skew * pivot.layoutRight, 0, 0, 1);
            state.transformed = progress > 0;
        }
        return state;
    }
    // spin_flip: translate(center) * shear(0, skew) * scale(progress) * translate(-center).
    state.opacity = progress;
    if (clamped < 1.f) {
        const float skew = float(side) * skewOf;
        const float cx = (pivot.layoutLeft + pivot.layoutRight) * .5f, cy = pivot.centerY;
        state.matrix = SkMatrix::MakeAll(clamped, 0, cx * (1.f - clamped),
                                         clamped * skew, clamped, cy * (1.f - clamped) - clamped * skew * cx, 0, 0, 1);
        state.transformed = true;
    }
    return state;
}
}
