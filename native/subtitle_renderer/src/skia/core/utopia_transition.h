#pragma once

#include "singing_scale.h"

namespace subtitle::skia {
struct UtopiaTransition {
    enum class ExitMatrix { Glyph, ZoomGlyph, Signal };
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

    CharacterTransformState exitAt(int elapsed, const CharacterPivot& pivot, float shrinkOverlay,
                                   ExitMatrix mode) const {
        constexpr float pi = 3.14159265358979323846f;
        const float progress = std::clamp(float(elapsed) / 750.f, 0.f, 1.f);
        CharacterTransformState state;
        state.opacity = 1.f - progress;
        state.utopiaExit = true;
        if (state.opacity <= 0.f) return state;
        const float scaleY = state.opacity * shrinkOverlay;
        const float scaleX = scaleY * std::cos(pi * progress);
        const float rotation = -180.f * progress;
        const float dx = -amplitude * (progress <= .5f ? std::sin(pi * progress)
                                                      : 1.f + std::sin((progress - .5f) * pi));
        const float dy = std::sin(pi * progress * .5f) * amplitude;
        const float cx = (pivot.layoutLeft + pivot.layoutRight) * .5f;
        const float cy = pivot.centerY;
        if (mode == ExitMatrix::Signal) {
            state.matrix.setScale(scaleX, scaleY, cx, cy);
            state.matrix.postRotate(rotation, cx, cy);
        } else {
            state.matrix.setRotate(rotation, cx, cy);
            state.matrix.postScale(scaleX, scaleY, mode == ExitMatrix::ZoomGlyph ? cx : pivot.layoutLeft,
                                   mode == ExitMatrix::ZoomGlyph ? cy : pivot.baseline);
        }
        state.matrix.postTranslate(dx, dy);
        state.transformed = dx != 0 || dy != 0 || rotation != 0 || scaleX != 1 || scaleY != 1;
        return state;
    }

    CharacterTransformState at(int time, int displayStart, int index, int count, int followingDone,
                               const CharacterPivot& pivot, const SingingScale& scale,
                               int wipeStart, int wipeEnd) const {
        const float cx = (pivot.layoutLeft + pivot.layoutRight) * .5f;
        const float cy = pivot.centerY;
        if (inEntryWindow(time, displayStart))
            return entryAt(time, displayStart, index, count, scale.zoomPulse ? cx : pivot.layoutLeft,
                           scale.zoomPulse ? cy : pivot.baseline);
        const auto singing = scale.at(time, wipeStart, wipeEnd, pivot);
        if (leave && time > followingDone)
            return exitAt(time - followingDone, pivot, scale.zoomPulse ? singing.matrix.getScaleX() : 1.f,
                          scale.zoomPulse ? ExitMatrix::ZoomGlyph : ExitMatrix::Glyph);
        return singing;
    }
};
}
