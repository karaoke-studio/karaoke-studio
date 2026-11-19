#pragma once

#include "character_transform.h"
#include "../../effects/geometry.h"
#include <algorithm>
#include <optional>
#include <string>

namespace subtitle::skia {
struct CharacterGeometry {
    std::string entry, exit;
    int entryMs = 0, exitMs = 0, singingEnd = 0;
    float fontPx = 0;

    bool active() const { return entryMs > 0 || exitMs > 0; }

    CharacterTransformState at(int time, int displayStart, int displayEnd, int index, int count,
                               const CharacterPivot& pivot, std::optional<float> lineCenter) const {
        const int exitStart = std::max(singingEnd, displayEnd - std::clamp(exitMs, 120, 3000));
        const bool exiting = exitMs > 0 && time >= exitStart;
        if (!exiting && (entryMs <= 0 || time > displayStart + std::clamp(entryMs, 120, 3000)))
            return {};
        const float x = (pivot.layoutLeft + pivot.layoutRight) * .5f;
        const auto value = krok::subtitle::native::effects::geoCharState(
            exiting ? exit : entry, fontPx, index, count, time,
            exiting ? exitStart : displayStart, exiting ? exitMs : entryMs,
            exiting, x, lineCenter.value_or(0.f), lineCenter.has_value());
        CharacterTransformState state;
        state.opacity = value.opacity;
        state.transformed = value.dx != 0.f || value.dy != 0.f || value.rotation != 0.f
            || value.scaleX != 1.f || value.scaleY != 1.f;
        if (state.transformed) {
            state.matrix.setTranslate(x + value.dx, pivot.centerY + value.dy);
            state.matrix.preRotate(value.rotation);
            state.matrix.preScale(value.scaleX, value.scaleY);
            state.matrix.preTranslate(-x, -pivot.centerY);
        }
        return state;
    }
};
}
