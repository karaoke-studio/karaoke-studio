#pragma once

#include "particle_outline.h"
#include "particle_ring.h"
#include "solid_outline.h"
#include "../../effects/particles.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPath.h"
#include <algorithm>
#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

namespace subtitle::skia {
// The shared evaluator owns the trajectory. This module places the 1000-unit
// sprite path on the specified text layer.
struct SingParticleBurst {
    krok::subtitle::native::ParticleBurst burst; // Physical size/travel, absolute clocks.
    SkPath sprite;                                // Winding fill, 1000-unit em space.
    std::uint32_t argb = 0xffffffff;
    float originX = 0, originY = 0, boxW = 1, boxH = 0; // Character pivot and anchor box.
    std::optional<RingStroke> ring;
};

struct ParticleVisibility {
    int entryLeadMs = 0;
    bool exitTail = false;

    bool outside(const std::vector<SingParticleBurst>& bursts, int time, int start) const {
        const bool prelude = entryLeadMs > 0 && time < start && time >= start - entryLeadMs - 100;
        // The existing tail gate admits active bursts outside the text
        // window, including an early burst from the matching entry.
        if (!prelude && !exitTail) return false;
        return std::any_of(bursts.begin(), bursts.end(), [&](const SingParticleBurst& item) {
            const auto& burst = item.burst;
            return (exitTail || burst.sweep > 0) && time >= burst.startMs && time < burst.endMs;
        });
    }
};

inline void drawSingParticles(SkCanvas& canvas, const std::vector<SingParticleBurst>& bursts, int tMs, bool front,
                              bool vertical,
                              std::string_view sampledKind, std::string_view quantizedKind) {
    SkPaint paint;
    paint.setAntiAlias(true);
    for (const auto& item : bursts) {
        if (item.burst.front != front) continue;
        paint.setColor(item.argb);
        const float colorAlpha = paint.getAlphaf();
        const bool flameBody = item.burst.kind == "flame";
        const bool flameCore = item.burst.kind == "flame_alt";
        const bool sampled = item.burst.kind == sampledKind;
        const bool quantized = item.burst.kind == quantizedKind;
        // The scanline evaluator works in local axes. A vertical column maps
        // its along-sweep axis to screen Y and its cross-sweep axis to -X.
        const bool verticalScanline = vertical && item.burst.kind == "scanline";
        const float originAlong = verticalScanline ? item.originY : item.originX;
        const float originAcross = verticalScanline ? -item.originX : item.originY;
        for (const auto& particle : krok::subtitle::native::effects::burstParticlesAt(
                 item.burst, tMs, originAlong, originAcross,
                 verticalScanline ? item.boxH : item.boxW, verticalScanline ? item.boxW : item.boxH)) {
            if (particle.alpha <= 0.f || particle.sizePx <= 0.f) continue;
            const float alpha = colorAlpha * std::clamp(particle.alpha, 0.f, 1.f);
            const SkPoint center = verticalScanline ? SkPoint{-particle.y, particle.x} : SkPoint{particle.x, particle.y};
            if (item.ring) {
                auto color = paint.getColor4f();
                color.fA = alpha;
                drawParticleRing(canvas, center, *item.ring, particle.sizePx / 1000.f,
                                 color, quantized);
                continue;
            }
            auto transform = SkMatrix::Translate(center.x(), center.y());
            transform.preRotate(particle.rotationDeg);
            transform.preScale(particle.sizePx * particle.stretchX / 1000.f,
                               particle.sizePx * particle.stretchY / 1000.f);
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
            const auto outline = particle_detail::spriteOutline(item.sprite, transform);
            if (sampled)
                drawSamplePatternPath(canvas, outline, paint, quantized);
            else
                canvas.drawPath(outline, paint);
        }
    }
}
}
