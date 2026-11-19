#pragma once

#include "solid_outline.h"
#include "../../effects/effect_constants.h"
#include "include/core/SkPaint.h"
#include <algorithm>

namespace subtitle::skia {
inline float strokeFlashBaseWidth(float fontPx, float outlinePx) {
    return std::max(outlinePx, std::max(fontPx, 1.f) * krok::subtitle::native::effects::kStrokeFlashMinWidthEm);
}

inline void drawStrokeFlash(SkCanvas& canvas, const SkPath& body, int elapsedMs, float baseWidth) {
    using namespace krok::subtitle::native::effects;
    if (baseWidth <= 0 || elapsedMs < 0 || elapsedMs >= kStrokeFlashMs || body.isEmpty()) return;
    const float amount = 1.f - float(elapsedMs) / kStrokeFlashMs;
    SkPaint paint;
    paint.setColor(SK_ColorWHITE);
    paint.setAlphaf(kStrokeFlashAlpha * amount);
    paint.setAntiAlias(true);
    drawFixedOutline(canvas, body, paint, baseWidth * (1.f + kStrokeFlashWidthBoost * amount));
}
}
