#include "scanline_feather.h"

#include "include/core/SkCanvas.h"
#include "include/core/SkPaint.h"
#include "include/core/SkString.h"
#include "include/effects/SkRuntimeEffect.h"
#include <stdexcept>

namespace subtitle::skia {
void clipScanlineSlice(SkCanvas& canvas, float low, float high, bool vertical) {
    // The path is already in physical line coordinates. Use explicit pixel
    // coverage so Ganesh and Raster agree at the shared edge of adjacent slices.
    static const auto effect = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform float2 edges;
            uniform float2 axis;
            half4 main(float2 p) {
                float along = dot(p, axis);
                float covered = min(along + 0.5, edges.y) - max(along - 0.5, edges.x);
                return half4(clamp(covered, 0.0, 1.0));
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    SkRuntimeShaderBuilder builder(effect);
    builder.uniform("edges") = SkV2{low, high};
    builder.uniform("axis") = vertical ? SkV2{0, 1} : SkV2{1, 0};
    auto shader = builder.makeShader();
    if (!shader) throw std::runtime_error("Skia scanline slice shader unavailable");
    const auto bounds = vertical ? SkRect::MakeLTRB(-1e6f, low - 1.f, 1e6f, high + 1.f)
                                 : SkRect::MakeLTRB(low - 1.f, -1e6f, high + 1.f, 1e6f);
    canvas.clipRect(bounds, SkClipOp::kIntersect, false);
    canvas.clipShader(std::move(shader));
}

void applyScanlineFeather(SkCanvas& canvas, const SkRect& layer, float front, float halfWidth,
                         float softness, float maskLeft, float maskTop, bool vertical) {
    // Reproduce the byte mask's texel centers and screen-aligned Bayer order.
    // DstIn applies the mask once to the composed band, including overlaps.
    static const auto effect = [] {
        auto result = SkRuntimeEffect::MakeForShader(SkString(R"(
            uniform float front;
            uniform float halfWidth;
            uniform float softness;
            uniform float2 origin;
            uniform float2 axis;

            half4 main(float2 p) {
                float2 pixel = floor(p - origin);
                float distance = abs(dot(origin + pixel, axis) + 0.5 - front);
                float strength = smoothstep(0.0, softness, halfWidth - distance);
                float2 bits = mod(pixel, 16.0);
                float bayer = 0.0;
                float weight = 1.0;
                for (int step = 0; step < 4; ++step) {
                    float2 low = mod(bits, 2.0);
                    bayer += weight * (2.0 * low.x + low.y * (3.0 - 4.0 * low.x));
                    bits = floor(bits * 0.5);
                    weight *= 4.0;
                }
                float alpha = clamp(floor(strength * 255.0 + bayer / 256.0), 0.0, 255.0) / 255.0;
                return half4(alpha);
            }
        )"));
        if (!result.effect) throw std::runtime_error(result.errorText.c_str());
        return result.effect;
    }();
    SkRuntimeShaderBuilder builder(effect);
    builder.uniform("front") = front;
    builder.uniform("halfWidth") = halfWidth;
    builder.uniform("softness") = softness;
    builder.uniform("origin") = SkV2{maskLeft, maskTop};
    builder.uniform("axis") = vertical ? SkV2{0, 1} : SkV2{1, 0};
    auto shader = builder.makeShader();
    if (!shader) throw std::runtime_error("Skia scanline feather shader unavailable");
    SkPaint paint;
    paint.setShader(std::move(shader));
    paint.setBlendMode(SkBlendMode::kDstIn);
    canvas.drawRect(layer, paint);
}
}
