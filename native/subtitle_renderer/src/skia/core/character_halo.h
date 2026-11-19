#pragma once

#include "character_transform.h"
#include "solid_outline.h"
#include "../../effects/effect_constants.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"

namespace subtitle::skia {
inline void drawCharacterHalo(SkCanvas& canvas, const SkPath& path,
                              const CharacterTransformState& state, float fontPx) {
    if (state.opacity <= 0 || state.glowAlpha <= 0 || state.glowRadiusEm <= 0 || path.isEmpty()) return;
    using namespace krok::subtitle::native::effects;
    const float radius = state.glowRadiusEm * std::max(fontPx, 1.f);
    SkPaint paint;
    paint.setColor(SK_ColorWHITE);
    paint.setAntiAlias(true);
    if (state.glowEcho) {
        float decay = 1.f;
        for (int index = 1; index <= kGlowEchoCopies; ++index) {
            decay *= kGlowEchoDecay;
            const float offset = radius * kGlowEchoPitchEm * float(index);
            if (offset < .5f) continue;
            const float alpha = state.glowAlpha * decay;
            if (alpha <= .008f) break;
            paint.setAlphaf(std::clamp(alpha, 0.f, 1.f));
            SkAutoCanvasRestore restore(&canvas, true);
            canvas.translate(offset, 0);
            canvas.drawPath(path, paint);
            canvas.translate(-2.f * offset, 0);
            canvas.drawPath(path, paint);
        }
        return;
    }
    for (const auto& stroke : kGlowHaloStrokes) {
        const float width = radius * stroke.widthEm;
        if (width < .5f) continue;
        paint.setAlphaf(std::clamp(state.opacity * state.glowAlpha * stroke.strength, 0.f, 1.f));
        drawFixedOutline(canvas, path, paint, width);
    }
}
}
