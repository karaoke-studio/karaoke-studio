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
    bool hasHalo() const {
        return entry == "glow_in" || entry == "stretch_in" || exit == "glow_out" || exit == "stretch_out";
    }
    bool inEntryWindow(int time, int displayStart) const {
        return entryMs > 0 && time <= displayStart + std::clamp(entryMs, 120, 3000);
    }
    int exitStart(int displayEnd) const {
        return std::max(singingEnd, displayEnd - std::clamp(exitMs, 120, 3000));
    }
    bool inWindow(int time, int displayStart, int displayEnd) const {
        return inEntryWindow(time, displayStart) || (exitMs > 0 && time >= exitStart(displayEnd));
    }

    CharacterTransformState at(int time, int displayStart, int displayEnd, int index, int count,
                               const CharacterPivot& pivot, std::optional<float> lineCenter, bool entryOnly = false,
                               float exitScale = 1.f) const {
        const int leave = exitStart(displayEnd);
        const bool exiting = !entryOnly && exitMs > 0 && time >= leave;
        if (!exiting && !inEntryWindow(time, displayStart))
            return {};
        const float x = (pivot.layoutLeft + pivot.layoutRight) * .5f;
        const auto& effect = exiting ? exit : entry;
        auto value = krok::subtitle::native::effects::geoCharState(
            effect, fontPx, index, count, time,
            exiting ? leave : displayStart, exiting ? exitMs : entryMs,
            exiting, x, lineCenter.value_or(0.f), lineCenter.has_value());
        // The zoom pulse keeps shrinking back under an exit transition.
        if (exiting) {
            value.scaleX *= exitScale;
            value.scaleY *= exitScale;
        }
        CharacterTransformState state;
        state.opacity = value.opacity;
        state.glowAlpha = value.glowAlpha;
        state.glowRadiusEm = value.glowRadiusEm;
        state.glowEcho = effect == "glow_in" || effect == "glow_out";
        state.transformed = (state.opacity > 0 || state.glowEcho) && (value.dx != 0.f || value.dy != 0.f || value.rotation != 0.f
            || value.scaleX != 1.f || value.scaleY != 1.f);
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
