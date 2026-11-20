#include "scanline_shape.h"

#include "../../effects/scanline_column.h"
#include "../../effects/unit_hash.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkImage.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkRRect.h"
#include "include/effects/SkGradient.h"
#include <algorithm>
#include <cmath>

namespace subtitle::skia {
namespace {
using krok::subtitle::native::RgbaColor;

float smooth(float value) {
    const float v = std::clamp(value, 0.f, 1.f);
    return v * v * (3.f - 2.f * v);
}

RgbaColor lighten(const RgbaColor& color, float amount) {
    const auto mix = [amount](std::uint8_t channel) {
        const float unit = float(channel) / 255.f;
        return std::uint8_t(std::lround(std::clamp(unit + (1.f - unit) * amount, 0.f, 1.f) * 255.f));
    };
    return {mix(color.red), mix(color.green), mix(color.blue), color.alpha};
}

SkColor4f opaque(const RgbaColor& color, float alpha) {
    return {color.red / 255.f, color.green / 255.f, color.blue / 255.f, alpha};
}

// The flame column: the shared 72x108 premultiplied raster stretched with linear filtering.
void drawFlame(SkCanvas& canvas, const ScanlineShape& shape, int timeMs, float front, const SkRect& ink,
               float targetTop, bool hasTarget, float opacity) {
    const float lineHeight = std::max(ink.height(), 1.f);
    const float width = std::max(shape.bandWidth * 4.f, lineHeight * .70f);
    const float baseHeight = std::max(lineHeight * 1.65f, width * 2.7f);
    float height = baseHeight;
    float top = ink.centerY() - height * .5f - height * .12f;
    const float bottom = top + height;
    if (hasTarget && targetTop < top) {
        top = targetTop;
        height = std::max(bottom - top, baseHeight);
    }
    const float left = front - width * .5f;
    constexpr unsigned kWidth = 72, kHeight = 108;
    const float inkWidth = std::max(ink.width(), 1.f);
    const auto pixels = krok::subtitle::native::effects::scanlineColumnPixels(
        "flame", shape.outer, shape.inner, shape.core, nullptr, timeMs, kWidth, kHeight,
        (left - ink.left()) / inkWidth, width / inkWidth);
    if (pixels.empty()) return;
    const auto info = SkImageInfo::Make(kWidth, kHeight, kBGRA_8888_SkColorType, kPremul_SkAlphaType);
    const auto image = SkImages::RasterFromPixmapCopy(SkPixmap(info, pixels.data(), kWidth * 4));
    if (!image) return;
    SkPaint paint;
    paint.setAntiAlias(true);
    paint.setAlphaf(opacity);
    canvas.drawImageRect(image, SkRect::MakeIWH(kWidth, kHeight), SkRect::MakeLTRB(left, top, left + width, top + height),
                         SkSamplingOptions(SkFilterMode::kLinear), &paint, SkCanvas::kStrict_SrcRectConstraint);
}

// Rising bubbles around a soft pillar: even bubbles behind it, odd in front.
void drawBubbles(SkCanvas& canvas, const ScanlineShape& shape, int timeMs, float front, const SkRect& ink,
                 float targetTop, bool hasTarget, float opacity) {
    using krok::subtitle::native::effects::fxUnitHash;
    const float t = float(timeMs) / 1000.f;
    const float fontPx = std::max(shape.fontPx, 1.f);
    const float lineHeight = std::max(ink.height(), 1.f);
    const float bottom = ink.bottom() + lineHeight * .01f;
    float rise = lineHeight * 1.55f;
    const float spread = std::max(lineHeight * .24f, fontPx * .22f);
    float pillarHeight = lineHeight * 1.65f;
    float pillarTop = ink.centerY() - pillarHeight * .5f - pillarHeight * .12f;
    const float pillarBottom = pillarTop + pillarHeight;
    if (hasTarget && targetTop < pillarTop) {
        pillarTop = targetTop;
        pillarHeight = std::max(pillarBottom - pillarTop, lineHeight * 1.65f);
    }
    rise = std::max(rise, bottom - pillarTop + fontPx * .10f);
    const float pillarWidth = std::max(fontPx * .42f, lineHeight * .22f);
    SkPaint paint;
    paint.setAntiAlias(true);
    const auto bubble = [&](int i, bool inFront) {
        const auto index = std::uint32_t(i);
        const float phase = fxUnitHash(index, 601u);
        const float speed = .80f + .50f * fxUnitHash(index, 602u);
        const float cycle = std::fmod(phase + t * speed, 1.f);
        const float fade = std::min(cycle / .08f, 1.f) * std::min((1.f - cycle) / .20f, 1.f);
        if (fade <= 0.f) return;
        const float x = front + (fxUnitHash(index, 603u) - .5f) * spread * 2.f
            + std::sin(t * 2.6f + phase * 6.2831853f) * fontPx * .045f;
        const float y = bottom - cycle * rise;
        const float size = fontPx * (.060f + .060f * fxUnitHash(index, 604u)) * (.65f + .45f * cycle)
            * (inFront ? 1.f : .88f);
        if (size <= .5f) return;
        const float alpha = std::clamp(opacity * fade * (inFront ? 1.f : .58f), 0.f, 1.f);
        paint.setStyle(SkPaint::kFill_Style);
        paint.setColor4f(opaque(shape.outer, alpha * .27f));
        canvas.drawCircle(x, y, size, paint);
        paint.setStyle(SkPaint::kStroke_Style);
        paint.setStrokeWidth(std::max(.8f, size * .10f));
        paint.setColor4f(opaque(lighten(shape.outer, .30f), alpha));
        canvas.drawCircle(x, y, size, paint);
        paint.setStyle(SkPaint::kFill_Style);
        paint.setColor4f(opaque(lighten(shape.outer, .60f), alpha));
        canvas.drawCircle(x - size * .32f, y - size * .32f, size * .18f, paint);
    };
    const auto pillar = [&] {
        const SkColor4f colors[5] = {opaque(shape.outer, 0.f), opaque(shape.outer, .18f), opaque(shape.outer, .50f),
                                     opaque(shape.outer, .18f), opaque(shape.outer, 0.f)};
        const float positions[5] = {0.f, .14f, .50f, .86f, 1.f};
        const SkPoint points[2] = {{front, pillarTop}, {front, pillarTop + pillarHeight}};
        const SkGradient gradient({colors, positions, SkTileMode::kClamp}, SkGradient::Interpolation{});
        SkPaint fill;
        fill.setAntiAlias(true);
        fill.setShader(SkShaders::LinearGradient(points, gradient));
        const float opacities[3] = {.35f, .55f, .45f}, scales[3] = {1.f, .58f, .24f};
        const float pulse = .90f + .10f * std::sin(t * 3.2f);
        for (int layer = 0; layer < 3; ++layer) {
            const float width = std::max(pillarWidth * scales[layer] * pulse, 2.f);
            fill.setAlphaf(std::clamp(opacity * opacities[layer], 0.f, 1.f));
            const auto rect = SkRect::MakeLTRB(front - width * .5f, pillarTop, front + width * .5f,
                                               pillarTop + pillarHeight);
            canvas.drawRRect(SkRRect::MakeRectXY(rect, width * .5f, width * .5f), fill);
        }
    };
    for (int i = 0; i < 18; i += 2) bubble(i, false);
    pillar();
    for (int i = 1; i < 18; i += 2) bubble(i, true);
}
}

ScanlineShape ScanlineShape::make(const std::string& name, RgbaColor base, float bandWidth, float fontPx,
                                  int displayStart, int displayEnd, int lastCharEnd) {
    ScanlineShape shape;
    shape.kind = name == "flame" ? Kind::Flame : name == "bubble" ? Kind::Bubble : Kind::None;
    if (!shape) return shape;
    const bool flame = shape.kind == Kind::Flame;
    // The `color` mode palette: base, then a lighter inner and core.
    shape.outer = base;
    shape.inner = lighten(base, flame ? .40f : .55f);
    shape.core = flame ? lighten(base, .80f) : RgbaColor{255, 255, 255, 255};
    shape.bandWidth = bandWidth;
    shape.fontPx = fontPx;
    shape.displayStart = displayStart;
    shape.displayEnd = displayEnd;
    shape.lastCharEnd = lastCharEnd;
    return shape;
}

float ScanlineShape::fadeAt(int timeMs) const {
    if (!*this || displayEnd <= displayStart) return 1.f;
    const float fadeIn = smooth((float(timeMs) - displayStart) / 160.f);
    const int fadeOutStart = std::max(lastCharEnd, displayEnd - 220);
    float fadeOut = 1.f;
    if (fadeOutStart < displayEnd && timeMs > fadeOutStart)
        fadeOut = smooth((displayEnd - float(timeMs)) / float(std::max(displayEnd - fadeOutStart, 1)));
    return std::min(fadeIn, fadeOut);
}

void ScanlineShape::draw(SkCanvas& canvas, int timeMs, float front, const SkRect& bodyInk,
                         std::optional<float> rubyTop) const {
    if (!*this || bodyInk.isEmpty()) return;
    const float opacity = std::clamp(fadeAt(timeMs), 0.f, 1.f);
    const float lineHeight = std::max(bodyInk.height(), 1.f);
    // Without readings the GPU's line-local target is the baseline, which never rises above the shape.
    const float targetTop = rubyTop ? *rubyTop - lineHeight * .18f : 0.f;
    if (kind == Kind::Flame) drawFlame(canvas, *this, timeMs, front, bodyInk, targetTop, rubyTop.has_value(), opacity);
    else drawBubbles(canvas, *this, timeMs, front, bodyInk, targetTop, rubyTop.has_value(), opacity);
}
}
