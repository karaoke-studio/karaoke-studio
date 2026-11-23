#pragma once

#include "singing_scale.h"

namespace subtitle::skia {
struct UtopiaTransition {
    static constexpr float entryPeakScale = 1.3f;
    bool enter = false, leave = false;
    int tailMs = 0;
    float amplitude = 0;

    bool active() const { return enter || leave; }
    bool inEntryWindow(int time, int displayStart) const { return enter && time <= displayStart + 700; }
    static SkMatrix entryScaleMatrix(const CharacterPivot& pivot, float amount) {
        SkMatrix matrix;
        matrix.setScale(amount, amount, pivot.layoutLeft, pivot.baseline);
        return matrix;
    }

    static CharacterTransformState entryAt(int time, int displayStart, int index, int count, float x, float y) {
        const int delay = count > 1 ? (200 / (count - 1)) * index : 0;
        const int elapsed = std::max(time - displayStart - delay, 0);
        CharacterTransformState state;
        state.opacity = std::min(float(elapsed) / 400.f, 1.f);
        float scale = 1.f;
        if (elapsed < 400) scale = entryPeakScale * float(elapsed) / 400.f;
        else if (elapsed < 500) scale = 1.f + .3f * float(500 - elapsed) / 100.f;
        if (state.opacity > 0 && scale != 1.f) {
            state.matrix.setScale(scale, scale, x, y);
            state.transformed = true;
        }
        return state;
    }

    CharacterTransformState at(int time, int displayStart, int index, int count, int followingDone,
                               const CharacterPivot& pivot, const SingingScale& scale,
                               int wipeStart, int wipeEnd) const {
        constexpr float pi = 3.14159265358979323846f;
        const float cx = (pivot.layoutLeft + pivot.layoutRight) * .5f;
        const float cy = pivot.centerY;
        if (inEntryWindow(time, displayStart))
            return entryAt(time, displayStart, index, count, scale.zoomPulse ? cx : pivot.layoutLeft,
                           scale.zoomPulse ? cy : pivot.baseline);
        const auto singing = scale.at(time, wipeStart, wipeEnd, pivot);
        float scaleX = 1.f, scaleY = 1.f, rotation = 0.f, dx = 0.f, dy = 0.f;
        CharacterTransformState state;
        if (leave && time > followingDone) {
            const float progress = std::clamp(float(time - followingDone) / 750.f, 0.f, 1.f);
            state.opacity = 1.f - progress;
            state.utopiaExit = true;
            const float shrink = state.opacity * (scale.zoomPulse ? singing.matrix.getScaleX() : 1.f);
            scaleX = shrink * std::cos(pi * progress);
            scaleY = shrink;
            rotation = -180.f * progress;
            dx = -amplitude * (progress <= .5f ? std::sin(pi * progress)
                                              : 1.f + std::sin((progress - .5f) * pi));
            dy = std::sin(pi * progress * .5f) * amplitude;
        } else {
            return singing;
        }
        if (state.opacity <= 0.f) return state;
        state.matrix.setRotate(rotation, cx, cy);
        state.matrix.postScale(scaleX, scaleY, scale.zoomPulse ? cx : pivot.layoutLeft,
                               scale.zoomPulse ? cy : pivot.baseline);
        state.matrix.postTranslate(dx, dy);
        state.transformed = dx != 0 || dy != 0 || rotation != 0 || scaleX != 1 || scaleY != 1;
        return state;
    }
};
}
