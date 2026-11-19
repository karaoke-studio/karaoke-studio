#pragma once

#include "../../effects/particles.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPath.h"
#include "include/core/SkPathBuilder.h"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace subtitle::skia {
// Both of the existing GPU's solid-color singing star effects use this sprite. The
// shared deterministic evaluator owns every trajectory; this module only maps
// each particle onto the 1000-unit sprite path (scale, rotate, translate).
struct SingParticleBurst {
    krok::subtitle::native::ParticleBurst burst; // Physical size/travel, absolute clocks.
    SkPath sprite;                                // Winding fill, 1000-unit em space.
    std::uint32_t argb = 0xffffffff;
    float originX = 0, originY = 0, boxW = 1, boxH = 0; // Character pivot and anchor box.
};

namespace particle_detail {
inline SkPoint midpoint(SkPoint a, SkPoint b) {
    return {(a.x() + b.x()) * .5f, (a.y() + b.y()) * .5f};
}

inline void flattenQuadratic(SkPathBuilder& builder, SkPoint start, SkPoint control, SkPoint end,
                             unsigned depth = 0) {
    // The legacy star's quadratic simplifier subdivides in device axes.
    // At its 0.25px tolerance the measured second-difference limit is 0.75px,
    // including when the sprite is rotated. Generate these chords before AA.
    constexpr float secondDifferenceLimit = .75f;
    const float dx = start.x() - 2.f * control.x() + end.x();
    const float dy = start.y() - 2.f * control.y() + end.y();
    if (std::max(std::abs(dx), std::abs(dy)) <= secondDifferenceLimit || depth == 10) {
        builder.lineTo(end);
        return;
    }
    const auto left = midpoint(start, control), right = midpoint(control, end);
    const auto center = midpoint(left, right);
    flattenQuadratic(builder, start, left, center, depth + 1);
    flattenQuadratic(builder, center, right, end, depth + 1);
}

inline SkPath spriteOutline(const SkPath& sprite, const SkMatrix& transform) {
    const auto positioned = sprite.makeTransform(transform);
    SkPathBuilder builder(positioned.getFillType());
    SkPath::RawIter iter(positioned);
    SkPoint points[4];
    for (auto verb = iter.next(points); verb != SkPath::kDone_Verb; verb = iter.next(points)) {
        switch (verb) {
        case SkPath::kMove_Verb: builder.moveTo(points[0]); break;
        case SkPath::kLine_Verb: builder.lineTo(points[1]); break;
        case SkPath::kQuad_Verb: flattenQuadratic(builder, points[0], points[1], points[2]); break;
        case SkPath::kConic_Verb: builder.conicTo(points[1], points[2], iter.conicWeight()); break;
        case SkPath::kCubic_Verb: builder.cubicTo(points[1], points[2], points[3]); break;
        case SkPath::kClose_Verb: builder.close(); break;
        case SkPath::kDone_Verb: break;
        }
    }
    return builder.detach();
}
}

inline void drawSingParticles(SkCanvas& canvas, const std::vector<SingParticleBurst>& bursts, int tMs) {
    SkPaint paint;
    paint.setAntiAlias(true);
    for (const auto& item : bursts) {
        paint.setColor(item.argb);
        const float colorAlpha = paint.getAlphaf();
        for (const auto& particle : krok::subtitle::native::effects::burstParticlesAt(
                 item.burst, tMs, item.originX, item.originY, item.boxW, item.boxH)) {
            if (particle.alpha <= 0.f || particle.sizePx <= 0.f) continue;
            auto transform = SkMatrix::Translate(particle.x, particle.y);
            transform.preRotate(particle.rotationDeg);
            transform.preScale(particle.sizePx * particle.stretchX / 1000.f,
                               particle.sizePx * particle.stretchY / 1000.f);
            paint.setAlphaf(colorAlpha * std::clamp(particle.alpha, 0.f, 1.f));
            // Give Skia the final physical outline, as for the subtitle glyphs.
            canvas.drawPath(particle_detail::spriteOutline(item.sprite, transform), paint);
        }
    }
}
}
