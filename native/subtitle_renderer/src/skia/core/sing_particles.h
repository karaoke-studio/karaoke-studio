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
// The shared evaluator owns the trajectory. This module places the 1000-unit
// sprite path on the specified text layer.
struct SingParticleBurst {
    krok::subtitle::native::ParticleBurst burst; // Physical size/travel, absolute clocks.
    SkPath sprite;                                // Winding fill, 1000-unit em space.
    std::uint32_t argb = 0xffffffff;
    float originX = 0, originY = 0, boxW = 1, boxH = 0; // Character pivot and anchor box.
};

struct ParticleVisibility {
    int entryLeadMs = 0;
    bool exitTail = false;

    bool outside(const std::vector<SingParticleBurst>& bursts, int time, int start, int end) const {
        const bool prelude = entryLeadMs > 0 && time < start && time >= start - entryLeadMs - 100;
        const bool tail = exitTail && time >= end;
        if (!prelude && !tail) return false;
        return std::any_of(bursts.begin(), bursts.end(), [&](const SingParticleBurst& item) {
            const auto& burst = item.burst;
            return (tail || burst.sweep > 0) && time >= burst.startMs && time < burst.endMs;
        });
    }
};

namespace particle_detail {
inline SkPoint midpoint(SkPoint a, SkPoint b) {
    return {(a.x() + b.x()) * .5f, (a.y() + b.y()) * .5f};
}

inline float axisSecondDifference(SkPoint a, SkPoint b, SkPoint c) {
    return std::max(std::abs(a.x() - 2.f * b.x() + c.x()), std::abs(a.y() - 2.f * b.y() + c.y()));
}

inline void flattenQuadratic(SkPathBuilder& builder, SkPoint start, SkPoint control, SkPoint end,
                             unsigned depth = 0) {
    // The legacy star's quadratic simplifier subdivides in device axes.
    // At its 0.25px tolerance the measured second-difference limit is 0.75px,
    // including when the sprite is rotated. Generate these chords before AA.
    constexpr float secondDifferenceLimit = .75f;
    if (axisSecondDifference(start, control, end) <= secondDifferenceLimit || depth == 10) {
        builder.lineTo(end);
        return;
    }
    const auto left = midpoint(start, control), right = midpoint(control, end);
    const auto center = midpoint(left, right);
    flattenQuadratic(builder, start, left, center, depth + 1);
    flattenQuadratic(builder, center, right, end, depth + 1);
}

inline void flattenCubic(SkPathBuilder& builder, SkPoint a, SkPoint b, SkPoint c, SkPoint d,
                         unsigned depth = 0) {
    // Device-space subdivision matches the legacy 0.25px cubic tolerance.
    if (std::max(axisSecondDifference(a, b, c), axisSecondDifference(b, c, d)) <= .25f || depth == 10) {
        builder.lineTo(d);
        return;
    }
    const auto ab = midpoint(a, b), bc = midpoint(b, c), cd = midpoint(c, d);
    const auto left = midpoint(ab, bc), right = midpoint(bc, cd);
    const auto split = midpoint(left, right);
    flattenCubic(builder, a, ab, left, split, depth + 1);
    flattenCubic(builder, split, right, cd, d, depth + 1);
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
        case SkPath::kCubic_Verb: flattenCubic(builder, points[0], points[1], points[2], points[3]); break;
        case SkPath::kClose_Verb: builder.close(); break;
        case SkPath::kDone_Verb: break;
        }
    }
    return builder.detach();
}
}

inline void drawSingParticles(SkCanvas& canvas, const std::vector<SingParticleBurst>& bursts, int tMs, bool front) {
    SkPaint paint;
    paint.setAntiAlias(true);
    for (const auto& item : bursts) {
        if (item.burst.front != front) continue;
        paint.setColor(item.argb);
        const float colorAlpha = paint.getAlphaf();
        const bool flameBody = item.burst.kind == "flame";
        const bool flameCore = item.burst.kind == "flame_alt";
        for (const auto& particle : krok::subtitle::native::effects::burstParticlesAt(
                 item.burst, tMs, item.originX, item.originY, item.boxW, item.boxH)) {
            if (particle.alpha <= 0.f || particle.sizePx <= 0.f) continue;
            auto transform = SkMatrix::Translate(particle.x, particle.y);
            transform.preRotate(particle.rotationDeg);
            transform.preScale(particle.sizePx * particle.stretchX / 1000.f,
                               particle.sizePx * particle.stretchY / 1000.f);
            const float alpha = colorAlpha * std::clamp(particle.alpha, 0.f, 1.f);
            // Solid fire paints a larger translucent silhouette before each
            // tongue. Keep its overlap order with the following particles.
            if (flameBody || flameCore) {
                auto halo = transform;
                const float scale = flameBody ? 1.70f : 1.50f;
                halo.preScale(scale, scale);
                paint.setAlphaf(alpha * (flameBody ? .24f : .18f));
                canvas.drawPath(particle_detail::spriteOutline(item.sprite, halo), paint);
            }
            paint.setAlphaf(alpha);
            // Give Skia the final physical outline, as for the subtitle glyphs.
            canvas.drawPath(particle_detail::spriteOutline(item.sprite, transform), paint);
        }
    }
}
}
