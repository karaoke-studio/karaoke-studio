#include "subtitle_gradient.h"

#include "include/core/SkColorSpace.h"
#include "include/core/SkRect.h"
#include "include/effects/SkGradient.h"
#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace subtitle::skia {
sk_sp<SkShader> subtitleGradient(const krok::subtitle::native::PaintStyle& paint, const SkRect& bounds) {
    const bool horizontal = paint.mode == "gradient_horizontal";
    const bool split = paint.mode == "split_vertical";
    if ((!horizontal && !split && paint.mode != "gradient_vertical") || paint.stops.empty()
        || !bounds.isFinite()) throw std::runtime_error("Skia 无法准备字幕渐变");
    auto stops = paint.stops;
    for (auto& stop : stops) {
        if (!std::isfinite(stop.position)) throw std::runtime_error("Skia 渐变色阶位置无效");
        stop.position = std::clamp(stop.position, 0.f, 1.f);
    }
    std::stable_sort(stops.begin(), stops.end(), [](const auto& a, const auto& b) {
        return a.position < b.position;
    });
    const auto color = [](auto c) {
        return SkColor4f{float(c.red)/255, float(c.green)/255, float(c.blue)/255, float(c.alpha)/255};
    };
    std::vector<SkColor4f> colors;
    std::vector<float> positions;
    colors.reserve(split ? stops.size() * 2 : stops.size());
    positions.reserve(colors.capacity());
    for (std::size_t i = 0; i < stops.size(); ++i) {
        if (split && i > 0) {
            colors.push_back(color(stops[i-1].color));
            positions.push_back(stops[i].position);
        }
        colors.push_back(color(stops[i].color));
        positions.push_back(stops[i].position);
    }
    if (colors.size() == 1 || (horizontal ? bounds.width() : bounds.height()) <= 0)
        return SkShaders::Color(colors.front(), SkColorSpace::MakeSRGB());
    const SkPoint points[2] = {
        {bounds.left(), bounds.top()},
        horizontal ? SkPoint{bounds.right(), bounds.top()} : SkPoint{bounds.left(), bounds.bottom()},
    };
    SkGradient::Interpolation interpolation;
    interpolation.fColorSpace = SkGradient::Interpolation::ColorSpace::kSRGB;
    const SkGradient gradient({colors, positions, SkTileMode::kClamp}, interpolation);
    auto shader = SkShaders::LinearGradient(points, gradient);
    if (!shader) throw std::runtime_error("Skia 无法创建字幕渐变");
    return shader;
}

sk_sp<SkShader> lineGradient(const krok::subtitle::native::PaintStyle& paint, const SkRect& ink, float visualPad) {
    if (paint.mode == "solid") return nullptr;
    if (paint.mode == "gradient_horizontal")
        return subtitleGradient(paint, SkRect::MakeLTRB(ink.left(), ink.top(), std::max(ink.right(), ink.left() + 1.f), ink.bottom()));
    return subtitleGradient(paint, SkRect::MakeLTRB(ink.left(), ink.top() - visualPad, ink.right(), ink.bottom() + visualPad));
}

float outlineVisualPad(float strokeWidth, float stroke2Width, float layoutScale) {
    const float scale = std::max(layoutScale, .01f);
    return std::ceil((std::max(strokeWidth / scale, 0.f) + std::max(stroke2Width / scale, 0.f)) * .5f) * scale;
}

bool paintNeedsBodyProtection(const krok::subtitle::native::PaintStyle& paint) {
    if (paint.mode == "gradient_horizontal" || paint.mode == "gradient_vertical" || paint.mode == "split_vertical")
        return std::any_of(paint.stops.begin(), paint.stops.end(), [](const auto& stop) { return stop.color.alpha < 255; });
    return paint.mode != "solid" || paint.color.alpha < 255;
}
}
