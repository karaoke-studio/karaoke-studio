#include "subtitle_gradient.h"
#include "scanline_paint.h"

#include "include/core/SkColor.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkMatrix.h"
#include "include/core/SkRect.h"
#include "include/effects/SkGradient.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
sk_sp<SkShader> subtitleGradient(const krok::subtitle::native::PaintStyle& paint, const SkRect& bounds) {
    const bool horizontal = paint.mode == "gradient_horizontal";
    const bool radial = paint.mode == "gradient_radial";
    const bool split = paint.mode == "split_vertical";
    if (!krok::subtitle::native::isGradientMode(paint.mode) || paint.stops.empty()
        || !bounds.isFinite()) throw std::runtime_error("Skia 无法准备字幕渐变");
    auto stops = paint.stops;
    for (auto& stop : stops) {
        if (!std::isfinite(stop.position)) throw std::runtime_error("Skia 渐变色阶位置无效");
        stop.position = std::clamp(stop.position, 0.f, 1.f);
    }
    std::stable_sort(stops.begin(), stops.end(), [](const auto& a, const auto& b) {
        return a.position < b.position;
    });
    const bool linear = std::ranges::any_of(stops, [](const auto& stop) { return bool(stop.color.linear); });
    const auto space = linear ? SkColorSpace::MakeSRGBLinear() : SkColorSpace::MakeSRGB();
    const auto color = [linear](auto c) {
        if (linear) {
            const auto v = krok::subtitle::native::linearColor(c);
            return SkColor4f{v[0], v[1], v[2], v[3]};
        }
        return SkColor4f{float(c.red)/255, float(c.green)/255, float(c.blue)/255, float(c.alpha)/255};
    };
    SkGradient::Interpolation interpolation;
    interpolation.fColorSpace = linear ? SkGradient::Interpolation::ColorSpace::kSRGBLinear
                                      : SkGradient::Interpolation::ColorSpace::kSRGB;

    if (radial) {
        std::vector<SkColor4f> colors;
        std::vector<float> positions;
        colors.reserve(stops.size());
        positions.reserve(stops.size());
        for (const auto& stop : stops) {
            colors.push_back(color(stop.color));
            positions.push_back(stop.position);
        }
        if (colors.size() == 1 || (bounds.width() <= 0.f && bounds.height() <= 0.f))
            return SkShaders::Color(colors.front(), space);
        const float cx = bounds.left() + bounds.width() * std::clamp(paint.radialCenterX, -0.5f, 1.5f);
        const float cy = bounds.top() + bounds.height() * std::clamp(paint.radialCenterY, -0.5f, 1.5f);
        const float rx = std::max(bounds.width() * std::clamp(paint.radialRadiusX, 0.01f, 2.f), 1e-4f);
        const float ry = std::max(bounds.height() * std::clamp(paint.radialRadiusY, 0.01f, 2.f), 1e-4f);
        SkMatrix local;
        local.setScaleTranslate(rx, ry, cx, cy);
        const SkGradient gradient({colors, positions, SkTileMode::kClamp, space}, interpolation);
        auto shader = SkShaders::RadialGradient({0.f, 0.f}, 1.f, gradient, &local);
        if (!shader) throw std::runtime_error("Skia 无法创建径向渐变");
        return shader;
    }

    const bool customAngle = paint.gradientAngleDeg.has_value() && std::isfinite(*paint.gradientAngleDeg);
    float deg = customAngle ? std::fmod(*paint.gradientAngleDeg, 360.f) : (horizontal ? 0.f : 90.f);
    if (deg < 0.f) deg += 360.f;
    const float mod90 = std::fmod(deg, 90.f);
    const bool axisAligned = !customAngle || mod90 < 1e-3f || (90.f - mod90) < 1e-3f;

    SkPoint points[2];
    float span = 0.f;
    if (!customAngle) {
        span = horizontal ? bounds.width() : bounds.height();
        points[0] = {bounds.left(), bounds.top()};
        points[1] = horizontal ? SkPoint{bounds.right(), bounds.top()} : SkPoint{bounds.left(), bounds.bottom()};
    } else {
        constexpr float kPi = 3.14159265358979323846f;
        const float rad = deg * (kPi / 180.f);
        const float dx = std::cos(rad);
        const float dy = std::sin(rad);
        span = std::abs(bounds.width() * dx) + std::abs(bounds.height() * dy);
        const float half = span * 0.5f;
        const float cx = bounds.centerX();
        const float cy = bounds.centerY();
        points[0] = {cx - dx * half, cy - dy * half};
        points[1] = {cx + dx * half, cy + dy * half};
    }

    std::vector<SkColor4f> colors;
    std::vector<float> positions;
    colors.reserve(split ? stops.size() * 2 : stops.size());
    positions.reserve(colors.capacity());
    const float softness = (split && !axisAligned) ? std::min(0.015f, 0.5f / std::max(span, 1.f)) : 0.f;
    float prevPos = 0.f;
    for (std::size_t i = 0; i < stops.size(); ++i) {
        const float pos = stops[i].position;
        if (split && i > 0) {
            if (softness > 0.f) {
                const float lo = std::max(prevPos, pos - softness);
                const float hi = std::min(1.f, std::max(lo, pos + softness));
                colors.push_back(color(stops[i - 1].color));
                positions.push_back(lo);
                colors.push_back(color(stops[i].color));
                positions.push_back(hi);
                prevPos = hi;
                continue;
            }
            colors.push_back(color(stops[i - 1].color));
            positions.push_back(pos);
        }
        const float clamped = std::max(prevPos, pos);
        colors.push_back(color(stops[i].color));
        positions.push_back(clamped);
        prevPos = clamped;
    }
    if (colors.size() == 1 || span <= 0.f)
        return SkShaders::Color(colors.front(), space);
    const SkGradient gradient({colors, positions, SkTileMode::kClamp, space}, interpolation);
    auto shader = SkShaders::LinearGradient(points, gradient);
    if (!shader) throw std::runtime_error("Skia 无法创建字幕渐变");
    return shader;
}

sk_sp<SkShader> lineGradient(const krok::subtitle::native::PaintStyle& paint, const SkRect& ink, float visualPad,
                             const SkRect& canvasBounds) {
    if (!krok::subtitle::native::isGradientMode(paint.mode))
        return nullptr;
    if (paint.gradientSpace == "canvas" && !canvasBounds.isEmpty())
        return subtitleGradient(paint, canvasBounds);
    if (paint.mode == "gradient_horizontal" && !paint.gradientAngleDeg.has_value())
        return subtitleGradient(paint, SkRect::MakeLTRB(ink.left(), ink.top(), std::max(ink.right(), ink.left() + 1.f), ink.bottom()));
    return subtitleGradient(paint, SkRect::MakeLTRB(ink.left(), ink.top() - visualPad,
                                                    std::max(ink.right(), ink.left() + 1.f), ink.bottom() + visualPad));
}

sk_sp<SkShader> scanlineGradient(const krok::subtitle::native::PaintStyle& paint, const SkRect& ink,
                                float visualPad, const ScanlinePaint& highlight) {
    if (paint.mode != "gradient_vertical" || highlight.mode != ScanlineColorMode::Brighten || ink.isEmpty())
        return nullptr;
    auto gradient = paint;
    for (auto& stop : gradient.stops) {
        auto& color = stop.color;
        const auto argb = SkColorSetARGB(color.alpha, color.red, color.green, color.blue);
        const auto bright = highlight.shade(argb, argb, false);
        color = {std::uint8_t(SkColorGetR(bright)), std::uint8_t(SkColorGetG(bright)),
                 std::uint8_t(SkColorGetB(bright)), color.alpha};
    }
    return lineGradient(gradient, ink, visualPad);
}

float outlineVisualPad(float strokeWidth, float stroke2Width, float layoutScale) {
    const float scale = std::max(layoutScale, .01f);
    return std::ceil((std::max(strokeWidth / scale, 0.f) + std::max(stroke2Width / scale, 0.f)) * .5f) * scale;
}

bool paintNeedsBodyProtection(const krok::subtitle::native::PaintStyle& paint) {
    if (krok::subtitle::native::isGradientMode(paint.mode))
        return std::any_of(paint.stops.begin(), paint.stops.end(), [](const auto& stop) { return stop.color.alpha < 255; });
    return paint.mode != "solid" || paint.color.alpha < 255;
}
}
